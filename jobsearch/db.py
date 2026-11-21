"""SQLite storage for saved searches, jobs, and API usage."""

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB_PATH = os.path.join(REPO_DIR, "jobs.db")
SEED_PATH = os.path.join(REPO_DIR, "seed", "seed.json")

STATUSES = ["new", "interested", "applied", "interviewing", "offer", "rejected", "not_a_fit"]

# Company priority, numbered as in the original Columbus spreadsheet (lower = more interesting).
PRIORITIES = {1: "High", 2: "Medium", 4: "TBD – Med-Hi", 5: "TBD – Med", 6: "TBD – Low", 7: "Low"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS searches (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    query           TEXT NOT NULL,
    location        TEXT,
    remote_only     INTEGER NOT NULL DEFAULT 0,
    date_posted     TEXT NOT NULL DEFAULT 'all',
    employment_types TEXT,
    num_pages       INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL,
    last_run_at     TEXT
);

-- Target companies. ats/ats_token say where to read openings from
-- (greenhouse/lever/ashby board name), or 'search' to use JSearch.
CREATE TABLE IF NOT EXISTS companies (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL UNIQUE COLLATE NOCASE,
    careers_url     TEXT,
    ats             TEXT NOT NULL DEFAULT 'search',
    ats_token       TEXT,
    keywords        TEXT,
    locations       TEXT,
    priority        INTEGER,
    rating          REAL,
    notes           TEXT,
    created_at      TEXT NOT NULL,
    last_checked_at TEXT
);

CREATE TABLE IF NOT EXISTS meta (
    key             TEXT PRIMARY KEY,
    value           TEXT
);

CREATE TABLE IF NOT EXISTS jobs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id          TEXT NOT NULL UNIQUE,
    company_id      INTEGER REFERENCES companies(id) ON DELETE SET NULL,
    title           TEXT NOT NULL,
    company         TEXT,
    location        TEXT,
    is_remote       INTEGER NOT NULL DEFAULT 0,
    employment_type TEXT,
    posted_at       TEXT,
    salary_min      REAL,
    salary_max      REAL,
    salary_period   TEXT,
    salary_text     TEXT,
    apply_link      TEXT,
    publisher       TEXT,
    description     TEXT,
    status          TEXT NOT NULL DEFAULT 'new',
    notes           TEXT,
    first_seen_at   TEXT NOT NULL,
    last_seen_at    TEXT NOT NULL,
    raw_json        TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);

-- Which saved search found which job (a job can match several searches).
CREATE TABLE IF NOT EXISTS search_jobs (
    search_id       INTEGER NOT NULL REFERENCES searches(id) ON DELETE CASCADE,
    job_id          INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    first_seen_at   TEXT NOT NULL,
    PRIMARY KEY (search_id, job_id)
);

-- One row per JSearch call, so the UI can show usage against the free quota.
CREATE TABLE IF NOT EXISTS api_calls (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    called_at       TEXT NOT NULL,
    num_pages       INTEGER NOT NULL,
    query           TEXT
);
"""

# Columns the UI may change on a job; everything else comes from JSearch.
EDITABLE_JOB_FIELDS = ("status", "notes")


def db_path():
    return os.getenv("JOBSEARCH_DB", DEFAULT_DB_PATH)


def now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@contextmanager
def connect():
    conn = sqlite3.connect(db_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db(seed_path=SEED_PATH):
    with connect() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)
    seed_db(seed_path)


def _migrate(conn):
    """Add columns introduced after a database was first created."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(companies)")}
    for name, decl in (("priority", "INTEGER"), ("rating", "REAL")):
        if name not in cols:
            conn.execute(f"ALTER TABLE companies ADD COLUMN {name} {decl}")


def seed_db(seed_path=SEED_PATH):
    """Load the starter companies and saved searches once, on a brand-new database.

    Runs only the first time (tracked in meta), so deleting seeded companies
    later doesn't bring them back.
    """
    with connect() as conn:
        if conn.execute("SELECT 1 FROM meta WHERE key = 'seeded'").fetchone():
            return
        has_data = conn.execute("SELECT (SELECT COUNT(*) FROM companies) + (SELECT COUNT(*) FROM searches)").fetchone()[0]
        conn.execute("INSERT INTO meta (key, value) VALUES ('seeded', ?)", (now_iso(),))
    if has_data or not seed_path or not os.path.exists(seed_path):
        return
    with open(seed_path, encoding="utf-8") as f:
        seed = json.load(f)
    defaults = seed.get("defaults", {})
    for company in seed.get("companies", []):
        add_company({**defaults, **company})
    for search in seed.get("searches", []):
        add_search(search)


# --- searches ---------------------------------------------------------------

def list_searches():
    with connect() as conn:
        rows = conn.execute("""
            SELECT s.*, COUNT(sj.job_id) AS job_count
            FROM searches s LEFT JOIN search_jobs sj ON sj.search_id = s.id
            GROUP BY s.id ORDER BY s.name COLLATE NOCASE
        """).fetchall()
    return [dict(r) for r in rows]


def get_search(search_id):
    with connect() as conn:
        row = conn.execute("SELECT * FROM searches WHERE id = ?", (search_id,)).fetchone()
    return dict(row) if row else None


def add_search(params):
    with connect() as conn:
        cur = conn.execute("""
            INSERT INTO searches (name, query, location, remote_only, date_posted,
                                  employment_types, num_pages, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            params["name"], params["query"], params.get("location"),
            int(bool(params.get("remote_only"))), params.get("date_posted") or "all",
            params.get("employment_types"), int(params.get("num_pages") or 1), now_iso(),
        ))
        return cur.lastrowid


def delete_search(search_id):
    with connect() as conn:
        conn.execute("DELETE FROM searches WHERE id = ?", (search_id,))


# --- companies --------------------------------------------------------------

COMPANY_FIELDS = ("name", "careers_url", "ats", "ats_token", "keywords", "locations", "priority", "rating", "notes")


def company_defaults(seed_path=SEED_PATH):
    """Keywords/locations to prefill when adding a company (from the seed file's defaults)."""
    try:
        with open(seed_path, encoding="utf-8") as f:
            defaults = json.load(f).get("defaults", {})
    except (OSError, ValueError):
        defaults = {}
    return {"keywords": defaults.get("keywords", ""), "locations": defaults.get("locations", "")}


def list_companies():
    with connect() as conn:
        rows = conn.execute("""
            SELECT c.*, COUNT(j.id) AS job_count
            FROM companies c LEFT JOIN jobs j ON j.company_id = c.id
            GROUP BY c.id ORDER BY COALESCE(c.priority, 99), c.name COLLATE NOCASE
        """).fetchall()
    return [dict(r) for r in rows]


def get_company(company_id):
    with connect() as conn:
        row = conn.execute("SELECT * FROM companies WHERE id = ?", (company_id,)).fetchone()
    return dict(row) if row else None


def add_company(fields):
    """Insert a company. Raises ValueError if the name already exists."""
    values = {k: fields.get(k) for k in COMPANY_FIELDS}
    try:
        with connect() as conn:
            cur = conn.execute(
                f"INSERT INTO companies ({', '.join(values)}, created_at) "
                f"VALUES ({', '.join('?' * len(values))}, ?)",
                list(values.values()) + [now_iso()],
            )
            return cur.lastrowid
    except sqlite3.IntegrityError:
        raise ValueError(f"{fields.get('name')} is already in your company list")


def update_company(company_id, fields):
    updates = {k: fields[k] for k in COMPANY_FIELDS if k in fields}
    if not updates:
        return
    try:
        with connect() as conn:
            conn.execute(
                f"UPDATE companies SET {', '.join(f'{k} = ?' for k in updates)} WHERE id = ?",
                list(updates.values()) + [company_id],
            )
    except sqlite3.IntegrityError:
        raise ValueError(f"{fields.get('name')} is already in your company list")


def delete_company(company_id):
    with connect() as conn:
        conn.execute("DELETE FROM companies WHERE id = ?", (company_id,))


# --- jobs -------------------------------------------------------------------

def _job_values(job):
    """Map one JSearch result to our jobs columns."""
    location = job.get("job_location")
    if not location:
        parts = [job.get("job_city"), job.get("job_state"), job.get("job_country")]
        location = ", ".join(p for p in parts if p) or None
    return {
        "job_id": job["job_id"],
        "title": job.get("job_title") or "(untitled)",
        "company": job.get("employer_name"),
        "location": location,
        "is_remote": int(bool(job.get("job_is_remote"))),
        "employment_type": job.get("job_employment_type"),
        "posted_at": job.get("job_posted_at_datetime_utc"),
        "salary_min": job.get("job_min_salary"),
        "salary_max": job.get("job_max_salary"),
        "salary_period": job.get("job_salary_period"),
        "salary_text": job.get("job_salary_string"),
        "apply_link": job.get("job_apply_link"),
        "publisher": job.get("job_publisher"),
        "description": job.get("job_description"),
        "raw_json": json.dumps(job),
    }


def save_results(results, search_id=None, company_id=None):
    """Insert new jobs and refresh existing ones. Returns (new_count, seen_count).

    Status and notes on existing jobs are never touched, so re-running a
    search can't undo your tracking.
    """
    new_count = seen_count = 0
    ts = now_iso()
    with connect() as conn:
        for job in results:
            if not job.get("job_id"):
                continue
            v = _job_values(job)
            if company_id is not None:
                v["company_id"] = company_id
            existing = conn.execute("SELECT id FROM jobs WHERE job_id = ?", (v["job_id"],)).fetchone()
            if existing:
                row_id = existing["id"]
                conn.execute("""
                    UPDATE jobs SET title=?, company=?, location=?, is_remote=?, employment_type=?,
                        posted_at=?, salary_min=?, salary_max=?, salary_period=?, salary_text=?,
                        apply_link=?, publisher=?, description=?, raw_json=?, last_seen_at=?
                    WHERE id=?
                """, (v["title"], v["company"], v["location"], v["is_remote"], v["employment_type"],
                      v["posted_at"], v["salary_min"], v["salary_max"], v["salary_period"],
                      v["salary_text"], v["apply_link"], v["publisher"], v["description"],
                      v["raw_json"], ts, row_id))
                if company_id is not None:
                    conn.execute("UPDATE jobs SET company_id = ? WHERE id = ?", (company_id, row_id))
                seen_count += 1
            else:
                cols = list(v) + ["first_seen_at", "last_seen_at"]
                cur = conn.execute(
                    f"INSERT INTO jobs ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                    list(v.values()) + [ts, ts],
                )
                row_id = cur.lastrowid
                new_count += 1
            if search_id is not None:
                conn.execute(
                    "INSERT OR IGNORE INTO search_jobs (search_id, job_id, first_seen_at) VALUES (?, ?, ?)",
                    (search_id, row_id, ts),
                )
        if search_id is not None:
            conn.execute("UPDATE searches SET last_run_at = ? WHERE id = ?", (ts, search_id))
        if company_id is not None:
            conn.execute("UPDATE companies SET last_checked_at = ? WHERE id = ?", (ts, company_id))
    return new_count, seen_count


def list_jobs(status=None, search_id=None, company_id=None, text=None, remote_only=False):
    sql = ["SELECT j.id, j.company_id, j.title, j.company, j.location, j.is_remote, j.employment_type,",
           "j.posted_at, j.salary_min, j.salary_max, j.salary_period, j.salary_text,",
           "j.apply_link, j.publisher, j.status, j.notes, j.first_seen_at, j.last_seen_at",
           "FROM jobs j"]
    where, args = [], []
    if search_id:
        sql.append("JOIN search_jobs sj ON sj.job_id = j.id")
        where.append("sj.search_id = ?")
        args.append(search_id)
    if company_id:
        where.append("j.company_id = ?")
        args.append(company_id)
    if status:
        where.append("j.status = ?")
        args.append(status)
    if remote_only:
        where.append("j.is_remote = 1")
    if text:
        where.append("(j.title LIKE ? OR j.company LIKE ? OR j.description LIKE ? OR j.notes LIKE ?)")
        args += [f"%{text}%"] * 4
    if where:
        sql.append("WHERE " + " AND ".join(where))
    sql.append("ORDER BY j.first_seen_at DESC, j.posted_at DESC")
    with connect() as conn:
        rows = conn.execute(" ".join(sql), args).fetchall()
    return [dict(r) for r in rows]


def get_job(job_id):
    with connect() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if not row:
        return None
    job = dict(row)
    job.pop("raw_json", None)
    return job


def update_job(job_id, fields):
    updates = {k: fields[k] for k in EDITABLE_JOB_FIELDS if k in fields}
    if "status" in updates and updates["status"] not in STATUSES:
        raise ValueError(f"Unknown status: {updates['status']}")
    if not updates:
        return
    with connect() as conn:
        conn.execute(
            f"UPDATE jobs SET {', '.join(f'{k} = ?' for k in updates)} WHERE id = ?",
            list(updates.values()) + [job_id],
        )


def status_counts():
    with connect() as conn:
        rows = conn.execute("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status").fetchall()
    return {r["status"]: r["n"] for r in rows}


# --- API usage --------------------------------------------------------------

def record_api_call(num_pages, query):
    with connect() as conn:
        conn.execute("INSERT INTO api_calls (called_at, num_pages, query) VALUES (?, ?, ?)",
                     (now_iso(), num_pages, query))


def pages_used_this_month():
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    with connect() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(num_pages), 0) AS n FROM api_calls WHERE substr(called_at, 1, 7) = ?",
            (month,),
        ).fetchone()
    return row["n"]

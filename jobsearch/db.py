"""SQLite storage for saved searches, jobs, and API usage."""

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from jobsearch import benefits as benefits_mod, boards, geo, pay, workmode

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

-- Benefits per employer, keyed by a normalized company name so any job's company
-- can have one (not just saved companies). Scores use the spreadsheet's 1-4 scales.
CREATE TABLE IF NOT EXISTS benefits (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    company_key      TEXT NOT NULL UNIQUE,
    company_name     TEXT NOT NULL,
    retirement_score REAL,
    pto_score        REAL,
    commute_score    REAL,
    bonus_score      REAL,
    notes            TEXT,
    benefits_url     TEXT,
    lookup_json      TEXT,
    looked_up_at     TEXT,
    updated_at       TEXT NOT NULL
);

-- One row per ChatGPT benefits lookup, to show usage.
CREATE TABLE IF NOT EXISTS ai_calls (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    called_at        TEXT NOT NULL,
    kind             TEXT NOT NULL,
    company          TEXT
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
    req_number      TEXT,
    closes_at       TEXT,
    work_mode       TEXT,
    work_mode_source TEXT,
    apply_link      TEXT,
    publisher       TEXT,
    description     TEXT,
    status          TEXT NOT NULL DEFAULT 'new',
    applied_at      TEXT,
    interest_level  TEXT,
    also_found      TEXT,
    notes           TEXT,
    first_seen_at   TEXT NOT NULL,
    last_seen_at    TEXT NOT NULL,
    raw_json        TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);

-- Other listings of a job already saved (same job posted on Ladders and ZipRecruiter):
-- their ids point at the one job card so they aren't saved again.
CREATE TABLE IF NOT EXISTS job_aliases (
    alias_job_id    TEXT PRIMARY KEY,
    job_id          INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE
);

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
EDITABLE_JOB_FIELDS = ("status", "notes", "interest_level")
# How much she wants a job she's marked Interested or Applied.
INTEREST_LEVELS = ("high", "medium", "low")


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
    seed_benefits(seed_path)
    upgrade_seed_connectors(seed_path)
    split_default_keywords(seed_path)


def _migrate(conn):
    """Add columns introduced after a database was first created."""
    added = {
        "companies": (("priority", "INTEGER"), ("rating", "REAL")),
        "jobs": (("req_number", "TEXT"), ("closes_at", "TEXT"), ("applied_at", "TEXT"),
                 ("work_mode", "TEXT"), ("work_mode_source", "TEXT"), ("also_found", "TEXT"),
                 ("interest_level", "TEXT")),
    }
    for table, columns in added.items():
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, decl in columns:
            if name not in cols:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


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
    # Companies get the default locations; job titles come from the shared default
    # (see get_default_keywords), so a company's own titles start out empty.
    defaults = {k: v for k, v in seed.get("defaults", {}).items() if k != "keywords"}
    for company in seed.get("companies", []):
        add_company({**defaults, **company})
    for search in seed.get("searches", []):
        add_search(search)


def seed_benefits(seed_path=SEED_PATH):
    """Load the spreadsheet's benefit scores once (separately, so existing databases get them too)."""
    with connect() as conn:
        if conn.execute("SELECT 1 FROM meta WHERE key = 'seeded_benefits'").fetchone():
            return
        conn.execute("INSERT INTO meta (key, value) VALUES ('seeded_benefits', ?)", (now_iso(),))
    if not seed_path or not os.path.exists(seed_path):
        return
    with open(seed_path, encoding="utf-8") as f:
        for row in json.load(f).get("benefits", []):
            if not get_benefits(row["company"]):
                save_benefits(row["company"], {k: row.get(k) for k in BENEFIT_FIELDS if k in row})


def upgrade_seed_connectors(seed_path=SEED_PATH):
    """When a newer seed file can search a company directly, switch existing databases over too.

    Only touches companies that are still on JSearch, so a job board you set by hand
    is never overwritten. Safe to run on every start.
    """
    if not seed_path or not os.path.exists(seed_path):
        return
    with open(seed_path, encoding="utf-8") as f:
        seeded = {c["name"].lower(): c for c in json.load(f).get("companies", [])}
    with connect() as conn:
        for row in conn.execute("SELECT id, name FROM companies WHERE ats = 'search'").fetchall():
            c = seeded.get(row["name"].lower())
            if c and c.get("ats", "search") != "search":
                conn.execute("UPDATE companies SET ats = ?, ats_token = ?, careers_url = ? WHERE id = ?",
                             (c["ats"], c.get("ats_token"), c.get("careers_url"), row["id"]))


# --- benefits ---------------------------------------------------------------

BENEFIT_FIELDS = ("retirement_score", "pto_score", "commute_score", "bonus_score", "notes", "benefits_url")


def _benefits_row(row):
    if not row:
        return None
    d = dict(row)
    d["lookup"] = json.loads(d.pop("lookup_json") or "null")
    return d


def get_benefits(company):
    with connect() as conn:
        row = conn.execute("SELECT * FROM benefits WHERE company_key = ?",
                           (benefits_mod.company_key(company),)).fetchone()
    return _benefits_row(row)


def benefits_by_key():
    with connect() as conn:
        rows = conn.execute("SELECT * FROM benefits").fetchall()
    return {r["company_key"]: _benefits_row(r) for r in rows}


def save_benefits(company, fields=None, lookup=None):
    """Create or update a company's benefits. Only the given fields change."""
    key = benefits_mod.company_key(company)
    if not key:
        raise ValueError("Company name is required")
    updates = {k: fields[k] for k in BENEFIT_FIELDS if fields and k in fields}
    for k in ("retirement_score", "pto_score", "commute_score", "bonus_score"):
        if k in updates:
            v = updates[k]
            try:
                updates[k] = None if v in (None, "") else float(v)
            except (TypeError, ValueError):
                raise ValueError(f"{k.replace('_score', '').capitalize()} score must be a number")
            if updates[k] is not None and not 0 <= updates[k] <= 4:
                raise ValueError("Scores go from 0 to 4")
    if lookup is not None:
        updates["lookup_json"] = json.dumps(lookup)
        updates["looked_up_at"] = now_iso()
    updates["updated_at"] = now_iso()
    with connect() as conn:
        exists = conn.execute("SELECT 1 FROM benefits WHERE company_key = ?", (key,)).fetchone()
        if exists:
            conn.execute(f"UPDATE benefits SET {', '.join(f'{k} = ?' for k in updates)} WHERE company_key = ?",
                         list(updates.values()) + [key])
        else:
            cols = ["company_key", "company_name", *updates]
            conn.execute(f"INSERT INTO benefits ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                         [key, company, *updates.values()])
    return get_benefits(company)


def record_ai_call(kind, company):
    with connect() as conn:
        conn.execute("INSERT INTO ai_calls (called_at, kind, company) VALUES (?, ?, ?)", (now_iso(), kind, company))


def ai_calls_this_month():
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    with connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM ai_calls WHERE substr(called_at, 1, 7) = ?", (month,)).fetchone()[0]


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


def update_search(search_id, params):
    """Change a saved search's criteria; jobs it already found stay linked to it."""
    with connect() as conn:
        conn.execute("""
            UPDATE searches SET name = ?, query = ?, location = ?, remote_only = ?, date_posted = ?,
                                employment_types = ?, num_pages = ?
            WHERE id = ?
        """, (
            params["name"], params["query"], params.get("location") or None,
            int(bool(params.get("remote_only"))), params.get("date_posted") or "all",
            params.get("employment_types") or None, int(params.get("num_pages") or 1), search_id,
        ))


def delete_search(search_id):
    with connect() as conn:
        conn.execute("DELETE FROM searches WHERE id = ?", (search_id,))


# --- companies --------------------------------------------------------------

COMPANY_FIELDS = ("name", "careers_url", "ats", "ats_token", "keywords", "locations", "priority", "rating", "notes")


def _seed_defaults(seed_path=SEED_PATH):
    try:
        with open(seed_path, encoding="utf-8") as f:
            return json.load(f).get("defaults", {})
    except (OSError, ValueError, TypeError):
        return {}


def company_defaults(seed_path=SEED_PATH):
    """What to prefill when adding a company: the default locations, and no extra job titles."""
    return {"keywords": "", "locations": _seed_defaults(seed_path).get("locations", "")}


def get_default_keywords(seed_path=SEED_PATH):
    """Job titles searched at every company (comma-separated), e.g. "business analyst"."""
    with connect() as conn:
        row = conn.execute("SELECT value FROM meta WHERE key = 'default_keywords'").fetchone()
    return row["value"] if row else _seed_defaults(seed_path).get("keywords", "")


def set_default_keywords(text):
    text = ", ".join(t.strip() for t in (text or "").split(",") if t.strip())
    with connect() as conn:
        conn.execute("INSERT INTO meta (key, value) VALUES ('default_keywords', ?) "
                     "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (text,))
    return text


def _get_meta(key):
    with connect() as conn:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def _set_meta(key, value):
    with connect() as conn:
        conn.execute("INSERT INTO meta (key, value) VALUES (?, ?) "
                     "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, str(value)))


def get_area_settings(seed_path=SEED_PATH):
    """Home town and radius for on-site/hybrid jobs, e.g. ("Hanover, OH", 50)."""
    defaults = _seed_defaults(seed_path)
    home = _get_meta("home") or defaults.get("home") or "Columbus, OH"
    radius = float(_get_meta("radius_miles") or defaults.get("radius_miles") or 50)
    return home, radius


def set_area_settings(home, radius):
    _set_meta("home", home)
    _set_meta("radius_miles", radius)


def split_default_keywords(seed_path=SEED_PATH):
    """One-time upgrade: company titles used to repeat the default ("business analyst");
    now they only hold extras, so take the default terms out of each company's list."""
    with connect() as conn:
        if conn.execute("SELECT 1 FROM meta WHERE key = 'keywords_split'").fetchone():
            return
        conn.execute("INSERT INTO meta (key, value) VALUES ('keywords_split', ?)", (now_iso(),))
    default = get_default_keywords(seed_path)
    set_default_keywords(default)
    default_terms = {t.strip().lower() for t in default.split(",") if t.strip()}
    with connect() as conn:
        for row in conn.execute("SELECT id, keywords FROM companies").fetchall():
            terms = [t.strip() for t in (row["keywords"] or "").split(",") if t.strip()]
            extras = [t for t in terms if t.lower() not in default_terms]
            if extras != terms:
                conn.execute("UPDATE companies SET keywords = ? WHERE id = ?", (", ".join(extras) or None, row["id"]))


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


def link_jobs_to_company(company_id, key):
    """Attach already-saved jobs from this employer (matched by normalized name) to a company."""
    with connect() as conn:
        rows = conn.execute("SELECT id, company FROM jobs WHERE company_id IS NULL").fetchall()
        ids = [r["id"] for r in rows if benefits_mod.company_key(r["company"] or "") == key]
        for job_id in ids:
            conn.execute("UPDATE jobs SET company_id = ? WHERE id = ?", (company_id, job_id))
    return len(ids)


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
    salary_min, salary_max = job.get("job_min_salary"), job.get("job_max_salary")
    salary_period, salary_text = job.get("job_salary_period"), job.get("job_salary_string")
    # No salary field? Look for a posted pay range in the description (or the site's salary text).
    if not salary_min and not salary_max:
        found = pay.parse_pay(salary_text) or pay.parse_pay(job.get("job_description"))
        if found:
            salary_min, salary_max, salary_period = found["min"], found["max"], found["period"]
            salary_text = salary_text or found["text"]
    mode, mode_source = workmode.detect(
        listed=job.get("job_work_mode"), location=location, title=job.get("job_title"),
        description=job.get("job_description"), is_remote=job.get("job_is_remote"))
    return {
        "job_id": job["job_id"],
        "title": job.get("job_title") or "(untitled)",
        "company": job.get("employer_name"),
        "location": location,
        "is_remote": int(bool(job.get("job_is_remote"))),
        "employment_type": job.get("job_employment_type"),
        "posted_at": job.get("job_posted_at_datetime_utc"),
        "salary_min": salary_min,
        "salary_max": salary_max,
        "salary_period": (salary_period or "").upper() or None,
        "salary_text": salary_text,
        "req_number": job.get("job_req_id"),
        "closes_at": job.get("job_closes_at") or job.get("job_offer_expiration_datetime_utc"),
        "work_mode": mode,
        "work_mode_source": mode_source,
        "apply_link": job.get("job_apply_link"),
        "publisher": job.get("job_publisher"),
        "description": job.get("job_description"),
        "raw_json": json.dumps(job),
    }


def _same_place(a, b):
    """Two location strings name the same place (or neither says where)."""
    na, nb = boards.norm(a), boards.norm(b)
    if na == nb:
        return True
    pa, pb = geo.parse_points(a), geo.parse_points(b)
    if pa and pb:
        return any(geo.miles(x, y) < 2 for x in pa for y in pb)
    return False


def _dedupe_index(conn):
    """(company, title) -> saved jobs, for spotting the same job listed on another site."""
    index = {}
    for r in conn.execute("SELECT id, company, title, location, req_number FROM jobs").fetchall():
        key = (benefits_mod.company_key(r["company"] or ""), boards.norm(r["title"]))
        index.setdefault(key, []).append(dict(r))
    return index


def _find_duplicate(index, v):
    """A saved job with the same company, title and location whose req number (if both have one) agrees."""
    key = (benefits_mod.company_key(v["company"] or ""), boards.norm(v["title"]))
    for r in index.get(key, []):
        if r["req_number"] and v["req_number"] and boards.norm(r["req_number"]) != boards.norm(v["req_number"]):
            continue  # same title, different opening
        if _same_place(r["location"], v["location"]):
            return r["id"]
    return None


def _add_source(conn, row_id, publisher, link):
    """Remember another site the job is listed on ("Also on ZipRecruiter")."""
    row = conn.execute("SELECT publisher, apply_link, also_found FROM jobs WHERE id = ?", (row_id,)).fetchone()
    if not row or (link and link == row["apply_link"]):
        return
    sources = json.loads(row["also_found"] or "[]")
    if any(s.get("link") == link and s.get("publisher") == publisher for s in sources):
        return
    if publisher == row["publisher"] and not link:
        return
    sources.append({"publisher": publisher, "link": link})
    conn.execute("UPDATE jobs SET also_found = ? WHERE id = ?", (json.dumps(sources), row_id))


def save_results(results, search_id=None, company_id=None):
    """Insert new jobs and refresh existing ones. Returns (new_count, seen_count).

    Status and notes on existing jobs are never touched, so re-running a
    search can't undo your tracking. A job already saved from another site
    (same company, title and location) is merged into that card instead of
    becoming a second one.
    """
    new_count = seen_count = 0
    ts = now_iso()
    with connect() as conn:
        index = _dedupe_index(conn)
        for job in results:
            if not job.get("job_id"):
                continue
            v = _job_values(job)
            if company_id is not None:
                v["company_id"] = company_id
            existing = conn.execute("SELECT id FROM jobs WHERE job_id = ?", (v["job_id"],)).fetchone()
            if not existing:
                alias = conn.execute("SELECT job_id FROM job_aliases WHERE alias_job_id = ?", (v["job_id"],)).fetchone()
                dup_id = alias["job_id"] if alias else _find_duplicate(index, v)
                if dup_id:
                    # Same job from another site: keep one card, note where else it's listed.
                    conn.execute("INSERT OR IGNORE INTO job_aliases (alias_job_id, job_id) VALUES (?, ?)", (v["job_id"], dup_id))
                    _add_source(conn, dup_id, v["publisher"], v["apply_link"])
                    conn.execute("UPDATE jobs SET last_seen_at = ? WHERE id = ?", (ts, dup_id))
                    if company_id is not None:
                        conn.execute("UPDATE jobs SET company_id = COALESCE(company_id, ?) WHERE id = ?", (company_id, dup_id))
                    if search_id is not None:
                        conn.execute("INSERT OR IGNORE INTO search_jobs (search_id, job_id, first_seen_at) VALUES (?, ?, ?)",
                                     (search_id, dup_id, ts))
                    seen_count += 1
                    continue
            if existing:
                row_id = existing["id"]
                conn.execute("""
                    UPDATE jobs SET title=?, company=?, location=?, is_remote=?, employment_type=?,
                        posted_at=?, salary_min=?, salary_max=?, salary_period=?, salary_text=?,
                        req_number=?, closes_at=?, work_mode=?, work_mode_source=?,
                        apply_link=?, publisher=?, description=?,
                        raw_json=?, last_seen_at=?
                    WHERE id=?
                """, (v["title"], v["company"], v["location"], v["is_remote"], v["employment_type"],
                      v["posted_at"], v["salary_min"], v["salary_max"], v["salary_period"],
                      v["salary_text"], v["req_number"], v["closes_at"], v["work_mode"],
                      v["work_mode_source"], v["apply_link"],
                      v["publisher"], v["description"], v["raw_json"], ts, row_id))
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
                key = (benefits_mod.company_key(v["company"] or ""), boards.norm(v["title"]))
                index.setdefault(key, []).append({"id": row_id, "location": v["location"], "req_number": v["req_number"]})
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


JOB_SORTS = {
    "found": "j.first_seen_at DESC, j.posted_at DESC",
    "posted": "j.posted_at IS NULL, j.posted_at DESC",
    # Compare yearly-equivalent pay so hourly and salaried jobs sort together.
    "pay": "COALESCE(j.salary_max, j.salary_min) IS NULL, "
           "COALESCE(j.salary_max, j.salary_min) * CASE j.salary_period WHEN 'HOUR' THEN 2080 "
           "WHEN 'MONTH' THEN 12 ELSE 1 END DESC",
    "closes": "j.closes_at IS NULL, j.closes_at ASC",
    "interest": "CASE j.interest_level WHEN 'high' THEN 1 WHEN 'medium' THEN 2 WHEN 'low' THEN 3 ELSE 4 END, "
                "j.first_seen_at DESC",
    "company": "j.company COLLATE NOCASE, j.title COLLATE NOCASE",
}


def list_jobs(status=None, search_id=None, company_id=None, text=None, remote_only=False,
              hide_not_fit=False, sort="found", work_mode=None, interest=None):
    sql = ["SELECT j.id, j.company_id, j.title, j.company, j.location, j.is_remote, j.employment_type,",
           "j.posted_at, j.salary_min, j.salary_max, j.salary_period, j.salary_text,",
           "j.req_number, j.closes_at, j.applied_at, j.work_mode, j.work_mode_source, j.interest_level,",
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
        where.append("(j.is_remote = 1 OR j.work_mode = 'remote')")
    if work_mode == "unknown":
        where.append("j.work_mode IS NULL")
    elif work_mode in workmode.MODES:
        where.append("j.work_mode = ?")
        args.append(work_mode)
    if interest == "unset":
        where.append("j.interest_level IS NULL")
    elif interest in INTEREST_LEVELS:
        where.append("j.interest_level = ?")
        args.append(interest)
    if hide_not_fit and status != "not_a_fit":
        where.append("j.status != 'not_a_fit'")
    if text:
        where.append("(j.title LIKE ? OR j.company LIKE ? OR j.description LIKE ? OR j.notes LIKE ?"
                     " OR j.req_number LIKE ?)")
        args += [f"%{text}%"] * 5
    if where:
        sql.append("WHERE " + " AND ".join(where))
    sql.append("ORDER BY " + JOB_SORTS.get(sort, JOB_SORTS["found"]))
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
    if "interest_level" in updates:
        updates["interest_level"] = (updates["interest_level"] or "").lower() or None
        if updates["interest_level"] not in (None, *INTEREST_LEVELS):
            raise ValueError(f"Unknown interest level: {updates['interest_level']}")
    # Going back to New or Not a fit drops the interest level; Interested <-> Applied keeps it.
    if updates.get("status") in ("new", "not_a_fit") and "interest_level" not in updates:
        updates["interest_level"] = None
    if not updates:
        return
    with connect() as conn:
        # Remember when it was marked applied; un-marking (back to new/interested) clears it.
        if updates.get("status") == "applied":
            current = conn.execute("SELECT applied_at FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if current and not current["applied_at"]:
                updates["applied_at"] = now_iso()
        elif updates.get("status") in ("new", "interested"):
            updates["applied_at"] = None
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

"""Import jobs she tracked in her "Job Hunt" spreadsheet (.xlsx).

Columns are found by their header names (Status, Interest, Company, Source #, Job Title,
Salary, Location, Remote/Hybrid, Cov Ltr, Resume, Resume Update Page, Will they contact
me & Why?, Response to Resume, Interview, Posted, the ranking columns, My Notes, Source),
so moving columns around in the sheet doesn't break it. Reads .xlsx with the standard
library only; the file is read on her machine and never stored in the repo.
"""

import io
import json
import re
import zipfile
import xml.etree.ElementTree as ET
from datetime import date, timedelta

from jobsearch import benefits, boards, db

NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
      "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships"}
RID = "{%s}id" % NS["r"]

# Header text (normalized) -> our field name.
HEADERS = {
    "status": "status", "interest": "interest", "company": "company", "source": "source",
    "source #": "source_number", "job title link to job listing": "title", "job title": "title",
    "salary": "salary", "location": "location", "remote hybrid": "work_style", "cov ltr": "cover_letter",
    "resume": "resume", "resume update page": "resume_page", "will they contact me why": "callback",
    "response to resume": "response", "interview": "interview", "posted": "posted", "my notes": "notes",
    "salary ranking": "rank_salary", "retirement ranking": "rank_retirement", "sick pto ranking": "rank_pto",
    "commute ranking": "rank_commute", "bonus ranking": "rank_bonus",
}
STATUS_MAP = {"applied": "applied", "rejected": "rejected", "won't apply": "not_a_fit", "wont apply": "not_a_fit",
              "interested": "interested", "interviewing": "interviewing", "offer": "offer", "new": "new"}
INTEREST_MAP = {"high": "high", "med-high": "high", "medium": "medium", "med": "medium", "low": "low"}
# Her shorthand for employers that are saved under a slightly different name.
COMPANY_ALIASES = {"hanover ins group": "Hanover Insurance Group", "liberty mutual ins": "Liberty Mutual",
                   "cambium lexia learning": "Cambium Learning Group", "wexner medical": "Wexner Medical Center"}


class SheetError(Exception):
    pass


# --- reading the workbook ------------------------------------------------------------

def read_rows(data):
    """{row number: {column letter: value, letter + "_link": hyperlink}} for the first sheet."""
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
        wb = ET.fromstring(z.read("xl/workbook.xml"))
    except (zipfile.BadZipFile, KeyError, ET.ParseError):
        raise SheetError("That doesn't look like an Excel (.xlsx) file")
    shared = []
    if "xl/sharedStrings.xml" in z.namelist():
        for si in ET.fromstring(z.read("xl/sharedStrings.xml")).findall("m:si", NS):
            shared.append("".join(t.text or "" for t in si.iter("{%s}t" % NS["m"])))
    first = wb.find("m:sheets/m:sheet", NS)
    rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
    target = next(r.get("Target") for r in rels if r.get("Id") == first.get(RID))
    sheet_path = "xl/" + target.lstrip("/").replace("xl/", "")
    sheet = ET.fromstring(z.read(sheet_path))
    rel_path = sheet_path.replace("worksheets/", "worksheets/_rels/") + ".rels"
    link_targets = {r.get("Id"): r.get("Target") for r in ET.fromstring(z.read(rel_path))} if rel_path in z.namelist() else {}

    rows = {}
    for c in sheet.iter("{%s}c" % NS["m"]):
        col, row = re.match(r"([A-Z]+)(\d+)", c.get("r")).groups()
        v, t = c.find("m:v", NS), c.get("t")
        if t == "s" and v is not None:
            value = shared[int(v.text)]
        elif t == "inlineStr":
            value = "".join(x.text or "" for x in c.iter("{%s}t" % NS["m"]))
        else:
            value = v.text if v is not None else None
        if value not in (None, ""):
            rows.setdefault(int(row), {})[col] = value
    for h in sheet.findall("m:hyperlinks/m:hyperlink", NS):
        col, row = re.match(r"([A-Z]+)(\d+)", h.get("ref")).groups()
        url = link_targets.get(h.get(RID))
        if url:
            rows.setdefault(int(row), {})[col + "_link"] = url
    return rows


def _key(text):
    return " ".join(re.sub(r"[^a-z0-9#']+", " ", (text or "").lower()).split())


def find_columns(rows):
    """Locate the header row (the one with Company and Job Title) -> (row number, {field: column})."""
    for n in sorted(rows):
        cols = {}
        for col, text in rows[n].items():
            if col.endswith("_link"):
                continue
            field = HEADERS.get(_key(text))
            if field and field not in cols:
                cols[field] = col
        if "company" in cols and "title" in cols:
            return n, cols
    raise SheetError("Couldn't find the header row (it needs Company and Job Title columns)")


# --- cleaning values -------------------------------------------------------------------

def _excel_date(value):
    """Excel stores dates as day numbers; also accept 11/05/2026 or 2026-11-05 text."""
    if value is None:
        return None
    s = str(value).strip()
    if re.fullmatch(r"\d{5}(\.\d+)?", s):
        return (date(1899, 12, 30) + timedelta(days=int(float(s)))).isoformat()
    m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", s)
    if m:
        return f"{m.group(3)}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
    m = re.search(r"\d{4}-\d{2}-\d{2}", s)
    return m.group(0) if m else None


def _closing_date(value):
    m = re.search(r"clos\w*\s*:?\s*(\d{1,2})/(\d{1,2})/(\d{4})", str(value or ""), re.I)
    return f"{m.group(3)}-{int(m.group(1)):02d}-{int(m.group(2)):02d}" if m else None


def _salary(text):
    """'88-109k', '90K-189K (depends on geo)', '105k', '110-164 (probably 122-137)' -> (min, max) dollars."""
    s = str(text or "")
    m = re.search(r"\$?(\d+(?:\.\d+)?)\s*[kK]?\s*[-–to]+\s*\$?(\d+(?:\.\d+)?)\s*([kK])?", s)
    if m:
        lo, hi = float(m.group(1)), float(m.group(2))
    else:
        m = re.search(r"\$?(\d+(?:\.\d+)?)\s*[kK]", s)
        if not m:
            return None, None
        lo = hi = float(m.group(1))
    lo, hi = (lo * 1000 if lo < 1000 else lo), (hi * 1000 if hi < 1000 else hi)
    return (lo, hi) if 20000 <= lo <= hi <= 1_000_000 else (None, None)


def _work_style(text):
    t = (text or "").lower()
    if "hybrid" in t:
        return "hybrid"
    if "remote" in t:
        return "remote"
    if re.search(r"on-?\s?site|in[- ]office", t):
        return "onsite"
    return None


def _reference(text):
    s = re.sub(r"^(req(uisition)?\s*(id)?|id)\s*[:#]?\s*", "", (text or "").strip(), flags=re.I).lstrip("#").strip()
    return None if not s or set(s) <= {"?"} else s[:100]


def _yes_no(text):
    t = (text or "").strip().lower()
    return 1 if t in ("yes", "y", "true", "x") else 0 if t in ("no", "n", "false") else None


def _unknown(text):
    return not text or set(str(text).strip()) <= {"?"}


# --- turning rows into jobs ---------------------------------------------------------------

def parse(data):
    """Jobs found in the workbook, as dicts ready for import_jobs()."""
    rows = read_rows(data)
    header_row, cols = find_columns(rows)
    get = lambda r, f: (r.get(cols[f]) if f in cols else None)
    link = lambda r, f: (r.get(cols[f] + "_link") if f in cols else None)
    jobs = []
    for n in sorted(rows):
        r = rows[n]
        if n <= header_row or not get(r, "company") or not get(r, "title"):
            continue
        lo, hi = _salary(get(r, "salary"))
        resume_page = link(r, "resume_page") or (get(r, "resume_page") if re.match(r"https?://", get(r, "resume_page") or "") else None)
        ranks = [(label, get(r, f)) for f, label in (("rank_salary", "Salary"), ("rank_retirement", "Retirement"),
                 ("rank_pto", "Sick & PTO"), ("rank_commute", "Commute"), ("rank_bonus", "Bonus")) if get(r, f)]
        notes = (get(r, "notes") or "").strip()
        if ranks:
            notes = (notes + "\n\n" if notes else "") + "Spreadsheet rankings: " + " · ".join(f"{k} {v}" for k, v in ranks)
        location = get(r, "location")
        jobs.append({
            "row": n,
            "status": STATUS_MAP.get((get(r, "status") or "").strip().lower(), "applied"),
            "interest_level": INTEREST_MAP.get((get(r, "interest") or "").strip().lower()),
            "company": (get(r, "company") or "").strip(),
            "title": " ".join((get(r, "title") or "").split()),
            "apply_link": link(r, "title"),
            "req_number": _reference(get(r, "source_number")),
            "salary_text": None if _unknown(get(r, "salary")) else " ".join(get(r, "salary").split()),
            "salary_min": lo, "salary_max": hi,
            "location": None if _unknown(location) or _key(location) in ("remote", "remote ok") else location.strip(),
            "work_mode": _work_style(get(r, "work_style")) or _work_style(location),
            "cover_letter": _yes_no(get(r, "cover_letter")),
            "resume_submitted": _excel_date(get(r, "resume")),
            "resume_link": resume_page or link(r, "resume"),
            "callback_chances": (get(r, "callback") or "").strip()[:250] or None,
            "response_date": _excel_date(get(r, "response")),
            "interviewed": _yes_no(get(r, "interview")),
            "posted_at": _excel_date(get(r, "posted")),
            "closes_at": _closing_date(get(r, "posted")),
            "notes": notes or None,
            "found_where": (get(r, "source") or "").strip()[:50] or None,
        })
    if not jobs:
        raise SheetError("No job rows found under the header row")
    return jobs


def _saved_company(name, companies):
    # Compare aliases the same way names are compared (ignoring "Group", "Inc", punctuation...).
    aliases = {benefits.company_key(k): v for k, v in COMPANY_ALIASES.items()}
    key = benefits.company_key(aliases.get(benefits.company_key(name), name))
    return next((c for c in companies if benefits.company_key(c["name"]) == key), None)


def import_jobs(jobs):
    """Add the spreadsheet's jobs, or update the matching saved job (same company and the same
    reference number or title). Returns {"added": n, "updated": n}."""
    companies = db.list_companies()
    existing = db.list_jobs()
    added = updated = 0
    for j in jobs:
        company = _saved_company(j["company"], companies)
        name = company["name"] if company else j["company"]
        ckey = benefits.company_key(name)
        match = next((e for e in existing if benefits.company_key(e["company"] or "") == ckey and (
            (j["req_number"] and e.get("req_number") and boards.norm(e["req_number"]) == boards.norm(j["req_number"]))
            or boards.norm(e["title"]) == boards.norm(j["title"]))), None)
        if match:
            job_id = match["id"]
            updated += 1
        else:
            source_id = "jobhunt:" + (boards.norm(j["apply_link"]) if j["apply_link"] else f"{ckey}:{boards.norm(j['title'])}")
            db.save_results([{
                "job_id": source_id, "job_title": j["title"], "employer_name": name, "job_location": j["location"],
                "job_apply_link": j["apply_link"], "job_publisher": "Job Hunt spreadsheet",
                "job_req_id": j["req_number"], "job_posted_at_datetime_utc": j["posted_at"], "job_closes_at": j["closes_at"],
                "job_min_salary": j["salary_min"], "job_max_salary": j["salary_max"],
                "job_salary_period": "YEAR" if j["salary_min"] else None, "job_salary_string": j["salary_text"],
                "job_work_mode": j["work_mode"], "job_is_remote": j["work_mode"] == "remote",
            }], company_id=company["id"] if company else None)
            job_id = db.find_job_id(source_id)
            added += 1
        db.apply_spreadsheet_row(job_id, j, mark_by_hand=not match)
    return {"added": added, "updated": updated}

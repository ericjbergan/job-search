"""Company career boards: Greenhouse, Lever and Ashby public job feeds.

Each fetcher returns jobs in the same shape as JSearch results (job_id,
job_title, employer_name, ...) so db.save_results can store them unchanged.
Companies on any other platform fall back to a JSearch "jobs at <company>" query.
"""

import html
import re
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests

from jobsearch import jsearch

ATS_TYPES = ["greenhouse", "lever", "ashby", "search"]
ATS_LABELS = {"greenhouse": "Greenhouse", "lever": "Lever", "ashby": "Ashby", "search": "JSearch"}

_URL_PATTERNS = [
    ("greenhouse", re.compile(r"^(?:job-)?boards(?:\.eu)?\.greenhouse\.io$")),
    ("greenhouse", re.compile(r"^boards-api\.greenhouse\.io$")),
    ("lever", re.compile(r"^jobs(?:\.eu)?\.lever\.co$")),
    ("ashby", re.compile(r"^jobs\.ashbyhq\.com$")),
]


class BoardError(Exception):
    pass


def detect_ats(url):
    """Guess (ats, token) from a careers URL, e.g. jobs.lever.co/spotify -> ("lever", "spotify")."""
    if not url:
        return "search", None
    parsed = urlparse(url if "://" in url else f"https://{url}")
    host = (parsed.hostname or "").lower()
    parts = [p for p in parsed.path.split("/") if p]
    for ats, pattern in _URL_PATTERNS:
        if pattern.match(host) and parts:
            # boards-api.greenhouse.io/v1/boards/<token>/jobs
            if host.startswith("boards-api") and len(parts) >= 3 and parts[1] == "boards":
                return ats, parts[2]
            return ats, parts[0]
    return "search", None


def html_to_text(markup):
    if not markup:
        return ""
    text = html.unescape(markup)  # Greenhouse double-escapes its HTML
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</h\d>", "\n", text)
    text = re.sub(r"(?i)<li[^>]*>", "\n• ", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _split(csv_text):
    return [k.strip().lower() for k in (csv_text or "").split(",") if k.strip()]


def matches(job, keywords, locations):
    """Keyword filter on title; location filter on location text (remote always passes)."""
    title = (job.get("job_title") or "").lower()
    if keywords and not any(k in title for k in keywords):
        return False
    if locations:
        loc = (job.get("job_location") or "").lower()
        if not (job.get("job_is_remote") or any(l in loc for l in locations)):
            return False
    return True


def _get(url, params=None):
    try:
        resp = requests.get(url, params=params, timeout=60)
    except requests.RequestException as e:
        raise BoardError(f"Could not reach job board: {e}") from e
    if resp.status_code == 404:
        raise BoardError("Job board not found; check the careers URL or board name")
    if resp.status_code != 200:
        raise BoardError(f"Job board returned HTTP {resp.status_code}")
    return resp.json()


def _ms_to_iso(ms):
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).isoformat()
    except (TypeError, ValueError):
        return None


def fetch_greenhouse(token, company_name):
    data = _get(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs", {"content": "true"})
    jobs = []
    for j in data.get("jobs", []):
        location = (j.get("location") or {}).get("name")
        jobs.append({
            "job_id": f"greenhouse:{token}:{j['id']}",
            "job_title": j.get("title"),
            "employer_name": j.get("company_name") or company_name,
            "job_location": location,
            "job_is_remote": "remote" in (location or "").lower(),
            "job_employment_type": None,
            "job_posted_at_datetime_utc": j.get("first_published") or j.get("updated_at"),
            "job_apply_link": j.get("absolute_url"),
            "job_publisher": "Greenhouse",
            "job_description": html_to_text(j.get("content")),
        })
    return jobs


def fetch_lever(token, company_name):
    data = _get(f"https://api.lever.co/v0/postings/{token}", {"mode": "json"})
    jobs = []
    for j in data:
        cats = j.get("categories") or {}
        location = cats.get("location") or ", ".join(cats.get("allLocations") or [])
        desc = "\n\n".join(filter(None, [
            j.get("descriptionPlain"),
            *[f"{l.get('text')}\n{html_to_text(l.get('content'))}" for l in j.get("lists") or []],
            j.get("additionalPlain"),
        ]))
        jobs.append({
            "job_id": f"lever:{token}:{j['id']}",
            "job_title": j.get("text"),
            "employer_name": company_name,
            "job_location": location,
            "job_is_remote": j.get("workplaceType") == "remote" or "remote" in (location or "").lower(),
            "job_employment_type": cats.get("commitment"),
            "job_posted_at_datetime_utc": _ms_to_iso(j.get("createdAt")),
            "job_apply_link": j.get("hostedUrl"),
            "job_publisher": "Lever",
            "job_description": desc,
        })
    return jobs


def fetch_ashby(token, company_name):
    data = _get(f"https://api.ashbyhq.com/posting-api/job-board/{token}", {"includeCompensation": "true"})
    jobs = []
    for j in data.get("jobs", []):
        if j.get("isListed") is False:
            continue
        comp = (j.get("compensation") or {}).get("compensationTierSummary")
        jobs.append({
            "job_id": f"ashby:{token}:{j['id']}",
            "job_title": (j.get("title") or "").strip(),
            "employer_name": company_name,
            "job_location": j.get("location"),
            "job_is_remote": bool(j.get("isRemote")),
            "job_employment_type": j.get("employmentType"),
            "job_posted_at_datetime_utc": j.get("publishedAt"),
            "job_apply_link": j.get("jobUrl"),
            "job_publisher": "Ashby",
            "job_salary_string": comp,
            "job_description": j.get("descriptionPlain") or html_to_text(j.get("descriptionHtml")),
        })
    return jobs


def _fetcher(ats):
    return {"greenhouse": fetch_greenhouse, "lever": fetch_lever, "ashby": fetch_ashby}.get(ats)


def check_company(company):
    """Fetch a company's openings, filtered by its keywords and locations.

    Returns (jobs, jsearch_pages_used). Board feeds are free; only the JSearch
    fallback uses API quota.
    """
    keywords = _split(company.get("keywords"))
    locations = _split(company.get("locations"))
    ats = company.get("ats") or "search"

    fetch = _fetcher(ats)
    if fetch:
        if not company.get("ats_token"):
            raise BoardError(f"No {ATS_LABELS[ats]} board name set for {company['name']}")
        jobs = fetch(company["ats_token"], company["name"])
        return [j for j in jobs if matches(j, keywords, locations)], 0

    # Fallback: ask JSearch, then keep only results from this employer.
    terms = " OR ".join(keywords) if keywords else "jobs"
    results, pages = jsearch.search(f"{terms} at {company['name']}", date_posted="month")
    name = company["name"].lower()
    own = []
    for r in results:
        employer = (r.get("employer_name") or "").lower()
        if employer and (name in employer or employer in name):
            own.append(r)
    return [j for j in own if matches(j, [], locations)], pages

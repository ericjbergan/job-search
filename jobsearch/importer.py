"""Add one job from the link to its listing on the company's careers site.

read_job(url) recognizes the careers platform from the link and reads that one job
the way the company-check connectors do (Workday, Oracle, Greenhouse, Lever, Ashby,
UKG, ADP, Dayforce, DirectEmployers). Other pages are read from the standard job data
many careers sites embed (schema.org JobPosting). When a page has neither, the page
text (or text she pasted) goes to ChatGPT to pull out the title, company, location
and so on; the text itself becomes the description.

Big job boards (Indeed, Glassdoor, LinkedIn, ZipRecruiter) refuse automated requests,
so for those the listing text has to be pasted.
"""

import hashlib
import json
import os
import re
from urllib.parse import parse_qs, urlparse

from jobsearch import benefits, boards

_BLOCKING_SITES = ("indeed.com", "glassdoor.com", "linkedin.com", "ziprecruiter.com", "monster.com", "simplyhired.com")
MAX_TEXT = 15000


class ImportFailed(Exception):
    pass


def _host(url):
    return (urlparse(url).hostname or "").lower()


def _site_name(url):
    host = _host(url)
    parts = [p for p in host.split(".") if p not in ("www", "jobs", "careers", "apply", "job-boards", "boards")]
    return (parts[0] if parts else host).capitalize()


def blocked_site(url):
    return any(_host(url) == s or _host(url).endswith("." + s) for s in _BLOCKING_SITES)


# --- platform readers ----------------------------------------------------------

def _workday(url):
    host = _host(url)
    parts = [p for p in urlparse(url).path.split("/") if p and not re.fullmatch(r"[a-z]{2}-[A-Z]{2}", p)]
    if "job" not in parts:
        return None
    site, path = parts[0], "/" + "/".join(parts[parts.index("job"):])
    tenant = host.split(".")[0]
    base = f"https://{host}/wday/cxs/{tenant}/{site}"
    info = boards._json("GET", f"{base}{path}").get("jobPostingInfo") or {}
    job = boards._job(f"workday:{tenant}:{path.rsplit('_', 1)[-1]}", info.get("title"), None, info.get("location"),
                      link=url, publisher="Workday")
    boards.workday_details(job, base, path)
    return job


def _oracle(url):
    m = re.search(r"/sites/([^/]+)/job/(\d+)", urlparse(url).path)
    if not m:
        return None
    host, site, req_id = _host(url), m.group(1), m.group(2)
    data = boards._json("GET", f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails",
                        params={"expand": "all", "onlyData": "true", "finder": f'ById;Id="{req_id}",siteNumber={site}'})
    d = (data.get("items") or [{}])[0]
    job = boards._job(f"oracle:{host.split('.')[0]}:{req_id}", d.get("Title"), None, d.get("PrimaryLocation"),
                      link=url, publisher="Oracle", req=req_id, work_mode=d.get("WorkplaceType") or None)
    boards.oracle_details(job, host, site, req_id)
    return job


def _greenhouse(url, token):
    m = re.search(r"/jobs/(\d+)", urlparse(url).path) or re.search(r"gh_jid=(\d+)", url)
    if not m:
        return None
    j = boards._json("GET", f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs/{m.group(1)}", params={"content": "true"})
    return boards._job(f"greenhouse:{token}:{j['id']}", j.get("title"), j.get("company_name"),
                       (j.get("location") or {}).get("name"), posted=j.get("first_published") or j.get("updated_at"),
                       link=j.get("absolute_url") or url, publisher="Greenhouse",
                       description=boards.html_to_text(j.get("content")))


def _lever(url, token):
    parts = [p for p in urlparse(url).path.split("/") if p]
    if len(parts) < 2:
        return None
    jobs = [j for j in boards.fetch_lever(token, None) if j["job_id"].endswith(":" + parts[1])]
    return jobs[0] if jobs else None


def _ashby(url, token):
    parts = [p for p in urlparse(url).path.split("/") if p]
    if len(parts) < 2:
        return None
    jobs = [j for j in boards.fetch_ashby(token, None) if j["job_id"].endswith(":" + parts[1])]
    return jobs[0] if jobs else None


def _adp_wfn(url, cid):
    job_id = (parse_qs(urlparse(url).query).get("jobId") or [None])[0]
    if not job_id:
        return None
    jobs = [j for j in boards.fetch_adp(cid, None) if j["job_id"] == f"adp:{cid}:{job_id}"]
    if not jobs:
        return None
    boards.adp_details(jobs[0], cid, job_id)
    return jobs[0]


def _adp_myjobs(url, site):
    req_id = (parse_qs(urlparse(url).query).get("reqId") or [None])[0]
    if not req_id:
        return None
    orgoid = boards._json("GET", f"https://myjobs.adp.com/public/staffing/v1/career-site/{site}").get("orgoid")
    data = boards._json("GET", f"https://myjobs.adp.com/public/staffing/v1/job-requisitions/{req_id}", headers={"orgoid": orgoid})
    r = (data.get("jobRequisitions") or [{}])[0]
    job = boards._job(f"adp_myjobs:{site}:{req_id}", r.get("jobTitle") or r.get("publishedJobTitle"), None, None,
                      link=url, publisher="ADP", req=r.get("clientRequisitionID"))
    boards.adp_myjobs_details(job, orgoid, req_id)
    return job


def _jobsyn(url, host):
    m = re.search(r"/([0-9A-F]{32})/job", url, re.I)
    if not m:
        return None
    data = boards._json("GET", "https://prod-search-api.jobsyn.org/api/v1/solr/search",
                        params={"q": f"guid:{m.group(1)}", "num_items": 1}, headers={"x-origin": host})
    j = (data.get("jobs") or [None])[0]
    if not j:
        return None
    other = {}
    try:
        other = json.loads(j.get("other") or "{}")
    except ValueError:
        pass
    return boards._job(f"jobsyn:{host}:{j.get('guid')}", j.get("title_exact"), j.get("company_exact"), j.get("location_exact"),
                       posted=j.get("date_new"), link=url, publisher="DirectEmployers", description=j.get("description"),
                       req=j.get("reqid"), work_mode=other.get("WorkPlaceTypeCode"))


def _ukg(url, host_tenant):
    page = boards._request("GET", url, headers={"Accept": "text/html"}).text
    m = (re.search(r"CandidateOpportunityDetail\((\{.*?\})\);", page, re.S)
         or re.search(r"opportunity\s*[:=]\s*(\{.*?\})\s*[,;]\s*\n", page, re.S))
    if not m:
        return None
    d = json.loads(m.group(1))
    locs = [l.get("LocalizedDescription") or ", ".join(filter(None, [(l.get("Address") or {}).get("City"),
            ((l.get("Address") or {}).get("State") or {}).get("Code") if isinstance((l.get("Address") or {}).get("State"), dict) else None]))
            for l in d.get("Locations") or []]
    job = boards._job(f"ukg:{host_tenant.split('/')[1]}:{d.get('Id')}", d.get("Title"), None, "; ".join(filter(None, locs)),
                      posted=d.get("PostedDate"), link=url, publisher="UKG", req=d.get("RequisitionNumber"),
                      work_mode="remote" if d.get("JobLocationType") == 2 else None)
    boards.ukg_details(job, url)
    return job


def _dayforce(url, token):
    page = boards._request("GET", url, headers={"Accept": "text/html"}).text
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', page, re.S)
    if not m:
        return None
    found = []

    def walk(o):
        if isinstance(o, dict):
            if "jobPostingId" in o and "jobTitle" in o and "jobPostingContent" in o:
                found.append(o)
                return
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(json.loads(m.group(1)))
    if not found:
        return None
    d = found[0]
    content = d.get("jobPostingContent") or {}
    desc = "\n\n".join(boards.html_to_text(v) for v in content.values() if isinstance(v, str) and v.strip())
    locs = [l.get("formattedAddress") for l in d.get("postingLocations") or []]
    ns = token.split("/")[0]
    return boards._job(f"dayforce:{ns}:{d['jobPostingId']}", d.get("jobTitle"), None, "; ".join(filter(None, locs)),
                       remote=d.get("hasVirtualLocation"), posted=d.get("postingStartTimestampUTC"),
                       closes=d.get("postingExpiryTimestampUTC"), link=url, publisher="Dayforce",
                       description=desc, req=d.get("jobReqId"))


def _jsonld(url, page):
    """The schema.org JobPosting block many careers pages embed."""
    for block in re.findall(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', page, re.S):
        try:
            data = json.loads(block.strip())
        except ValueError:
            continue
        for item in (data if isinstance(data, list) else data.get("@graph", [data]) if isinstance(data, dict) else []):
            if not (isinstance(item, dict) and item.get("@type") == "JobPosting"):
                continue
            org = item.get("hiringOrganization")
            company = org.get("name") if isinstance(org, dict) else org
            locs = []
            for place in (item.get("jobLocation") if isinstance(item.get("jobLocation"), list) else [item.get("jobLocation")]):
                addr = (place or {}).get("address") if isinstance(place, dict) else None
                if isinstance(addr, dict):
                    bits = [addr.get("addressLocality"), addr.get("addressRegion"),
                            addr.get("addressCountry") if isinstance(addr.get("addressCountry"), str) else None]
                    locs.append(", ".join(b for b in bits if b and str(b).strip().lower() not in ("unavailable", "n/a", "none")))
            job = boards._job(f"url:{_url_key(url)}", boards.html_to_text(item.get("title")),
                              boards.html_to_text(company) if company else None, "; ".join(filter(None, locs)) or None,
                              link=url, publisher=_site_name(url))
            boards._apply_jsonld(job, item)
            return job
    return None


# --- ChatGPT fallback ------------------------------------------------------------

_EXTRACT = """From this job listing text, return JSON with these fields (null when the text doesn't say):
{"title": "", "company": "", "location": "city, state (or several separated by ;)",
 "work_style": "remote | hybrid | onsite | null", "employment_type": "", "pay": "pay range exactly as written",
 "req_number": "", "posted_date": "YYYY-MM-DD", "closing_date": "YYYY-MM-DD"}
Copy values from the text; don't guess. Respond with JSON only."""


def _from_text(url, text):
    text = re.sub(r"\n{3,}", "\n\n", (text or "").strip())[:MAX_TEXT]
    if len(text) < 200:
        raise ImportFailed("There isn't enough listing text to read. Copy the whole job description and paste it in.")
    if not os.getenv("OPENAI_API_KEY"):
        raise ImportFailed("Reading pasted text needs OPENAI_API_KEY in .env")
    resp = benefits._client().chat.completions.create(
        model=benefits.MODEL, temperature=0, response_format={"type": "json_object"},
        messages=[{"role": "system", "content": _EXTRACT}, {"role": "user", "content": text}])
    try:
        d = json.loads(resp.choices[0].message.content)
    except ValueError:
        raise ImportFailed("ChatGPT's answer couldn't be read; try again")
    if not d.get("title"):
        raise ImportFailed("Couldn't find a job title in that text")
    return boards._job(f"url:{_url_key(url)}" if url else f"text:{hashlib.sha1(text.encode()).hexdigest()[:16]}",
                       d.get("title"), d.get("company"), d.get("location"), link=url or None,
                       publisher=_site_name(url) if url else "Added by hand", description=text,
                       employment_type=d.get("employment_type"), salary=d.get("pay"), req=d.get("req_number"),
                       posted=d.get("posted_date"), closes=d.get("closing_date"), work_mode=d.get("work_style"))


def _url_key(url):
    p = urlparse(url)
    return f"{(p.hostname or '').lower()}{p.path.rstrip('/')}" + (f"?{p.query}" if p.query else "")


# --- entry point -------------------------------------------------------------------

def read_job(url, text=None, companies=()):
    """Read one job from its listing link (or pasted text). Returns a job dict ready for db.save_results,
    plus "company_id" when the link belongs to one of her saved companies."""
    url = (url or "").strip()
    if url and not re.match(r"https?://", url):
        url = "https://" + url
    job = None
    if text and text.strip():
        job = _from_text(url, text)
    elif not url:
        raise ImportFailed("Paste the link to the job listing")
    elif blocked_site(url):
        raise ImportFailed(f"{_site_name(url)} doesn't let apps read its pages. Open the listing, press Ctrl+A then Ctrl+C, "
                           "and paste the text into the box below the link (or use the company's own careers page link).")
    else:
        ats, token = boards.detect_ats(url)
        try:
            readers = {
                "workday": lambda: _workday(url), "oracle": lambda: _oracle(url),
                "greenhouse": lambda: _greenhouse(url, token), "lever": lambda: _lever(url, token),
                "ashby": lambda: _ashby(url, token), "adp": lambda: _adp_wfn(url, token),
                "adp_myjobs": lambda: _adp_myjobs(url, token), "jobsyn": lambda: _jobsyn(url, token),
                "ukg": lambda: _ukg(url, token), "dayforce": lambda: _dayforce(url, token),
            }
            if ats in readers:
                job = readers[ats]()
            if not job:
                page = boards._request("GET", url, headers={"Accept": "text/html"}).text
                job = _jsonld(url, page)
                if not job:
                    # Talentegy-style pages keep the text in a "Description" element; otherwise use the whole page.
                    inner = boards._element_html(page, r"(?:[^\"]*\s)?Description(?:\s[^\"]*)?")
                    body = boards.html_to_text(inner) if inner else benefits._page_text(page)
                    # The page's own heading and title usually carry the job title (and often location).
                    heads = [boards.html_to_text(h) for h in re.findall(r"<h1[^>]*>(.*?)</h1>", page, re.S)[:2]]
                    title_tag = re.search(r"<title[^>]*>(.*?)</title>", page, re.S)
                    lead = "\n".join(filter(None, [*heads, boards.html_to_text(title_tag.group(1)) if title_tag else ""]))
                    job = _from_text(url, f"{lead}\n\n{body}" if lead else body)
        except boards.BoardError as e:
            raise ImportFailed(f"Couldn't read that page ({e}). Open it, press Ctrl+A then Ctrl+C, and paste the text below the link.")

    for key in [k for k in job if k.startswith("_")]:
        job.pop(key)  # connector helpers (detail fetchers), not job data
    job["job_title"] = " ".join((job.get("job_title") or "").split())
    if not job["job_title"]:
        raise ImportFailed("Couldn't find the job title on that page. Paste the listing text instead.")

    # Fill in the company from her saved companies when the link is from their careers site.
    ats, token = boards.detect_ats(url) if url else ("search", None)
    host = _host(url) if url else ""
    for c in companies:
        same_board = ats != "search" and c.get("ats") == ats and (c.get("ats_token") or "") == (token or "")
        # A matching web address only counts for a company's own domain; shared careers hosts
        # (jobs.dayforcehcm.com, workforcenow.adp.com, ...) serve many companies.
        same_site = ats == "search" and host and c.get("careers_url") and _host(c["careers_url"]) == host
        named = job.get("employer_name") and benefits.company_key(job["employer_name"]) == benefits.company_key(c["name"])
        if same_board or same_site or named:
            job["employer_name"] = c["name"]
            job["company_id"] = c["id"]
            break
    if not job.get("employer_name"):
        job["employer_name"] = _site_name(url) if url else None
    return job

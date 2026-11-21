"""Company career sites: search each hiring platform the way its own careers page does.

Every connector takes (token, company_name, query, locations) and returns jobs in the
same shape as JSearch results (job_id, job_title, employer_name, ...) so
db.save_results can store them unchanged. Companies on a platform without a
connector fall back to a JSearch "<keywords> at <company>" query.

None of these are official APIs except Greenhouse, Lever and Ashby; they are the
same endpoints the companies' own search pages call, so a site redesign can
break one. Failures surface as BoardError ("couldn't check <company>").
"""

import html
import json
import re
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import requests

from jobsearch import jsearch

ATS_LABELS = {
    "workday": "Workday",
    "oracle": "Oracle",
    "ukg": "UKG",
    "adp": "ADP",
    "eightfold": "Eightfold",
    "jibe": "iCIMS",
    "radancy": "Radancy",
    "phenom": "Phenom",
    "rippling": "Rippling",
    "amazon": "Amazon",
    "greenhouse": "Greenhouse",
    "lever": "Lever",
    "ashby": "Ashby",
    "search": "JSearch",
}
ATS_TYPES = list(ATS_LABELS)

# What goes in the "board ID" field for each platform (shown in the UI).
ATS_TOKEN_HINTS = {
    "workday": "host/tenant/site, e.g. osu.wd1.myworkdayjobs.com/osu/OSUCareers",
    "oracle": "host/site, e.g. jpmc.fa.oraclecloud.com/CX_1001",
    "ukg": "host/tenant/board-id, e.g. recruiting.ultipro.com/PAR1025PNATB/78198a68-...",
    "adp": "the cid value from the ADP careers URL",
    "eightfold": "host|domain, e.g. libertymutual.eightfold.ai|libertymutual.com",
    "jibe": "careers site host, e.g. careers.mheducation.com",
    "radancy": "careers site host, e.g. careers.mckesson.com",
    "phenom": "careers site host, e.g. jobs.battelle.org",
    "rippling": "board name, e.g. joinroot",
    "amazon": "(none needed)",
    "greenhouse": "board name, e.g. stripe",
    "lever": "board name, e.g. spotify",
    "ashby": "board name, e.g. ramp",
    "search": "(none needed)",
}

MAX_RESULTS = 100        # per company check, before title/location filtering
MAX_DETAIL_FETCHES = 30  # detail pages (description, pay, dates) cost one request per matching job

UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/130.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
}

# Locations outside the US; a "remote" job mentioning one of these doesn't count as US-remote.
_FOREIGN = re.compile(
    r"\b(india|uk|united kingdom|england|canada|mexico|philippines|poland|ireland|germany|france|"
    r"australia|brazil|costa rica|portugal|spain|singapore|japan|china|emea|apac|europe|latam|"
    r"argentina|colombia|netherlands|romania|hungary|malaysia|israel|korea)\b")


class BoardError(Exception):
    pass


# --- matching ---------------------------------------------------------------

def norm(text):
    """Lowercase and turn punctuation into spaces: 'Dublin, OH' -> 'dublin oh'."""
    return " ".join(re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).split())


def split_terms(csv_text):
    return [norm(t) for t in (csv_text or "").split(",") if norm(t)]


def is_us_remote(job):
    loc = norm(job.get("job_location"))
    remote = job.get("job_is_remote") or "remote" in loc
    return bool(remote) and not _FOREIGN.search(loc)


def location_unknown(job):
    loc = norm(job.get("job_location"))
    return not loc or re.fullmatch(r"\d+ locations|multiple( locations)?", loc) is not None


def title_matches(job, keywords):
    title = norm(job.get("job_title"))
    return not keywords or any(k in title for k in keywords)


def location_matches(job, locations, allow_unknown=False):
    if not locations or is_us_remote(job):
        return True
    if allow_unknown and location_unknown(job):
        return True
    loc = norm(job.get("job_location"))
    return any(term in loc for term in locations)


def matches(job, keywords, locations):
    """Title must contain a keyword; location must match a term (US-remote always passes)."""
    return title_matches(job, keywords) and location_matches(job, locations)


# --- helpers ----------------------------------------------------------------

def html_to_text(markup):
    if not markup:
        return ""
    text = html.unescape(markup)  # Greenhouse double-escapes its HTML
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</h\d>", "\n", text)
    text = re.sub(r"(?i)<li[^>]*>", "\n• ", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _request(method, url, retries=3, **kwargs):
    """HTTP with retries: several of these sites drop the first connection now and then."""
    kwargs.setdefault("timeout", 45)
    kwargs["headers"] = {**UA, **kwargs.get("headers", {})}
    last = None
    for attempt in range(retries):
        try:
            resp = requests.request(method, url, **kwargs)
        except requests.RequestException as e:
            last = e
            time.sleep(1.5 * (attempt + 1))
            continue
        if resp.status_code == 404:
            raise BoardError("Careers site not found (404); the board ID may have changed")
        if resp.status_code in (429, 502, 503, 504):
            last = BoardError(f"Careers site busy (HTTP {resp.status_code})")
            time.sleep(2 * (attempt + 1))
            continue
        if resp.status_code != 200:
            raise BoardError(f"Careers site returned HTTP {resp.status_code}")
        return resp
    raise BoardError(f"Could not reach careers site: {last}")


def _json(method, url, **kwargs):
    resp = _request(method, url, **kwargs)
    try:
        return resp.json()
    except ValueError:
        raise BoardError("Careers site returned a page instead of data; it may have changed")


def _ms_to_iso(ms):
    try:
        return datetime.fromtimestamp(int(ms) / (1000 if int(ms) > 10**11 else 1), tz=timezone.utc).isoformat()
    except (TypeError, ValueError):
        return None


def _parse_date(text, formats):
    for fmt in formats:
        try:
            return datetime.strptime(text.strip(), fmt).replace(tzinfo=timezone.utc).isoformat()
        except (ValueError, AttributeError):
            continue
    return None


def _workday_posted(text):
    """'Posted Today' / 'Posted Yesterday' / 'Posted 3 Days Ago' / 'Posted 30+ Days Ago' -> ISO date."""
    t = (text or "").lower()
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    if "today" in t:
        return today.isoformat()
    if "yesterday" in t:
        return (today - timedelta(days=1)).isoformat()
    m = re.search(r"(\d+)\+?\s*day", t)
    return (today - timedelta(days=int(m.group(1)))).isoformat() if m else None


def _job(id_, title, company, location=None, remote=False, posted=None, link=None,
         publisher=None, description=None, employment_type=None, salary=None, req=None, closes=None):
    return {
        "job_id": id_, "job_title": " ".join((title or "").split()), "employer_name": company,
        "job_location": location, "job_is_remote": bool(remote) or "remote" in norm(location),
        "job_employment_type": employment_type, "job_posted_at_datetime_utc": posted,
        "job_apply_link": link, "job_publisher": publisher, "job_description": description,
        "job_salary_string": salary, "job_req_id": str(req) if req not in (None, "") else None,
        "job_closes_at": closes,
    }


# --- detail pages (description, pay, closing date) ---------------------------------
# A connector can attach job["_enrich"] = (function, args); check_company calls it only
# for jobs that pass the title/location filters, so the extra requests stay few.

def jsonld_details(job, url):
    """Read the schema.org JobPosting block many careers pages embed (Radancy, Phenom, ...)."""
    page = _request("GET", url, headers={"Accept": "text/html"}).text
    for block in re.findall(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', page, re.S):
        try:
            data = json.loads(block.strip())
        except ValueError:
            continue
        for item in data if isinstance(data, list) else [data]:
            if isinstance(item, dict) and item.get("@type") == "JobPosting":
                _apply_jsonld(job, item)
                return


def _apply_jsonld(job, item):
    if item.get("description"):
        job["job_description"] = html_to_text(item["description"])
    if item.get("datePosted"):
        job["job_posted_at_datetime_utc"] = item["datePosted"]
    if item.get("validThrough"):
        job["job_closes_at"] = item["validThrough"]
    ident = item.get("identifier")
    if isinstance(ident, dict) and ident.get("value"):
        job["job_req_id"] = str(ident["value"])
    if item.get("employmentType") and not job.get("job_employment_type"):
        et = item["employmentType"]
        job["job_employment_type"] = ", ".join(et) if isinstance(et, list) else et
    salary = item.get("baseSalary")
    value = salary.get("value") if isinstance(salary, dict) else None
    if isinstance(value, dict):
        lo, hi = value.get("minValue"), value.get("maxValue")
        if lo or hi or value.get("value"):
            job["job_min_salary"] = _num(lo or value.get("value"))
            job["job_max_salary"] = _num(hi or lo or value.get("value"))
            job["job_salary_period"] = (value.get("unitText") or "").upper() or None


def _num(v):
    try:
        return float(str(v).replace(",", "").replace("$", ""))
    except (TypeError, ValueError):
        return None


def oracle_details(job, host, site, req_id):
    data = _json("GET", f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails",
                 params={"expand": "all", "onlyData": "true", "finder": f'ById;Id="{req_id}",siteNumber={site}'})
    d = (data.get("items") or [{}])[0]
    parts = [html_to_text(d.get(k)) for k in ("ExternalDescriptionStr", "ExternalResponsibilitiesStr",
                                              "ExternalQualificationsStr", "CorporateDescriptionStr") if d.get(k)]
    if parts:
        job["job_description"] = "\n\n".join(parts)
    if d.get("ExternalPostedStartDate"):
        job["job_posted_at_datetime_utc"] = d["ExternalPostedStartDate"]
    if d.get("ExternalPostedEndDate"):
        job["job_closes_at"] = d["ExternalPostedEndDate"]
    if d.get("JobSchedule"):
        job["job_employment_type"] = d["JobSchedule"]


def ukg_details(job, url):
    page = _request("GET", url, headers={"Accept": "text/html"}).text
    m = (re.search(r"CandidateOpportunityDetail\((\{.*?\})\);", page, re.S)
         or re.search(r"opportunity\s*[:=]\s*(\{.*?\})\s*[,;]\s*\n", page, re.S))
    if not m:
        return
    d = json.loads(m.group(1))
    if d.get("Description"):
        job["job_description"] = html_to_text(d["Description"])
    pay = d.get("PayRange") or {}
    if pay.get("PayRangeMinimum") or pay.get("PayRangeMaximum"):
        job["job_min_salary"] = _num(pay.get("PayRangeMinimum"))
        job["job_max_salary"] = _num(pay.get("PayRangeMaximum") or pay.get("PayRangeMinimum"))


def adp_details(job, cid, item_id):
    d = _json("GET", "https://workforcenow.adp.com/mascsr/default/careercenter/public/events/staffing/v1/"
                     f"job-requisitions/{item_id}",
              params={"cid": cid, "ccId": "19000101_000001", "lang": "en_US", "locale": "en_US"})
    if d.get("requisitionDescription"):
        job["job_description"] = html_to_text(d["requisitionDescription"])


# --- URL detection ------------------------------------------------------------

def detect_ats(url):
    """Guess (ats, token) from a careers URL. Returns ("search", None) when unrecognized.

    Sites on custom domains (iCIMS/Jibe, Radancy, Phenom) can't be recognized from
    the URL alone; pick the job board type by hand in the company form.
    """
    if not url:
        return "search", None
    parsed = urlparse(url if "://" in url else f"https://{url}")
    host = (parsed.hostname or "").lower()
    parts = [p for p in parsed.path.split("/") if p]
    query = parse_qs(parsed.query)

    if re.match(r"^(?:job-)?boards(?:\.eu)?\.greenhouse\.io$", host) and parts:
        return "greenhouse", parts[0]
    if host == "boards-api.greenhouse.io" and len(parts) >= 3 and parts[1] == "boards":
        return "greenhouse", parts[2]
    if re.match(r"^jobs(?:\.eu)?\.lever\.co$", host) and parts:
        return "lever", parts[0]
    if host == "jobs.ashbyhq.com" and parts:
        return "ashby", parts[0]
    if host.endswith(".myworkdayjobs.com"):
        site_parts = [p for p in parts if not re.fullmatch(r"[a-z]{2}-[A-Z]{2}", p)]
        if site_parts:
            return "workday", f"{host}/{host.split('.')[0]}/{site_parts[0]}"
    if host.endswith(".oraclecloud.com") and "sites" in parts:
        i = parts.index("sites")
        if i + 1 < len(parts):
            return "oracle", f"{host}/{parts[i + 1]}"
    if (host == "recruiting.ultipro.com" or host.endswith(".rec.pro.ukg.net")) and "JobBoard" in parts:
        i = parts.index("JobBoard")
        if i >= 1 and i + 1 < len(parts):
            return "ukg", f"{host}/{parts[i - 1]}/{parts[i + 1]}"
    if host == "workforcenow.adp.com" and query.get("cid"):
        return "adp", query["cid"][0]
    if host.endswith(".eightfold.ai") and query.get("domain"):
        return "eightfold", f"{host}|{query['domain'][0]}"
    if host == "ats.rippling.com" and parts:
        return "rippling", parts[0]
    if host.endswith("amazon.jobs"):
        return "amazon", "amazon"
    return "search", None


# --- list-everything feeds (filtered locally) ----------------------------------

def fetch_greenhouse(token, company_name, query=None, locations=None):
    data = _json("GET", f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs", params={"content": "true"})
    return [_job(f"greenhouse:{token}:{j['id']}", j.get("title"), j.get("company_name") or company_name,
                 (j.get("location") or {}).get("name"), posted=j.get("first_published") or j.get("updated_at"),
                 link=j.get("absolute_url"), publisher="Greenhouse", description=html_to_text(j.get("content")),
                 req=j.get("requisition_id") if j.get("requisition_id") != "See Opening ID" else None)
            for j in data.get("jobs", [])]


def fetch_lever(token, company_name, query=None, locations=None):
    data = _json("GET", f"https://api.lever.co/v0/postings/{token}", params={"mode": "json"})
    jobs = []
    for j in data:
        cats = j.get("categories") or {}
        location = cats.get("location") or ", ".join(cats.get("allLocations") or [])
        desc = "\n\n".join(filter(None, [
            j.get("descriptionPlain"),
            *[f"{l.get('text')}\n{html_to_text(l.get('content'))}" for l in j.get("lists") or []],
            j.get("additionalPlain"),
        ]))
        jobs.append(_job(f"lever:{token}:{j['id']}", j.get("text"), company_name, location,
                         remote=j.get("workplaceType") == "remote", posted=_ms_to_iso(j.get("createdAt")),
                         link=j.get("hostedUrl"), publisher="Lever", description=desc,
                         employment_type=cats.get("commitment")))
    return jobs


def fetch_ashby(token, company_name, query=None, locations=None):
    data = _json("GET", f"https://api.ashbyhq.com/posting-api/job-board/{token}",
                 params={"includeCompensation": "true"})
    jobs = []
    for j in data.get("jobs", []):
        if j.get("isListed") is False:
            continue
        comp = (j.get("compensation") or {}).get("compensationTierSummary")
        jobs.append(_job(f"ashby:{token}:{j['id']}", j.get("title"), company_name, j.get("location"),
                         remote=j.get("isRemote"), posted=j.get("publishedAt"), link=j.get("jobUrl"),
                         publisher="Ashby", description=j.get("descriptionPlain") or html_to_text(j.get("descriptionHtml")),
                         employment_type=j.get("employmentType"), salary=comp))
    return jobs


def fetch_rippling(token, company_name, query=None, locations=None):
    jobs, page = [], 0
    while len(jobs) < 500:
        data = _json("GET", f"https://ats.rippling.com/api/v2/board/{token}/jobs",
                     params={"page": page, "pageSize": 50})
        for j in data.get("items", []):
            locs = j.get("locations") or []
            jobs.append(_job(f"rippling:{token}:{j['id']}", j.get("name"), company_name,
                             "; ".join(l.get("name", "") for l in locs),
                             remote=any(l.get("workplaceType") == "REMOTE" for l in locs),
                             link=j.get("url"), publisher="Rippling"))
        page += 1
        if page >= (data.get("totalPages") or 0):
            break
    return jobs


def fetch_adp(token, company_name, query=None, locations=None):
    url = "https://workforcenow.adp.com/mascsr/default/careercenter/public/events/staffing/v1/job-requisitions"
    jobs, skip = [], 0
    while len(jobs) < 500:
        data = _json("GET", url, params={"cid": token, "ccId": "19000101_000001", "lang": "en_US",
                                         "locale": "en_US", "$top": 50, "$skip": skip})
        batch = data.get("jobRequisitions") or []
        for j in batch:
            locs = [l.get("nameCode", {}).get("shortName") for l in j.get("requisitionLocations") or []]
            link = ("https://workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html"
                    f"?cid={token}&ccId=19000101_000001&jobId={j.get('itemID')}&lang=en_US")
            job = _job(f"adp:{token}:{j.get('itemID')}", j.get("requisitionTitle"), company_name,
                       "; ".join(filter(None, locs)), posted=j.get("postDate"), link=link, publisher="ADP",
                       employment_type=(j.get("workLevelCode") or {}).get("shortName"), req=j.get("clientRequisitionID"))
            job["_enrich"] = (adp_details, (token, j.get("itemID")))
            jobs.append(job)
        skip += len(batch)
        if len(batch) < 50:
            break
    return jobs


# --- keyword-search connectors ---------------------------------------------------

def fetch_workday(token, company_name, query, locations=None):
    host, tenant, site = token.split("/")
    base = f"https://{host}/wday/cxs/{tenant}/{site}"
    body = {"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": query or ""}
    first = _json("POST", f"{base}/jobs", json=body)

    # Narrow to matching locations using Workday's own location filters when it has them.
    facets = _workday_location_facets(first.get("facets") or [], locations or [])
    if facets:
        body["appliedFacets"] = facets
        first = _json("POST", f"{base}/jobs", json=body)

    postings = list(first.get("jobPostings") or [])
    total = min(first.get("total") or 0, MAX_RESULTS)
    while len(postings) < total:
        body["offset"] = len(postings)
        batch = _json("POST", f"{base}/jobs", json=body).get("jobPostings") or []
        if not batch:
            break
        postings += batch

    jobs = []
    for p in postings:
        path = p.get("externalPath") or ""
        job = _job(f"workday:{tenant}:{path.rsplit('_', 1)[-1] or path}", p.get("title"), company_name,
                   p.get("locationsText"), posted=_workday_posted(p.get("postedOn")),
                   link=f"https://{host}/{site}{path}", publisher="Workday",
                   req=(p.get("bulletFields") or [None])[0])
        job["_enrich"] = (workday_details, (base, path))
        jobs.append(job)
    return jobs


def _workday_location_facets(facets, locations):
    """Pick Workday location facet values whose names match the company's location terms."""
    applied = {}

    def walk(items):
        for f in items:
            values = f.get("values") or []
            param = f.get("facetParameter") or ""
            if values and "values" in values[0]:
                walk(values)
                continue
            if "location" not in param.lower() or "country" in param.lower():
                continue
            ids = [v["id"] for v in values
                   if v.get("id") and (any(t in norm(v.get("descriptor")) for t in locations)
                                       or "remote" in norm(v.get("descriptor")))]
            if ids:
                applied[param] = ids

    if locations:
        walk(facets)
    if not applied:
        return {}
    # Workday ANDs separate facets together, so use only the one with the most matches.
    best = max(applied, key=lambda k: len(applied[k]))
    return {best: applied[best]}


def workday_details(job, base, path):
    """Fill in description, exact location, dates and req ID for one Workday job (one request)."""
    info = _json("GET", f"{base}{path}").get("jobPostingInfo") or {}
    extra = info.get("additionalLocations") or []
    if info.get("location"):
        job["job_location"] = "; ".join([info["location"], *extra])
        job["job_is_remote"] = job["job_is_remote"] or "remote" in norm(job["job_location"])
    job["job_description"] = html_to_text(info.get("jobDescription"))
    job["job_employment_type"] = info.get("timeType")
    if info.get("startDate"):
        job["job_posted_at_datetime_utc"] = f"{info['startDate']}T00:00:00+00:00"
    if info.get("externalUrl"):
        job["job_apply_link"] = info["externalUrl"]
    if info.get("jobReqId"):
        job["job_req_id"] = info["jobReqId"]
    if info.get("endDate"):
        job["job_closes_at"] = f"{info['endDate']}T00:00:00+00:00"


def fetch_oracle(token, company_name, query, locations=None):
    host, site = token.split("/")
    api = f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
    kw = (query or "").replace(",", " ")

    def finder(extra, offset=0, limit=25):
        return {"onlyData": "true", "expand": "requisitionList.secondaryLocations",
                "finder": f"findReqs;siteNumber={site},keyword={kw},limit={limit},offset={offset}{extra}"}

    # Use Oracle's location filter for each matching location, plus one unfiltered pass for remote jobs.
    passes = [""]
    if locations:
        facet = _json("GET", api, params={"onlyData": "true", "expand": "locationsFacet",
                                          "finder": f"findReqs;siteNumber={site},keyword={kw},limit=1,facetsList=LOCATIONS"})
        items = (facet.get("items") or [{}])[0].get("locationsFacet") or []
        ids = [f["Id"] for f in items if any(t in norm(f.get("Name")) for t in locations)]
        passes = [f",locationId={i}" for i in ids[:5]] + [""]

    seen, jobs = set(), []
    for extra in passes:
        offset = 0
        while offset < (MAX_RESULTS if extra else 25):
            data = _json("GET", api, params=finder(extra, offset))
            item = (data.get("items") or [{}])[0]
            reqs = item.get("requisitionList") or []
            for r in reqs:
                if r["Id"] in seen:
                    continue
                seen.add(r["Id"])
                locs = [r.get("PrimaryLocation")] + [s.get("Name") for s in r.get("secondaryLocations") or []]
                job = _job(f"oracle:{host.split('.')[0]}:{r['Id']}", r.get("Title"), company_name,
                           "; ".join(filter(None, locs)), remote=norm(r.get("WorkplaceType")) == "remote",
                           posted=r.get("PostedDate"), publisher="Oracle", req=r["Id"],
                           closes=r.get("PostingEndDate"),
                           link=f"https://{host}/hcmUI/CandidateExperience/en/sites/{site}/job/{r['Id']}",
                           description=r.get("ShortDescriptionStr"))
                job["_enrich"] = (oracle_details, (host, site, r["Id"]))
                jobs.append(job)
            offset += len(reqs)
            if len(reqs) < 25 or offset >= (item.get("TotalJobsCount") or 0):
                break
    return jobs


def fetch_ukg(token, company_name, query, locations=None):
    host, tenant, board = token.split("/")
    url = f"https://{host}/{tenant}/JobBoard/{board}/JobBoardView/LoadSearchResults"
    jobs, skip = [], 0
    while skip < MAX_RESULTS:
        body = {"opportunitySearch": {"Top": 50, "Skip": skip, "QueryString": query or "",
                                      "OrderBy": [{"Value": "postedDateDesc", "PropertyName": "PostedDate", "Ascending": False}],
                                      "Filters": []},
                "matchCriteria": {"PreferredJobs": [], "Educations": [], "LicenseAndCertifications": [],
                                  "Skills": [], "hasNoLicenses": False, "SkippedSkills": []}}
        data = _json("POST", url, json=body)
        batch = data.get("opportunities") or []
        for o in batch:
            locs = []
            for l in o.get("Locations") or []:
                addr = l.get("Address") or {}
                state = (addr.get("State") or {}).get("Code") if isinstance(addr.get("State"), dict) else addr.get("State")
                city_state = ", ".join(filter(None, [addr.get("City"), state]))
                locs.append(l.get("LocalizedDescription") if not city_state or addr.get("City") == "Remote"
                            else f"{l.get('LocalizedDescription') or ''} ({city_state})".strip())
            link = f"https://{host}/{tenant}/JobBoard/{board}/OpportunityDetail?opportunityId={o['Id']}"
            job = _job(f"ukg:{tenant}:{o['Id']}", o.get("Title"), company_name, "; ".join(filter(None, locs)),
                       posted=o.get("PostedDate"), publisher="UKG", description=o.get("BriefDescription"), link=link,
                       employment_type="Full time" if o.get("FullTime") else "Part time", req=o.get("RequisitionNumber"))
            job["_enrich"] = (ukg_details, (link,))
            jobs.append(job)
        skip += len(batch)
        if len(batch) < 50 or skip >= (data.get("totalCount") or 0):
            break
    return jobs


def fetch_eightfold(token, company_name, query, locations=None):
    host, domain = token.split("|")
    jobs, start = [], 0
    while start < MAX_RESULTS:
        data = _json("GET", f"https://{host}/api/apply/v2/jobs",
                     params={"domain": domain, "query": query or "", "start": start, "num": 10})
        batch = data.get("positions") or []
        for p in batch:
            locs = p.get("locations") or [p.get("location")]
            jobs.append(_job(f"eightfold:{domain}:{p['id']}", p.get("name"), company_name,
                             "; ".join(filter(None, locs)),
                             remote="remote" in norm(p.get("work_location_option")),
                             posted=_ms_to_iso(p.get("t_create")), publisher="Eightfold",
                             link=p.get("canonicalPositionUrl") or f"https://{host}/careers/job/{p['id']}",
                             description=html_to_text(p.get("job_description")),
                             req=p.get("display_job_id")))
        start += len(batch)
        if len(batch) < 10 or start >= (data.get("count") or 0):
            break
    return jobs


def fetch_jibe(token, company_name, query, locations=None):
    host = token
    jobs, page = [], 1
    while len(jobs) < MAX_RESULTS:
        data = _json("GET", f"https://{host}/api/jobs", params={"keywords": query or "", "page": page})
        batch = [j.get("data") or {} for j in data.get("jobs") or []]
        for j in batch:
            remote = any("remote" in norm(t) for t in (j.get("tags2") or [])) or "remote" in norm(j.get("location_name"))
            jobs.append(_job(f"jibe:{host}:{j.get('req_id') or j.get('slug')}", j.get("title"), company_name,
                             j.get("full_location") or j.get("location_name"), remote=remote,
                             posted=j.get("posted_date"), publisher="iCIMS",
                             link=f"https://{host}/jobs/{j.get('slug')}?lang=en-us",
                             description=html_to_text(j.get("description")),
                             employment_type=j.get("employment_type"), req=j.get("req_id")))
        page += 1
        if not batch or len(jobs) >= (data.get("totalCount") or 0):
            break
    return jobs


def fetch_radancy(token, company_name, query, locations=None):
    host = token
    params = {"ActiveFacetID": 0, "CurrentPage": 1, "RecordsPerPage": MAX_RESULTS, "Distance": 50,
              "RadiusUnitType": 0, "Keywords": query or "", "Location": "", "ShowRadius": "False",
              "IsPagination": "False", "FacetType": 0, "SearchResultsModuleName": "Search Results",
              "SearchFiltersModuleName": "Search Filters", "SortCriteria": 0, "SortDirection": 0, "SearchType": 5}
    data = _json("GET", f"https://{host}/search-jobs/results", params=params,
                 headers={"X-Requested-With": "XMLHttpRequest"})
    markup = data.get("results") or ""
    jobs = []
    for li in re.findall(r"<li[^>]*>(.*?)</li>", markup, re.S):
        a = re.search(r'<a[^>]*href="([^"]+)"[^>]*data-job-id="(\d+)"[^>]*>(.*?)</a>', li, re.S)
        if not a:
            continue
        href, job_id, title_html = a.groups()
        title = html_to_text(re.sub(r"<h2[^>]*>|</h2>", "", title_html))
        loc = re.search(r'class="[^"]*job-location[^"]*"[^>]*>(.*?)<', li, re.S)
        date = re.search(r'class="[^"]*date-posted[^"]*"[^>]*>(.*?)<', li, re.S)
        job = _job(f"radancy:{host}:{job_id}", title, company_name,
                   html.unescape(loc.group(1).strip()) if loc else None,
                   posted=_parse_date(date.group(1), ["%m/%d/%Y", "%d/%m/%Y"]) if date else None,
                   link=f"https://{host}{href}", publisher="Radancy")
        job["_enrich"] = (jsonld_details, (f"https://{host}{href}",))
        jobs.append(job)
    return jobs


def fetch_phenom(token, company_name, query, locations=None):
    host = token
    jobs, start = [], 0
    while start < MAX_RESULTS:
        body = {"lang": "en_us", "deviceType": "desktop", "country": "us", "pageName": "search-results",
                "ddoKey": "refineSearch", "sortBy": "", "subsearch": "", "from": start, "jobs": True,
                "counts": True, "all_fields": [], "size": 50, "clearAll": False, "jdsource": "facets",
                "isSliderEnable": False, "pageId": "page20", "siteType": "external", "keywords": query or "",
                "global": True, "selected_fields": {}, "locationData": {}}
        data = (_json("POST", f"https://{host}/widgets", json=body).get("refineSearch") or {})
        batch = (data.get("data") or {}).get("jobs") or []
        for j in batch:
            loc = "; ".join(j.get("multi_location") or []) or j.get("cityStateCountry") or j.get("location")
            link = f"https://{host}/us/en/job/{j.get('jobId')}"
            job = _job(f"phenom:{host}:{j.get('jobId') or j.get('reqId')}", j.get("title"), company_name, loc,
                       posted=j.get("dateCreated") or j.get("postedDate"), publisher="Phenom", link=link,
                       description=j.get("descriptionTeaser"), employment_type=j.get("type"), req=j.get("reqId"))
            job["_enrich"] = (jsonld_details, (link,))
            jobs.append(job)
        start += len(batch)
        if len(batch) < 50 or start >= (data.get("totalHits") or 0):
            break
    return jobs


def fetch_amazon(token, company_name, query, locations=None):
    jobs, offset = [], 0
    while offset < MAX_RESULTS:
        data = _json("GET", "https://www.amazon.jobs/en/search.json",
                     params={"base_query": query or "", "result_limit": 50, "offset": offset,
                             "country": "USA", "sort": "recent"})
        batch = data.get("jobs") or []
        for j in batch:
            jobs.append(_job(f"amazon:{j.get('id_icims') or j.get('id')}", j.get("title"), company_name,
                             j.get("normalized_location") or j.get("location"),
                             posted=_parse_date(j.get("posted_date"), ["%B %d, %Y"]), publisher="Amazon",
                             link=f"https://www.amazon.jobs{j.get('job_path')}",
                             description=html_to_text(j.get("description")),
                             employment_type=j.get("job_schedule_type"), req=j.get("id_icims")))
        offset += len(batch)
        if len(batch) < 50 or offset >= (data.get("hits") or 0):
            break
    return jobs


_CONNECTORS = {
    "greenhouse": "fetch_greenhouse", "lever": "fetch_lever", "ashby": "fetch_ashby",
    "rippling": "fetch_rippling", "adp": "fetch_adp", "workday": "fetch_workday",
    "oracle": "fetch_oracle", "ukg": "fetch_ukg", "eightfold": "fetch_eightfold",
    "jibe": "fetch_jibe", "radancy": "fetch_radancy", "phenom": "fetch_phenom", "amazon": "fetch_amazon",
}


def _connector(ats):
    # Looked up at call time so tests can patch individual fetchers.
    name = _CONNECTORS.get(ats)
    return globals()[name] if name else None


# --- company check -------------------------------------------------------------

def check_company(company):
    """Search one company's openings, filtered by its keywords and locations.

    Returns (jobs, jsearch_pages_used). Direct career-site searches are free;
    only the JSearch fallback uses API quota.
    """
    keyword_text = (company.get("keywords") or "").strip()
    keywords = split_terms(keyword_text)
    locations = split_terms(company.get("locations"))
    ats = company.get("ats") or "search"
    # Search engines on these sites take one phrase; use the first keyword and filter titles for the rest.
    query = (keyword_text.split(",")[0].strip() if keyword_text else "")

    fetch = _connector(ats)
    if fetch:
        token = company.get("ats_token") or ("amazon" if ats == "amazon" else None)
        if not token:
            raise BoardError(f"No {ATS_LABELS[ats]} board ID set for {company['name']}")
        try:
            jobs = fetch(token, company["name"], query, locations)
        except (ValueError, KeyError) as e:
            raise BoardError(f"Bad board ID for {ATS_LABELS[ats]} ({token}): {e}") from e
        jobs = [j for j in jobs if title_matches(j, keywords) and location_matches(j, locations, allow_unknown=True)]
        for j in jobs[:MAX_DETAIL_FETCHES]:
            if "_enrich" in j:
                fn, args = j.pop("_enrich")
                try:
                    fn(j, *args)
                except (BoardError, ValueError, KeyError):
                    pass  # keep the job; it just won't have the extra details
        for j in jobs:
            j.pop("_enrich", None)
        # Jobs listed only as "Multiple"/"3 Locations" stay in, so nothing local gets missed.
        return [j for j in jobs if location_matches(j, locations, allow_unknown=True)], 0

    # Fallback: ask JSearch, then keep only results from this employer.
    terms = " OR ".join(k.strip() for k in keyword_text.split(",") if k.strip()) or "jobs"
    results, pages = jsearch.search(f"{terms} at {company['name']}", date_posted="month")
    name = norm(company["name"])
    own = []
    for r in results:
        employer = norm(r.get("employer_name"))
        if employer and (name in employer or employer in name):
            own.append(r)
    return [j for j in own if matches(j, keywords, locations)], pages

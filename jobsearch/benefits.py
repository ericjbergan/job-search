"""Company benefits: quick tags from posting text, and an AI-assisted lookup (ChatGPT or Claude, see ai.py).

Two levels:
  * tags_from_text(): no AI. Pulls clearly stated benefits ("6% 401(k) match",
    "15 days PTO", "pension") out of a job description.
  * lookup(): finds the company's benefits page(s) by following "benefits" links
    from the listing and careers site, has the AI pull structured facts from
    them (quotes are checked against the page text), and falls back to
    its web search when no page turns up. Every fact keeps its quote and
    source so the result can be checked rather than trusted.

Scores use the scales from the original Columbus spreadsheet (see SCALES).
"""

import html
import json
import os
import re
from urllib.parse import urljoin, urlparse

from jobsearch import ai, boards

# The 1-4 scales from the spreadsheet's ranking columns. 2 means "no info".
SCALES = {
    "retirement": {"label": "Retirement", "levels": {4: "6% or better", 3: "5%", 2: "No info", 1: "4% or less"}},
    "pto": {"label": "Sick & PTO", "levels": {4: "Unlimited or 30+ days", 3: "25-30 days", 2: "No info", 1: "24 days or less"}},
    "commute": {"label": "Commute", "levels": {4: "Remote only", 3: "No info", 2: "Hybrid, 30 min or less",
                                                 1: "On-site or hybrid, long drive", 0: "On-site, long drive"}},
    "bonus": {"label": "Bonus", "levels": {4: "Yes, 10%+", 3: "Yes, no $ info", 2: "No bonus info", 1: "No bonus"}},
}
TOPICS = {
    "retirement_match": "401(k) match", "retirement_other": "Pension / other retirement", "pto": "Vacation / PTO",
    "sick": "Sick time", "holidays": "Holidays", "bonus": "Bonus", "parental_leave": "Parental leave",
    "tuition": "Tuition", "health": "Health insurance", "other": "Other",
}
# Filler the model sometimes returns for topics the sources don't cover.
_NO_INFO = re.compile(r"\b(not (provided|specified|available|mentioned|stated|found|disclosed)|no (specific )?(information|details|data))\b", re.I)
MAX_PAGE_CHARS = 15000
# "Bonuses" that aren't an annual performance/incentive bonus.
_NOT_PAY_BONUS = re.compile(r"\b(referr\w*|sign[- ]?on|signing|retention|relocation|401\s*\(?k\)?|retirement|profit[- ]sharing)\b", re.I)


class BenefitsError(Exception):
    pass


# --- company names -------------------------------------------------------------

_SUFFIXES = re.compile(r"\b(the|inc|incorporated|llc|ltd|corp|corporation|co|company|group|plc|lp|holdings|na)\b")


def company_key(name):
    """'JPMorgan Chase & Co.' and 'JPMorgan Chase' -> 'jpmorgan chase', so jobs and companies line up."""
    name = re.sub(r"\([^)]*\)", " ", name or "")      # "American Electric Power (AEP)"
    name = re.sub(r"['’]", "", name)
    return " ".join(_SUFFIXES.sub(" ", boards.norm(name)).split())


# --- tags from posting text (no AI) ----------------------------------------------

_TAG_RULES = [
    ("retirement_match", re.compile(
        r"(\d{1,2}(?:\.\d)?)\s*%[^.;]{0,60}?(?:401\s*\(?k\)?|retirement)[^.;]{0,40}?match"
        r"|(?:401\s*\(?k\)?|retirement)[^.;]{0,50}?match[^.;]{0,40}?(\d{1,2}(?:\.\d)?)\s*%"
        r"|(?:401\s*\(?k\)?|retirement)[^.;]{0,40}?(\d{1,2}(?:\.\d)?)\s*%[^.;]{0,30}?match"
        r"|match\w*[^.;]{0,30}?(\d{1,2}(?:\.\d)?)\s*%[^.;]{0,40}?(?:401\s*\(?k\)?|retirement)", re.I),
     lambda m: f"401(k) match {next(g for g in m.groups() if g)}%"),
    ("retirement_match", re.compile(r"401\s*\(?k\)?[^.;]{0,40}?\bmatch", re.I), lambda m: "401(k) match"),
    ("retirement_other", re.compile(r"\bpension\b", re.I), lambda m: "Pension"),
    ("retirement_other", re.compile(r"\b401\s*\(?k\)?", re.I), lambda m: "401(k)"),
    ("pto", re.compile(r"\b(unlimited|flexible|discretionary) (pto|paid time off|time off|vacation)\b", re.I),
     lambda m: f"{m.group(1).capitalize()} PTO"),
    ("pto", re.compile(r"\b(\d{1,2})\s*(\+\s*)?(days|weeks)\s+(?:of\s+)?(?:paid\s+)?(pto|paid time off|vacation|time off)\b", re.I),
     lambda m: f"{m.group(1)} {m.group(3).lower()} PTO"),
    ("sick", re.compile(r"\b(\d{1,3})\s+(days|hours)\s+(?:of\s+)?(?:paid\s+)?sick\b", re.I),
     lambda m: f"{m.group(1)} {m.group(2).lower()} sick"),
    ("holidays", re.compile(r"\b(\d{1,2})\s+(?:paid\s+|company\s+)?holidays\b", re.I), lambda m: f"{m.group(1)} holidays"),
    ("bonus", re.compile(r"\b(\d{1,2})\s*%\s*(?:annual\s+|target\s+)?bonus", re.I), lambda m: f"{m.group(1)}% bonus"),
    ("bonus", re.compile(r"\b(annual|performance|incentive|target) bonus|bonus (eligib|program|plan)", re.I), lambda m: "Bonus"),
    ("parental_leave", re.compile(r"\b(\d{1,2})\s+weeks?\s+(?:of\s+)?(?:paid\s+)?(parental|maternity|paternity|family) leave", re.I),
     lambda m: f"{m.group(1)} wks parental leave"),
    ("parental_leave", re.compile(r"\bpaid (parental|maternity|paternity|family) leave\b", re.I), lambda m: "Paid parental leave"),
    ("tuition", re.compile(r"\btuition (reimbursement|assistance)\b", re.I), lambda m: "Tuition help"),
    ("other", re.compile(r"\bemployee stock purchase|\bESPP\b", re.I), lambda m: "ESPP"),
]


def tags_from_text(text):
    """Clearly stated benefits in a description: [{"topic", "label", "quote"}], most specific first per topic."""
    if not text:
        return []
    tags, seen_topics, seen_labels = [], set(), set()
    for topic, rx, label_fn in _TAG_RULES:
        m = rx.search(text)
        if not m:
            continue
        label = label_fn(m)
        # Keep one tag per topic (the first rule for a topic is the most specific), except "other".
        if (topic in seen_topics and topic != "other") or label in seen_labels:
            continue
        seen_topics.add(topic)
        seen_labels.add(label)
        start, end = max(0, m.start() - 60), min(len(text), m.end() + 60)
        tags.append({"topic": topic, "label": label, "quote": " ".join(text[start:end].split())})
    # "401(k)" alone is redundant once there's a match tag.
    if any(t["label"].startswith("401(k) match") for t in tags):
        tags = [t for t in tags if t["label"] != "401(k)"]
    return tags


# --- finding benefits pages -------------------------------------------------------

def _site(url):
    host = (urlparse(url).hostname or "").lower()
    parts = host.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def find_benefit_pages(start_urls, limit=2):
    """Follow links that mention benefits from the given pages (same site only). Returns [(url, text)]."""
    candidates = []
    for start in start_urls:
        if not start:
            continue
        try:
            page = boards._request("GET", start, retries=1, headers={"Accept": "text/html"}).text
        except boards.BoardError:
            continue
        if re.search(r"benefit", urlparse(start).path, re.I):
            candidates.append(start)
        for href, label in re.findall(r'<a[^>]+href="([^"#]+)"[^>]*>(.*?)</a>', page, re.S | re.I):
            text = boards.html_to_text(label)
            if not re.search(r"benefit|perks|total rewards|rewards", href + " " + text, re.I):
                continue
            url = urljoin(start, html.unescape(href))
            if url.startswith("http") and _site(url) == _site(start) and url not in candidates:
                candidates.append(url)
    pages = []
    for url in candidates:
        if len(pages) >= limit:
            break
        try:
            page = boards._request("GET", url, retries=1, headers={"Accept": "text/html"}).text
        except boards.BoardError:
            continue
        text = _page_text(page)
        if len(text) > 300 and re.search(r"401|retire|pto|vacation|paid time|holiday|benefit", text, re.I):
            pages.append((url, text[:MAX_PAGE_CHARS]))
    return pages


def _page_text(page):
    page = re.sub(r"(?is)<(script|style|noscript|svg|header|footer|nav)[^>]*>.*?</\1>", " ", page)
    return boards.html_to_text(page)


# --- AI (see ai.py) -----------------------------------------------------------------

_INSTRUCTIONS = f"""You extract employee benefits for a job seeker comparing employers.
Only report what the provided sources explicitly say about this employer's US employee benefits.
Never guess, never use general knowledge, and never infer numbers that aren't stated.
Leave a topic out entirely when the sources say nothing about it (no "not provided" entries).
For every fact give a short plain summary, an exact quote copied word-for-word from the source,
and the source URL. Topics: {", ".join(TOPICS)}.

Also fill in "figures" with numbers stated in the sources (null when not stated):
- employer_match_pct: the most the employer puts into the 401(k)/retirement plan as a percent of pay,
  counting the match plus any automatic employer contribution. "100% of the first 6%" = 6;
  "50% of the first 6%" = 3; "3% automatic + match up to 4%" = 7.
- pension: true if the employer offers a pension / defined-benefit plan, false only if it says it doesn't.
- pto_days: vacation/PTO days per year for a new hire (include personal/floating days; not holidays).
  Convert weeks to days (1 week = 5 days) and hours to days (8 hours = 1 day).
- sick_days: separate paid sick days per year, if listed apart from PTO.
- unlimited_pto: true if PTO/vacation is unlimited or discretionary.
- bonus: true if a cash bonus or incentive-pay plan is offered, false only if it says there is none.
  401(k)/retirement contributions, profit sharing paid into retirement, employee referral bonuses,
  sign-on, retention and relocation bonuses are NOT bonuses.
- bonus_target_pct: target annual cash bonus as a percent of salary, if stated (a range like 10-15% = 10).
Give each non-null figure a word-for-word quote in "figure_quotes" under the same name.
A figure with nothing to quote must be null.

Respond with JSON only:
{{"facts": [{{"topic": "...", "summary": "...", "quote": "...", "source_url": "..."}}],
  "figures": {{"employer_match_pct": null, "pension": null, "pto_days": null, "sick_days": null,
              "unlimited_pto": null, "bonus": null, "bonus_target_pct": null}},
  "figure_quotes": {{}}}}"""


def _parse_json(text):
    found = ai.first_json_object(text)
    if not found:
        raise BenefitsError(f"{ai.name('benefits')} didn't return a usable answer; try again")
    try:
        return json.loads(found)
    except ValueError as e:
        raise BenefitsError(f"{ai.name('benefits')}'s answer wasn't valid JSON; try again") from e


def _ask(fn, *args):
    try:
        return fn(*args)
    except ai.AIError as e:
        raise BenefitsError(str(e))


def _from_pages(company, sources):
    """sources: [(url, text)]. Facts whose quote isn't really in the text are dropped."""
    docs = "\n\n".join(f"=== SOURCE: {url}\n{text}" for url, text in sources)
    data = _parse_json(_ask(ai.json_answer, _INSTRUCTIONS, f"Employer: {company}\n\n{docs}", "benefits"))
    by_url = {url: boards.norm(text) for url, text in sources}
    facts = []
    for f in data.get("facts") or []:
        quote = boards.norm(f.get("quote"))
        hit = next((u for u, t in by_url.items() if quote and quote in t), None)
        if hit:
            facts.append({**f, "source_url": hit, "verified": True})
    data["facts"] = facts
    data["figures"], data["figure_quotes"] = _quoted_figures(data, lambda q: any(q in t for t in by_url.values()))
    return data


def _quoted_figures(data, quote_ok):
    """Keep only figures backed by a real quote (and, for fetched pages, one that's actually on the page)."""
    figures, quotes = data.get("figures") or {}, data.get("figure_quotes") or {}
    kept_f, kept_q = {}, {}
    for name, value in figures.items():
        quote = quotes.get(name)
        if value is None or not quote or _NO_INFO.search(str(quote)) or not quote_ok(boards.norm(quote)):
            continue
        if name.startswith("bonus") and _NOT_PAY_BONUS.search(str(quote)):
            continue  # referral/sign-on/401(k) "bonuses" aren't the annual bonus she's rating
        kept_f[name], kept_q[name] = value, quote
    return kept_f, kept_q


def _from_web(company):
    reply, cited = _ask(ai.web_answer, _INSTRUCTIONS, (
        f"Find {company}'s US employee benefits: 401(k) match, pension, vacation/PTO and sick days, "
        "holidays, bonus, parental leave, tuition. Prefer the employer's own careers/benefits pages, "
        "then recent reputable sources. Ignore other companies with similar names."))
    data = _parse_json(reply)
    for f in data.get("facts") or []:
        f["verified"] = False  # can't check web-search quotes against page text
        if not f.get("source_url") and cited:
            f["source_url"] = sorted(cited)[0]
    data["figures"], data["figure_quotes"] = _quoted_figures(data, lambda q: True)
    return data


def lookup(company, start_urls=(), benefits_url=None, posting_text=None, use_web=True, work_mode=None):
    """Find and summarize a company's benefits.

    Returns {"facts", "figures", "scores", "score_reasons", "method", "sources"}; scores are
    calculated from the figures with calculate_scores(), not chosen by the AI.
    """
    sources = []
    if benefits_url:
        try:
            page = boards._request("GET", benefits_url, retries=1, headers={"Accept": "text/html"}).text
            sources.append((benefits_url, _page_text(page)[:MAX_PAGE_CHARS]))
        except boards.BoardError as e:
            raise BenefitsError(f"Couldn't open the benefits page: {e}")
    else:
        sources += find_benefit_pages(list(start_urls))
    if posting_text:
        sources.append(("(this job posting)", posting_text[:MAX_PAGE_CHARS]))

    result, method = {"facts": []}, []
    if sources:
        result = _from_pages(company, sources)
        method.append("pages")
    if use_web and len(result.get("facts") or []) < 3:
        web = _from_web(company)
        method.append("web search")
        known = {boards.norm(f.get("quote")) for f in result.get("facts") or []}
        result["facts"] = (result.get("facts") or []) + [f for f in web.get("facts") or []
                                                          if boards.norm(f.get("quote")) not in known]
        for key in ("figures", "figure_quotes"):
            merged = dict(web.get(key) or {})
            merged.update({k: v for k, v in (result.get(key) or {}).items() if v not in (None, "")})
            result[key] = merged
    if not method:
        raise BenefitsError("No benefits page found and web search is off")

    facts = [f for f in result.get("facts") or []
             if f.get("topic") in TOPICS and f.get("summary") and f.get("quote") and not _NO_INFO.search(f["summary"])]
    figures = _clean_figures(result.get("figures") or {})
    scores, reasons = calculate_scores(figures, work_mode=work_mode)
    return {"facts": facts, "figures": figures, "figure_quotes": result.get("figure_quotes") or {},
            "scores": scores, "score_reasons": reasons,
            "method": " + ".join(method), "sources": [u for u, _ in sources]}


# --- scores from figures --------------------------------------------------------------

def _clean_figures(raw):
    def num(v):
        try:
            return float(str(v).replace("%", "").strip()) if v not in (None, "") else None
        except ValueError:
            return None

    def flag(v):
        if isinstance(v, bool) or v is None:
            return v
        return {"true": True, "yes": True, "false": False, "no": False}.get(str(v).strip().lower())

    return {
        "employer_match_pct": num(raw.get("employer_match_pct")),
        "pension": flag(raw.get("pension")),
        "pto_days": num(raw.get("pto_days")),
        "sick_days": num(raw.get("sick_days")),
        "unlimited_pto": flag(raw.get("unlimited_pto")),
        "bonus": flag(raw.get("bonus")),
        "bonus_target_pct": num(raw.get("bonus_target_pct")),
    }


def _fmt(n):
    return f"{n:g}"


def calculate_scores(figures, work_mode=None):
    """Apply the spreadsheet's scales to the figures. Returns (scores, reasons).

    Retirement: 4 = 6%+ (or a pension), 3 = 5%, 1 = 4% or less, 2 = no info.
    Sick & PTO: 4 = unlimited or 30+ days, 3 = 25-30, 1 = 24 or less, 2 = no info.
    Bonus: 4 = 10%+, 3 = bonus with no % given, 1 = no bonus, 2 = no info.
    Commute (needs a job): 4 = remote, 3 = not stated; hybrid/on-site depend on the drive,
    so those are left for you to rate.
    """
    f = figures or {}
    scores, reasons = {}, {}

    match = f.get("employer_match_pct")
    if f.get("pension"):
        scores["retirement"], reasons["retirement"] = 4, "Pension offered" + (f" (+{_fmt(match)}% 401k)" if match else "")
    elif match is not None:
        if match >= 6:
            scores["retirement"] = 4
        elif match >= 5:
            scores["retirement"] = 3
        else:
            scores["retirement"] = 1
        reasons["retirement"] = f"{_fmt(match)}% employer 401(k) contribution"
    else:
        scores["retirement"], reasons["retirement"] = 2, "No retirement match info found"

    pto, sick = f.get("pto_days"), f.get("sick_days")
    if f.get("unlimited_pto"):
        scores["pto"], reasons["pto"] = 4, "Unlimited PTO"
    elif pto is not None or sick is not None:
        total = (pto or 0) + (sick or 0)
        scores["pto"] = 4 if total >= 30 else 3 if total >= 25 else 1
        parts = [f"{_fmt(pto)} PTO" if pto is not None else "", f"{_fmt(sick)} sick" if sick is not None else ""]
        reasons["pto"] = f"{_fmt(total)} days/year ({' + '.join(p for p in parts if p)})"
    else:
        scores["pto"], reasons["pto"] = 2, "No PTO info found"

    target = f.get("bonus_target_pct")
    if target is not None and target >= 10:
        scores["bonus"], reasons["bonus"] = 4, f"{_fmt(target)}% target bonus"
    elif f.get("bonus") or target is not None:
        scores["bonus"] = 3
        reasons["bonus"] = f"Bonus offered ({_fmt(target)}% target)" if target is not None else "Bonus offered, no % given"
    elif f.get("bonus") is False:
        scores["bonus"], reasons["bonus"] = 1, "No bonus"
    else:
        scores["bonus"], reasons["bonus"] = 2, "No bonus info found"

    if work_mode == "remote":
        scores["commute"], reasons["commute"] = 4, "This job is remote"
    elif work_mode is None:
        scores["commute"], reasons["commute"] = 3, "Work style not stated for this job"
    # hybrid / on-site: depends on her drive, so no automatic score
    return scores, reasons

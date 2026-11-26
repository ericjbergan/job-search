"""Rank her Interested jobs by how well they match her.

Five scores, each 0-100, combined with her weights (her order of importance):
  skills & qualifications (5) - AI compares the listing with her resume
  interests (4)               - AI compares the listing with the interests she wrote
  salary (3)                  - ranked against the other jobs being compared (top pay = 100, lowest = 0)
  benefits (2)                - the company's retirement / PTO / bonus scores from the Benefits panel
  commute (1)                 - remote is best; otherwise distance from home and work style
A score that can't be worked out (no pay posted, no benefits info, no resume yet...) counts as the
middle (50) and is shown as missing, so a job with little information lands mid-pack rather than at
the top (leaving it out would let one good number carry the whole score). Scores she sets herself
always win.
"""

MISSING_SCORE = 50

import hashlib
import json
import re
from concurrent.futures import ThreadPoolExecutor

from jobsearch import ai, benefits, db, geo

CRITERIA = [  # (name, label, weight)
    ("skills", "Skills & qualifications", 5),
    ("interests", "Interests", 4),
    ("salary", "Salary", 3),
    ("benefits", "Benefits", 2),
    ("commute", "Commute", 1),
]
MAX_TEXT = 15000
PROMPT_VERSION = "1"
_YEARLY = {"YEAR": 1, "MONTH": 12, "WEEK": 52, "DAY": 260, "HOUR": 2080}

_INSTRUCTIONS = """You help a job seeker judge how well a job listing fits her. You get her resume, the
interests she wrote, and the listing. Respond with JSON only:
{"skills": {"score": 0, "matched": [], "partial": [], "missing": [], "reason": ""},
 "interests": {"score": 0, "reason": ""}}

skills.score (0-100): how well her experience, skills, tools, certifications and education meet the
listing's qualifications. Required qualifications count more than preferred ones.
  90-100 meets nearly all required and most preferred; 70-89 meets most required;
  50-69 meets about half; 30-49 misses several required; below 30 misses most required.
skills.matched / partial / missing: the listing's qualifications she meets, partly meets, or lacks,
as short phrases (up to 8 each). Judge only from what the resume actually says.
skills.reason: one plain sentence.

interests.score (0-100): how well the role, industry, kind of work and setting fit the interests
she wrote, including anything she says she wants to avoid. Use 50 when her interests say nothing
that applies. interests.reason: one plain sentence."""


# --- the AI part (skills, interests) ------------------------------------------------------

def _clean_text(text):
    return re.sub(r"\n{3,}", "\n\n", (text or "").strip())[:MAX_TEXT]


def _ai_hash(profile, description):
    blob = "\x1f".join([PROMPT_VERSION, profile["resume"], profile["interests"], description or ""])
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]


def _score(value):
    try:
        return max(0.0, min(100.0, float(value)))
    except (TypeError, ValueError):
        return None


def _phrases(items):
    return [" ".join(str(x).split())[:120] for x in (items or []) if str(x).strip()][:8]


def read_match(profile, description):
    """Ask the AI how a listing matches her resume and interests. -> (skills, interests, details)"""
    text = (f"=== HER RESUME\n{profile['resume'][:MAX_TEXT]}\n\n"
            f"=== HER INTERESTS\n{profile['interests'] or '(none given)'}\n\n"
            f"=== JOB LISTING\n{_clean_text(description)}")
    reply = ai.json_answer(_INSTRUCTIONS, text, task="matching")
    data = json.loads(ai.first_json_object(reply) or "{}")
    s, i = data.get("skills") or {}, data.get("interests") or {}
    details = {"matched": _phrases(s.get("matched")), "partial": _phrases(s.get("partial")),
               "missing": _phrases(s.get("missing")), "skills_reason": str(s.get("reason") or "")[:300],
               "interests_reason": str(i.get("reason") or "")[:300]}
    interests = _score(i.get("score")) if profile["interests"] else None
    return _score(s.get("score")), interests, details


def jobs_needing_ai(jobs):
    """Jobs (with descriptions) whose AI scores are missing or out of date."""
    profile = db.get_profile()
    if not profile["resume"]:
        return profile, []
    matches = db.get_matches(j["id"] for j in jobs)
    todo = []
    for j in jobs:
        desc = j.get("description") or ""
        if len(desc) < 200:
            continue
        if (matches.get(j["id"]) or {}).get("ai_hash") != _ai_hash(profile, desc):
            todo.append(j)
    return profile, todo


def score_pending(status="interested", limit=6, workers=3):
    """Have the AI score up to `limit` jobs that need it. Returns read / remaining / errors."""
    if not ai.has_key("matching"):
        raise ai.AIError(f"Scoring needs {ai.key_name('matching')} in .env")
    jobs = [db.get_job(j["id"]) for j in db.list_jobs(status=status)]
    profile, todo = jobs_needing_ai(jobs)
    if not profile["resume"]:
        raise ai.AIError("Add her resume first (Her profile, in the sidebar)")
    batch, errors = todo[:limit], []

    def one(job):
        try:
            skills, interests, details = read_match(profile, job["description"])
            db.save_match_ai(job["id"], _ai_hash(profile, job["description"]), skills, interests, details)
            return True
        except Exception as e:  # tried again next time
            errors.append(f"{job.get('title')}: {e}")
            return False

    with ThreadPoolExecutor(max_workers=workers) as pool:
        done = sum(pool.map(one, batch))
    return {"read": done, "remaining": len(todo) - done, "errors": errors[:3]}


# --- the calculated parts (salary, benefits, commute) --------------------------------------

def yearly_pay(job):
    """Middle of the posted range, as yearly pay (hourly x 2080 and so on). None if not posted."""
    lo, hi = job.get("salary_min"), job.get("salary_max")
    if not lo and not hi:
        return None
    mid = ((lo or hi) + (hi or lo)) / 2
    return mid * _YEARLY.get((job.get("salary_period") or "YEAR").upper(), 1)


def salary_scores(jobs):
    """Rank pay against the other jobs: highest = 100, lowest = 0, ties share a score."""
    pays = {j["id"]: yearly_pay(j) for j in jobs}
    known = sorted(p for p in pays.values() if p)
    out = {}
    for job_id, p in pays.items():
        if not p:
            continue
        if len(known) == 1:
            out[job_id] = (100.0, f"~${p:,.0f}/yr (the only one with pay posted)")
            continue
        below = sum(1 for q in known if q < p)
        same = sum(1 for q in known if q == p) - 1
        score = 100 * (below + same / 2) / (len(known) - 1)
        out[job_id] = (round(score), f"~${p:,.0f}/yr, #{len(known) - below - same} of {len(known)} by pay")
    return out


def benefits_score(record):
    """Average of the company's retirement, PTO and bonus scores (1-4 scales; "no info" left out)."""
    if not record:
        return None
    parts = []
    for key, label in (("retirement_score", "retirement"), ("pto_score", "PTO"), ("bonus_score", "bonus")):
        v = record.get(key)
        if v is None or v == 2:  # 2 means "no info" on these scales
            continue
        parts.append(((max(1.0, min(4.0, v)) - 1) / 3 * 100, f"{label} {v:g}/4"))
    if not parts:
        return None
    return round(sum(p for p, _ in parts) / len(parts)), ", ".join(r for _, r in parts)


def commute_score(job, home):
    """Remote 100. Hybrid and on-site go down with distance from home (on-site faster)."""
    mode = job.get("work_mode")
    if mode == "remote" or (job.get("is_remote") and not mode):
        return 100, "Remote"
    points = geo.parse_points(job.get("location") or "")
    if not home or not points:
        return None
    miles = min(geo.miles(home, p) for p in points)
    if mode == "hybrid":
        return round(max(10, 90 - miles * 1.2)), f"Hybrid, ~{miles:.0f} miles from home"
    if mode == "onsite":
        return round(max(0, 75 - miles * 1.5)), f"On-site, ~{miles:.0f} miles from home"
    return round(max(5, 60 - miles * 1.2)), f"Work style not stated, ~{miles:.0f} miles from home"


# --- putting it together ------------------------------------------------------------------

def rank(status="interested"):
    """Score every job with this status. Returns {"jobs": {job id: breakdown}, "profile_ready": bool}."""
    jobs = [db.get_job(j["id"]) for j in db.list_jobs(status=status)]
    profile = db.get_profile()
    matches = db.get_matches(j["id"] for j in jobs)
    pay = salary_scores(jobs)
    home = (geo.parse_points(db.get_area_settings()[0]) or [None])[0]
    by_company = db.benefits_by_key()
    _, stale = jobs_needing_ai(jobs)
    stale_ids = {j["id"] for j in stale}

    out = {}
    for j in jobs:
        m = matches.get(j["id"]) or {}
        d = m.get("details") or {}
        auto = {
            "skills": (m.get("skills_score"), d.get("skills_reason")) if m.get("skills_score") is not None else None,
            "interests": (m.get("interests_score"), d.get("interests_reason")) if m.get("interests_score") is not None else None,
            "salary": pay.get(j["id"]),
            "benefits": benefits_score(by_company.get(benefits.company_key(j.get("company") or ""))),
            "commute": commute_score(j, home),
        }
        rows, total, weight_sum = [], 0.0, 0
        for name, label, weight in CRITERIA:
            override = m.get(f"{name}_override")
            if override is not None:
                score, reason, source = override, "Your score", "yours"
            elif auto[name]:
                (score, reason), source = auto[name], "auto"
            else:
                score, reason, source = None, _missing_reason(name, j, profile), "missing"
            total += weight * (MISSING_SCORE if score is None else score)
            weight_sum += weight
            rows.append({"name": name, "label": label, "weight": weight, "score": None if score is None else round(score),
                         "reason": reason, "source": source})
        known = sum(1 for r in rows if r["score"] is not None)
        out[j["id"]] = {
            "score": round(total / weight_sum) if known else None,
            "based_on": known,
            "criteria": rows,
            "matched": d.get("matched", []), "partial": d.get("partial", []), "missing": d.get("missing", []),
            "needs_ai": j["id"] in stale_ids,
        }
    return {"jobs": out, "profile_ready": bool(profile["resume"]), "needs_ai": len(stale_ids),
            "weights": [{"name": n, "label": l, "weight": w} for n, l, w in CRITERIA]}


def _missing_reason(name, job, profile):
    if name in ("skills", "interests"):
        if not profile["resume"]:
            return "Add her resume to score this"
        if name == "interests" and not profile["interests"]:
            return "Add her interests to score this"
        if len(job.get("description") or "") < 200:
            return "No description yet (try Reload from listing)"
        return "Not scored yet (click Score jobs)"
    return {"salary": "Pay not posted", "benefits": "No benefits info for this company",
            "commute": "Location not known"}[name]

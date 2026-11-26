"""Rank her jobs (every job except Rejected and Not a fit) by how much they matter to her.

Seven parts, weighted (Jen's "Rank my jobs based on criteria"):
  Qualify (3)    - AI compares the listing with her resume (0-100), ranked against the other jobs
  Interest (3)   - AI estimates the job's mix of work against her interest lists (0-100), ranked
  Salary (2)     - rating on the top of the pay range: 5 $135k+, 4 $125-135k, 3 not listed,
                   2 $120-125k, 1 under $120k
  Retirement (2), Sick & PTO (1), Bonus (1) - the company's 1-4 ratings from the Benefits panel
                   (2 = no info, as in her spreadsheet)
  Commute (1)    - the spreadsheet's 0-4 commute scale from work style and distance
Ratings are put on 0-1; Qualify and Interest count by their rank (1st = 1, last = 0). The weighted
mix is the overall score, and the overall rank number orders the jobs
(1 = best). A job not yet read by the AI counts as middling for Qualify / Interest. Scores she sets
herself for Qualify / Interest always win.
"""

import hashlib
import json
import re
from concurrent.futures import ThreadPoolExecutor

from jobsearch import ai, benefits, db, geo

CRITERIA = [  # (name, label, weight)
    ("qualify", "Qualify", 3),
    ("interest", "Interest", 3),
    ("salary", "Salary", 2),
    ("retirement", "Retirement", 2),
    ("pto", "Sick & PTO", 1),
    ("commute", "Commute", 1),
    ("bonus", "Bonus", 1),
]
SCALES = {"qualify": (0, 100), "interest": (0, 100), "salary": (1, 5), "retirement": (1, 4),
          "pto": (1, 4), "commute": (0, 4), "bonus": (1, 4)}
NO_INFO = {"retirement_score": 2, "pto_score": 2, "bonus_score": 2}  # the spreadsheet's "no info" values
NEAR_MILES = 25          # about a 30-minute drive
SKIP_STATUSES = ("rejected", "not_a_fit")
MAX_TEXT = 15000
PROMPT_VERSION = "2"
_YEARLY = {"YEAR": 1, "MONTH": 12, "WEEK": 52, "DAY": 260, "HOUR": 2080}

# Her interests come in three lists. The AI estimates what share of a job's work falls in each,
# and the interests score is the weighted mix (so the weighting is set here or by her, not by the AI).
# These are the defaults; her own weights (profile/interests.txt or the card) replace them.
MIX_VALUES = {"likes": 100, "ok": 55, "minimal": 0, "other": 35}
MIX_LABELS = {"likes": "work she likes", "ok": "fine in small doses", "minimal": "keep to a minimum",
              "other": "not on her lists"}
# When she also wrote "anything else" (industries, things to avoid), it counts this much.
OTHER_PREFS_WEIGHT = 0.2

_INSTRUCTIONS = """You help a job seeker judge how well a job listing fits her. You get her resume, her
interests, and the listing. Respond with JSON only:
{"skills": {"score": 0, "matched": [], "partial": [], "missing": [], "reason": ""},
 "interests": {"likes": 0, "ok": 0, "minimal": 0, "other": 0, "prefs_score": null, "reason": ""}}

skills.score (0-100): how well her experience, skills, tools, certifications and education meet the
listing's qualifications. Required qualifications count more than preferred ones.
  90-100 meets nearly all required and most preferred; 70-89 meets most required;
  50-69 meets about half; 30-49 misses several required; below 30 misses most required.
skills.matched / partial / missing: the listing's qualifications she meets, partly meets, or lacks,
as short phrases (up to 8 each). Judge only from what the resume actually says (read it carefully:
a tool or skill named anywhere in the resume counts as met).
skills.reason: one plain sentence.

interests: estimate how the job's day-to-day work divides among her lists, as percentages that add up
to 100: "likes" (work she likes), "ok" (fine as a smaller part of the job), "minimal" (work she wants
kept to a minimum), "other" (work on none of her lists, e.g. sales or coding). Judge from the
responsibilities and the job title: a role that is mainly product owner, project manager, people
manager or strategy work puts most of its share in those lists, even if it mentions analysis.
interests.prefs_score (0-100, or null if she wrote nothing under OTHER PREFERENCES): how well the job
fits those other preferences. interests.reason: one plain sentence naming the main kinds of work."""


# --- the AI part (skills, interests) ------------------------------------------------------

def _clean_text(text):
    return re.sub(r"\n{3,}", "\n\n", (text or "").strip())[:MAX_TEXT]


def _ai_hash(profile, description):
    parts = [PROMPT_VERSION] + [profile.get(k, "") for k in ("resume", "likes", "ok", "minimal", "interests")]
    blob = "\x1f".join(parts + [description or ""])
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]


def has_interests(profile):
    return any(profile.get(k) for k in ("likes", "ok", "minimal", "interests"))


def _interests_text(profile):
    sections = [("WORK SHE LIKES", "likes"), ("FINE AS A SMALLER PART OF THE JOB", "ok"),
                ("KEEP TO A MINIMUM", "minimal"), ("OTHER PREFERENCES", "interests")]
    return "\n\n".join(f"--- {title}\n{profile.get(key) or '(none given)'}" for title, key in sections)


def interests_from_mix(answer, profile):
    """The interests score from the AI's estimate of the job's mix of work.
    -> (score, mix as whole percentages) or (None, None) when there's nothing to go on."""
    try:
        raw = {k: max(0.0, float(answer.get(k) or 0)) for k in MIX_VALUES}
    except (TypeError, ValueError):
        return None, None
    total = sum(raw.values())
    if not total:
        return None, None
    mix = {k: round(v * 100 / total) for k, v in raw.items()}
    return interest_score(mix, _score(answer.get("prefs_score")), profile), mix


def mix_values(profile):
    """How much each kind of work counts: her weights where she set them, else the defaults."""
    out = {}
    for k, default in MIX_VALUES.items():
        try:
            v = float(profile.get(f"{k}_weight"))
        except (TypeError, ValueError):
            v = default
        out[k] = max(0.0, min(100.0, v))
    return out


def interest_score(mix, prefs, profile):
    """Interest score from the job's mix of work (percentages) and her weights. Worked out again at
    ranking time, so changing the weights re-ranks without reading the listings again."""
    values = mix_values(profile)
    total = sum(mix.get(k, 0) for k in values) or 100
    score = sum(mix.get(k, 0) * values[k] for k in values) / total
    prefs = prefs if profile.get("interests") else None
    if prefs is not None and any(profile.get(k) for k in ("likes", "ok", "minimal")):
        score = score * (1 - OTHER_PREFS_WEIGHT) + prefs * OTHER_PREFS_WEIGHT
    elif prefs is not None:
        score = prefs  # only "anything else" was given
    return score


# Contact details aren't needed to judge fit, so they're left out of what's sent to the AI.
_CONTACT = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+|\(?\b\d{3}\)?[\s.-]*\d{3}[\s.-]*\d{4}\b|\b(?:mobile|phone|cell|email)\s*:?", re.I)


def resume_for_ai(resume):
    """Her resume without email addresses and phone numbers (the file keeps them)."""
    lines = []
    for line in (resume or "").split("\n"):
        cleaned = _CONTACT.sub("", line)
        cleaned = re.sub(r"(\s*\|\s*)+$", "", re.sub(r"^\s*(\|\s*)+", "", re.sub(r"(\s*\|\s*){2,}", " | ", cleaned)))
        lines.append(cleaned.rstrip())
    return "\n".join(lines)


def _score(value):
    try:
        return max(0.0, min(100.0, float(value)))
    except (TypeError, ValueError):
        return None


def _phrases(items):
    return [" ".join(str(x).split())[:120] for x in (items or []) if str(x).strip()][:8]


def read_match(profile, description):
    """Ask the AI how a listing matches her resume and interests. -> (skills, interests, details)"""
    text = (f"=== HER RESUME\n{resume_for_ai(profile['resume'])[:MAX_TEXT]}\n\n"
            f"=== HER INTERESTS\n{_interests_text(profile)}\n\n"
            f"=== JOB LISTING\n{_clean_text(description)}")
    reply = ai.json_answer(_INSTRUCTIONS, text, task="matching")
    data = json.loads(ai.first_json_object(reply) or "{}")
    s, i = data.get("skills") or {}, data.get("interests") or {}
    interests, mix = interests_from_mix(i, profile) if has_interests(profile) else (None, None)
    reason = str(i.get("reason") or "").strip()
    if mix:
        parts = [f"{mix[k]}% {MIX_LABELS[k]}" for k in ("likes", "ok", "minimal", "other") if mix[k]]
        reason = f"{reason} ({', '.join(parts)})" if reason else ", ".join(parts)
    details = {"matched": _phrases(s.get("matched")), "partial": _phrases(s.get("partial")),
               "missing": _phrases(s.get("missing")), "skills_reason": str(s.get("reason") or "")[:300],
               "interests_reason": reason[:400], "work_mix": mix, "prefs_score": _score(i.get("prefs_score"))}
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


def score_pending(limit=6, workers=3):
    """Have the AI score up to `limit` ranked jobs that need it. Returns read / remaining / errors."""
    if not ai.has_key("matching"):
        raise ai.AIError(f"Ranking needs {ai.key_name('matching')} in .env")
    jobs = rankable_jobs()
    profile, todo = jobs_needing_ai(jobs)
    if not profile["resume"]:
        raise ai.AIError("Add her resume first (Resume & interests, in the sidebar)")
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


def rankable_jobs():
    """Every saved job except Rejected and Not a fit, with descriptions."""
    return [db.get_job(j["id"]) for j in db.list_jobs() if j["status"] not in SKIP_STATUSES]


# --- the ratings (salary, retirement, PTO, commute, bonus) ----------------------------------

def top_of_range(job):
    """High end of the posted pay, as yearly pay (hourly x 2080 and so on). None if not posted."""
    top = job.get("salary_max") or job.get("salary_min")
    if not top:
        return None
    return top * _YEARLY.get((job.get("salary_period") or "YEAR").upper(), 1)


def salary_rating(job):
    """Her scale, on the high end of the range: 5 = $135k+, 4 = $125-135k, 3 = not posted,
    2 = $120-125k, 1 = under $120k."""
    top = top_of_range(job)
    if top is None:
        return 3, "No salary listed"
    rating = 5 if top >= 135000 else 4 if top >= 125000 else 2 if top >= 120000 else 1
    return rating, f"Top of range ~${top:,.0f}/yr"


def benefit_rating(record, field, label):
    """The company's score from the Benefits panel (1-4 scale; 2 = no info when there's none)."""
    v = (record or {}).get(field)
    if v is None:
        return NO_INFO[field], f"No {label} info"
    return v, f"{label.capitalize()} {v:g} (Benefits panel)"


def commute_rating(job, home, record):
    """The spreadsheet's commute scale: 4 remote, 3 no info, 2 hybrid within ~30 minutes,
    1 on-site nearby or hybrid with a long drive, 0 on-site with a long drive."""
    mode = job.get("work_mode")
    if mode == "remote" or (job.get("is_remote") and not mode):
        return 4, "Remote"
    points = geo.parse_points(job.get("location") or "")
    if mode in ("hybrid", "onsite") and home and points:
        miles = min(geo.miles(home, p) for p in points)
        near = miles <= NEAR_MILES
        if mode == "hybrid":
            return (2 if near else 1), f"Hybrid, ~{miles:.0f} miles from home"
        return (1 if near else 0), f"On-site, ~{miles:.0f} miles from home"
    if record and record.get("commute_score") is not None:
        return record["commute_score"], f"Commute {record['commute_score']:g} (Benefits panel)"
    return 3, "No commute info"


def _norm(name, value):
    """A rating or score on 0-1, so different scales can be weighted together."""
    lo, hi = SCALES[name]
    return (max(lo, min(hi, value)) - lo) / (hi - lo)


def _competition_rank(values):
    """{id: value} -> {id: rank}, highest value = 1, ties share a rank (1, 2, 2, 4)."""
    ordered = sorted(values.values(), reverse=True)
    return {k: ordered.index(v) + 1 for k, v in values.items()}


# --- putting it together ------------------------------------------------------------------

def rank():
    """Rank every job except Rejected / Not a fit.

    Returns {"jobs": {job id: breakdown}, "count", "profile_ready", "needs_ai", "weights"}. Each
    breakdown has overall_rank / overall (0-100), qualify and interest (score, rank, summary) and
    the five ratings."""
    jobs = rankable_jobs()
    profile = db.get_profile()
    matches = db.get_matches(j["id"] for j in jobs)
    home = (geo.parse_points(db.get_area_settings()[0]) or [None])[0]
    by_company = db.benefits_by_key()
    _, stale = jobs_needing_ai(jobs)
    stale_ids = {j["id"] for j in stale}

    out = {}
    for j in jobs:
        m = matches.get(j["id"]) or {}
        d = m.get("details") or {}
        record = by_company.get(benefits.company_key(j.get("company") or ""))
        parts = {}
        for name, ai_field, override_field, reason_key in (("qualify", "skills_score", "skills_override", "skills_reason"),
                                                         ("interest", "interests_score", "interests_override", "interests_reason")):
            if m.get(override_field) is not None:
                parts[name] = {"score": m[override_field], "reason": "Your score", "source": "yours"}
            elif m.get(ai_field) is not None:
                score = m[ai_field]
                if name == "interest" and d.get("work_mix"):  # her current weights, no new AI call
                    score = interest_score(d["work_mix"], d.get("prefs_score"), profile)
                parts[name] = {"score": score, "reason": d.get(reason_key) or "", "source": "auto"}
            else:
                parts[name] = {"score": None, "reason": _missing_reason(name, j, profile), "source": "missing"}
        ratings = {"salary": salary_rating(j),
                   "retirement": benefit_rating(record, "retirement_score", "retirement"),
                   "pto": benefit_rating(record, "pto_score", "sick & PTO"),
                   "commute": commute_rating(j, home, record),
                   "bonus": benefit_rating(record, "bonus_score", "bonus")}
        for name, (value, reason) in ratings.items():
            parts[name] = {"score": value, "reason": reason, "source": "auto"}

        out[j["id"]] = {
            "parts": parts,
            "matched": d.get("matched", []), "partial": d.get("partial", []), "missing": d.get("missing", []),
            "work_mix": d.get("work_mix"),
            "needs_ai": j["id"] in stale_ids,
        }

    # Qualify / Interest rank numbers (1 = best), for jobs that have those scores.
    for name in ("qualify", "interest"):
        scored = {k: v["parts"][name]["score"] for k, v in out.items() if v["parts"][name]["score"] is not None}
        ranks = _competition_rank(scored)
        for k, v in out.items():
            v["parts"][name]["rank"] = ranks.get(k)
            v["parts"][name]["of"] = len(scored)

    # Overall: Qualify and Interest count by their RANK (her spec: "Qualify ranking", "Interest ranking"),
    # spread from 1 (ranked first) to 0 (ranked last). The AI's raw scores bunch together (most BA jobs
    # land 70-90), so weighting the scores would let the 1-5 ratings drown them out.
    criteria = criteria_weights(profile)
    weight_sum = sum(w for _, _, w in criteria) or 1
    for v in out.values():
        total = 0.0
        for name, _, w in criteria:
            p = v["parts"][name]
            if name in ("qualify", "interest"):
                value = 0.5 if p["rank"] is None else (1.0 if p["of"] <= 1 else 1 - (p["rank"] - 1) / (p["of"] - 1))
            else:
                value = _norm(name, p["score"])
            total += w * value
        v["overall"] = round(100 * total / weight_sum)
    overall = _competition_rank({k: v["overall"] for k, v in out.items()})
    for k, v in out.items():
        v["overall_rank"] = overall[k]
    for v in out.values():
        for name, label, weight in criteria:
            v["parts"][name].update(label=label, weight=weight)
    return {"jobs": out, "count": len(out), "profile_ready": bool(profile["resume"]), "needs_ai": len(stale_ids),
            "weights": [{"name": n, "label": l, "weight": w} for n, l, w in criteria]}


def criteria_weights(profile):
    """CRITERIA with her own Rank Jobs weights where she set them (0-10; blank = the default)."""
    out = []
    for name, label, default in CRITERIA:
        try:
            w = float(profile.get(f"weight_{name}"))
        except (TypeError, ValueError):
            w = default
        w = max(0.0, min(10.0, w))
        out.append((name, label, int(w) if w == int(w) else w))
    return out


def _missing_reason(name, job, profile):
    if not profile["resume"]:
        return "Add her resume to rank this"
    if name == "interest" and not has_interests(profile):
        return "Add her interests to rank this"
    if len(job.get("description") or "") < 200:
        return "No description yet (try Reload from listing)"
    return "Not ranked yet (click Rank Jobs)"

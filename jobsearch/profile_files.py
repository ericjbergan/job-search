"""Her resume and interests as files in the repo (profile/), so a fresh install fills them in.

profile/Resume.docx (or resume.txt) - her resume.
profile/interests.txt                - her interest lists, each under a heading with its weight:

    [Work she likes] 100
    Requirements gathering
    ...
    [Fine in small doses] 55
    [Keep to a minimum] 0
    [Not on her lists] 35
    [Anything else]

The Resume & interests card fills from these when it's empty, and "Load from profile files"
replaces what's in the card with them.
"""

import os
import re

from jobsearch import ranking_files

PROFILE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "profile")
RESUME_NAMES = ("Resume.docx", "resume.docx", "Resume.txt", "resume.txt")
INTERESTS_NAME = "interests.txt"

# Heading (lower case, words only) -> (profile field for the list, profile field for the weight)
HEADINGS = {
    "work she likes": ("likes", "likes_weight"),
    "fine in small doses": ("ok", "ok_weight"),
    "keep to a minimum": ("minimal", "minimal_weight"),
    "not on her lists": (None, "other_weight"),
    "anything else": ("interests", None),
}


def profile_dir():
    return os.getenv("JOBSEARCH_PROFILE_DIR") or PROFILE_DIR


def read_resume():
    """Resume text from profile/, or None if there's no resume file."""
    for name in RESUME_NAMES:
        path = os.path.join(profile_dir(), name)
        if os.path.exists(path):
            with open(path, "rb") as f:
                return ranking_files.resume_text(name, f.read())
    return None


# "[Rank Jobs weights]" lines ("Qualify: 3") -> profile field
RANK_WEIGHTS = {"qualify": "weight_qualify", "interest": "weight_interest", "salary": "weight_salary",
                "retirement": "weight_retirement", "sick pto": "weight_pto", "pto": "weight_pto",
                "commute": "weight_commute", "bonus": "weight_bonus"}


def parse_interests(text):
    """interests.txt text -> {"likes": "...", "ok": ..., "minimal": ..., "interests": ...,
    "likes_weight": "100", ..., "weight_qualify": "3", ...}. Unknown headings are ignored."""
    out, field, in_weights = {}, None, False
    lines = {}
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = re.fullmatch(r"\[([^\]]+)\]\s*(-?\d+(?:\.\d+)?)?", line)
        if m:
            heading = " ".join(re.findall(r"[a-z]+", m.group(1).lower()))
            in_weights = heading == "rank jobs weights"
            field, weight_field = HEADINGS.get(heading, (None, None))
            if weight_field and m.group(2) is not None:
                out[weight_field] = m.group(2)
            if field:
                lines.setdefault(field, [])
            continue
        if in_weights:
            w = re.fullmatch(r"(.+?)\s*[:=]\s*(-?\d+(?:\.\d+)?)", line)
            name = " ".join(re.findall(r"[a-z]+", w.group(1).lower())) if w else ""
            if name in RANK_WEIGHTS:
                out[RANK_WEIGHTS[name]] = w.group(2)
            continue
        if field:
            lines[field].append(line)
    for field, items in lines.items():
        out[field] = "\n".join(items)
    return out


def read_interests():
    """Interest lists and weights from profile/interests.txt, or None if there's no file."""
    path = os.path.join(profile_dir(), INTERESTS_NAME)
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8-sig") as f:
        return parse_interests(f.read())


def read_all():
    """Everything the profile files hold, as profile fields (missing files are just left out)."""
    fields = {}
    resume = read_resume()
    if resume:
        fields["resume"] = resume
    fields.update(read_interests() or {})
    return fields

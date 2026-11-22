"""Work style (remote / hybrid / on-site) for a job.

Some platforms have a field for it (Workday's remoteType, Ashby/Lever/Rippling
workplaceType). Otherwise it's usually stated in the location ("Ohio Remote"),
the title ("... (Hybrid)") or somewhere in the description text, which is a
guess and is marked as such so the UI can say "check the listing".
"""

import re

MODES = ("remote", "hybrid", "onsite")
LABELS = {"remote": "Remote", "hybrid": "Hybrid", "onsite": "On-site"}

_HYBRID = re.compile(r"\bhybrid\b", re.I)
_REMOTE_WORD = re.compile(r"\b(remote|telecommut\w*|work from home|wfh|virtual)\b", re.I)
_ONSITE_WORD = re.compile(r"\b(on-?site|in[- ]office|office|in[- ]person)\b", re.I)

# Recruiters' LinkedIn tags (#LI-Hybrid, #LI-Remote, #LI-Onsite) are deliberate labels, so they count as listed.
_LI_TAG = re.compile(r"#LI[- ](hybrid|remote|on-?site)\b", re.I)

# In descriptions, only phrases that describe *this* job count; a bare "remote"
# shows up in benefits boilerplate ("remote learning stipend") too often.
_DESC_REMOTE = re.compile(
    r"\b((fully|100%|completely|entirely)\s+remote|remote[- ](first|position|role|opportunity|eligible|work)"
    r"|work (from|at) home|(this|the) (is a |)(role|position|job) is (fully |)remote"
    r"|telecommut\w*)\b", re.I)
_DESC_ONSITE = re.compile(
    r"\b((this|the) (role|position|job) is (an )?(on-?site|in[- ]office)|on-?site (role|position|job)"
    r"|(must|required to) (work|be) (on-?site|in[- ]office|in the office)|fully on-?site|100% on-?site"
    r"|in[- ]office (role|position|5 days))\b", re.I)


# Strong signs a job flagged "remote" is really hybrid or on-site. Stricter than the
# phrases detect() uses, because a hit here drops the job from results.
_NOT_REMOTE = re.compile(
    r"\b(hybrid (role|position|schedule|work|model|work schedule|environment|opportunity)"
    r"|(this|the) (role|position|job) is (a )?(hybrid|on-?site|in[- ]office)"
    r"|\d\s*(\+\s*)?days? (per|a|each) week (in|at) (the |our )?(office|on-?site)"
    r"|(must|required to) (work|be) (on-?site|in[- ]office|in the office)|fully on-?site|100% on-?site"
    r"|#LI[- ](hybrid|on-?site))\b", re.I)


def contradicts_remote(title=None, location=None, description=None):
    """True when the title, location or description clearly says hybrid/on-site."""
    for text in (title, location):
        if text and (_HYBRID.search(text) or re.search(r"\bon-?site\b", text, re.I)):
            return True
    return bool(description and _NOT_REMOTE.search(description))


def normalize(value):
    """Map a platform's value ('Hybrid', 'REMOTE', 'Office', 'OnSite', 'TELECOMMUTE'...) to remote/hybrid/onsite."""
    if not value:
        return None
    v = str(value)
    if _HYBRID.search(v) or "flex" in v.lower():
        return "hybrid"
    if _REMOTE_WORD.search(v):
        return "remote"
    if _ONSITE_WORD.search(v) or re.search(r"on_?site", v, re.I):
        return "onsite"
    return None


def detect(listed=None, location=None, title=None, description=None, is_remote=False):
    """Return (mode, source): source is 'listed' when the site said so, 'description' when inferred."""
    mode = normalize(listed)
    if mode:
        return mode, "listed"
    for text in (location, title):
        if text and _HYBRID.search(text):
            return "hybrid", "listed"
        if text and _REMOTE_WORD.search(text):
            return "remote", "listed"
    if is_remote:
        return "remote", "listed"
    tag = _LI_TAG.search(description or "")
    if tag:
        return normalize(tag.group(1)), "listed"
    if description:
        if _HYBRID.search(description):
            return "hybrid", "description"
        if _DESC_REMOTE.search(description):
            return "remote", "description"
        if _DESC_ONSITE.search(description):
            return "onsite", "description"
    return None, None

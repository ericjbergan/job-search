"""Which certifications (CBAP, PMP...) and tools (Power BI, SQL, Jira...) listings ask for.

Each saved listing is read once by the AI used for listings (ChatGPT by default; see ai.py).
What it finds is stored per job (db.job_skills), and is read again only if the description
changes. Names are grouped so "PowerBI", "Power BI" and "Microsoft Power BI" count as one.
"""

import json
import re
from concurrent.futures import ThreadPoolExecutor

from jobsearch import ai, db

MAX_TEXT = 15000
NEEDS = ("required", "preferred", "mentioned")

_INSTRUCTIONS = """You read a job listing and list the professional certifications and the named
software tools it asks for.

- "certs": professional certifications or credentials (e.g. CBAP, CCBA, ECBA, PMI-PBA, PMP, CAPM,
  Certified ScrumMaster, SAFe Agilist, Six Sigma Green Belt, ITIL, AWS Certified ...). Use the usual short
  name when there is one (CBAP, PMP, CSM). Not college degrees.
- "tools": named software, platforms, languages or applications (e.g. Power BI, Tableau, Excel, SQL,
  Jira, Confluence, Visio, Salesforce, ServiceNow, SAP, Workday, Azure DevOps, SharePoint, Python).
  Not methods or skills (Agile, Scrum, requirements gathering, data analysis, communication).
- "need" for each: "required" (listed as required, minimum or must-have), "preferred" (preferred,
  desired, nice to have, a plus, bonus), or "mentioned" (in the listing but neither).
Only list what the listing actually names. List each item once.

Respond with JSON only:
{"certs": [{"name": "...", "need": "required"}], "tools": [{"name": "...", "need": "preferred"}]}"""

# Spellings that mean the same thing -> the name to show. Keys are compared after key().
ALIASES = {
    "powerbi": "Power BI", "power bi desktop": "Power BI", "power bi": "Power BI",
    "excel": "Excel", "advanced excel": "Excel", "office": "Microsoft Office", "office 365": "Microsoft Office",
    "365": "Microsoft Office", "microsoft 365": "Microsoft Office", "microsoft office": "Microsoft Office",
    "office suite": "Microsoft Office", "visio": "Visio", "word": "Word", "powerpoint": "PowerPoint",
    "jira": "Jira", "confluence": "Confluence", "sharepoint": "SharePoint", "teams": "Teams",
    "azure devops": "Azure DevOps", "ado": "Azure DevOps", "vsts": "Azure DevOps",
    "sql": "SQL", "sql queries": "SQL", "tsql": "SQL", "t sql": "SQL", "structured query language": "SQL",
    "tableau": "Tableau", "salesforce": "Salesforce", "salesforce com": "Salesforce", "servicenow": "ServiceNow",
    "service now": "ServiceNow", "sap": "SAP", "workday": "Workday", "python": "Python", "lucidchart": "Lucidchart",
    "cbap": "CBAP", "certified business analysis professional": "CBAP",
    "ccba": "CCBA", "certification of capability in business analysis": "CCBA",
    "ecba": "ECBA", "entry certificate in business analysis": "ECBA",
    "pmi pba": "PMI-PBA", "pba": "PMI-PBA", "pmi professional in business analysis": "PMI-PBA",
    "pmp": "PMP", "project management professional": "PMP",
    "capm": "CAPM", "certified associate in project management": "CAPM",
    "csm": "CSM", "certified scrummaster": "CSM", "certified scrum master": "CSM", "scrum master certification": "CSM",
    "cspo": "CSPO", "certified scrum product owner": "CSPO",
    "safe": "SAFe", "safe agilist": "SAFe Agilist", "leading safe": "SAFe Agilist",
    "itil": "ITIL", "itil foundation": "ITIL", "itil v4": "ITIL", "itil 4": "ITIL",
    "six sigma": "Six Sigma", "lean six sigma": "Six Sigma",
    "six sigma green belt": "Six Sigma Green Belt", "lean six sigma green belt": "Six Sigma Green Belt",
    "six sigma black belt": "Six Sigma Black Belt", "lean six sigma black belt": "Six Sigma Black Belt",
}
# The same table keyed the way key() compares names (spaces ignored).
_ALIAS_KEYS = None


def _alias(k):
    global _ALIAS_KEYS
    if _ALIAS_KEYS is None:
        _ALIAS_KEYS = {a.replace(" ", ""): name for a, name in ALIASES.items()}
    return _ALIAS_KEYS.get(k)


# Vendor names dropped before grouping ("Microsoft Power BI" -> "power bi").
_VENDOR = re.compile(r"^(microsoft|ms|atlassian|oracle|ibm)\s+")


def key(name):
    """Grouping key: lower case, no vendor prefix or trailing "certification", spaces ignored
    ("Lucid Chart" and "Lucidchart" are the same thing)."""
    k = re.sub(r"[^a-z0-9+#]+", " ", (name or "").lower()).strip()
    k = re.sub(r"\b(certification|certified|certificate|software|tool|tools|platform)\b$", "", k).strip()
    k = _VENDOR.sub("", k)
    return k.replace(" ", "")


def clean(data):
    """The AI's answer -> [{"kind", "key", "name", "need"}], one entry per item, names grouped."""
    items = {}
    for kind, field in (("cert", "certs"), ("tool", "tools")):
        for it in data.get(field) or []:
            if not isinstance(it, dict):
                continue
            raw = " ".join(str(it.get("name") or "").split())[:80]
            k = key(raw)
            if not k or len(k) < 2 and not raw.isupper():
                continue
            name = _alias(k) or raw
            k = key(name)
            need = str(it.get("need") or "").strip().lower()
            need = need if need in NEEDS else "mentioned"
            prev = items.get((kind, k))
            # Same thing listed twice: keep the strongest wording (required > preferred > mentioned).
            if not prev or NEEDS.index(need) < NEEDS.index(prev["need"]):
                items[(kind, k)] = {"kind": kind, "key": k, "name": name, "need": need}
    return list(items.values())


def read_listing(text):
    text = re.sub(r"\n{3,}", "\n\n", (text or "").strip())[:MAX_TEXT]
    reply = ai.json_answer(_INSTRUCTIONS, text)
    return clean(json.loads(ai.first_json_object(reply) or "{}"))


def analyze(limit=8, workers=4):
    """Read up to `limit` listings that haven't been read yet. Returns how many were read,
    how many are left, and any errors (a failed listing is tried again next time)."""
    if not ai.has_key():
        raise ai.AIError(f"Reading listings needs {ai.key_name()} in .env")
    todo = db.jobs_needing_skills()
    batch, errors = todo[:limit], []

    def one(job):
        job_id, text, h = job
        try:
            db.save_job_skills(job_id, h, read_listing(text))
            return True
        except Exception as e:  # keep going; this one is retried on the next run
            errors.append(f"job {job_id}: {e}")
            return False

    with ThreadPoolExecutor(max_workers=workers) as pool:
        done = sum(pool.map(one, batch))
    return {"read": done, "remaining": len(todo) - done, "errors": errors[:3]}

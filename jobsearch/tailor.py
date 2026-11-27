"""Tailor resume: help her fit her resume to one job, then save a Word copy with the wording she chose.

Two parts on one screen (Jen's KAN-29 and KAN-30):
  * Keywords: what an applicant tracking system (ATS) would look for in the listing that her resume doesn't
    have, each with the summary/bullet(s) or skills section where it could fit, as current text and suggested
    text. Words new to her resume are the point here, but they're flagged so she only keeps what's true.
  * Skills sections (Key Skills, Common Artifacts Produced, Recent Tools & Technology): current items,
    suggested items (added, removed, reordered) and advice on what to keep or could drop for this job.
Each place has a text box she edits; saving writes "<Company>-<Job Title> - Suggested Resume.docx" with the
text of every box she changed. Her name, contact line, headings, employers, titles, dates and other lines
can't be changed, and her own resume file is never edited.

It starts from the resume file she last loaded with "Load resume file..." (kept in the database), or
profile/Resume.docx if she hasn't loaded one. The Word file is edited as text inside its XML, so her fonts,
spacing and bullets stay as they are.
"""

import base64
import datetime
import hashlib
import html
import io
import json
import os
import re
import zipfile

from . import ai, db, profile_files, ranking

SEP = "•"  # the bullet character between items on her skills lines
MAX_KEYWORDS = 12
MAX_PLACES = 3
MAX_TEXT = 1500  # longest text a box may hold
CACHE_VERSION = "2"
# Skills sections Tailor resume works on (by their label); others (e.g. Methodologies) stay as they are.
SKILLS_LABEL = re.compile(r"skill|artifact|tool|technolog", re.I)


class TailorError(Exception):
    """A problem to show her as-is."""


# --- reading and writing the Word file ------------------------------------------------------------

_PARA = re.compile(r"<w:p[ >].*?</w:p>", re.S)
_TEXT = re.compile(r"(<w:t(?: [^>]*)?>)([^<]*)(</w:t>)")


def _para_text(xml):
    return html.unescape("".join(m.group(2) for m in _TEXT.finditer(xml)))


def _style(xml):
    m = re.search(r'<w:pStyle w:val="([^"]+)"', xml)
    return m.group(1) if m else ""


def _is_heading(style):
    return style.lower().startswith(("heading", "title"))


def read_paragraphs(data):
    """Her resume as paragraphs: [{i, text, style, kind, section, job, skills}] where kind says what may change:
    "summary" (her professional summary), "bullet" (a bullet under a job), "skills" (an item line of a skills
    section; `skills` is that section's number), or None (fixed)."""
    xml = _document(data)
    paras, heading, out = _PARA.findall(xml), "", []
    for i, p in enumerate(paras):
        text, style = _para_text(p), _style(p)
        if _is_heading(style):
            if style.lower() in ("heading1", "title"):
                heading = text
            kind = None
        elif "<w:numPr>" in p or style == "ListParagraph":
            kind = "bullet"
        elif re.search(r"professional summary|^summary$|profile|objective", heading, re.I) and len(text) > 80:
            kind = "summary"
        else:
            kind = None
        out.append({"i": i, "text": text, "style": style, "kind": kind, "section": heading, "skills": None})
    _mark_jobs(out)
    _mark_skills(out)
    return out


def _mark_jobs(paras):
    """Bullets belong to the job heading above them (Heading3 = "ANDERSEN WINDOWS, ...")."""
    job = ""
    for p in paras:
        if p["style"].lower() in ("heading3", "heading2"):
            job = p["text"].split(",")[0].strip()
        if p["style"].lower() == "heading1":
            job = ""
        p["job"] = job if p["kind"] == "bullet" else ""


def _inline_label(text):
    """ "Key Skills: SQL • Jira" -> "Key Skills: " ("" when the line has no label)."""
    m = re.match(r"\s*([^:•]{1,40}:\s*)", text)
    return m.group(1) if m else ""


def _mark_skills(paras):
    """Find the skills sections: a short label line ("Key Skills") followed by item lines ("SQL • Jira •"),
    which may wrap onto a line without a bullet ("End-to-end testing"), or one line with its label inline
    ("Key Skills: SQL • Jira"). Only sections whose label matches SKILLS_LABEL become editable."""
    filled = [p for p in paras if p["text"].strip()]
    found, current = [], None  # found: (paragraph, section)
    for n, p in enumerate(filled):
        text = p["text"].strip()
        if _is_heading(p["style"]) or p["kind"] in ("bullet", "summary"):
            current = None
            continue
        nxt = filled[n + 1] if n + 1 < len(filled) else None
        # a label is followed by an item line (one with a single item has no bullet)
        if (SEP not in text and len(text) <= 50 and nxt and not _is_heading(nxt["style"])
                and nxt["kind"] is None and (SEP in nxt["text"] or SKILLS_LABEL.search(text))):
            current = {"label": text.rstrip(": ").strip()}
            continue
        if SEP in text and _inline_label(text):
            current = {"label": _inline_label(text).rstrip(": ").strip()}
        if current is not None:
            found.append((p, current))
    numbers = {}
    for p, sec in found:
        if SKILLS_LABEL.search(sec["label"]):
            p["kind"], p["skills"] = "skills", numbers.setdefault(id(sec), len(numbers))
            p["skills_label"] = sec["label"]


def skills_sections(paras):
    """[{n, label, inline, paras: [item paragraph numbers], items: [...], width}] in resume order."""
    out = {}
    for p in paras:
        if p["kind"] != "skills":
            continue
        sec = out.setdefault(p["skills"], {"n": p["skills"], "label": p["skills_label"], "paras": [], "lines": []})
        sec["paras"].append(p["i"])
        sec["lines"].append(p["text"])
    for sec in out.values():
        first = sec["lines"][0]
        sec["inline"] = _inline_label(first)
        joined = f" {SEP} ".join([first[len(sec["inline"]):]] + sec["lines"][1:])
        sec["items"] = [x.strip() for x in joined.split(SEP) if x.strip()]
        sec["width"] = max(60, max(len(line) for line in sec["lines"]))
    return [out[k] for k in sorted(out)]


def _document(data):
    try:
        xml = zipfile.ZipFile(io.BytesIO(data)).read("word/document.xml").decode("utf-8")
    except (zipfile.BadZipFile, KeyError, UnicodeDecodeError):
        raise TailorError("The resume file isn't a Word (.docx) file that can be edited")
    if "txbxContent" in xml:
        raise TailorError("Her resume uses text boxes, which Tailor resume can't edit safely")
    return xml


def _set_text(p_xml, text):
    """Put `text` in the paragraph's first run (keeping its formatting) and empty the others.
    A leading bold label ("Key Skills") is kept as its own run when the new text starts with it."""
    matches = list(_TEXT.finditer(p_xml))
    if not matches:
        return p_xml
    keep, rest = 0, text
    # keep leading runs whose text the new text still starts with (labels, bold lead-ins)
    while keep < len(matches) - 1:
        piece = html.unescape(matches[keep].group(2))
        if piece and rest.startswith(piece) and len(piece) < len(rest):
            rest, keep = rest[len(piece):], keep + 1
        else:
            break
    out, last = [], 0
    for n, m in enumerate(matches):
        out.append(p_xml[last:m.start()])
        if n < keep:
            out.append(m.group(0))
        elif n == keep:
            out.append('<w:t xml:space="preserve">' + html.escape(rest, quote=False) + "</w:t>")
        else:
            out.append(m.group(1) + m.group(3))
        last = m.end()
    out.append(p_xml[last:])
    return "".join(out)


def pack_lines(items, width):
    """Items -> lines of "A • B • C" about as long as her lines are, each but the last ending in " •"."""
    lines, cur = [], []
    for item in items:
        if cur and len(f" {SEP} ".join(cur + [item])) > width:
            lines.append(cur)
            cur = []
        cur.append(item)
    if cur:
        lines.append(cur)
    return [f" {SEP} ".join(line) + ("" if n == len(lines) - 1 else f" {SEP}") for n, line in enumerate(lines)]


def _clone(p_xml):
    """A copy of a paragraph for an extra line (without Word's paragraph ids, which must be unique)."""
    return re.sub(r'\s+w14:(paraId|textId)="[^"]*"', "", p_xml)


def write_resume(data, edits):
    """A copy of the .docx with her edits applied. edits: {"para": n, "text": "..."} for a summary or bullet
    paragraph, {"section": n, "items": [...]} for a skills section (its lines are repacked, adding or removing
    lines as needed with the formatting of her item lines)."""
    xml = _document(data)
    spans = [m.span() for m in _PARA.finditer(xml)]
    paras = [xml[a:b] for a, b in spans]
    new = list(paras)
    sections = {s["n"]: s for s in skills_sections(read_paragraphs(data))}
    for e in edits:
        if "para" in e:
            new[e["para"]] = _set_text(paras[e["para"]], e["text"])
        elif e.get("section") in sections:
            sec = sections[e["section"]]
            lines = pack_lines(e["items"], sec["width"]) or [""]
            lines[0] = sec["inline"] + lines[0]
            slots = sec["paras"]
            for k, idx in enumerate(slots):
                new[idx] = _set_text(paras[idx], lines[k]) if k < len(lines) else ""
            if len(lines) > len(slots):
                last = paras[slots[-1]]
                new[slots[-1]] += "".join(_set_text(_clone(last), t) for t in lines[len(slots):])
    pieces, last = [], 0
    for (a, b), p in zip(spans, new):
        pieces.append(xml[last:a])
        pieces.append(p)
        last = b
    pieces.append(xml[last:])
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as src, zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
        for item in src.infolist():
            body = "".join(pieces).encode("utf-8") if item.filename == "word/document.xml" else src.read(item)
            dst.writestr(item, body)
    return out.getvalue()


# --- which resume file ----------------------------------------------------------------------------------

UPLOAD_KEY = "tailor_resume_file"


def keep_upload(name, data):
    """Keep the resume she loaded with "Load resume file..." so Tailor resume starts from it.
    Only Word files can be tailored (a .txt is skipped). Returns what Tailor resume uses now."""
    if not (name or "").lower().endswith(".docx"):
        return source_info()
    read_paragraphs(data)  # raises TailorError if it can't be edited
    db._set_meta(UPLOAD_KEY, json.dumps({
        "name": os.path.basename(name), "at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "data": base64.b64encode(data).decode("ascii")}))
    return source_info()


def _upload():
    try:
        kept = json.loads(db._get_meta(UPLOAD_KEY) or "{}")
        return kept if kept.get("data") else None
    except ValueError:
        return None


def source_info():
    """Which resume Tailor resume starts from: {"name", "at", "from": "upload"|"profile"} or None."""
    kept = _upload()
    if kept:
        return {"name": kept["name"], "at": kept["at"], "from": "upload"}
    path = profile_files.resume_path()
    if path and path.lower().endswith(".docx"):
        return {"name": os.path.basename(path), "at": None, "from": "profile"}
    return None


def resume_data():
    """The bytes of the resume Tailor resume starts from."""
    kept = _upload()
    if kept:
        return base64.b64decode(kept["data"])
    path = profile_files.resume_path()
    if not path or not path.lower().endswith(".docx"):
        raise TailorError("Tailor resume needs her resume as a Word (.docx) file. "
                          "Load one with \"Load resume file…\" in Resume & interests.")
    with open(path, "rb") as f:
        return f.read()


# --- checking the AI's answer ------------------------------------------------------------------------

_SMALL = {"a", "an", "and", "the", "of", "to", "in", "for", "with", "on", "by", "as", "at", "or", "from", "into",
          "across", "through", "via", "using", "while"}


def unsupported_terms(new_text, resume_text):
    """Names, tools and numbers in `new_text` that her resume doesn't contain (case-insensitive).
    The first word of a sentence is skipped (it's capitalized for grammar, not because it's a name)."""
    resume = resume_text.lower()
    found = []
    for m in re.finditer(r"[A-Za-z][A-Za-z0-9+#./&-]*|\$?\d[\d,.]*%?\+?", new_text):
        word = m.group(0).rstrip(".,")
        before = new_text[:m.start()].rstrip()
        sentence_start = not before or before.endswith((".", ":", ";", "!", "?", "•"))
        is_number = word[:1].isdigit() or word.startswith("$")
        is_name = word[:1].isupper() and not sentence_start
        if not (is_number or is_name) or word.lower() in _SMALL:
            continue
        if word.lower() not in resume and word not in found:
            found.append(word)
    return found


def _norm(text):
    """Lower case, with dashes, slashes and runs of spaces as one space (for "is it in there" checks)."""
    return re.sub(r"[\s\-–—/]+", " ", str(text or "").lower()).strip()


def _clean(text, limit):
    return re.sub(r"\s+", " ", str(text or "")).strip()[:limit]


def targets(paras):
    """Every place she can edit, by id: "p<n>" for a summary or bullet paragraph, "s<n>" for a skills section.
    {id: {"kind", "where", "current"}}; a section's current text is its items, one per line."""
    out = {}
    for p in paras:
        if p["kind"] == "summary":
            out[f"p{p['i']}"] = {"kind": "summary", "where": "Professional Summary", "current": p["text"].strip()}
        elif p["kind"] == "bullet":
            out[f"p{p['i']}"] = {"kind": "bullet", "where": p["job"] or "Experience", "current": p["text"].strip()}
    for sec in skills_sections(paras):
        out[f"s{sec['n']}"] = {"kind": "section", "where": sec["label"], "current": "\n".join(sec["items"]),
                               "items": sec["items"]}
    return out


def _advice(raw, lower, taken):
    out = []
    for a in raw if isinstance(raw, list) else []:
        name = a.get("item") if isinstance(a, dict) else a
        item = lower.get(_clean(name, 80).lower())
        if item and item not in taken:
            taken.add(item)
            out.append({"item": item, "why": _clean(a.get("why") if isinstance(a, dict) else "", 200)})
    return out


def check_answer(answer, paras, resume_text, listing):
    """Keep only what fits the rules. A keyword must appear in the listing and be missing from her resume;
    each of its places must be a summary/bullet paragraph whose new text contains it, or a skills section
    (where it's added as an item). Skills advice only names items she has. New words are flagged."""
    answer = answer if isinstance(answer, dict) else {}
    places_ok = targets(paras)
    by_label = {_norm(t["where"]): tid for tid, t in places_ok.items() if t["kind"] == "section"}
    resume_n, listing_n = _norm(resume_text), _norm(listing)

    keywords, seen = [], set()
    for k in answer.get("keywords") if isinstance(answer.get("keywords"), list) else []:
        if not isinstance(k, dict):
            continue
        word = _clean(k.get("keyword"), 60).strip(" .,;")
        key = _norm(word)
        if not key or key in seen or key not in listing_n or key in resume_n:
            continue
        seen.add(key)
        places = []
        for pl in k.get("places") if isinstance(k.get("places"), list) else []:
            if not isinstance(pl, dict) or len(places) >= MAX_PLACES:
                continue
            used = [x["target"] for x in places]
            if pl.get("section") is not None:
                tid = by_label.get(_norm(pl.get("section")))
                if tid and tid not in used:
                    places.append({"target": tid, "add": word, "unsupported": [word]})
                continue
            n = pl.get("para")
            tid = f"p{n}" if isinstance(n, int) and not isinstance(n, bool) else None
            text = _clean(pl.get("text"), MAX_TEXT)
            t = places_ok.get(tid)
            if (not t or t["kind"] == "section" or tid in used or not text or text == t["current"]
                    or key not in _norm(text) or len(text) > max(600, 2 * len(t["current"]))):
                continue
            others = [w for w in unsupported_terms(text, resume_text) if _norm(w) not in key]
            places.append({"target": tid, "text": text, "unsupported": [word] + others})
        keywords.append({"keyword": word, "why": _clean(k.get("why"), 200), "places": places})
        if len(keywords) >= MAX_KEYWORDS:
            break

    by_name = {}
    for s in answer.get("sections") if isinstance(answer.get("sections"), list) else []:
        tid = by_label.get(_norm(s.get("name"))) if isinstance(s, dict) else None
        if tid and tid not in by_name:
            by_name[tid] = s
    sections = []
    for tid, t in places_ok.items():
        if t["kind"] != "section":
            continue
        s, current = by_name.get(tid, {}), t["items"]
        lower = {x.lower(): x for x in current}
        suggested = []
        for item in s.get("items") if isinstance(s.get("items"), list) else []:
            item = _clean(item, 60).strip(f" {SEP},;")
            item = lower.get(item.lower(), item)
            if item and item.lower() not in [x.lower() for x in suggested]:
                suggested.append(item)
        suggested = suggested or list(current)
        taken = set()
        keep = _advice(s.get("keep"), lower, taken)
        drop = _advice(s.get("drop"), lower, taken)
        added = [x for x in suggested if x.lower() not in lower]
        kept = {x.lower() for x in suggested}
        sections.append({
            "target": tid, "name": t["where"], "current": current, "suggested": suggested, "added": added,
            "removed": [x for x in current if x.lower() not in kept],
            "unsupported": [x for x in added if _norm(x) not in resume_n], "keep": keep, "drop": drop})
    return {"keywords": keywords, "sections": sections}


# --- asking the AI ----------------------------------------------------------------------------------

INSTRUCTIONS = """You help a job seeker get her resume past an applicant tracking system (ATS) for one job
listing. You get her resume as numbered paragraphs marked [summary] (her professional summary), [bullet]
(a bullet under a job; the job is in parentheses) or [fixed], and her skills sections as lists of items.

1. KEYWORDS: list the keywords and qualifications an ATS would scan this listing for (skills, tools,
   methods, deliverables, certifications, domain terms) that her resume does NOT already contain. Use the
   listing's exact wording, 1 to 4 words each, most important first, at most 12. For each, suggest 1 to 3
   places where it could fit naturally:
   - a [summary] or [bullet] paragraph: give its number and the full new text, changing only what's needed
     to work the keyword in, keeping what she did and about the same length; or
   - a skills section, by its name (the keyword gets added there as an item).
   Choose places that already describe related work. She decides whether each one is true for her.
2. SKILLS SECTIONS: for each skills section, give the item list you'd suggest for this job: most relevant
   first, with items the listing asks for added (short, in the listing's wording) and items that don't help
   for this job left out. Then say which of her CURRENT items are important to keep for this job and which
   she could take off, with a short reason each.
Never change employers, job titles, dates, her name or contact details.
Return JSON:
{"keywords": [{"keyword": "...", "why": "<what the listing says about it, short>",
               "places": [{"para": <number>, "text": "<full new text>"}, {"section": "<section name>"}]}],
 "sections": [{"name": "<section name>", "items": ["<suggested items, in order>"],
               "keep": [{"item": "<current item>", "why": "..."}],
               "drop": [{"item": "<current item>", "why": "..."}]}]}"""


def suggest(job, match=None, again=False):
    """Keywords and skills suggestions for this job, plus the boxes she has saved. Kept per job, so opening
    it again costs nothing until her resume file or the listing changes (or she asks again)."""
    data = resume_data()
    paras = read_paragraphs(data)
    resume_text = "\n".join(p["text"] for p in paras)
    description = (job.get("description") or "").strip()
    if len(description) < db.MIN_DESCRIPTION:
        raise TailorError("This job has no description yet. Reload it from the listing or paste it in first.")
    key = hashlib.sha256((CACHE_VERSION + hashlib.sha256(data).hexdigest() + "\n" + description)
                         .encode("utf-8")).hexdigest()[:16]
    saved = _saved(job["id"])
    places = targets(paras)
    if saved.get("key") == key and "keywords" in saved and not again:
        return _result(job["id"], saved, places)

    numbered = "\n".join(
        f"[{p['i']}] [{p['kind'] if p['kind'] in ('summary', 'bullet') else 'fixed'}]"
        f"{' (' + p['job'] + ')' if p.get('job') else ''} {ranking.resume_for_ai(p['text'])}"
        for p in paras if p["text"].strip() and p["kind"] != "skills")
    skills = "\n".join(f"{s['label']}: " + " | ".join(s["items"]) for s in skills_sections(paras)) or "(none)"
    notes = ""
    if match:
        notes = ("\n\nWHAT RANK JOBS FOUND:\nMeets: " + ", ".join(match.get("matched") or []) +
                 "\nPartly: " + ", ".join(match.get("partial") or []) +
                 "\nMissing: " + ", ".join(match.get("missing") or []))
    text = (f"HER RESUME:\n{numbered}\n\nHER SKILLS SECTIONS:\n{skills}\n\n"
            f"THE JOB: {job.get('title') or ''} at {job.get('company') or ''}\n{description[:10000]}{notes}")
    found = ai.first_json_object(ai.json_answer(INSTRUCTIONS, text, task="applications"))
    if not found:
        raise ai.AIError("The AI's answer couldn't be read")
    try:
        answer = json.loads(found)
    except ValueError:
        raise ai.AIError("The AI's answer couldn't be read")
    checked = check_answer(answer, paras, resume_text, description)
    drafts = saved.get("drafts") if saved.get("key") == key else {}  # her boxes stay when she asks again
    saved = {**saved, "key": key, **checked, "drafts": drafts or {}}
    _save(job["id"], saved)
    return _result(job["id"], saved, places)


def _result(job_id, saved, places):
    used = {p["target"] for k in saved["keywords"] for p in k["places"]} | {s["target"] for s in saved["sections"]}
    return {"keywords": saved["keywords"], "sections": saved["sections"],
            "targets": {tid: {k: v for k, v in t.items() if k != "items"} for tid, t in places.items() if tid in used},
            "drafts": {k: v for k, v in (saved.get("drafts") or {}).items() if k in places},
            "source": source_info(), "saved_path": tailored_path(job_id)}


def _saved(job_id):
    try:
        return json.loads(db._get_meta(f"tailor_{job_id}") or "{}")
    except ValueError:
        return {}


def _save(job_id, value):
    db._set_meta(f"tailor_{job_id}", json.dumps(value))


def tailored_path(job_id):
    """The suggested resume saved for this job, if the file is still there."""
    path = _saved(job_id).get("path")
    return path if path and os.path.exists(path) else None


def folder():
    """Where suggested resumes go: TAILORED_DIR in .env, else "Job Search tailored resumes" in her OneDrive."""
    custom = (os.getenv("TAILORED_DIR") or "").strip()
    if custom:
        return custom
    onedrive = os.getenv("OneDrive") or os.getenv("OneDriveConsumer") or os.getenv("OneDriveCommercial")
    if not onedrive:
        raise TailorError("OneDrive isn't set up on this computer. Add TAILORED_DIR=<folder> to .env "
                          "to choose where tailored resumes go.")
    return os.path.join(onedrive, "Job Search tailored resumes")


def file_name(job):
    """ "<Company>-<Job Title> - Suggested Resume.docx" """
    clean = lambda s: re.sub(r"[^\w &.,()-]+", " ", s or "").strip()[:60].strip()
    name = "-".join(p for p in (clean(job.get("company")), clean(job.get("title"))) if p)
    return (name + " - " if name else "") + "Suggested Resume.docx"


def box_items(text):
    """A skills box (one item per line, or "A • B") -> items."""
    return [x.strip(f" \t{SEP},;") for x in re.split(rf"[\r\n{SEP}]+", text or "") if x.strip(f" \t{SEP},;")]


def save(job, drafts):
    """Keep her boxes, and write the suggested resume with the text of every box she changed.
    drafts: {target id: text}."""
    data = resume_data()
    places = targets(read_paragraphs(data))
    drafts = {k: v[:MAX_TEXT * 4] for k, v in (drafts if isinstance(drafts, dict) else {}).items()
              if k in places and isinstance(v, str)}
    edits = []
    for tid, text in drafts.items():
        t = places[tid]
        if t["kind"] == "section":
            items = [x[:80] for x in box_items(text)]
            if items and items != t["items"]:
                edits.append({"section": int(tid[1:]), "items": items})
        else:
            text = re.sub(r"\s+", " ", text).strip()[:MAX_TEXT]
            if text and text != t["current"]:
                edits.append({"para": int(tid[1:]), "text": text})
    _save(job["id"], {**_saved(job["id"]), "drafts": drafts})
    if not edits:
        raise TailorError("Nothing is changed yet. Edit at least one box, then save.")
    target_dir = folder()
    os.makedirs(target_dir, exist_ok=True)
    path = os.path.join(target_dir, file_name(job))
    try:
        with open(path, "wb") as f:
            f.write(write_resume(data, edits))
    except PermissionError:
        raise TailorError(f"Couldn't save {os.path.basename(path)}. Is it open in Word? Close it and try again.")
    _save(job["id"], {**_saved(job["id"]), "path": path})
    return path

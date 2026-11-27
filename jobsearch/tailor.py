"""Tailor resume: help her fit her resume to one job, then download a Word copy with the boxes she saved.

The window goes through her resume section by section (Jen's KAN-31, building on KAN-29 and KAN-30):
  * Professional Summary: current, suggested, and a summary of what changed and why.
  * Skills sections (Key Skills, Common Artifacts Produced, Recent Tools & Technology): current items,
    suggested items, what was added and removed, and why the rest changed (such as the order).
  * Experience: for each job, the bullets worth changing, each with the ATS keywords it now includes and
    the listing's qualifications it addresses.
Every one has an editable box and a "Save to New Resume" button; saved boxes are kept per job. The preview
shows the new resume as text with the saved boxes, plus what's Still Missing (ATS keywords that aren't in it,
and the listing's qualifications nothing covers yet). Save downloads "<Company>-<Job Title> - Suggested
Resume.docx". Her name, contact line, headings, employers, titles, dates and other lines can't be changed,
and her own resume file is never edited. Words new to her resume are flagged so she only keeps what's true.

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
MAX_KEYWORDS = 20
MAX_QUALIFICATIONS = 15
MAX_BULLETS = 15
MAX_TEXT = 1500  # longest text a box may hold
CACHE_VERSION = "3"
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


def _loose(text):
    """Letters and digits only, one space between words (for matching sentences copied from the listing,
    where bullets, quotes and punctuation may differ)."""
    text = str(text or "").lower().replace("’", "'")
    return re.sub(r"\s+", " ", re.sub(r"[^\w']+", " ", text)).strip()


def _clean(text, limit):
    return re.sub(r"\s+", " ", str(text or "")).strip()[:limit]


def targets(paras):
    """Every place she can edit, by id: "p<n>" for a summary or bullet paragraph, "s<n>" for a skills section.
    {id: {"kind", "where", "current", ...}}: a bullet also has its job and its number within that job;
    a section's current text is its items, one per line."""
    out, numbers = {}, {}
    for p in paras:
        if p["kind"] == "summary":
            out[f"p{p['i']}"] = {"kind": "summary", "where": "Professional Summary", "current": p["text"].strip()}
        elif p["kind"] == "bullet":
            job = p["job"] or "Experience"
            numbers[job] = numbers.get(job, 0) + 1
            out[f"p{p['i']}"] = {"kind": "bullet", "where": job, "job": job, "number": numbers[job],
                                 "current": p["text"].strip()}
    for sec in skills_sections(paras):
        out[f"s{sec['n']}"] = {"kind": "section", "where": sec["label"], "current": "\n".join(sec["items"]),
                               "items": sec["items"]}
    return out


def jobs(paras):
    """Her jobs with bullets, in resume order (the window gives each its own color by this order)."""
    out = []
    for p in paras:
        if p["kind"] == "bullet" and (p["job"] or "Experience") not in out:
            out.append(p["job"] or "Experience")
    return out


def _para_id(n):
    return f"p{n}" if isinstance(n, int) and not isinstance(n, bool) else None


def _new_text(raw, t):
    """The AI's new text for a summary or bullet, if it's usable: changed, and not wildly longer."""
    text = _clean(raw, MAX_TEXT)
    if not text or text == t["current"] or len(text) > max(600, 2 * len(t["current"])):
        return None
    return text


def check_answer(answer, paras, resume_text, listing):
    """Keep only what fits the rules:
      * a keyword must appear in the listing and be missing from her resume;
      * a qualification must be copied from the listing;
      * summary and bullet suggestions must point at her summary or bullet paragraphs; a bullet only claims
        the keywords its new text really adds and qualifications from the list;
      * skills suggestions are compared with her items here (added / removed aren't taken from the AI).
    New words are flagged so she only keeps what's true for her."""
    answer = answer if isinstance(answer, dict) else {}
    places = targets(paras)
    resume_n, listing_n, listing_l = _norm(resume_text), _norm(listing), _loose(listing)
    as_list = lambda key: answer.get(key) if isinstance(answer.get(key), list) else []

    def keyword_ok(word):
        key = _norm(word)
        return bool(key) and key in listing_n and key not in resume_n

    keywords, seen = [], set()
    for k in as_list("keywords"):
        word = _clean(k.get("keyword") if isinstance(k, dict) else k, 60).strip(" .,;")
        if keyword_ok(word) and _norm(word) not in seen and len(keywords) < MAX_KEYWORDS:
            seen.add(_norm(word))
            keywords.append(word)

    qualifications, by_loose = [], {}
    for q in as_list("qualifications"):
        text = _clean(q.get("text") if isinstance(q, dict) else q, 400).lstrip(f"{SEP}*-–· ").strip()
        loose = _loose(text)
        if len(loose) < 12 or loose not in listing_l or loose in by_loose or len(qualifications) >= MAX_QUALIFICATIONS:
            continue
        on_resume = bool(q.get("on_resume")) if isinstance(q, dict) else False
        by_loose[loose] = text
        qualifications.append({"text": text, "on_resume": on_resume})

    summary = []
    for s in as_list("summary"):
        tid = _para_id(s.get("para")) if isinstance(s, dict) else None
        t = places.get(tid)
        if not t or t["kind"] != "summary" or any(x["target"] == tid for x in summary):
            continue
        text = _new_text(s.get("text"), t)
        if text:
            summary.append({"target": tid, "text": text, "why": _clean(s.get("why"), 600),
                            "unsupported": unsupported_terms(text, resume_text)})

    bullets = []
    for b in as_list("bullets"):
        tid = _para_id(b.get("para")) if isinstance(b, dict) else None
        t = places.get(tid)
        if not t or t["kind"] != "bullet" or any(x["target"] == tid for x in bullets) or len(bullets) >= MAX_BULLETS:
            continue
        text = _new_text(b.get("text"), t)
        if not text:
            continue
        text_n, current_n = _norm(text), _norm(t["current"])
        kws = []
        for word in b.get("keywords") if isinstance(b.get("keywords"), list) else []:
            word = _clean(word, 60).strip(" .,;")
            key = _norm(word)
            if keyword_ok(word) and key in text_n and key not in current_n and key not in [_norm(x) for x in kws]:
                kws.append(next((k for k in keywords if _norm(k) == key), word))
        quals = []
        for q in b.get("qualifications") if isinstance(b.get("qualifications"), list) else []:
            found = by_loose.get(_loose(q))
            if found and found not in quals:
                quals.append(found)
        if not kws and not quals:
            continue  # a change that adds nothing the listing asks for
        for word in kws:  # a keyword a bullet works in is one the listing wants
            if _norm(word) not in seen:
                seen.add(_norm(word))
                keywords.append(word)
        bullets.append({"target": tid, "text": text, "keywords": kws, "qualifications": quals,
                        "unsupported": unsupported_terms(text, resume_text)})
    order = list(places)
    bullets.sort(key=lambda b: order.index(b["target"]))

    by_label = {_norm(t["where"]): tid for tid, t in places.items() if t["kind"] == "section"}
    by_name = {}
    for s in as_list("sections"):
        tid = by_label.get(_norm(s.get("name"))) if isinstance(s, dict) else None
        if tid and tid not in by_name:
            by_name[tid] = s
    sections = []
    for tid, t in places.items():
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
        added = [x for x in suggested if x.lower() not in lower]
        kept = {x.lower() for x in suggested}
        sections.append({
            "target": tid, "name": t["where"], "current": current, "suggested": suggested, "added": added,
            "removed": [x for x in current if x.lower() not in kept], "why": _clean(s.get("why"), 600),
            "unsupported": [x for x in added if _norm(x) not in resume_n]})
    return {"keywords": keywords, "qualifications": qualifications, "summary": summary,
            "sections": sections, "bullets": bullets}


# --- asking the AI ----------------------------------------------------------------------------------

INSTRUCTIONS = """You help a job seeker tailor her resume to one job listing, so it gets past an applicant
tracking system (ATS) and shows how she meets the listing's qualifications. You get her resume as numbered
paragraphs marked [summary] (her professional summary), [bullet] (a bullet under a job; the job is in
parentheses) or [fixed], her skills sections as lists of items, and the listing.

1. KEYWORDS: the keywords an ATS would scan this listing for (skills, tools, methods, deliverables,
   certifications, domain terms) that her resume does NOT already contain. Use the listing's exact wording,
   1 to 4 words each, most important first, at most 20.
2. QUALIFICATIONS: the listing's requirements and qualifications, each copied word for word from the listing
   as one full sentence or bullet, most important first, at most 15. For each, say whether her current
   resume already shows it ("on_resume": true or false).
3. SUMMARY: rewrite her [summary] paragraph for this job: keep what's true of her, bring forward what this
   listing values, and work in keywords where they fit naturally. In "why", summarize what you changed and why.
4. SKILLS SECTIONS: for each skills section, the item list you'd suggest for this job: most relevant first,
   with items the listing asks for added (short, in the listing's wording) and items that don't help for this
   job left out. In "why", summarize any other changes, such as why the order changed.
5. BULLETS: pick the [bullet] paragraphs that could show a missing keyword or a qualification her resume
   doesn't show yet, and rewrite each one, changing only what's needed and keeping what she did and about the
   same length. For each, list the keywords from 1 that it now includes and the qualifications from 2 (copied
   exactly as you wrote them there) that it addresses. At most 15 bullets, only where a change helps.
Choose places that already describe related work; she decides whether each change is true for her. Never
change employers, job titles, dates, her name or contact details, and never invent numbers.
Return JSON:
{"keywords": ["..."],
 "qualifications": [{"text": "<copied from the listing>", "on_resume": false}],
 "summary": [{"para": <number>, "text": "<full new text>", "why": "..."}],
 "sections": [{"name": "<section name>", "items": ["<suggested items, in order>"], "why": "..."}],
 "bullets": [{"para": <number>, "text": "<full new text>", "keywords": ["..."], "qualifications": ["..."]}]}"""


def _resume():
    data = resume_data()
    paras = read_paragraphs(data)
    return data, paras, "\n".join(p["text"] for p in paras)


def suggest(job, match=None, again=False):
    """Suggestions for this job, plus the boxes she has saved. Kept per job, so opening it again costs
    nothing until her resume file or the listing changes (or she asks again)."""
    data, paras, resume_text = _resume()
    description = (job.get("description") or "").strip()
    if len(description) < db.MIN_DESCRIPTION:
        raise TailorError("This job has no description yet. Reload it from the listing or paste it in first.")
    key = hashlib.sha256((CACHE_VERSION + hashlib.sha256(data).hexdigest() + "\n" + description)
                         .encode("utf-8")).hexdigest()[:16]
    saved = _saved(job["id"])
    if saved.get("key") == key and "bullets" in saved and not again:
        return _result(job, saved, paras)

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
    boxes = saved.get("boxes") if saved.get("key") == key else {}  # her saved boxes stay when she asks again
    saved = {"key": key, **checked, "boxes": boxes or {}}
    _save(job["id"], saved)
    return _result(job, saved, paras)


def _result(job, saved, paras):
    places = targets(paras)
    return {"keywords": saved["keywords"], "qualifications": saved["qualifications"],
            "summary": saved["summary"], "sections": saved["sections"], "bullets": saved["bullets"],
            "targets": {tid: {k: v for k, v in t.items() if k != "items"} for tid, t in places.items()
                        if t["kind"] != "bullet" or any(b["target"] == tid for b in saved["bullets"])},
            "jobs": jobs(paras), "saved": {k: v for k, v in (saved.get("boxes") or {}).items() if k in places},
            "source": source_info(), "file_name": file_name(job), "listing": job.get("apply_link") or ""}


def _saved(job_id):
    try:
        return json.loads(db._get_meta(f"tailor_{job_id}") or "{}")
    except ValueError:
        return {}


def _save(job_id, value):
    db._set_meta(f"tailor_{job_id}", json.dumps(value))


# --- her saved boxes, the preview and the download ----------------------------------------------------

def box_items(text):
    """A skills box (one item per line, or "A • B") -> items."""
    return [x.strip(f" \t{SEP},;") for x in re.split(rf"[\r\n{SEP}]+", text or "") if x.strip(f" \t{SEP},;")]


def save_box(job, target, text):
    """"Save to New Resume" for one box (text None = take it out of the new resume again).
    Returns all her saved boxes for this job."""
    places = targets(read_paragraphs(resume_data()))
    if target not in places:
        raise TailorError("That part of her resume isn't one Tailor resume can change")
    saved = _saved(job["id"])
    boxes = dict(saved.get("boxes") or {})
    if text is None:
        boxes.pop(target, None)
    else:
        if not isinstance(text, str) or not text.strip():
            raise TailorError("The box is empty. Put the text she wants in it, then save.")
        if places[target]["kind"] == "section":
            if not box_items(text):
                raise TailorError("The box has no items. Put one item per line, then save.")
            boxes[target] = text[:MAX_TEXT * 4]
        else:
            boxes[target] = re.sub(r"\s+", " ", text).strip()[:MAX_TEXT]
    _save(job["id"], {**saved, "boxes": boxes})
    return {k: v for k, v in boxes.items() if k in places}


def _edits(places, boxes):
    """Her saved boxes -> write_resume edits (boxes that match her resume as it is are left out)."""
    edits = []
    for tid, text in boxes.items():
        t = places.get(tid)
        if not t:
            continue
        if t["kind"] == "section":
            items = [x[:80] for x in box_items(text)]
            if items and items != t["items"]:
                edits.append({"section": int(tid[1:]), "items": items})
        else:
            text = re.sub(r"\s+", " ", text).strip()[:MAX_TEXT]
            if text and text != t["current"]:
                edits.append({"para": int(tid[1:]), "text": text})
    return edits


def _new_resume(job):
    data, paras, _ = _resume()
    edits = _edits(targets(paras), _saved(job["id"]).get("boxes") or {})
    return data, paras, edits, (write_resume(data, edits) if edits else data)


def preview(job):
    """The new resume as text with her saved boxes, and what's still missing from it:
    {"paragraphs": [{"text", "kind": title|heading|job|bullet|text, "changed"}],
     "missing": {"keywords": [...], "qualifications": [...]}, "changes": <number of saved changes>}"""
    data, paras, edits, new = _new_resume(job)
    before = {p["text"].strip() for p in paras}
    out = []
    for p in read_paragraphs(new):
        text, style = p["text"].strip(), p["style"].lower()
        if not text:
            continue
        kind = ("title" if style == "title" else "heading" if style == "heading1"
                else "job" if style.startswith("heading") else "bullet" if p["kind"] == "bullet" else "text")
        out.append({"text": text, "kind": kind, "changed": text not in before})
    saved = _saved(job["id"])
    new_n = _norm(" ".join(p["text"] for p in out))
    covered = {q for b in saved.get("bullets") or [] if b["target"] in (saved.get("boxes") or {})
               for q in b["qualifications"]}
    return {"paragraphs": out, "changes": len(edits),
            "missing": {"keywords": [k for k in saved.get("keywords") or [] if _norm(k) not in new_n],
                        "qualifications": [q["text"] for q in saved.get("qualifications") or []
                                           if not q["on_resume"] and q["text"] not in covered]}}


def file_name(job):
    """ "<Company>-<Job Title> - Suggested Resume.docx" """
    clean = lambda s: re.sub(r"[^\w &.,()-]+", " ", s or "").strip()[:60].strip()
    name = "-".join(p for p in (clean(job.get("company")), clean(job.get("title"))) if p)
    return (name + " - " if name else "") + "Suggested Resume.docx"


def download(job):
    """The suggested resume (a copy of hers with the saved boxes) as (bytes, file name)."""
    _, _, edits, new = _new_resume(job)
    if not edits:
        raise TailorError("Nothing is saved to the new resume yet. Click \"Save to New Resume\" on at least "
                          "one box first.")
    return new, file_name(job)

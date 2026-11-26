"""Tailor resume: suggest edits to her resume for one job, then save a Word copy with the edits she picks.

Her real resume (profile/Resume.docx) is never changed. The AI may only:
  * rewrite the summary paragraph,
  * rephrase or reorder bullets within the same job,
  * reorder the items on a skills line (same items, new order).
Headings, her name and contact line, employers, titles and dates are never touched. Every rewrite is
checked against her resume: a tool, name or number that isn't in it is flagged, and that change starts
unticked. What the listing asks for that her resume doesn't show is listed separately as "gaps".

The Word file is edited as text inside its XML, so her fonts, spacing and bullets stay exactly as they are.
"""

import hashlib
import html
import io
import json
import os
import re
import zipfile

from . import ai, db, profile_files, ranking

SEP = "•"  # the bullet character between items on her skills lines
MAX_CHANGES = 14


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


def read_paragraphs(data):
    """Her resume as paragraphs: [{i, text, style, kind}] where kind says what may change:
    "summary" (rewrite), "bullet" (rephrase / reorder within its job), "list" (reorder items), or None."""
    xml = _document(data)
    paras, heading, out = _PARA.findall(xml), "", []
    for i, p in enumerate(paras):
        text, style = _para_text(p), _style(p)
        if style.lower().startswith(("heading", "title")):
            if style.lower() in ("heading1", "title"):
                heading = text
            kind = None
        elif "<w:numPr>" in p or style == "ListParagraph":
            kind = "bullet"
        elif text.count(SEP) >= 1 and len(text) < 400:
            kind = "list"
        elif re.search(r"professional summary|^summary$|profile|objective", heading, re.I) and len(text) > 80:
            kind = "summary"
        else:
            kind = None
        out.append({"i": i, "text": text, "style": style, "kind": kind, "section": heading})
    _mark_jobs(out)
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


def write_resume(data, changes):
    """A copy of the .docx with the chosen changes applied (rewrites and reorders)."""
    xml = _document(data)
    spans = [m.span() for m in _PARA.finditer(xml)]
    paras = [xml[a:b] for a, b in spans]
    new = list(paras)
    for c in changes:
        if c["type"] == "rewrite":
            new[c["para"]] = _set_text(paras[c["para"]], c["text"])
    for c in changes:
        if c["type"] == "reorder":
            current = [new[i] for i in c["paras"]]
            for slot, src in zip(c["paras"], c["order"]):
                new[slot] = current[c["paras"].index(src)]
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


# --- checking the AI's changes ---------------------------------------------------------------------

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


def _label_and_items(text):
    """ "Key Skills: SQL • Jira" -> ("Key Skills: ", ["SQL", "Jira"]); a line without a label has ""."""
    m = re.match(r"\s*([^:•]{1,40}:\s*)", text)
    label = m.group(1) if m else ""
    return label, [x.strip() for x in text[len(m.group(0)) if m else 0:].split(SEP) if x.strip()]


def check_changes(raw, paras, resume_text):
    """Keep only allowed changes; flag rewrites that bring in anything her resume doesn't have."""
    by_i = {p["i"]: p for p in paras}
    kept = []
    for c in raw if isinstance(raw, list) else []:
        if not isinstance(c, dict):
            continue
        why = str(c.get("why") or "")[:300]
        if c.get("type") == "rewrite":
            p = by_i.get(c.get("para")) if isinstance(c.get("para"), int) else None
            text = re.sub(r"\s+", " ", str(c.get("text") or "")).strip()
            if not p or not p["kind"] or not text or text == p["text"].strip():
                continue
            if p["kind"] == "list":  # same label and items, new order only
                label, items = _label_and_items(p["text"])
                new_label, new_items = _label_and_items(text)
                if sorted(new_items) != sorted(items) or new_label.strip() != label.strip() or new_items == items:
                    continue
                text = label + f" {SEP} ".join(new_items)
            if len(text) > max(600, 2 * len(p["text"])):
                continue
            kept.append({"type": "rewrite", "para": p["i"], "before": p["text"].strip(), "text": text,
                         "why": why, "kind": p["kind"], "job": p.get("job", ""),
                         "unsupported": unsupported_terms(text, resume_text)})
        elif c.get("type") == "reorder":
            block, order = c.get("paras"), c.get("order")
            if not (isinstance(block, list) and isinstance(order, list) and len(block) > 1):
                continue
            block = sorted(set(block)) if all(isinstance(i, int) for i in block) else []
            if (not block or sorted(order) != block or order == block or
                    block != list(range(block[0], block[-1] + 1)) or
                    any((by_i.get(i) or {}).get("kind") != "bullet" for i in block) or
                    len({by_i[i]["job"] for i in block}) != 1):
                continue
            kept.append({"type": "reorder", "paras": block, "order": order, "why": why,
                         "job": by_i[block[0]]["job"],
                         "before": [by_i[i]["text"] for i in block], "after": [by_i[i]["text"] for i in order],
                         "unsupported": []})
    return kept[:MAX_CHANGES]


# --- asking the AI ----------------------------------------------------------------------------------

INSTRUCTIONS = """You tailor a job seeker's resume to one job listing, staying strictly within her real
experience. You get her resume as numbered paragraphs, each marked with what may change:
  [summary]  may be rewritten (same length or shorter; lead with what this job cares about)
  [bullet]   may be rephrased, and bullets may be reordered WITHIN the same job (most relevant first)
  [list]     items separated by • may be reordered (same items exactly; most relevant first)
  [fixed]    must not change

Rules:
- Never add a skill, tool, technology, certification, employer, title, industry, date or number that isn't
  already in her resume. Use the listing's wording only where it truthfully describes what her resume says.
- Keep each rewritten bullet about the same length, plain and specific. Don't change what she did.
- Make only changes that help for this job (at most 12). Skip paragraphs that are already a good fit.
- List what the listing asks for that her resume doesn't show as "gaps" (short names).
Return JSON:
{"changes": [{"type": "rewrite", "para": <number>, "text": "<new text>", "why": "<short reason>"},
             {"type": "reorder", "paras": [<numbers of one job's bullets, in current order>],
              "order": [<the same numbers, new order>], "why": "<short reason>"}],
 "gaps": ["..."]}"""


def resume_file():
    path = profile_files.resume_path()
    if not path or not path.lower().endswith(".docx"):
        raise TailorError("Tailor resume needs her resume as a Word file: profile/Resume.docx")
    return path


def suggest(job, profile, match=None):
    """Suggested changes for this job: {"changes": [...], "gaps": [...]}. Saved per job, so opening it
    again costs nothing until her resume or the listing changes."""
    with open(resume_file(), "rb") as f:
        data = f.read()
    paras = read_paragraphs(data)
    resume_text = "\n".join(p["text"] for p in paras)
    description = (job.get("description") or "").strip()
    if len(description) < db.MIN_DESCRIPTION:
        raise TailorError("This job has no description yet. Reload it from the listing or paste it in first.")
    key = hashlib.sha256((resume_text + "\n" + description).encode("utf-8")).hexdigest()[:16]
    saved = _saved(job["id"])
    if saved.get("key") == key and "changes" in saved:
        return {"changes": saved["changes"], "gaps": saved.get("gaps", []), "saved_path": tailored_path(job["id"]),
                "picked": saved.get("picked")}

    numbered = "\n".join(
        f"[{p['i']}] [{p['kind'] or 'fixed'}]{' (' + p['job'] + ')' if p.get('job') else ''} "
        f"{ranking.resume_for_ai(p['text'])}" for p in paras if p["text"].strip())
    notes = ""
    if match:
        notes = ("\n\nWHAT RANK JOBS FOUND:\nMeets: " + ", ".join(match.get("matched") or []) +
                 "\nPartly: " + ", ".join(match.get("partial") or []) +
                 "\nMissing: " + ", ".join(match.get("missing") or []))
    text = (f"HER RESUME:\n{numbered}\n\nTHE JOB: {job.get('title') or ''} at {job.get('company') or ''}\n"
            f"{description[:10000]}{notes}")
    found = ai.first_json_object(ai.json_answer(INSTRUCTIONS, text, task="applications"))
    if not found:
        raise ai.AIError("The AI's answer couldn't be read")
    answer = json.loads(found)
    changes = check_changes(answer.get("changes"), paras, resume_text)
    gaps = [str(g)[:80] for g in (answer.get("gaps") or []) if str(g).strip()][:12]
    _save(job["id"], {**saved, "key": key, "changes": changes, "gaps": gaps})
    return {"changes": changes, "gaps": gaps, "saved_path": tailored_path(job["id"]), "picked": None}


def _saved(job_id):
    try:
        return json.loads(db._get_meta(f"tailor_{job_id}") or "{}")
    except ValueError:
        return {}


def _save(job_id, value):
    db._set_meta(f"tailor_{job_id}", json.dumps(value))


def tailored_path(job_id):
    """The tailored resume saved for this job, if the file is still there."""
    path = _saved(job_id).get("path")
    return path if path and os.path.exists(path) else None


def folder():
    """Where tailored resumes go: TAILORED_DIR in .env, else "Job Search tailored resumes" in her OneDrive."""
    custom = (os.getenv("TAILORED_DIR") or "").strip()
    if custom:
        return custom
    onedrive = os.getenv("OneDrive") or os.getenv("OneDriveConsumer") or os.getenv("OneDriveCommercial")
    if not onedrive:
        raise TailorError("OneDrive isn't set up on this computer. Add TAILORED_DIR=<folder> to .env "
                          "to choose where tailored resumes go.")
    return os.path.join(onedrive, "Job Search tailored resumes")


def file_name(job):
    clean = lambda s: re.sub(r"[^\w &.,()-]+", " ", s or "").strip()[:60].strip()
    parts = [p for p in ("Resume", clean(job.get("company")), clean(job.get("title"))) if p]
    return " - ".join(parts) + ".docx"


def save(job, picked):
    """Write the tailored copy with the changes she picked (indexes into the saved suggestions)."""
    saved = _saved(job["id"])
    changes = saved.get("changes") or []
    chosen = [changes[i] for i in picked if isinstance(i, int) and 0 <= i < len(changes)]
    if not chosen:
        raise TailorError("Pick at least one change to save")
    with open(resume_file(), "rb") as f:
        data = f.read()
    target_dir = folder()
    os.makedirs(target_dir, exist_ok=True)
    path = os.path.join(target_dir, file_name(job))
    try:
        with open(path, "wb") as f:
            f.write(write_resume(data, chosen))
    except PermissionError:
        raise TailorError(f"Couldn't save {os.path.basename(path)}. Is it open in Word? Close it and try again.")
    _save(job["id"], {**saved, "path": path, "picked": sorted(set(picked))})
    return path

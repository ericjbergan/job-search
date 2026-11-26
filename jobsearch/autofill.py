"""Fill application: fill in a job application form in a browser window the app opens.

How it works:
  1. open_job(url) opens the job's link in a real browser window (Google Chrome, or Edge if Chrome is missing;
     Playwright drives it). She clicks Apply and signs in there herself.
  2. fill_page(job, info, profile) reads every field on the page she's looking at (iframes included),
     fills what her saved Application info answers directly (name, email, phone, address, LinkedIn,
     resume upload), and asks the AI to answer the rest from her resume and the job listing.
     Filled fields get an outline: green = from her info, orange = drafted by the AI (check these).
  3. She checks the page, moves on, and clicks Fill this page again for the next step.
It never submits anything, never types passwords, and leaves agreement / consent boxes for her.

Phone, email and street address are typed in directly and never sent to the AI.

Playwright's sync API only works from the thread that started it, so every browser call runs on one
worker thread (_worker); Flask request threads hand it work and wait.
"""

import os
import queue
import re
import threading

from . import ai, db, profile_files, ranking, tailor


class FillError(Exception):
    """A problem to show her as-is."""


# --- reading the page's fields -------------------------------------------------------------------

# Runs inside each frame. Tags every field with data-jf="<id>" (radio / checkbox options also get
# data-jf-opt="<n>") so fill steps can find them again, and returns a description of each.
EXTRACT_JS = r"""
(prefix) => {
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const shown = el => {
    const r = el.getBoundingClientRect(), s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
  };
  // Text pieces joined by line breaks, so the label's first line can be told from help text after it.
  const textOf = n => {
    const out = [], w = document.createTreeWalker(n, NodeFilter.SHOW_TEXT);
    let x;
    while ((x = w.nextNode())) { const t = clean(x.nodeValue); if (t) out.push(t); }
    return out.join('\n');
  };
  const byIds = ids => (ids || '').split(/\s+/).filter(Boolean)
      .map(id => { const n = document.getElementById(id); return n ? textOf(n) : ''; }).filter(Boolean).join('\n');
  const ownText = (n, dropOptions) => {
    const c = n.cloneNode(true);
    if (dropOptions) {  // leave out the labels of the radio / checkbox choices themselves
      c.querySelectorAll('label').forEach(l => {
        const f = l.getAttribute('for'), target = f ? c.querySelector('#' + CSS.escape(f)) : null;
        if (l.querySelector('input[type=radio],input[type=checkbox]') || (target && /radio|checkbox/.test(target.type))) l.remove();
      });
    }
    c.querySelectorAll('input,select,textarea,button,option,script,style,[role=listbox],[role=option]').forEach(x => x.remove());
    return textOf(c);
  };
  const fieldsIn = n => n.querySelectorAll('input:not([type=hidden]), select, textarea').length;
  const GENERIC = /^(start typing|type to search|search|select|choose|please select|enter)\b/i;
  function labelOf(el) {
    let t = '';
    if (el.labels && el.labels.length) t = [...el.labels].map(l => ownText(l)).filter(Boolean).join('\n');
    if (!t) t = byIds(el.getAttribute('aria-labelledby'));
    if (!t) t = clean(el.getAttribute('aria-label'));
    const ph = clean(el.getAttribute('placeholder'));
    if (!t && ph && !GENERIC.test(ph)) t = ph;
    // text around the field, as long as the container holds no other field
    if (!t) { let n = el.parentElement; for (let i = 0; i < 4 && n && !t && fieldsIn(n) === 1; i++, n = n.parentElement) t = ownText(n); }
    if (!t) t = ph || clean(el.name || el.id);
    return t.slice(0, 400);
  }
  function containerLabel(box) {
    if (box.tagName === 'FIELDSET') {
      const lg = box.querySelector('legend');
      if (lg && textOf(lg)) return textOf(lg).slice(0, 400);
    }
    const t = byIds(box.getAttribute('aria-labelledby')) || clean(box.getAttribute('aria-label'));
    return (t || ownText(box, true)).slice(0, 400);
  }
  function groupLabel(el) {
    const fs = el.closest('fieldset,[role=radiogroup],[role=group]');
    if (fs) { const t = containerLabel(fs); if (t) return t; }
    // the question is the text next to the options (not the options' own labels)
    let n = (el.closest('label') || el).parentElement;
    for (let i = 0; i < 6 && n; i++, n = n.parentElement) {
      if (el.name && n.querySelectorAll(`input[name="${CSS.escape(el.name)}"]`).length !== fieldsIn(n)) break;
      const t = ownText(n, true); if (t) return t.slice(0, 400);
    }
    return clean(el.name);
  }
  // The heading a field sits under ("Work Experience 2", "Education 1"), so repeated sections can be told apart.
  const heads = [...document.querySelectorAll('h1,h2,h3,h4,h5,h6,[role=heading]')].filter(h => clean(h.textContent) && shown(h));
  const sectionOf = el => {
    for (let i = heads.length - 1; i >= 0; i--)
      if (heads[i].compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING) return clean(heads[i].textContent).slice(0, 150);
    return '';
  };
  // A labelled group around the field ("From" around Month / Year boxes).
  const groupOf = el => {
    const up = el.parentElement;
    // a fieldset's legend first (Workday: <legend>From*</legend> around a role=group of Month / Year)
    const g = up && ((up.closest('fieldset') && up.closest('fieldset').querySelector('legend') && up.closest('fieldset')) ||
                     up.closest('[role=group],fieldset'));
    return g ? clean(containerLabel(g).split('\n')[0]).slice(0, 150) : '';
  };
  const searchBox = el => /searchbox|multiselect/i.test(el.getAttribute('data-automation-id') || '') ||
      !!el.closest('[data-automation-id*=multiSelect],[data-automation-id*=multiselect]') ||
      /^(type to add|search)/i.test(el.getAttribute('placeholder') || '');
  document.querySelectorAll('[data-jf]').forEach(e => { e.removeAttribute('data-jf'); e.removeAttribute('data-jf-opt'); });
  const fields = [], groups = {}, containers = new Map(), elOf = {};
  let k = 0;
  const els = document.querySelectorAll('input, select, textarea, [role=combobox], button[aria-haspopup=listbox]');
  for (const el of els) {
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute('type') || '').toLowerCase();
    if (tag === 'input' && ['hidden', 'submit', 'button', 'reset', 'image', 'password', 'search'].includes(type)) continue;
    if (el.disabled) continue;
    const box = type === 'radio' || type === 'checkbox';
    if (type !== 'file' && !shown(el) && !(box && el.labels && [...el.labels].some(shown))) continue;
    const hint = clean([el.name, el.id, el.getAttribute('autocomplete'), el.getAttribute('placeholder'),
                        el.getAttribute('data-automation-id')].join(' ')).slice(0, 300);
    const required = !!(el.required || el.getAttribute('aria-required') === 'true');
    // checkboxes that belong together: same name, or several in one fieldset / group
    let many = null;
    if (type === 'checkbox') {
      if (el.name && document.querySelectorAll(`input[type=checkbox][name="${CSS.escape(el.name)}"]`).length > 1) many = 'name';
      else {
        const box = el.closest('fieldset,[role=group]');
        // a list of checkboxes, not a whole form section that happens to hold several ("I currently work here")
        if (box && box.querySelectorAll('input[type=checkbox]').length > 1 &&
            !box.querySelector('input:not([type=checkbox]):not([type=hidden]), select, textarea')) many = box;
      }
    }
    if (type === 'radio' || many) {
      let key = type + ':' + (el.name || ('_' + k));
      if (many && many !== 'name') {
        if (!containers.has(many)) containers.set(many, 'box' + containers.size);
        key = containers.get(many);
      }
      let g = groups[key];
      if (!g) {
        const label = many && many !== 'name' ? containerLabel(many) || groupLabel(el) : groupLabel(el);
        g = {id: prefix + 'f' + (k++), kind: type === 'radio' ? 'radio' : 'checkboxes', label,
             hint, options: [], required, filled: false};
        groups[key] = g; fields.push(g); elOf[g.id] = el;
      }
      el.setAttribute('data-jf', g.id); el.setAttribute('data-jf-opt', String(g.options.length));
      g.options.push(labelOf(el) || el.value);
      if (el.checked) g.filled = true;
      if (required) g.required = true;
      continue;
    }
    const f = {id: prefix + 'f' + (k++), label: labelOf(el), hint, required, options: []};
    el.setAttribute('data-jf', f.id);
    if (tag === 'select') {
      f.kind = 'select';
      f.options = [...el.options].map(o => clean(o.text));
      const o = el.options[el.selectedIndex];
      f.filled = !!(o && clean(o.value) && !/^(select|choose|please|--)/i.test(clean(o.text)));
    } else if (type === 'checkbox') {
      f.kind = 'checkbox'; f.filled = el.checked;
    } else if (type === 'file') {
      f.kind = 'file'; f.filled = !!(el.files && el.files.length);
    } else if (el.getAttribute('role') === 'combobox' || el.getAttribute('aria-haspopup') === 'listbox' ||
               (tag === 'input' && searchBox(el))) {
      f.kind = 'combobox';
      const now = clean(tag === 'input' ? el.value : el.textContent);
      // text left in a search box (Workday's skills) isn't a choice
      f.filled = !!now && !/^(select|choose|please)/i.test(now) && !(tag === 'input' && searchBox(el));
    } else {
      f.kind = tag === 'textarea' ? 'textarea' : 'text';
      f.input_type = type || 'text';
      f.filled = !!(el.value || '').trim();
    }
    fields.push(f); elOf[f.id] = el;
  }
  // Yes / No questions drawn as a pair of buttons (no radio buttons underneath).
  const pairs = new Set();
  for (const b of document.querySelectorAll('button, [role=button]')) {
    if (!/^(yes|no)$/i.test(clean(b.textContent)) || !shown(b) || b.closest('form') === null && !b.closest('[class*=form],[class*=Form]')) continue;
    const box = b.parentElement;
    if (!box || pairs.has(box) || fieldsIn(box)) continue;
    const opts = [...box.children].filter(x => /^(yes|no)$/i.test(clean(x.textContent)));
    if (opts.length !== 2 || box.children.length !== 2) continue;
    pairs.add(box);
    let label = '', n = box.parentElement;
    for (let i = 0; i < 4 && n && !label && fieldsIn(n) === 0; i++, n = n.parentElement) label = ownText(n);
    if (!label) continue;
    const g = {id: prefix + 'f' + (k++), kind: 'radio', label: label.slice(0, 400), hint: 'yes-no buttons',
               options: [], required: /\*/.test(label), filled: false};
    opts.forEach((o, i) => {
      o.setAttribute('data-jf', g.id); o.setAttribute('data-jf-opt', String(i)); g.options.push(clean(o.textContent));
      if (o.getAttribute('aria-pressed') === 'true' || o.getAttribute('aria-checked') === 'true' ||
          /\b(active|selected|checked)\b/i.test(o.className)) g.filled = true;
    });
    fields.push(g); elOf[g.id] = box;
  }
  for (const f of fields) {
    f.section = sectionOf(elOf[f.id]);
    const g = groupOf(elOf[f.id]);
    if (g && g !== f.label.split('\n')[0]) f.group = g;
  }
  return fields;
}
"""


# --- answering from her saved info ------------------------------------------------------------------

# autocomplete="..." values that say exactly what a field is
AUTOCOMPLETE = {"given-name": "first_name", "family-name": "last_name", "name": "full_name", "email": "email",
                "tel": "phone", "tel-national": "phone", "address-line1": "street", "street-address": "street",
                "address-level2": "city", "address-level1": "state", "postal-code": "zip",
                "country": "country", "country-name": "country", "url": "website"}

# (pattern on the field's label, info key). First match wins; only for short labels (not questions).
LABEL_RULES = [
    (r"linked ?in", "linkedin"),
    (r"e-?mail", "email"),
    (r"first and last name|full name|legal name\b|^name$", "full_name"),
    (r"preferred (first )?name|nick ?name", "preferred_name"),
    (r"\b(first|given) name\b|^first$", "first_name"),
    (r"\b(last|family) name\b|\bsurname\b|^last$", "last_name"),
    (r"^(full |legal |your )?name\b", "full_name"),
    (r"\b(phone|mobile|cell|telephone)\b", "phone"),
    (r"address line 1|^street|^address( 1)?\b|first line of (your )?address", "street"),
    (r"\bcity\b|\btown\b", "city"),
    (r"^state\b|\bstate$|province|state / province", "state"),
    (r"\bzip\b|postal|post ?code", "zip"),
    (r"^country\b|\bcountry$|country/region", "country"),
    (r"website|portfolio|personal (site|url)", "website"),
]
NOT_THESE = re.compile(r"line 2|second line|extension|\bext\b|country code|phone type|device type|confirm.*password|"
                       r"how did you|reference|emergency|manager|supervisor|employer|company|school", re.I)
# Field names (name= / id=) for sections about schools, past jobs or other people.
OTHER_PEOPLE = re.compile(r"education|school|employment|work.?history|experience|reference|emergency|employer", re.I)

# Never filled: she types these herself (or they shouldn't be on a first application at all).
SENSITIVE = re.compile(r"password|social security|\bssn\b|date of birth|birth ?date|\bdob\b|bank|routing|"
                       r"account number|credit card|card number|driver'?s? licen[cs]e number|passport", re.I)
# "Autofill from resume" uploads: the site would rewrite the form from the file, over our answers.
SITE_AUTOFILL = re.compile(r"autofill|auto-fill|automatically|apply with|fill out .* with", re.I)
# Agreement / consent boxes are hers to tick.
MAX_PICKS = 10  # most values put in one multi-pick box (skills)
CONSENT = re.compile(r"agree|consent|terms|privacy|certify|acknowledge|attest|authori[sz]e .* (to|the)|"
                     r"accurate|true and complete|text message|sms|subscribe|marketing|signature|sign", re.I)

US_STATES = {"AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California",
             "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware", "DC": "District of Columbia",
             "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana",
             "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
             "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri",
             "MT": "Montana", "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey",
             "NM": "New Mexico", "NY": "New York", "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio",
             "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
             "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont",
             "VA": "Virginia", "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming"}
COUNTRY_NAMES = {"united states", "united states of america", "usa", "us", "u.s.", "u.s.a."}


def _norm(s):
    return re.sub(r"[^a-z0-9+]+", " ", (s or "").lower()).strip()


def info_value(key, info):
    if key == "full_name":
        return " ".join(x for x in (info.get("first_name"), info.get("last_name")) if x)
    if key == "preferred_name":
        return info.get("preferred_name") or info.get("first_name") or ""
    if key == "country":
        return info.get("country") or "United States"
    return info.get(key) or ""


def _id_like(label):
    """No real label, only an id or name like "btn-resume" or "resumeUpload" (not a word like "Resume")."""
    return bool(re.fullmatch(r"[\w.-]+", label) and re.search(r"[-_.\d]|[a-z][A-Z]", label))


def known_key(field):
    """Which Application info value answers this field, or None (then the AI may)."""
    label = (field.get("head") or field.get("label") or "").strip()
    hint = (field.get("hint") or "").lower()
    if OTHER_PEOPLE.search(hint):  # e.g. educationHistory.city: the school's city, not hers
        return None
    for token in hint.split():
        if token in AUTOCOMPLETE and not NOT_THESE.search(label):
            return AUTOCOMPLETE[token]
    if len(label) > 60 or NOT_THESE.search(label):
        return None
    low = label.lower().rstrip(" *:")
    for pattern, key in LABEL_RULES:
        if re.search(pattern, low):
            return key
    return None


def pick_option(value, options):
    """Index of the option that best matches `value` (a state abbreviation matches its name, "USA"
    matches "United States"), or None."""
    want = _norm(value)
    if not want:
        return None
    names = {want}
    if value.strip().upper() in US_STATES:
        names.add(_norm(US_STATES[value.strip().upper()]))
    for abbr, full in US_STATES.items():
        if want == _norm(full):
            names.add(abbr.lower())
    if want in COUNTRY_NAMES:
        names |= COUNTRY_NAMES
    texts = [_norm(o) for o in options]
    for i, t in enumerate(texts):
        if t and t in names:
            return i
    for i, t in enumerate(texts):  # "United States (+1)", "Ohio - OH"
        if t and any(re.search(rf"\b{re.escape(n)}\b", t) for n in names if len(n) > 2):
            return i
    for i, t in enumerate(texts):  # the answer is a longer version of the option ("Yes, I am")
        if t and len(t) > 1 and re.match(rf"{re.escape(t)}\b", want):
            return i
    return None


# --- the plan: what goes in each field -------------------------------------------------------------

def build_plan(fields, info, ask_ai=None, resume_file=None):
    """Steps to fill the page plus what's left for her.
    Returns (steps, needs_you, ai_note). Each step: {id, kind, label, value | option(s), source}."""
    steps, needs_you, for_ai = [], [], []
    for f in fields:
        if f.get("filled"):
            continue
        label = f.get("label") or f.get("hint") or "(unlabeled field)"
        kind = f["kind"]
        if SENSITIVE.search(label):
            needs_you.append(label)
            continue
        if (kind in ("checkbox", "checkboxes") and CONSENT.search(label)) or \
                (kind == "radio" and any(re.search(r"consent|agree|opt.?in", o, re.I) for o in f["options"])):
            if f.get("required"):
                needs_you.append(label)
            continue
        if kind == "file":
            text = f"{label} {f.get('hint', '')} {f.get('section', '')} {f.get('group', '')}".lower()
            if (re.search(r"resume|\bcv\b|curriculum", text) and "cover" not in text and resume_file
                    and not SITE_AUTOFILL.search(label)
                    and not _id_like(label)):
                steps.append({"id": f["id"], "kind": "file", "label": label, "value": resume_file, "source": "info"})
            elif f.get("required"):
                needs_you.append(label)
            continue
        key = known_key(f) if kind in ("text", "textarea", "select", "combobox") else None
        value = info_value(key, info) if key else ""
        if value:
            if kind == "select":
                i = pick_option(value, f["options"])
                if i is not None:
                    steps.append({"id": f["id"], "kind": kind, "label": label, "option": i, "source": "info"})
                    continue
                for_ai.append(f)  # her answer isn't one of the choices as written
                continue
            steps.append({"id": f["id"], "kind": kind, "label": label, "value": value, "source": "info"})
            continue
        if key in ("email", "phone", "street", "zip"):  # private: never guessed by the AI
            if f.get("required"):
                needs_you.append(label)
            continue
        for_ai.append(f)

    ai_note = ""
    if for_ai and ask_ai:
        try:
            answers = ask_ai(for_ai)
        except ai.AIError as e:
            answers, ai_note = {}, f"The AI couldn't answer the other questions: {e}"
        for f in for_ai:
            step = _ai_step(f, answers.get(f["id"]))
            if step:
                steps.append(step)
            elif f.get("required"):
                needs_you.append(f.get("label") or "(unlabeled field)")
    else:
        needs_you += [f.get("label") or "(unlabeled field)" for f in for_ai if f.get("required")]
    return steps, needs_you, ai_note


def _ai_step(f, answer):
    """A fill step from the AI's answer, checked against the field's choices; None if unusable."""
    if answer is None or answer == "" or answer == []:
        return None
    label, kind = f.get("label") or "(unlabeled field)", f["kind"]
    base = {"id": f["id"], "kind": kind, "label": label, "source": "ai", "section": f.get("section") or ""}
    if kind in ("select", "radio"):
        i = pick_option(str(answer), f["options"])
        return None if i is None else {**base, "option": i}
    if kind == "checkboxes":
        picks = answer if isinstance(answer, list) else [answer]
        found = sorted({i for a in picks for i in [pick_option(str(a), f["options"])] if i is not None})
        return {**base, "options": found} if found else None
    if kind == "checkbox":
        return {**base, "value": True} if str(answer).strip().lower() in ("yes", "true", "check", "checked") else None
    if isinstance(answer, list):
        values = [str(a)[:200] for a in answer if str(a).strip()][:MAX_PICKS]
        if not values:
            return None
        return {**base, "values": values} if kind == "combobox" else {**base, "value": ", ".join(values)}
    return {**base, "value": str(answer)[:5000]}


AI_INSTRUCTIONS = """You are helping a job seeker fill in an online job application. For each form field listed,
give the answer she would enter, using only her resume, her application info, and the job listing.

Rules:
- For "select", "radio" and "combobox" fields, answer with the exact text of one of the field's options
  (combobox fields may have no options listed: give the text to choose).
- For "checkboxes" fields, answer with a list of the option texts to tick. For a "checkbox" field, answer "yes" or "no".
- For text fields, write the answer itself. Dates as the field expects (if unsure, MM/DD/YYYY; "date" inputs YYYY-MM-DD).
- Each field may have a "section" (the heading it sits under, e.g. "Work Experience 2") and a "group" (e.g. "From"
  around Month / Year boxes). Fields under "Work Experience N" / "Education N" get entry N of her work history /
  education list below (entry 1 = the first one listed). Month boxes take MM, year boxes YYYY. For "I currently
  work here", answer "yes" only for a current job.
- Skills boxes ("Type to Add Skills"): answer with a list of up to 10 skills from her resume that fit this job.
- Open-ended questions ("why are you interested", "describe your experience"): 2-4 sentences in the first
  person, plain and specific to this job and her resume. No made-up facts, numbers or employers.
- Demographic / EEO questions: use her stated choice; if it's blank, pick the option that declines to answer.
- Yes/no eligibility questions (work authorization, sponsorship, relocation, age 18+, background check):
  use her application info; if it doesn't say, leave the field out.
- Leave out any field you can't answer truthfully from what's given. Never answer for passwords,
  Social Security numbers, dates of birth or bank details.
Return JSON: {"answers": {"<field id>": <answer>, ...}}"""

PRIVATE_INFO = ("email", "phone", "street", "zip")


def ask_ai_for(info, profile, job, history=None):
    """A function that asks the AI to answer form fields (her phone, email and street aren't sent).
    `history`: her work history and education from resume_entries(), when the page has those sections."""
    import json

    def ask(fields):
        shared = {k: v for k, v in info.items() if v and k not in PRIVATE_INFO}
        listing = "\n".join(x for x in (
            f"Title: {job.get('title') or ''}", f"Company: {job.get('company') or ''}",
            f"Location: {job.get('location') or ''}", "", (job.get("description") or "")[:8000]) if x is not None)
        form = [{"id": f["id"], "type": f["kind"], "label": f.get("label") or f.get("hint") or "",
                 **({"section": f["section"]} if f.get("section") else {}),
                 **({"group": f["group"]} if f.get("group") else {}),
                 **({"options": [o for o in f["options"] if o]} if f.get("options") else {}),
                 **({"required": True} if f.get("required") else {})} for f in fields]
        text = ("HER APPLICATION INFO:\n" + json.dumps(shared, indent=1) +
                "\n\nHER RESUME:\n" + ranking.resume_for_ai(profile.get("resume") or "")[:20000] +
                ("\n\nHER WORK HISTORY AND EDUCATION (in form order):\n" + json.dumps(history, indent=1)
                 if history else "") +
                "\n\nTHE JOB:\n" + listing +
                "\n\nFORM FIELDS:\n" + json.dumps(form, indent=1))
        for k in PRIVATE_INFO:  # her exact values too, however the resume writes them
            if len(info.get(k) or "") >= 4:
                text = re.sub(re.escape(info[k]), "[removed]", text, flags=re.I)
        reply = ai.json_answer(AI_INSTRUCTIONS, text, task="applications")
        found = ai.first_json_object(reply)
        if not found:
            raise ai.AIError("The AI's answer couldn't be read")
        answers = json.loads(found).get("answers") or {}
        return answers if isinstance(answers, dict) else {}
    return ask


# --- the browser (one worker thread owns it) -----------------------------------------------------

class _Worker:
    def __init__(self):
        self._jobs = queue.Queue()
        self._thread = None
        self._lock = threading.Lock()

    def run(self, fn, timeout=180):
        with self._lock:
            if not self._thread or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._loop, name="autofill-browser", daemon=True)
                self._thread.start()
        done, box = threading.Event(), {}
        self._jobs.put((fn, box, done))
        if not done.wait(timeout):
            raise FillError("The browser window stopped answering. Close it and click Fill application again.")
        if "error" in box:
            raise box["error"]
        return box.get("value")

    def _loop(self):
        while True:
            fn, box, done = self._jobs.get()
            try:
                box["value"] = fn()
            except Exception as e:  # handed back to the request that asked
                box["error"] = e
            finally:
                done.set()


_worker = _Worker()
_browser = {"pw": None, "context": None}


def browser_dir(channel="chrome"):
    """Where the filler's browser keeps its sign-ins, next to the database (git-ignored):
    .browser-jobs for jobs.db, .browser-demo for demo.db (plus -msedge / -chromium for those browsers,
    since one browser can't open another's profile)."""
    path = db.db_path()
    stem = os.path.splitext(os.path.basename(path))[0]
    suffix = "" if channel == "chrome" else f"-{channel}"
    return os.path.join(os.path.dirname(os.path.abspath(path)), f".browser-{stem}{suffix}")


def _channels():
    chosen = (os.getenv("APPLY_BROWSER") or "").strip().lower()
    order = ["chrome", "msedge", "chromium"]  # Chrome first: what she uses
    return ([chosen] + [c for c in order if c != chosen]) if chosen else order


def _context():
    ctx = _browser["context"]
    if ctx is not None:
        try:
            if ctx.pages:
                return ctx
        except Exception:
            pass
        _close_quietly()
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise FillError("Fill application isn't set up on this computer yet (the 'playwright' package is "
                        "missing). Close the app, double-click setup.bat in the job-search folder, then start "
                        "the app again.")
    if _browser["pw"] is None:
        _browser["pw"] = sync_playwright().start()
    errors = []
    for channel in _channels():
        try:
            ctx = _browser["pw"].chromium.launch_persistent_context(
                browser_dir(channel), channel=None if channel == "chromium" else channel,
                headless=False, no_viewport=True)
            break
        except Exception as e:
            errors.append(f"{channel}: {str(e).splitlines()[0]}")
    else:
        raise FillError("Couldn't open a browser window (" + "; ".join(errors) + ")")
    ctx.on("close", lambda *_: _browser.update(context=None))
    _browser["context"] = ctx
    return ctx


def _close_quietly():
    ctx, _browser["context"] = _browser["context"], None
    if ctx is not None:
        try:
            ctx.close()
        except Exception:
            pass


def _current_page():
    ctx = _browser["context"]
    pages = []
    try:
        pages = [p for p in ctx.pages if not p.is_closed()] if ctx else []
    except Exception:
        pass
    if not pages:
        raise FillError("The application window is closed. Click Fill application on the job to open it again.")
    for p in reversed(pages):  # the tab she's looking at (others are hidden)
        try:
            if p.evaluate("document.visibilityState") == "visible":
                return p
        except Exception:
            continue
    return pages[-1]


def open_job(url):
    """Open the job's link in the filler's browser window."""
    def go():
        ctx = _context()
        pages = ctx.pages
        page = pages[0] if len(pages) == 1 and pages[0].url in ("about:blank", "") else ctx.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
        except Exception as e:
            if "timeout" not in str(e).lower():
                raise FillError(f"Couldn't open the listing: {str(e).splitlines()[0]}")
        page.bring_to_front()
        return page.url
    return _worker.run(go)


def close():
    _worker.run(_close_quietly, timeout=30)


def is_open():
    def check():
        try:
            return _current_page().url
        except FillError:
            return None
    return _worker.run(check, timeout=15)


def extract_fields(page):
    """Every fillable field on the page, iframes included (ids are "<frame>-f<n>")."""
    fields = []
    for i, frame in enumerate(page.frames):
        try:
            found = frame.evaluate(EXTRACT_JS, f"{i}-")
        except Exception:
            continue  # a frame that went away or won't run scripts
        for f in found:
            f["frame"] = i
            lines = (f.get("label") or "").split("\n")
            f["head"] = lines[0][:200]  # the label's first line ("Mobile Number", not its help text)
            if f["head"].rstrip().endswith("*"):  # how Workday and others mark required fields
                f["required"] = True
            f["label"] = " ".join(lines)[:300]
            f["options"] = [" ".join(o.split("\n")) for o in f.get("options") or []]
        fields += found
    return fields


def _frame_for(page, field_id):
    try:
        return page.frames[int(field_id.split("-", 1)[0])]
    except (ValueError, IndexError):
        return None


HIGHLIGHT_JS = """(el, color) => {
  const t = (el.type === 'radio' || el.type === 'checkbox') && el.labels && el.labels[0] ? el.labels[0] : el;
  t.style.outline = '2px solid ' + color; t.style.outlineOffset = '1px';
}"""
COLORS = {"info": "#2e9e5b", "ai": "#f0a020"}


def apply_plan(page, steps):
    """Fill the page. Returns (labels filled, labels that couldn't be filled)."""
    filled, failed = [], []
    for step in steps:
        frame = _frame_for(page, step["id"])
        try:
            if frame is None:
                raise FillError("field gone")
            _apply_step(page, frame, step)
            filled.append(step)
        except Exception:
            failed.append(step["label"])
    return filled, failed


def _apply_step(page, frame, step):
    sel = f'[data-jf="{step["id"]}"]'
    kind, color = step["kind"], COLORS[step["source"]]
    if kind in ("radio", "checkboxes"):
        for n in ([step["option"]] if kind == "radio" else step["options"]):
            box = frame.locator(f'{sel}[data-jf-opt="{n}"]').first
            box.evaluate("el => { if (!el.checked) el.click(); }")
            box.evaluate(HIGHLIGHT_JS, color)
        return
    loc = frame.locator(sel).first
    if kind == "checkbox":
        loc.evaluate("el => { if (!el.checked) el.click(); }")
    elif kind == "select":
        loc.select_option(index=step["option"], timeout=5000)
    elif kind == "file":
        loc.set_input_files(step["value"], timeout=10000)
        return
    elif kind == "combobox":
        if step.get("values"):  # several picks (skills): keep the ones the site knows
            picked = 0
            for value in step["values"]:
                try:
                    _choose_from_list(page, frame, loc, value, keep_text=False)
                    picked += 1
                except Exception:
                    continue
            if not picked:
                raise FillError("none of the choices matched")
        else:
            _choose_from_list(page, frame, loc, step["value"])
    else:
        loc.fill(step["value"], timeout=5000)
        if _same_text(loc.input_value(), step["value"]) is False:  # boxes that ignore fill: type it
            loc.click(timeout=5000, force=True)
            page.keyboard.press("Control+A")
            page.keyboard.type(step["value"], delay=40)
    loc.evaluate(HIGHLIGHT_JS, color)


OPTION_SELECTOR = "[role=option]:visible, [data-automation-id=promptOption]:visible"


def _visible_options(page, frame):
    """Option lists showing now (in the field's frame or the page)."""
    found = []
    for where in dict.fromkeys((frame, page.main_frame)):
        options = where.locator(OPTION_SELECTOR)
        if options.count():
            found.append(options)
    return found


def _search_terms(value):
    """What to type into a search-as-you-type box, best first: "Newark, OH" -> "Newark, OH",
    "Newark, Ohio", "Newark" (place pickers often know only full state names, or only the town)."""
    terms = [value]
    m = re.match(r"\s*([^,]+),\s*([A-Za-z]{2})\b", value)
    if m and m.group(2).upper() in US_STATES:
        terms.append(f"{m.group(1).strip()}, {US_STATES[m.group(2).upper()]}")
    if "," in value:
        terms.append(value.split(",")[0].strip())
    return list(dict.fromkeys(t for t in terms if t))


def _same_text(now, wanted):
    """Did the box keep what was typed? Numbers count as the same with or without leading zeros
    (Workday's month box shows 05 but holds 5), and boxes that reformat (phone numbers) count too."""
    now, wanted = (now or "").strip(), (wanted or "").strip()
    if now == wanted or (now.isdigit() and wanted.isdigit() and int(now) == int(wanted)):
        return True
    digits = lambda s: re.sub(r"\D", "", s)
    if digits(wanted) and digits(now) == digits(wanted):
        return True
    return False if not now else True  # empty means it didn't take; other text is the site's own format


def _choose_from_list(page, frame, loc, value, keep_text=True):
    """Dropdowns built from buttons and lists (Workday, React): open it, type if it takes typing,
    then click the matching choice. `keep_text`: if nothing matches, leave the answer typed in
    (an address); otherwise clear it (a skills box, where stray text isn't an answer)."""
    loc.click(timeout=5000)
    typed = loc.evaluate("el => el.tagName === 'INPUT'")
    queries = _search_terms(value)
    # match on the full answer ("Newark, Ohio"), never the town alone (Newark, New Jersey would win)
    wanted = queries[:2] if "," in value else queries

    def choose():
        for options in _visible_options(page, frame):
            texts = options.all_inner_texts()
            for want in wanted:
                i = pick_option(want, texts)
                if i is not None:
                    options.nth(i).click(timeout=5000)
                    return True
        return False

    for query in (queries if typed else [None]):
        if query is not None:
            loc.fill(query, timeout=5000)
        page.wait_for_timeout(1200)
        if choose():
            return
        if query is not None:  # boxes that search only on Enter (Workday's skills, schools)
            loc.press("Enter")
            page.wait_for_timeout(2500)
            if choose():
                return
    if typed:
        loc.fill(value if keep_text else "", timeout=5000)
    page.keyboard.press("Escape")
    if not typed or not keep_text:
        raise FillError("no matching choice")


# --- sections behind "Add" buttons (Workday's Work Experience, Education) -------------------------

ADDS_JS = r"""
(prefix) => {
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const shown = el => {
    const r = el.getBoundingClientRect(), s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
  };
  const heads = [...document.querySelectorAll('h1,h2,h3,h4,h5,h6,[role=heading]')].filter(h => clean(h.textContent) && shown(h));
  const before = el => {
    for (let i = heads.length - 1; i >= 0; i--)
      if (heads[i].compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING) return clean(heads[i].textContent);
    return '';
  };
  document.querySelectorAll('[data-jf-add]').forEach(e => e.removeAttribute('data-jf-add'));
  const out = [];
  let k = 0;
  for (const b of document.querySelectorAll('button, [role=button]')) {
    const text = clean(b.textContent), aria = clean(b.getAttribute('aria-label'));
    if (!shown(b) || !(/^add( another)?$/i.test(text) || /^add\b/i.test(aria))) continue;
    const named = aria.replace(/^add (another )?/i, '');
    const id = prefix + 'a' + (k++);
    b.setAttribute('data-jf-add', id);
    out.push({id, text: text || aria, section: named || before(b)});
  }
  return out;
}
"""

# Which "Add" sections to open, and how many entries at most (Workday forms get long).
ADD_SECTIONS = (("work", re.compile(r"work experience|work history|employment|experience", re.I), 5),
                ("education", re.compile(r"education", re.I), 3))


def find_adds(page):
    adds = []
    for i, frame in enumerate(page.frames):
        try:
            found = frame.evaluate(ADDS_JS, f"{i}-")
        except Exception:
            continue
        for a in found:
            a["frame"] = i
            a["kind"] = next((k for k, pattern, _ in ADD_SECTIONS if pattern.search(a["section"] or "")), None)
            a["empty"] = not re.search(r"another", a["text"], re.I)
            # entries already there: "Add Another" sits under the last one ("Work Experience 3")
            m = re.search(r"(\d+)\s*$", a["section"] or "")
            a["count"] = 0 if a["empty"] else int(m.group(1)) if m else 1
        adds += found
    return adds


def open_sections(page, adds, history):
    """Click Add until a section has one entry per resume entry (up to the section's limit).
    Never removes anything; entries already there are left as they are. Returns how many were added per kind."""
    added = {}
    for kind, _, most in ADD_SECTIONS:
        start = next((a for a in reversed(adds) if a["kind"] == kind), None)
        if not start:
            continue
        want = min(len(history.get(kind) or []), most) - start["count"]
        if want <= 0:
            continue
        button_id = start["id"]
        for n in range(want):
            frame = _frame_for(page, button_id)
            if frame is None:
                break
            frame.locator(f'[data-jf-add="{button_id}"]').first.click(timeout=5000)
            page.wait_for_timeout(1200)
            added[kind] = n + 1
            # the button is now "Add Another" in the same section; find it again
            again = [a for a in find_adds(page) if a["kind"] == kind]
            if not again:
                break
            button_id = again[-1]["id"]
    return added


HISTORY_INSTRUCTIONS = """Read this resume and list her work history and education, most recent first.
Return JSON: {"work": [{"title": "", "company": "", "location": "City, ST or Remote", "start": "MM/YYYY",
"end": "MM/YYYY or empty if current", "current": true|false, "description": "2-4 plain sentences from the resume"}],
"education": [{"school": "", "degree": "", "field": "", "start_year": "", "end_year": ""}]}
Use only what the resume says; leave unknown values empty. If a date has only a year, use 01 for the month."""


def resume_entries(profile):
    """Her work history and education from the resume (asked of the AI once per resume, then saved)."""
    import hashlib
    import json
    resume = ranking.resume_for_ai(profile.get("resume") or "")
    if not resume.strip():
        return {}
    digest = hashlib.sha256(resume.encode("utf-8")).hexdigest()[:16]
    try:
        saved = json.loads(db._get_meta("apply_history") or "{}")
    except ValueError:
        saved = {}
    if saved.get("hash") == digest:
        return saved.get("data") or {}
    found = ai.first_json_object(ai.json_answer(HISTORY_INSTRUCTIONS, resume[:20000], task="applications"))
    if not found:
        raise ai.AIError("The AI's answer couldn't be read")
    data = json.loads(found)
    data = {k: [e for e in (data.get(k) or []) if isinstance(e, dict)] for k in ("work", "education")}
    db._set_meta("apply_history", json.dumps({"hash": digest, "data": data}))
    return data


# --- what's on the page (for working out why a site isn't filled) ---------------------------------

def snapshot():
    """Save the page she's looking at (all frames) to a file in the temp folder; returns its path.
    It holds what's typed into the form, so it stays on this computer."""
    import tempfile

    def grab():
        page = _current_page()
        parts = [f"<!-- {page.url} -->"]
        for i, frame in enumerate(page.frames):
            try:
                parts.append(f"<!-- frame {i}: {frame.url} -->\n" + frame.content())
            except Exception:
                continue
        path = os.path.join(tempfile.gettempdir(), "jobsearch-apply-page.html")
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(parts))
        return path
    return _worker.run(grab)


def fill_page(job, info, profile):
    """Fill the page she's looking at. Returns what was filled and what's left for her."""
    note, history, added = "", {}, {}
    adds = _worker.run(lambda: find_adds(_current_page()))
    if any(a["kind"] for a in adds):
        try:
            history = resume_entries(profile)
        except ai.AIError as e:
            note = f"Couldn't read her work history from the resume: {e}"
        if history:
            added = _worker.run(lambda: open_sections(_current_page(), adds, history), timeout=300)
    fields = _worker.run(lambda: extract_fields(_current_page()))
    if not fields:
        return {"filled": [], "drafted": [], "needs_you": [], "failed": [], "note": note,
                "empty": True, "found": 0, "added": added}
    steps, needs_you, ai_note = build_plan(fields, info, ask_ai=ask_ai_for(info, profile, job, history),
                                           resume_file=resume_for(job))
    filled, failed = _worker.run(lambda: apply_plan(_current_page(), steps), timeout=600)  # long Workday pages
    uploaded = next((os.path.basename(s["value"]) for s in filled if s["kind"] == "file"), None)
    return {"uploaded": uploaded,
            "filled": [s["label"] for s in filled if s["source"] == "info"],
            "drafted": [_where(s) for s in filled if s["source"] == "ai"],
            "needs_you": needs_you, "failed": failed, "note": " ".join(x for x in (note, ai_note) if x),
            "found": len(fields), "already": sum(1 for f in fields if f.get("filled")), "added": added,
            "seen": [{"label": f.get("label"), "kind": f["kind"], "section": f.get("section"),
                      "filled": bool(f.get("filled"))} for f in fields]}


def resume_for(job):
    """The resume to upload for this job: its tailored copy if one was saved, else her resume."""
    return tailor.tailored_path(job["id"]) or profile_files.resume_path()


def _where(step):
    """ "Job Title (Work Experience 2)" when the label alone doesn't say which entry."""
    return f"{step['label']} ({step['section']})" if step.get("section") and \
        re.search(r"\d", step["section"]) else step["label"]

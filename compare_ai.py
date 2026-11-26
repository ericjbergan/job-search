"""Compare ChatGPT and Claude side by side on the same jobs, without changing anything saved.

    python compare_ai.py                      # 10 saved jobs that have descriptions
    python compare_ai.py --jobs 20            # more jobs
    python compare_ai.py --benefits "Cardinal Health" "Huntington"   # also compare benefits web searches
    python compare_ai.py --matching           # compare scoring her Interested jobs (resume + interests)

Both keys must be in .env (OPENAI_API_KEY and ANTHROPIC_API_KEY). The report is written to
ai-comparison.html (it holds her job data, so git ignores it) and opened in the browser.
Each value is marked with whether it actually appears in the listing text.
"""

import argparse
import html
import json
import os
import re
import sys
import time
import webbrowser

from dotenv import load_dotenv

load_dotenv()

from jobsearch import ai, benefits, boards, db, importer, ranking  # noqa: E402  (after .env is loaded)

FIELDS = ["title", "company", "location", "work_style", "pay", "req_number", "employment_type",
          "posted_date", "closing_date"]
SIDES = [("openai", "ChatGPT"), ("claude", "Claude")]
REPORT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ai-comparison.html")


def read_listing(text):
    """Both AIs read the same text with the same instructions the app uses."""
    out = {}
    for p, _ in SIDES:
        start = time.time()
        try:
            with ai.using(p):
                reply = ai.json_answer(importer._EXTRACT, text)
            out[p] = {"data": json.loads(ai.first_json_object(reply) or "{}"), "secs": time.time() - start}
        except Exception as e:  # show the error in the report and keep going
            out[p] = {"error": str(e), "secs": time.time() - start}
    return out


def blank(value):
    """No answer (some replies say "null" or "N/A" in words)."""
    return value is None or str(value).strip().lower() in ("", "null", "none", "n/a", "not specified", "unknown")


def in_text(value, text):
    """Is this value actually in the listing (so it wasn't made up)?"""
    if blank(value):
        return None
    v = str(value)
    digits = re.findall(r"\d[\d,]*", v)
    if digits:
        return all(d in text for d in digits)
    return boards.norm(v.split(",")[0].split(";")[0]) in boards.norm(text)


def jobs_to_compare(count):
    picked = []
    for j in db.list_jobs():
        full = db.get_job(j["id"])
        if len(full.get("description") or "") >= 500:
            picked.append(full)
        if len(picked) >= count:
            break
    return picked


def job_section(job, results):
    text = re.sub(r"\n{3,}", "\n\n", job["description"].strip())[:importer.MAX_TEXT]
    rows = []
    for f in FIELDS:
        cells, values = [], []
        for p, _ in SIDES:
            r = results[p]
            if "error" in r:
                cells.append('<td class="err">error</td>')
                values.append(None)
                continue
            v = r["data"].get(f)
            values.append(None if blank(v) else boards.norm(str(v)))
            ok = in_text(v, text)
            mark = "" if ok is None else (' <span class="ok">✓ in text</span>' if ok else ' <span class="bad">✗ not in text</span>')
            cells.append(f"<td>{'<span class=none>—</span>' if blank(v) else html.escape(str(v))}{mark}</td>")
        differ = values[0] != values[1]
        rows.append(f'<tr class="{"diff" if differ else ""}"><th>{f.replace("_", " ")}</th>{"".join(cells)}</tr>')
    errors = "".join(f'<p class="err">{name}: {html.escape(results[p]["error"])}</p>'
                     for p, name in SIDES if "error" in results[p])
    times = " · ".join(f'{name} {results[p]["secs"]:.1f}s' for p, name in SIDES)
    return f"""<section><h2>{html.escape(job['title'] or '')} <small>{html.escape(job['company'] or '')}</small></h2>
<p class="meta">Job #{job['id']} · {times}</p>{errors}
<table><tr><th></th>{''.join(f'<th>{n}</th>' for _, n in SIDES)}</tr>{''.join(rows)}</table>
<details><summary>Listing text the AIs read</summary><pre>{html.escape(text)}</pre></details></section>"""


def benefits_section(company):
    cols = []
    for p, name in SIDES:
        start = time.time()
        try:
            with ai.using(p):
                data = benefits._from_web(company)
            facts = "".join(f"<li><b>{html.escape(str(f.get('topic') or ''))}</b>: {html.escape(str(f.get('summary') or ''))}"
                            f"{' <a href=' + json.dumps(f['source_url']) + ' target=_blank>source</a>' if f.get('source_url') else ''}</li>"
                            for f in data.get("facts") or [])
            figures = "".join(f"<li>{html.escape(k)}: <b>{html.escape(str(v))}</b> — “{html.escape(str(data['figure_quotes'].get(k, '')))}”</li>"
                              for k, v in (data.get("figures") or {}).items())
            body = f"<h4>Figures used for scores</h4><ul>{figures or '<li>none found</li>'}</ul><h4>Facts</h4><ul>{facts or '<li>none</li>'}</ul>"
        except Exception as e:
            body = f'<p class="err">{html.escape(str(e))}</p>'
        cols.append(f"<div><h3>{name} <small>{time.time() - start:.0f}s</small></h3>{body}</div>")
    return f'<section><h2>Benefits: {html.escape(company)}</h2><div class="cols">{"".join(cols)}</div></section>'


# --- job scoring (resume + interests vs listing) ---------------------------------------------

# (provider, label, Claude model or None)
MATCH_SIDES = [("openai", "ChatGPT gpt-4o-mini", None), ("claude", "Claude Haiku 5.5", "claude-haiku-5-5"),
               ("claude", "Claude Sonnet 5.5", "claude-sonnet-5-5")]
_FILLER = set("""experience with in of and the a an strong skills skill knowledge ability years year using tools tool
working proven excellent plus or to for on at as hands background understanding familiarity proficiency proficient
demonstrated related relevant specific direct other similar etc including such required preferred systems system
data business analysis analyst work job role level senior minimum least degree software platform platforms""".split())


def misread_missing(missing, resume):
    """Missing items whose key words are in her resume after all (likely misreads)."""
    words = set(re.findall(r"[a-z0-9+#]+", resume.lower()))
    flagged = []
    for item in missing:
        keys = [w for w in re.findall(r"[a-z0-9+#]+", item.lower()) if len(w) >= 3 and w not in _FILLER]
        hits = [w for w in keys if w in words]
        if hits:
            flagged.append((item, hits))
    return flagged


def score_with(side, profile, description):
    provider, _, model = side
    before = os.environ.get("CLAUDE_MATCHING_MODEL")
    if model:
        os.environ["CLAUDE_MATCHING_MODEL"] = model
    start = time.time()
    try:
        with ai.using(provider):
            skills, interests, details = ranking.read_match(profile, description)
        return {"skills": skills, "interests": interests, "details": details, "secs": time.time() - start}
    except Exception as e:
        return {"error": str(e), "secs": time.time() - start}
    finally:
        if model:
            if before is None:
                os.environ.pop("CLAUDE_MATCHING_MODEL", None)
            else:
                os.environ["CLAUDE_MATCHING_MODEL"] = before


def matching_sections(status="interested"):
    profile = db.get_profile()
    if not profile["resume"]:
        sys.exit("Save her resume in the app first (Ranking Interested jobs)")
    jobs = [db.get_job(j["id"]) for j in db.list_jobs(status=status)]
    jobs = [j for j in jobs if len(j.get("description") or "") >= 200]
    results = {}
    for i, job in enumerate(jobs, 1):
        print(f"[scoring {i}/{len(jobs)}] {job['title']}")
        results[job["id"]] = [score_with(s, profile, job["description"]) for s in MATCH_SIDES]

    # Summary: each model's order, how often it flagged something she has, time.
    rows = []
    for k, side in enumerate(MATCH_SIDES):
        ok = [(j, results[j["id"]][k]) for j in jobs if "error" not in results[j["id"]][k]]
        misreads = sum(len(misread_missing(r["details"]["missing"], profile["resume"])) for _, r in ok)
        mixes = {json.dumps(r["details"].get("work_mix"), sort_keys=True) for _, r in ok}
        secs = sum(r["secs"] for j in jobs for r in [results[j["id"]][k]])
        order = sorted(ok, key=lambda jr: -((jr[1]["skills"] or 0) * 5 + (jr[1]["interests"] or 0) * 4))
        ranked = []
        for j, r in order:
            ranked.append("%s <small>(skills %.0f, interests %.0f)</small>"
                          % (html.escape(j["title"][:45]), r["skills"] or 0, r["interests"] or 0))
        rows.append(f"<tr><th>{side[1]}</th><td>{misreads}</td><td>{len(mixes)} of {len(ok)}</td><td>{secs:.0f}s</td>"
                    f"<td>{'<br>'.join(ranked)}</td></tr>")
    summary = f"""<section><h2>Job scoring: summary</h2>
<p>Same resume, interests, listings and instructions for each. <b>Missing items she actually has</b> counts
“missing” qualifications whose key word appears in her resume (likely misreads; fewer is better).
<b>Different work mixes</b>: how many distinct interest splits it gave (more means it read each job, not a template).</p>
<table><tr><th>Model</th><th>Missing items she has</th><th>Different work mixes</th><th>Time</th><th>Order by skills×5 + interests×4</th></tr>
{''.join(rows)}</table></section>"""

    sections = [summary]
    for job in jobs:
        cols = []
        for side, r in zip(MATCH_SIDES, results[job["id"]]):
            if "error" in r:
                cols.append(f"<div><h3>{side[1]}</h3><p class='err'>{html.escape(r['error'])}</p></div>")
                continue
            d = r["details"]
            flagged = dict(misread_missing(d["missing"], profile["resume"]))
            miss = "".join(f"<li>{html.escape(m)}" + (f" <span class='bad'>⚠ resume mentions {', '.join(flagged[m])}</span>" if m in flagged else "")
                           + "</li>" for m in d["missing"])
            lst = lambda items: "".join(f"<li>{html.escape(x)}</li>" for x in items) or "<li class='none'>—</li>"
            cols.append(f"""<div><h3>{side[1]} <small>{r['secs']:.1f}s</small></h3>
<p><b>Skills {r['skills']:.0f}</b>: {html.escape(d['skills_reason'])}</p>
<p><b>Interests {(r['interests'] or 0):.0f}</b>: {html.escape(d['interests_reason'])}</p>
<h4>Meets</h4><ul>{lst(d['matched'])}</ul><h4>Partly</h4><ul>{lst(d['partial'])}</ul>
<h4>Missing</h4><ul>{miss or "<li class='none'>—</li>"}</ul></div>""")
        sections.append(f"""<section><h2>{html.escape(job['title'])} <small>{html.escape(job['company'] or '')}</small></h2>
<div class="cols3">{''.join(cols)}</div>
<details><summary>Listing text</summary><pre>{html.escape(job['description'][:15000])}</pre></details></section>""")
    return sections


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--jobs", type=int, default=None, help="how many saved jobs to compare listing reads on (default 10)")
    ap.add_argument("--benefits", nargs="*", default=[], metavar="COMPANY", help="companies to compare benefits web searches for")
    ap.add_argument("--matching", action="store_true",
                    help="compare scoring her Interested jobs against her resume and interests (ChatGPT, Claude Haiku, Claude Sonnet)")
    args = ap.parse_args()
    missing = [ai.PROVIDERS[p][1] for p, _ in SIDES if not ai.key_set(p)]
    if missing:
        sys.exit(f"Add {' and '.join(missing)} to .env first")
    if args.jobs is None:
        args.jobs = 0 if (args.matching or args.benefits) else 10

    db.init_db()
    sections = matching_sections() if args.matching else []
    jobs = jobs_to_compare(args.jobs) if args.jobs else []
    for i, job in enumerate(jobs, 1):
        print(f"[{i}/{len(jobs)}] {job['title']} ({job['company']})")
        text = re.sub(r"\n{3,}", "\n\n", job["description"].strip())[:importer.MAX_TEXT]
        sections.append(job_section(job, read_listing(text)))
    for company in args.benefits:
        print(f"Benefits: {company}")
        sections.append(benefits_section(company))

    page = f"""<!doctype html><meta charset="utf-8"><title>ChatGPT vs Claude</title>
<style>
body {{ font-family: Segoe UI, sans-serif; margin: 24px; color: #222; background: #f4f5f7; }}
section {{ background: #fff; border-radius: 8px; padding: 14px 18px; margin-bottom: 16px; }}
h2 small, h3 small {{ color: #777; font-weight: normal; font-size: 14px; }}
table {{ border-collapse: collapse; width: 100%; }} th, td {{ text-align: left; padding: 5px 8px; border-bottom: 1px solid #eee; vertical-align: top; }}
th {{ width: 130px; color: #555; font-weight: 600; }} tr.diff {{ background: #fff8e1; }}
.ok {{ color: #1e8e3e; font-size: 12px; }} .bad {{ color: #c0392b; font-size: 12px; font-weight: 600; }} .none {{ color: #aaa; }}
.err {{ color: #c0392b; }} .meta {{ color: #777; font-size: 13px; margin-top: -6px; }}
pre {{ white-space: pre-wrap; font-size: 12px; max-height: 300px; overflow: auto; background: #fafafa; padding: 8px; }}
.cols {{ display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }}
.cols3 {{ display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 16px; font-size: 13px; }}
h4 {{ margin: 8px 0 2px; }} ul {{ margin: 0; padding-left: 18px; }}
</style>
<h1>ChatGPT ({ai.openai_model()}) vs Claude ({ai.claude_model()}; web: {ai.claude_web_model()})</h1>
<p>Rows where the two disagree are highlighted. “✗ not in text” means that value isn't in the listing, so it was guessed;
the app throws those away when filling in missing details.</p>
{''.join(sections)}"""
    with open(REPORT, "w", encoding="utf-8") as f:
        f.write(page)
    print(f"Report: {REPORT}")
    webbrowser.open(REPORT)


if __name__ == "__main__":
    main()

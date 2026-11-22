"""Job Search web UI: run JSearch queries on demand and track the results."""

import os
import subprocess
from datetime import date

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request

# Load .env before anything reads credentials.
load_dotenv()

from jobsearch import benefits, boards, db, geo, jsearch

app = Flask(__name__)
app.config["TEMPLATES_AUTO_RELOAD"] = True
db.init_db()

PORT = int(os.getenv("JOBSEARCH_PORT", "5001"))  # 5000 is the book catalog


def _error(message, code=400):
    return jsonify({"error": message}), code


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/favicon.ico")
def favicon():
    return ("", 204)


@app.route("/api/meta")
def api_meta():
    return jsonify({
        "statuses": db.STATUSES,
        "status_counts": db.status_counts(),
        "date_posted_options": jsearch.DATE_POSTED_OPTIONS,
        "employment_types": jsearch.EMPLOYMENT_TYPES,
        "max_pages": jsearch.MAX_PAGES,
        "ats_labels": boards.ATS_LABELS,
        "ats_token_hints": boards.ATS_TOKEN_HINTS,
        "priorities": db.PRIORITIES,
        "company_defaults": db.company_defaults(),
        "default_keywords": db.get_default_keywords(),
        "area": dict(zip(("home", "radius_miles"), db.get_area_settings())),
        "pages_used_this_month": db.pages_used_this_month(),
        "has_api_key": bool(os.getenv("JSEARCH_API_KEY")),
        "has_openai_key": bool(os.getenv("OPENAI_API_KEY")),
        "benefit_scales": benefits.SCALES,
        "benefit_topics": list(benefits.TOPICS.items()),  # a list keeps the display order
        "ai_lookups_this_month": db.ai_calls_this_month(),
    })


# --- searches ---------------------------------------------------------------

@app.route("/api/searches")
def api_list_searches():
    return jsonify(db.list_searches())


@app.route("/api/searches", methods=["POST"])
def api_add_search():
    data = request.get_json() or {}
    if not (data.get("query") or "").strip():
        return _error("Search text is required")
    data["name"] = (data.get("name") or "").strip() or jsearch.build_query(data["query"], data.get("location"))
    search_id = db.add_search(data)
    return jsonify(db.get_search(search_id)), 201


@app.route("/api/searches/<int:search_id>", methods=["DELETE"])
def api_delete_search(search_id):
    db.delete_search(search_id)
    return jsonify({"ok": True})


def _home_area(extra_location=None):
    """Jobs within the set radius of home (on-site/hybrid). A one-off search somewhere
    else ("Chicago, IL") also keeps jobs within the radius of that place."""
    home_text, radius = db.get_area_settings()
    home = (geo.parse_points(home_text) or [None])[0]
    if home is None:
        return None  # home town not recognized: don't filter by location
    extra = (geo.parse_points(extra_location) or [None])[0] if extra_location else None
    return boards.Area(home, radius, extra_centers=[extra])


def _search_area(location):
    return _home_area(location)


def _run(params, search_id=None):
    try:
        results, pages = jsearch.search(
            query=params["query"],
            location=params.get("location"),
            remote_only=bool(params.get("remote_only")),
            date_posted=params.get("date_posted") or "all",
            employment_types=params.get("employment_types"),
            num_pages=params.get("num_pages") or 1,
        )
    except jsearch.JSearchError as e:
        return _error(str(e), 502)
    db.record_api_call(pages, jsearch.build_query(params["query"], params.get("location")))
    area = _search_area(params.get("location"))
    kept = [j for j in results if boards.in_area(j, area)]
    new_count, seen_count = db.save_results(kept, search_id=search_id)
    return jsonify({
        "found": len(kept),
        "outside_area": len(results) - len(kept),
        "new": new_count,
        "already_saved": seen_count,
        "pages_used_this_month": db.pages_used_this_month(),
    })


@app.route("/api/searches/<int:search_id>/run", methods=["POST"])
def api_run_search(search_id):
    search = db.get_search(search_id)
    if not search:
        return _error("Saved search not found", 404)
    return _run(search, search_id=search_id)


@app.route("/api/search", methods=["POST"])
def api_quick_search():
    """Run a one-off search without saving it."""
    data = request.get_json() or {}
    if not (data.get("query") or "").strip():
        return _error("Search text is required")
    return _run(data)


# --- companies --------------------------------------------------------------

@app.route("/api/settings/area", methods=["PUT"])
def api_set_area():
    data = request.get_json() or {}
    home = (data.get("home") or "").strip()
    if not geo.parse_points(home):
        return _error(f"Couldn't find \"{home}\". Use a US town and state, like Hanover, OH")
    try:
        radius = float(data.get("radius_miles"))
    except (TypeError, ValueError):
        return _error("Distance must be a number of miles")
    if not 5 <= radius <= 300:
        return _error("Distance should be between 5 and 300 miles")
    db.set_area_settings(home, radius)
    return jsonify({"home": home, "radius_miles": radius})


@app.route("/api/settings/default_keywords", methods=["PUT"])
def api_set_default_keywords():
    text = ((request.get_json() or {}).get("default_keywords") or "").strip()
    if not text:
        return _error("Enter at least one job title (e.g. business analyst)")
    return jsonify({"default_keywords": db.set_default_keywords(text)})


def _company_fields(data):
    """Clean form input; fill in ats/ats_token from the careers URL when not given."""
    fields = {}
    for k in db.COMPANY_FIELDS:
        if k not in data:
            continue
        value = data[k]
        if k in ("priority", "rating"):
            try:
                fields[k] = None if value in (None, "") else (int(value) if k == "priority" else float(value))
            except (TypeError, ValueError):
                raise ValueError(f"{k.capitalize()} must be a number")
        else:
            fields[k] = (str(value) if value is not None else "").strip() or None
    if "careers_url" in fields and not fields.get("ats"):
        fields["ats"], token = boards.detect_ats(fields["careers_url"])
        fields["ats_token"] = fields.get("ats_token") or token
    if fields.get("ats") and fields["ats"] not in boards.ATS_TYPES:
        raise ValueError(f"Unknown job board type: {fields['ats']}")
    if fields.get("ats") not in (None, "search", "amazon") and not fields.get("ats_token"):
        raise ValueError(f"{boards.ATS_LABELS[fields['ats']]} needs a board ID: {boards.ATS_TOKEN_HINTS[fields['ats']]}")
    return fields


@app.route("/api/companies")
def api_list_companies():
    scores = db.benefits_by_key()
    companies = db.list_companies()
    for c in companies:
        b = scores.get(benefits.company_key(c["name"])) or {}
        c["benefits"] = {k: b.get(k) for k in ("retirement_score", "pto_score", "commute_score", "bonus_score")}
    return jsonify(companies)


@app.route("/api/companies", methods=["POST"])
def api_add_company():
    data = request.get_json() or {}
    if not (data.get("name") or "").strip():
        return _error("Company name is required")
    data.setdefault("careers_url", "")
    try:
        fields = _company_fields(data)
        fields.setdefault("ats", "search")
        company_id = db.add_company(fields)
    except ValueError as e:
        return _error(str(e))
    return jsonify(db.get_company(company_id)), 201


@app.route("/api/companies/<int:company_id>", methods=["PATCH"])
def api_update_company(company_id):
    if not db.get_company(company_id):
        return _error("Company not found", 404)
    try:
        db.update_company(company_id, _company_fields(request.get_json() or {}))
    except ValueError as e:
        return _error(str(e))
    company = db.get_company(company_id)
    # A rename can make job-board listings line up with this company ("Huntington Bank" -> "Huntington").
    db.link_jobs_to_company(company_id, benefits.company_key(company["name"]))
    return jsonify(company)


@app.route("/api/companies/<int:company_id>", methods=["DELETE"])
def api_delete_company(company_id):
    db.delete_company(company_id)
    return jsonify({"ok": True})


@app.route("/api/companies/<int:company_id>/check", methods=["POST"])
def api_check_company(company_id):
    company = db.get_company(company_id)
    if not company:
        return _error("Company not found", 404)
    try:
        jobs, pages = boards.check_company(company, default_keywords=db.get_default_keywords(),
                                           area=_home_area())
    except (boards.BoardError, jsearch.JSearchError) as e:
        return _error(str(e), 502)
    if pages:
        db.record_api_call(pages, f"company: {company['name']}")
    new_count, seen_count = db.save_results(jobs, company_id=company_id)
    return jsonify({
        "found": len(jobs),
        "new": new_count,
        "already_saved": seen_count,
        "pages_used_this_month": db.pages_used_this_month(),
    })


# --- jobs -------------------------------------------------------------------

@app.route("/api/jobs")
def api_list_jobs():
    jobs = db.list_jobs(
        status=request.args.get("status") or None,
        search_id=request.args.get("search_id", type=int),
        company_id=request.args.get("company_id", type=int),
        text=(request.args.get("q") or "").strip() or None,
        remote_only=request.args.get("remote") == "1",
        hide_not_fit=request.args.get("hide_not_fit") == "1",
        sort=request.args.get("sort") or "found",
        work_mode=request.args.get("work_mode") or None,
    )
    # Flag jobs whose employer is already a saved company (by name, so job-board results count too).
    saved = {benefits.company_key(c["name"]): c["id"] for c in db.list_companies()}
    for j in jobs:
        j["saved_company_id"] = j.get("company_id") or saved.get(benefits.company_key(j.get("company") or ""))
    return jsonify(jobs)


@app.route("/api/jobs/<int:job_id>/save_company", methods=["POST"])
def api_save_job_company(job_id):
    """Add a job's employer to the saved companies, guessing its job board from the listing link."""
    job = db.get_job(job_id)
    if not job:
        return _error("Job not found", 404)
    name = (job.get("company") or "").strip()
    if not name:
        return _error("This listing doesn't name a company")
    key = benefits.company_key(name)
    if any(benefits.company_key(c["name"]) == key for c in db.list_companies()):
        return _error(f"{name} is already in your company list")

    # If the listing links straight to the employer's job board (Workday, Greenhouse...), use it;
    # job-board links (LinkedIn, Indeed...) aren't careers sites, so those start on JSearch.
    ats, token = boards.detect_ats(job.get("apply_link"))
    defaults = db.company_defaults()
    fields = {"name": name, "ats": ats, "ats_token": token,
              "careers_url": job.get("apply_link") if ats != "search" else None,
              "keywords": defaults.get("keywords"), "locations": defaults.get("locations"),
              "notes": "Added from a job search listing" + (f" ({job['publisher']})" if job.get("publisher") else "")}
    try:
        company_id = db.add_company(fields)
    except ValueError as e:
        return _error(str(e))
    db.link_jobs_to_company(company_id, key)
    return jsonify(db.get_company(company_id)), 201


@app.route("/api/jobs/<int:job_id>")
def api_get_job(job_id):
    job = db.get_job(job_id)
    if not job:
        return _error("Job not found", 404)
    job["benefit_tags"] = benefits.tags_from_text(job.get("description"))
    job["company_benefits"] = db.get_benefits(job["company"]) if job.get("company") else None
    return jsonify(job)


# --- benefits -----------------------------------------------------------------

@app.route("/api/benefits")
def api_get_benefits():
    company = (request.args.get("company") or "").strip()
    if not company:
        return _error("Company is required")
    return jsonify(db.get_benefits(company) or {"company_name": company, "lookup": None})


@app.route("/api/benefits", methods=["PUT"])
def api_save_benefits():
    data = request.get_json() or {}
    company = (data.get("company") or "").strip()
    if not company:
        return _error("Company is required")
    try:
        return jsonify(db.save_benefits(company, data))
    except ValueError as e:
        return _error(str(e))


@app.route("/api/benefits/lookup", methods=["POST"])
def api_lookup_benefits():
    """Find a company's benefits from a job listing or a saved company (ChatGPT-assisted)."""
    data = request.get_json() or {}
    company = (data.get("company") or "").strip()
    job = db.get_job(data["job_id"]) if data.get("job_id") else None
    if job and not company:
        company = job.get("company") or ""
    if not company:
        return _error("Company is required")

    saved = db.get_benefits(company) or {}
    benefits_url = (data.get("benefits_url") or "").strip() or saved.get("benefits_url")
    start_urls = []
    if job and job.get("apply_link"):
        start_urls.append(job["apply_link"])
    for c in db.list_companies():
        if benefits.company_key(c["name"]) == benefits.company_key(company) and c.get("careers_url"):
            start_urls.append(c["careers_url"])
    try:
        result = benefits.lookup(company, start_urls=start_urls, benefits_url=benefits_url,
                                 posting_text=job.get("description") if job else None,
                                 use_web=data.get("use_web", True),
                                 work_mode=job.get("work_mode") if job else "company")
    except benefits.BenefitsError as e:
        return _error(str(e), 502)
    except Exception as e:  # OpenAI/network errors
        return _error(f"Benefits lookup failed: {e}", 502)
    db.record_ai_call(result["method"], company)
    fields = {"benefits_url": benefits_url} if benefits_url else {}
    # Calculated scores fill only empty boxes; scores she already set are left alone.
    filled = []
    for key, score in result["scores"].items():
        if saved.get(f"{key}_score") is None:
            fields[f"{key}_score"] = score
            filled.append(key)
    record = db.save_benefits(company, fields, lookup=result)
    record["filled"] = filled
    record["ai_lookups_this_month"] = db.ai_calls_this_month()
    return jsonify(record)


@app.route("/api/jobs/<int:job_id>", methods=["PATCH"])
def api_update_job(job_id):
    if not db.get_job(job_id):
        return _error("Job not found", 404)
    try:
        db.update_job(job_id, request.get_json() or {})
    except ValueError as e:
        return _error(str(e))
    return jsonify(db.get_job(job_id))


# --- backup -----------------------------------------------------------------

def _git(*args):
    return subprocess.run(["git", *args], cwd=db.REPO_DIR, capture_output=True, text=True, timeout=120)


@app.route("/api/backup", methods=["POST"])
def api_backup():
    """Commit jobs.db and push it to GitHub."""
    rel_path = os.path.relpath(db.db_path(), db.REPO_DIR)
    if rel_path.startswith(".."):
        return _error("Database is outside the repo folder; nothing to back up")

    _git("add", rel_path)
    if _git("diff", "--cached", "--quiet", "--", rel_path).returncode == 0:
        committed = False
    else:
        commit = _git("commit", "-m", f"Back up job search data ({date.today().isoformat()})", "--", rel_path)
        if commit.returncode != 0:
            return _error(f"git commit failed: {commit.stderr or commit.stdout}", 500)
        committed = True

    push = _git("push")
    if push.returncode != 0:
        return _error(f"git push failed: {push.stderr or push.stdout}", 500)
    return jsonify({"ok": True, "committed": committed})


if __name__ == "__main__":
    # Auto-restart on Python file changes so a page refresh picks them up.
    # The reloader is separate from debug mode, which stays off (its console is an RCE vector).
    app.run(debug=False, use_reloader=True, port=PORT)

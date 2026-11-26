"""Job Search web UI: run JSearch queries on demand and track the results."""

import os
from datetime import date

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request

# Load .env before anything reads credentials.
load_dotenv()

from jobsearch import (ai, benefits, boards, db, geo, importer, jobhunt, jsearch, profile_files, ranking,
                       ranking_files, skills)
from jobsearch.pay import format_pay

app = Flask(__name__)
app.config["TEMPLATES_AUTO_RELOAD"] = True
db.init_db()


def fill_profile_from_files(replace=False):
    """Copy her resume / interests / weights from profile/ into her profile: only the empty ones,
    or all of them with replace=True ("Load from profile files"). Returns the fields copied."""
    try:
        found = profile_files.read_all()
    except ValueError:
        return []
    current = db.get_profile()
    copy = {k: v for k, v in found.items() if k in db.PROFILE_FIELDS and (replace or not current.get(k))}
    if copy:
        db.set_profile(**copy)
    return sorted(copy)


fill_profile_from_files()  # a fresh install starts with her resume and interests filled in

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
        "ai_name": ai.name("listings"),
        "has_benefits_ai": ai.has_key("benefits"),
        "benefits_ai_name": ai.name("benefits"),
        "benefits_ai_key": ai.key_name("benefits"),
        "matching_ai_name": ai.name("matching"),
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


@app.route("/api/searches/<int:search_id>", methods=["PATCH"])
def api_update_search(search_id):
    if not db.get_search(search_id):
        return _error("Saved search not found", 404)
    data = request.get_json() or {}
    if not (data.get("query") or "").strip():
        return _error("Search text is required")
    data["name"] = (data.get("name") or "").strip() or jsearch.build_query(data["query"], data.get("location"))
    db.update_search(search_id, data)
    return jsonify(db.get_search(search_id))


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
        interest=request.args.get("interest") or None,
        skill=request.args.get("skill") or None,
        no_description=request.args.get("no_description") == "1",
    )
    # Flag jobs whose employer is already a saved company (by name, so job-board results count too).
    saved = {benefits.company_key(c["name"]): c["id"] for c in db.list_companies()}
    for j in jobs:
        j["saved_company_id"] = j.get("company_id") or saved.get(benefits.company_key(j.get("company") or ""))
    # Rank numbers (every job except Applied / Rejected / Not a fit). Calculated from saved scores only:
    # the AI never runs here, only when she clicks Rank Jobs.
    if any(j["status"] not in ranking.SKIP_STATUSES for j in jobs):
        ranked = ranking.rank()["jobs"]
        for j in jobs:
            r = ranked.get(j["id"])
            if r:
                j["rank"] = {"overall": r["overall_rank"], "qualify": r["parts"]["qualify"]["rank"],
                             "interest": r["parts"]["interest"]["rank"], "salary": r["parts"]["salary"]["score"]}
    if request.args.get("sort") == "rank":
        jobs.sort(key=lambda j: (j.get("rank") is None, (j.get("rank") or {}).get("overall") or 0))
    return jsonify(jobs)


# --- ranking her jobs -----------------------------------------------------------------------

@app.route("/api/profile", methods=["GET", "POST"])
def api_profile():
    """Her resume and interests (kept in her own database, used for match scores)."""
    if request.method == "POST":
        data = request.get_json() or {}
        db.set_profile(**{k: data.get(k) for k in db.PROFILE_FIELDS})
    else:
        fill_profile_from_files()
    return jsonify({**db.get_profile(), "weight_defaults": {
        **{f"{k}_weight": v for k, v in ranking.MIX_VALUES.items()},
        **{f"weight_{n}": w for n, _, w in ranking.CRITERIA}}})


@app.route("/api/profile/load_files", methods=["POST"])
def api_profile_load_files():
    """Replace her resume, interests and weights with what's in the profile/ folder."""
    copied = fill_profile_from_files(replace=True)
    if not copied:
        return _error("No profile files found (profile/Resume.docx and profile/interests.txt)")
    return jsonify({**db.get_profile(), "copied": copied})


@app.route("/api/profile/resume_file", methods=["POST"])
def api_profile_resume_file():
    """Pull the text out of an uploaded resume (.docx or .txt) so it can be checked before saving."""
    upload = request.files.get("file")
    if not upload or not upload.filename:
        return _error("Choose the resume file")
    try:
        text = ranking_files.resume_text(upload.filename, upload.read())
    except ValueError as e:
        return _error(str(e))
    return jsonify({"text": text})


@app.route("/api/ranking")
def api_ranking():
    """Rank numbers and ratings for every job except Applied / Rejected / Not a fit (no AI calls)."""
    r = ranking.rank()
    return jsonify({k: r[k] for k in ("count", "profile_ready", "needs_ai", "weights")})


@app.route("/api/ranking/score", methods=["POST"])
def api_ranking_score():
    """The Rank Jobs button: have the AI read a batch of jobs that need it (the page repeats this
    until none are left). Only runs when she clicks; nothing scores automatically."""
    try:
        result = ranking.score_pending()
    except ai.AIError as e:
        return _error(str(e))
    return jsonify(result)


@app.route("/api/jobs/<int:job_id>/match", methods=["GET", "PATCH"])
def api_job_match(job_id):
    """One job's ranking breakdown; PATCH sets her own Qualify / Interest scores
    ({"qualify": 80, "interest": ""}; blank goes back to the AI's score)."""
    if not db.get_job(job_id):
        return _error("Job not found", 404)
    if request.method == "PATCH":
        data = request.get_json() or {}
        names = {"qualify": "skills", "interest": "interests"}
        try:
            db.set_match_overrides(job_id, {names[k]: v for k, v in data.items() if k in names})
        except ValueError as e:
            return _error(str(e))
    r = ranking.rank()
    job = r["jobs"].get(job_id)
    order = [name for name, _, _ in ranking.CRITERIA]
    return jsonify({**job, "count": r["count"], "weights_order": order} if job else None)


@app.route("/api/jobs/<int:job_id>/reload", methods=["POST"])
def api_reload_job(job_id):
    """Re-read one job's listing page to fill in its description and details."""
    job = db.get_job(job_id)
    if not job:
        return _error("Job not found", 404)
    if not job.get("apply_link"):
        return _error("This job has no listing link to reload from")
    try:
        fresh = importer.read_job(job["apply_link"])
    except importer.ImportFailed as e:
        return _error(str(e))
    except Exception as e:
        return _error(f"Couldn't read the listing: {e}", 502)
    # The site's own data comes first; ChatGPT then reads the description for anything still missing.
    if not fresh.get("job_description"):
        fresh["job_description"] = job.get("description")
    try:
        by_ai = importer.fill_gaps(fresh)
    except Exception:
        by_ai = []  # ChatGPT unavailable: keep what the site gave
    changed = db.refresh_job(job_id, fresh)
    return jsonify({"changed": changed, "filled_by_chatgpt": by_ai, "job": db.get_job(job_id)})


@app.route("/api/skills")
def api_skills():
    """How often each certification and tool shows up across all saved jobs."""
    return jsonify(db.skills_summary())


@app.route("/api/skills/analyze", methods=["POST"])
def api_skills_analyze():
    """Read a batch of not-yet-read listings for certifications and tools (the page calls this
    repeatedly until nothing is left, showing progress)."""
    limit = max(1, min(int((request.get_json(silent=True) or {}).get("limit") or 8), 20))
    try:
        result = skills.analyze(limit=limit)
    except ai.AIError as e:
        return _error(str(e))
    return jsonify({**result, "summary": db.skills_summary()})


@app.route("/api/jobs/import/spreadsheet", methods=["POST"])
def api_import_spreadsheet():
    """Import her Job Hunt spreadsheet (.xlsx). The file is read here and not kept."""
    upload = request.files.get("file")
    if not upload or not upload.filename:
        return _error("Choose the spreadsheet file to import")
    if not upload.filename.lower().endswith(".xlsx"):
        return _error("Save the spreadsheet as an Excel .xlsx file first")
    try:
        jobs = jobhunt.parse(upload.read())
    except jobhunt.SheetError as e:
        return _error(str(e))
    return jsonify({**jobhunt.import_jobs(jobs), "rows": len(jobs)})


@app.route("/api/jobs/import/preview", methods=["POST"])
def api_import_preview():
    """Read one job from its listing link (or pasted text) so she can check it before saving."""
    data = request.get_json() or {}
    try:
        job = importer.read_job(data.get("url"), text=data.get("text"), companies=db.list_companies())
    except importer.ImportFailed as e:
        return _error(str(e))
    except Exception as e:  # network / ChatGPT trouble
        return _error(f"Couldn't read that listing: {e}", 502)
    v = db._job_values(job)
    pay = (format_pay(v["salary_min"], v["salary_max"], v["salary_period"])
           if v["salary_min"] or v["salary_max"] else v["salary_text"])
    return jsonify({"job": job, "duplicates": _duplicates(job), "preview": {k: v[k] for k in (
        "title", "company", "location", "work_mode", "employment_type", "req_number",
        "posted_at", "closes_at", "publisher", "apply_link")} | {"salary_text": pay, "description_chars": len(v["description"] or "")}})


def _duplicates(job):
    """Saved jobs this one looks like: {"blocking": [...], "rejected": [...]}. A copy of a Rejected
    job may be a relisting she wants to apply to again; a copy of anything else is a duplicate."""
    matches = db.find_existing(job)
    return {"blocking": [m for m in matches if m["status"] != "rejected"],
            "rejected": [m for m in matches if m["status"] == "rejected"]}


@app.route("/api/jobs/import/save", methods=["POST"])
def api_import_save():
    """Save a previewed job, with any title/company/location corrections she made.
    Jobs added by hand skip the title and distance filters: she chose this one.

    A job she already has (any status but Rejected) isn't saved again (409). A copy of a Rejected
    job is saved only when she confirms it's a relisting ("save_relisted": true), as a separate
    New job so the rejected one keeps its history."""
    data = request.get_json() or {}
    job = data.get("job") or {}
    if not job.get("job_id"):
        return _error("Nothing to save; read the listing first")
    if (data.get("interest_level") or None) not in (None, *db.INTEREST_LEVELS):
        return _error(f"Unknown interest level: {data['interest_level']}")
    edits = data.get("edits") or {}
    for field, key in (("title", "job_title"), ("company", "employer_name"), ("location", "job_location")):
        if (edits.get(field) or "").strip():
            job[key] = edits[field].strip()
    dups = _duplicates(job)
    if dups["blocking"]:
        d = dups["blocking"][0]
        status = STATUS_NAMES.get(d["status"], d["status"])
        return jsonify({"error": f"Not saved: this job is already in your list as “{d['title']}” at "
                                 f"{d['company'] or 'the same company'} ({status}).",
                        "duplicate": d}), 409
    relisted = bool(dups["rejected"])
    if relisted and not data.get("save_relisted"):
        return jsonify({"error": "This looks like a job you were rejected for. Confirm to save it as a new job.",
                        "needs_confirm": True, "rejected": dups["rejected"]}), 409
    if relisted:
        # A separate card: its own id, so it isn't merged back into the rejected one.
        job["job_id"] = f"{job['job_id']}#relisted-{db.now_iso()}"
    company_id = job.pop("company_id", None)
    if company_id is None and job.get("employer_name"):
        key = benefits.company_key(job["employer_name"])
        company_id = next((c["id"] for c in db.list_companies() if benefits.company_key(c["name"]) == key), None)
    new, seen = db.save_results([job], company_id=company_id, force_new=relisted)
    saved = db.find_job_id(job["job_id"])
    db.mark_added_by_hand(saved, found_where=data.get("found_where"),
                          interest_level=data.get("interest_level") or None, is_new=bool(new))
    return jsonify({"id": saved, "new": bool(new), "merged": bool(seen), "relisted": relisted}), 201


STATUS_NAMES = {"new": "New", "interested": "Interested", "applied": "Applied", "interviewing": "Interviewing",
                "offer": "Offer", "rejected": "Rejected", "not_a_fit": "Not a fit"}


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
    except Exception as e:  # AI service / network errors
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

def backup_folder():
    """Where Back up saves copies: BACKUP_DIR from .env, else "Job Search backups" in her OneDrive
    (never GitHub: the repo is public and the database holds her own records)."""
    custom = (os.getenv("BACKUP_DIR") or "").strip()
    if custom:
        return custom
    onedrive = os.getenv("OneDrive") or os.getenv("OneDriveConsumer") or os.getenv("OneDriveCommercial")
    return os.path.join(onedrive, "Job Search backups") if onedrive else None


@app.route("/api/backup", methods=["POST"])
def api_backup():
    """Save a dated copy of the database to OneDrive (one file per day; a second backup the
    same day replaces that day's copy). OneDrive then syncs it off the computer."""
    folder = backup_folder()
    if not folder:
        return _error("OneDrive isn't set up on this computer. Add BACKUP_DIR=<folder> to .env "
                      "to choose where backups go.")
    try:
        os.makedirs(folder, exist_ok=True)
        target = os.path.join(folder, f"jobs-{date.today().isoformat()}.db")
        db.backup_to(target)
    except OSError as e:
        return _error(f"Couldn't save the backup: {e}", 500)
    return jsonify({"ok": True, "path": target, "folder": folder})


if __name__ == "__main__":
    # Auto-restart on Python file changes so a page refresh picks them up.
    # The reloader is separate from debug mode, which stays off (its console is an RCE vector).
    app.run(debug=False, use_reloader=True, port=PORT)

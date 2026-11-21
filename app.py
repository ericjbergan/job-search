"""Job Search web UI: run JSearch queries on demand and track the results."""

import os
import subprocess
from datetime import date

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request

# Load .env before anything reads credentials.
load_dotenv()

from jobsearch import boards, db, jsearch

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
        "pages_used_this_month": db.pages_used_this_month(),
        "has_api_key": bool(os.getenv("JSEARCH_API_KEY")),
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
    new_count, seen_count = db.save_results(results, search_id=search_id)
    return jsonify({
        "found": len(results),
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

def _company_fields(data):
    """Clean form input; fill in ats/ats_token from the careers URL when not given."""
    fields = {k: (data.get(k) or "").strip() or None for k in db.COMPANY_FIELDS if k in data}
    if "careers_url" in fields and not fields.get("ats"):
        fields["ats"], token = boards.detect_ats(fields["careers_url"])
        fields["ats_token"] = fields.get("ats_token") or token
    if fields.get("ats") and fields["ats"] not in boards.ATS_TYPES:
        raise ValueError(f"Unknown job board type: {fields['ats']}")
    if fields.get("ats") in ("greenhouse", "lever", "ashby") and not fields.get("ats_token"):
        raise ValueError("That job board type needs a board name (the part after the domain in the careers URL)")
    return fields


@app.route("/api/companies")
def api_list_companies():
    return jsonify(db.list_companies())


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
    return jsonify(db.get_company(company_id))


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
        jobs, pages = boards.check_company(company)
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
    return jsonify(db.list_jobs(
        status=request.args.get("status") or None,
        search_id=request.args.get("search_id", type=int),
        company_id=request.args.get("company_id", type=int),
        text=(request.args.get("q") or "").strip() or None,
        remote_only=request.args.get("remote") == "1",
    ))


@app.route("/api/jobs/<int:job_id>")
def api_get_job(job_id):
    job = db.get_job(job_id)
    if not job:
        return _error("Job not found", 404)
    return jsonify(job)


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

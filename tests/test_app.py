"""Tests for storage, company boards, and the Flask API. Run: python -m unittest

No network: JSearch and job-board calls are replaced with fixtures.
"""

import io
import json
import shutil
import sqlite3
import subprocess
import os
import tempfile
import unittest
import zipfile
from unittest import mock

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "jsearch_sample.json")

# Point the app at a throwaway database before importing it.
_tmp = tempfile.TemporaryDirectory()
os.environ["JOBSEARCH_DB"] = os.path.join(_tmp.name, "test.db")
os.environ.setdefault("JSEARCH_API_KEY", "test-key")

import app as app_module  # noqa: E402
from jobsearch import boards, db  # noqa: E402


def sample_results():
    with open(FIXTURE, encoding="utf-8") as f:
        return json.load(f)["data"]


def board_job(n, title, location="New York, NY", remote=False):
    return {
        "job_id": f"greenhouse:acme:{n}", "job_title": title, "employer_name": "Acme",
        "job_location": location, "job_is_remote": remote, "job_publisher": "Greenhouse",
        "job_apply_link": f"https://example.com/{n}", "job_description": "desc",
    }


class FreshDbTest(unittest.TestCase):
    def setUp(self):
        if os.path.exists(os.environ["JOBSEARCH_DB"]):
            os.remove(os.environ["JOBSEARCH_DB"])
        db.init_db(seed_path=None)
        self.client = app_module.app.test_client()


class StorageTests(FreshDbTest):
    def test_rerun_dedupes_and_keeps_tracking(self):
        results = sample_results()
        self.assertEqual(db.save_results(results), (len(results), 0))
        job = db.list_jobs()[0]
        db.update_job(job["id"], {"status": "applied", "notes": "sent resume"})

        self.assertEqual(db.save_results(results), (0, len(results)))
        again = db.get_job(job["id"])
        self.assertEqual(again["status"], "applied")
        self.assertEqual(again["notes"], "sent resume")

    def test_bad_status_rejected(self):
        db.save_results(sample_results()[:1])
        with self.assertRaises(ValueError):
            db.update_job(db.list_jobs()[0]["id"], {"status": "bogus"})


class BoardTests(unittest.TestCase):
    def test_detect_ats(self):
        self.assertEqual(boards.detect_ats("https://boards.greenhouse.io/stripe"), ("greenhouse", "stripe"))
        self.assertEqual(boards.detect_ats("job-boards.greenhouse.io/stripe/jobs/123"), ("greenhouse", "stripe"))
        self.assertEqual(boards.detect_ats("https://jobs.lever.co/spotify"), ("lever", "spotify"))
        self.assertEqual(boards.detect_ats("https://jobs.ashbyhq.com/ramp"), ("ashby", "ramp"))
        self.assertEqual(boards.detect_ats("https://careers.jpmorgan.com"), ("search", None))
        self.assertEqual(boards.detect_ats(""), ("search", None))

    def test_keyword_and_location_filter(self):
        kw, loc = ["java", "backend"], ["chicago"]
        self.assertTrue(boards.matches(board_job(1, "Senior Java Engineer", "Chicago, IL"), kw, loc))
        self.assertTrue(boards.matches(board_job(2, "Backend Engineer", "Remote", remote=True), kw, loc))
        self.assertFalse(boards.matches(board_job(3, "Java Engineer", "Austin, TX"), kw, loc))
        self.assertFalse(boards.matches(board_job(4, "Sales Lead", "Chicago, IL"), kw, loc))
        self.assertTrue(boards.matches(board_job(5, "Anything"), [], []))

    def test_html_to_text(self):
        self.assertEqual(boards.html_to_text("&lt;p&gt;Hi &amp;amp; bye&lt;/p&gt;&lt;ul&gt;&lt;li&gt;one&lt;/li&gt;&lt;/ul&gt;"),
                         "Hi & bye\n\n• one")

    def test_jsearch_fallback_keeps_only_that_employer(self):
        results = [
            {"job_id": "a", "job_title": "Java Dev", "employer_name": "JPMorgan Chase & Co."},
            {"job_id": "b", "job_title": "Java Dev", "employer_name": "Some Staffing Firm"},
            {"job_id": "c", "job_title": "Java Dev", "employer_name": None},
        ]
        with mock.patch("jobsearch.jsearch.search", return_value=(results, 1)):
            jobs, pages = boards.check_company({"name": "JPMorgan Chase", "ats": "search", "keywords": "java"})
        self.assertEqual([j["job_id"] for j in jobs], ["a"])
        self.assertEqual(pages, 1)


class ApiTests(FreshDbTest):
    def test_saved_search_run(self):
        r = self.client.post("/api/searches", json={"query": "java developer", "remote_only": True})
        self.assertEqual(r.status_code, 201)
        sid = r.get_json()["id"]
        with mock.patch("jobsearch.jsearch.search", return_value=(sample_results(), 1)):
            r = self.client.post(f"/api/searches/{sid}/run")
        body = r.get_json()
        self.assertEqual(body["new"], 10)
        self.assertEqual(body["pages_used_this_month"], 1)
        self.assertEqual(len(self.client.get(f"/api/jobs?search_id={sid}").get_json()), 10)

    def test_company_add_detects_board_and_check_saves(self):
        r = self.client.post("/api/companies", json={
            "name": "Acme", "careers_url": "https://boards.greenhouse.io/acme", "keywords": "java"})
        self.assertEqual(r.status_code, 201, r.get_json())
        company = r.get_json()
        self.assertEqual((company["ats"], company["ats_token"]), ("greenhouse", "acme"))

        dup = self.client.post("/api/companies", json={"name": "acme"})
        self.assertEqual(dup.status_code, 400)

        feed = [board_job(1, "Java Developer", "Columbus, OH"), board_job(2, "Office Manager", "Columbus, OH")]
        with mock.patch("jobsearch.boards.fetch_greenhouse", return_value=feed):
            r = self.client.post(f"/api/companies/{company['id']}/check")
        body = r.get_json()
        self.assertEqual((body["found"], body["new"]), (1, 1))
        # Board feeds are free, so no quota is recorded.
        self.assertEqual(body["pages_used_this_month"], 0)
        jobs = self.client.get(f"/api/jobs?company_id={company['id']}").get_json()
        self.assertEqual([j["title"] for j in jobs], ["Java Developer"])

    def test_company_without_url_uses_jsearch(self):
        r = self.client.post("/api/companies", json={"name": "JPMorgan Chase"})
        self.assertEqual(r.get_json()["ats"], "search")

    def test_update_job_status(self):
        db.save_results(sample_results()[:1])
        jid = db.list_jobs()[0]["id"]
        r = self.client.patch(f"/api/jobs/{jid}", json={"status": "interested"})
        self.assertEqual(r.get_json()["status"], "interested")
        self.assertEqual(self.client.patch(f"/api/jobs/{jid}", json={"status": "nope"}).status_code, 400)


if __name__ == "__main__":
    unittest.main()


class BackupTests(FreshDbTest):
    """Back up saves a dated copy of the database to OneDrive (never GitHub: the repo is public)."""

    def test_backup_copies_database_to_onedrive(self):
        db.save_results(sample_results()[:2])
        with tempfile.TemporaryDirectory() as onedrive:
            with mock.patch.dict(os.environ, {"OneDrive": onedrive, "BACKUP_DIR": ""}):
                first = self.client.post("/api/backup").get_json()
                again = self.client.post("/api/backup").get_json()   # same day: replaces that day's copy
            folder = os.path.join(onedrive, "Job Search backups")
            self.assertTrue(first["ok"])
            self.assertEqual(os.path.dirname(first["path"]), folder)
            self.assertEqual(first["path"], again["path"])
            self.assertEqual(len(os.listdir(folder)), 1)
            copy = sqlite3.connect(first["path"])
            try:
                self.assertEqual(copy.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 2)
            finally:
                copy.close()

    def test_backup_dir_setting_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "mine")
            with mock.patch.dict(os.environ, {"BACKUP_DIR": target, "OneDrive": tmp}):
                r = self.client.post("/api/backup").get_json()
            self.assertEqual(os.path.dirname(r["path"]), target)

    def test_no_onedrive_is_a_clear_error(self):
        env = {k: "" for k in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial", "BACKUP_DIR")}
        with mock.patch.dict(os.environ, env):
            r = self.client.post("/api/backup")
        self.assertEqual(r.status_code, 400)
        self.assertIn("BACKUP_DIR", r.get_json()["error"])


class SeedAndDetectTests(FreshDbTest):
    def test_seed_loads_once_and_not_after_deletes(self):
        os.remove(os.environ["JOBSEARCH_DB"])
        db.init_db()  # real seed file
        companies = db.list_companies()
        self.assertGreater(len(companies), 50)
        self.assertEqual(len(db.list_searches()), 2)
        osu = next(c for c in companies if c["name"] == "OSU")
        self.assertEqual((osu["ats"], osu["priority"], osu["rating"]), ("workday", 2, 4.1))
        self.assertIsNone(osu["keywords"])  # extras only; "business analyst" is the shared default
        self.assertEqual(db.get_default_keywords(), "business analyst")
        for c in companies:
            self.assertNotIn("password", (c.get("notes") or "").lower())

        db.delete_company(osu["id"])
        db.init_db()  # second start must not re-seed
        self.assertEqual(len(db.list_companies()), len(companies) - 1)

    def test_seed_board_ids_parse(self):
        with open(db.SEED_PATH, encoding="utf-8") as f:
            seed = json.load(f)
        for c in seed["companies"]:
            ats, token = c["ats"], c.get("ats_token")
            self.assertIn(ats, boards.ATS_TYPES, c["name"])
            if ats == "workday":
                self.assertEqual(len(token.split("/")), 3, c["name"])
            elif ats in ("ukg",):
                self.assertEqual(len(token.split("/")), 3, c["name"])
            elif ats == "oracle":
                self.assertEqual(len(token.split("/")), 2, c["name"])
            elif ats == "eightfold":
                self.assertIn("|", token, c["name"])

    def test_detect_more_platforms(self):
        d = boards.detect_ats
        self.assertEqual(d("https://osu.wd1.myworkdayjobs.com/en-US/OSUCareers?q=x"), ("workday", "osu.wd1.myworkdayjobs.com/osu/OSUCareers"))
        self.assertEqual(d("https://jpmc.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001/jobs"), ("oracle", "jpmc.fa.oraclecloud.com/CX_1001"))
        self.assertEqual(d("https://recruiting.ultipro.com/LIC1000LMHS/JobBoard/3a28-x/?q="), ("ukg", "recruiting.ultipro.com/LIC1000LMHS/3a28-x"))
        self.assertEqual(d("https://workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html?cid=abc&ccId=1"), ("adp", "abc"))
        self.assertEqual(d("https://libertymutual.eightfold.ai/careers?domain=libertymutual.com"), ("eightfold", "libertymutual.eightfold.ai|libertymutual.com"))
        self.assertEqual(d("https://ats.rippling.com/joinroot/jobs"), ("rippling", "joinroot"))

    def test_location_rules(self):
        locs = boards.split_terms("columbus, dublin oh, newark oh")
        job = lambda loc, remote=False: {"job_title": "Business Analyst", "job_location": loc, "job_is_remote": remote}
        self.assertTrue(boards.location_matches(job("Dublin, Ohio"), locs))
        self.assertFalse(boards.location_matches(job("Dublin, Ireland"), locs))
        self.assertFalse(boards.location_matches(job("Newark, DE, United States"), locs))
        self.assertTrue(boards.location_matches(job("Remote - USA", True), locs))
        self.assertFalse(boards.location_matches(job("UK Remote", True), locs))
        self.assertTrue(boards.location_matches(job("3 Locations"), locs, allow_unknown=True))
        self.assertTrue(boards.title_matches(job("x") | {"job_title": "Sr. Business-Analyst II"}, ["business analyst"]))

    def test_company_numeric_fields_via_api(self):
        r = self.client.post("/api/companies", json={"name": "Acme", "priority": "1", "rating": "3.9",
                                                     "careers_url": "https://osu.wd1.myworkdayjobs.com/OSUCareers"})
        self.assertEqual(r.status_code, 201, r.get_json())
        c = r.get_json()
        self.assertEqual((c["priority"], c["rating"], c["ats"]), (1, 3.9, "workday"))
        bad = self.client.post("/api/companies", json={"name": "Bad", "rating": "great"})
        self.assertEqual(bad.status_code, 400)
        no_token = self.client.post("/api/companies", json={"name": "NoTok", "ats": "jibe"})
        self.assertEqual(no_token.status_code, 400)


class PayAndFlagTests(FreshDbTest):
    def test_parse_pay(self):
        from jobsearch.pay import parse_pay
        cases = {
            "The base pay range for this position is $87,069 - $124,385 plus bonus": (87069, 124385, "YEAR"),
            "$112,800.00 - $180,200.00 annually": (112800, 180200, "YEAR"),
            "Hourly range $30.20 - $48.20 hourly": (30.2, 48.2, "HOUR"),
            "Salary: $95K – $120K": (95000, 120000, "YEAR"),
            "Earn $32.50 per hour": (32.5, 32.5, "HOUR"),
        }
        for text, expected in cases.items():
            p = parse_pay(text)
            self.assertEqual((p["min"], p["max"], p["period"]), expected, text)
        for text in ("401(k) with $5,000 sign-on bonus", "revenue of $3 - $5 billion", "", None):
            self.assertIsNone(parse_pay(text), text)

    def test_pay_req_and_closing_saved_from_description(self):
        job = {"job_id": "workday:x:1", "job_title": "Business Analyst", "employer_name": "Acme",
               "job_description": "Pay range: $70,000 - $90,000 annually.", "job_req_id": "R123",
               "job_closes_at": "2026-12-01T00:00:00+00:00"}
        db.save_results([job])
        saved = db.list_jobs()[0]
        self.assertEqual((saved["salary_min"], saved["salary_max"], saved["salary_period"]), (70000, 90000, "YEAR"))
        self.assertEqual((saved["req_number"], saved["closes_at"][:10]), ("R123", "2026-12-01"))
        self.assertEqual(len(db.list_jobs(text="R123")), 1)

    def test_applied_and_not_a_fit_flags(self):
        db.save_results(sample_results()[:3])
        a, b, c = [j["id"] for j in db.list_jobs()]
        self.client.patch(f"/api/jobs/{a}", json={"status": "applied"})
        self.assertIsNotNone(db.get_job(a)["applied_at"])
        self.client.patch(f"/api/jobs/{a}", json={"status": "interested"})
        self.assertIsNone(db.get_job(a)["applied_at"])

        self.client.patch(f"/api/jobs/{b}", json={"status": "not_a_fit"})
        shown = [j["id"] for j in self.client.get("/api/jobs?hide_not_fit=1").get_json()]
        self.assertNotIn(b, shown)
        self.assertEqual(len(shown), 2)
        only = self.client.get("/api/jobs?hide_not_fit=1&status=not_a_fit").get_json()
        self.assertEqual([j["id"] for j in only], [b])
        # Re-running the search must not undo the flag.
        db.save_results(sample_results()[:3])
        self.assertEqual(db.get_job(b)["status"], "not_a_fit")

    def test_sort_by_pay_mixes_hourly_and_yearly(self):
        base = {"employer_name": "Acme", "job_title": "Business Analyst"}
        db.save_results([
            {**base, "job_id": "1", "job_title": "BA 1", "job_min_salary": 60000, "job_max_salary": 70000, "job_salary_period": "YEAR"},
            {**base, "job_id": "2", "job_title": "BA 2", "job_min_salary": 50, "job_max_salary": 60, "job_salary_period": "HOUR"},  # ~$125k
            {**base, "job_id": "3", "job_title": "BA 3"},
        ])
        order = [j["salary_max"] for j in self.client.get("/api/jobs?sort=pay").get_json()]
        self.assertEqual(order, [60, 70000, None])

    def test_old_database_gets_new_columns(self):
        os.remove(os.environ["JOBSEARCH_DB"])
        conn = sqlite3.connect(os.environ["JOBSEARCH_DB"])
        conn.executescript("""
            CREATE TABLE companies (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, careers_url TEXT,
                ats TEXT NOT NULL DEFAULT 'search', ats_token TEXT, keywords TEXT, locations TEXT, notes TEXT,
                created_at TEXT NOT NULL, last_checked_at TEXT);
            CREATE TABLE jobs (id INTEGER PRIMARY KEY, job_id TEXT NOT NULL UNIQUE, company_id INTEGER,
                title TEXT NOT NULL, company TEXT, location TEXT, is_remote INTEGER NOT NULL DEFAULT 0,
                employment_type TEXT, posted_at TEXT, salary_min REAL, salary_max REAL, salary_period TEXT,
                salary_text TEXT, apply_link TEXT, publisher TEXT, description TEXT,
                status TEXT NOT NULL DEFAULT 'new', notes TEXT, first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL, raw_json TEXT);
        """)
        conn.close()
        db.init_db(seed_path=None)
        conn = sqlite3.connect(os.environ["JOBSEARCH_DB"])
        cols = {r[1] for r in conn.execute("PRAGMA table_info(jobs)")}
        conn.close()
        self.assertTrue({"req_number", "closes_at", "applied_at"} <= cols)


class WorkModeTests(FreshDbTest):
    def test_normalize_platform_values(self):
        from jobsearch.workmode import normalize
        for raw, mode in [("Hybrid", "hybrid"), ("REMOTE", "remote"), ("Office", "onsite"), ("OnSite", "onsite"),
                          ("ON_SITE", "onsite"), ("TELECOMMUTE", "remote"), ("Flexible", "hybrid"), ("", None), (None, None)]:
            self.assertEqual(normalize(raw), mode, raw)

    def test_detect_sources(self):
        from jobsearch.workmode import detect
        self.assertEqual(detect(listed="Hybrid"), ("hybrid", "listed"))
        self.assertEqual(detect(location="Ohio Remote"), ("remote", "listed"))
        self.assertEqual(detect(title="Business Analyst (Hybrid)"), ("hybrid", "listed"))
        self.assertEqual(detect(description="This is a hybrid role, 3 days in office."), ("hybrid", "description"))
        self.assertEqual(detect(description="This position is fully remote."), ("remote", "description"))
        self.assertEqual(detect(description="Must work onsite at our Dublin campus."), ("onsite", "description"))
        # Benefits boilerplate shouldn't count as remote.
        self.assertEqual(detect(description="We offer a remote learning stipend."), (None, None))
        self.assertEqual(detect(description="Great team. #LI-Onsite"), ("onsite", "listed"))
        self.assertEqual(detect(description="Apply today #LI-HYBRID #LI-JS1"), ("hybrid", "listed"))
        self.assertEqual(detect(description="Benefits include on-site health and wellness centers."), (None, None))

    def test_saved_and_filterable(self):
        base = {"employer_name": "Acme", "job_title": "Business Analyst"}
        db.save_results([
            {**base, "job_id": "1", "job_title": "BA 1", "job_work_mode": "Hybrid"},
            {**base, "job_id": "2", "job_title": "BA 2", "job_location": "Ohio Remote"},
            {**base, "job_id": "3", "job_title": "BA 3", "job_description": "This role is onsite in Columbus. Must work onsite daily."},
            {**base, "job_id": "4", "job_title": "BA 4"},
        ])
        got = {j["work_mode"] for j in self.client.get("/api/jobs").get_json()}
        self.assertEqual(got, {"hybrid", "remote", "onsite", None})
        for mode, expect in [("hybrid", 1), ("remote", 1), ("onsite", 1), ("unknown", 1)]:
            self.assertEqual(len(self.client.get(f"/api/jobs?work_mode={mode}").get_json()), expect, mode)
        onsite = self.client.get("/api/jobs?work_mode=onsite").get_json()[0]
        self.assertEqual(onsite["work_mode_source"], "description")


class AreaFilterTests(FreshDbTest):
    LOCS = boards.split_terms("columbus, new albany, dublin oh")

    def job(self, loc, remote=False, desc="", title="Business Analyst"):
        return {"job_id": loc + title + desc, "job_title": title, "employer_name": "Acme",
                "job_location": loc, "job_is_remote": remote, "job_description": desc}

    def test_in_area_rules(self):
        keep = [self.job("Columbus, OH"), self.job("Dublin, Ohio"), self.job("Anywhere", True),
                self.job("", False), self.job("Anywhere", True, "Great benefits incl. on-site wellness center")]
        drop = [self.job("San Francisco, CA"),
                self.job("San Francisco, CA", True, "This is a hybrid role with 3 days a week in the office."),
                self.job("Anywhere", True, "Hybrid schedule required. #LI-Hybrid"),
                self.job("Austin, TX", True, title="Business Analyst (Hybrid)"),
                self.job("Remote India", True)]
        for j in keep:
            self.assertTrue(boards.in_area(j, self.LOCS), j)
        for j in drop:
            self.assertFalse(boards.in_area(j, self.LOCS), j)
        # A local hybrid job is fine.
        self.assertTrue(boards.in_area(self.job("Columbus, OH", True, "This is a hybrid role."), self.LOCS))

    def test_search_run_skips_out_of_area(self):
        sid = self.client.post("/api/searches", json={"query": "business analyst", "remote_only": True}).get_json()["id"]
        results = [self.job("Anywhere", True), self.job("Los Angeles, CA"),
                   self.job("Denver, CO", True, "This role is hybrid, 2 days per week in the office.")]
        with mock.patch("jobsearch.jsearch.search", return_value=(results, 1)):
            body = self.client.post(f"/api/searches/{sid}/run").get_json()
        self.assertEqual((body["found"], body["outside_area"], body["new"]), (1, 2, 1))

    def test_one_off_search_keeps_its_own_city(self):
        results = [self.job("Chicago, IL"), self.job("Boston, MA")]
        with mock.patch("jobsearch.jsearch.search", return_value=(results, 1)):
            body = self.client.post("/api/search", json={"query": "business analyst", "location": "Chicago, IL"}).get_json()
        self.assertEqual((body["found"], body["outside_area"]), (1, 1))


class BenefitsTests(FreshDbTest):
    def test_posting_tags(self):
        from jobsearch.benefits import tags_from_text
        text = ("We offer a 401(k) with a 6% company match, 15 days of paid time off, 10 paid holidays, "
                "an annual bonus, tuition reimbursement and 12 weeks paid parental leave.")
        self.assertEqual([t["label"] for t in tags_from_text(text)],
                         ["401(k) match 6%", "15 days PTO", "10 holidays", "Bonus", "12 wks parental leave", "Tuition help"])
        self.assertEqual(tags_from_text("Benefits include on-site wellness centers and a retirement savings plan."), [])

    def test_company_key_matches_variants(self):
        from jobsearch.benefits import company_key
        self.assertEqual(company_key("JPMorgan Chase & Co."), company_key("JPMorgan Chase"))
        self.assertEqual(company_key("American Electric Power (AEP)"), company_key("American Electric Power"))
        self.assertEqual(company_key("Nationwide Children's"), company_key("Nationwide Childrens"))

    def test_seeded_scores_and_save_validation(self):
        os.remove(os.environ["JOBSEARCH_DB"])
        db.init_db()
        aep = db.get_benefits("American Electric Power")
        self.assertEqual((aep["retirement_score"], aep["pto_score"], aep["commute_score"], aep["bonus_score"]), (2.75, 2, 1, 3))
        self.assertIn("4.5% 401k match", aep["notes"])
        osu = next(c for c in self.client.get("/api/companies").get_json() if c["name"] == "OSU")
        self.assertEqual(osu["benefits"]["retirement_score"], 4)
        self.assertEqual(self.client.put("/api/benefits", json={"company": "OSU", "pto_score": "9"}).status_code, 400)
        r = self.client.put("/api/benefits", json={"company": "Brand New Co", "bonus_score": "3", "notes": "x"})
        self.assertEqual(r.get_json()["bonus_score"], 3)

    def test_page_facts_must_quote_the_page(self):
        from jobsearch import benefits
        page = "Our 401(k) plan matches 100% of the first 6% you contribute. Employees get 20 days of PTO."
        answer = {"facts": [
            {"topic": "retirement_match", "summary": "6% match", "quote": "matches 100% of the first 6% you contribute"},
            {"topic": "pto", "summary": "30 days PTO", "quote": "Employees get 30 days of PTO"},  # not on the page
        ], "scores": {"retirement": 4, "pto": 1, "bonus": None}}
        with mock.patch.object(benefits.ai, "json_answer", return_value=json.dumps(answer)):
            data = benefits._from_pages("Acme", [("https://acme.com/benefits", page)])
        self.assertEqual([f["summary"] for f in data["facts"]], ["6% match"])
        self.assertTrue(data["facts"][0]["verified"])

    def test_lookup_from_unsaved_job_company(self):
        from jobsearch import benefits
        db.save_results([{"job_id": "j1", "job_title": "Business Analyst", "employer_name": "Startup Corp",
                          "job_apply_link": "https://startup.example/jobs/1",
                          "job_description": "Great team. 401(k) with 4% match."}])
        job_id = db.list_jobs()[0]["id"]
        result = {"facts": [{"topic": "retirement_match", "summary": "4% match", "quote": "401(k) with 4% match.",
                             "source_url": "(this job posting)", "verified": True}],
                  "scores": {"retirement": 1}, "score_reasons": {"retirement": "4% or less"}, "method": "pages", "sources": []}
        with mock.patch.object(benefits, "lookup", return_value=result) as lk:
            r = self.client.post("/api/benefits/lookup", json={"job_id": job_id})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(lk.call_args.args[0], "Startup Corp")
        self.assertIn("https://startup.example/jobs/1", lk.call_args.kwargs["start_urls"])
        body = r.get_json()
        self.assertEqual((body["company_name"], body["ai_lookups_this_month"]), ("Startup Corp", 1))
        self.assertEqual(body["retirement_score"], 1)  # empty box filled from the calculation
        self.assertEqual(body["filled"], ["retirement"])
        job = self.client.get(f"/api/jobs/{job_id}").get_json()
        self.assertEqual(job["company_benefits"]["lookup"]["scores"]["retirement"], 1)
        self.assertEqual([t["label"] for t in job["benefit_tags"]], ["401(k) match 4%"])


class BenefitsFillerTests(unittest.TestCase):
    def test_no_info_facts_dropped(self):
        from jobsearch import benefits
        web = {"facts": [{"topic": "retirement_match", "summary": "50% match up to 8%", "quote": "matches 50%", "source_url": "https://x.com"},
                         {"topic": "pto", "summary": "Specific details about PTO are not provided.", "quote": "", "source_url": ""},
                         {"topic": "bonus", "summary": "No information on bonuses", "quote": "n/a", "source_url": "https://x.com"}],
               "scores": {"retirement": 4}}
        with mock.patch.object(benefits, "find_benefit_pages", return_value=[]), \
             mock.patch.object(benefits, "_from_web", return_value=web):
            out = benefits.lookup("Acme")
        self.assertEqual([f["topic"] for f in out["facts"]], ["retirement_match"])
        self.assertEqual(out["method"], "web search")


class MoreConnectorTests(FreshDbTest):
    def test_detect_new_platforms(self):
        d = boards.detect_ats
        self.assertEqual(d("https://jobs.dayforcehcm.com/en-US/thg/ALLCAREERS"), ("dayforce", "thg/ALLCAREERS"))
        self.assertEqual(d("https://jobs.dayforcehcm.com/drc/CANDIDATEPORTAL"), ("dayforce", "drc/CANDIDATEPORTAL"))
        self.assertEqual(d("https://myjobs.adp.com/ecsfederal/cx/job-listing?rb=CareerSite"), ("adp_myjobs", "ecsfederal"))
        self.assertEqual(d("https://pearson.jobs/locations/usa/jobs/"), ("jobsyn", "pearson.jobs"))
        self.assertEqual(d("https://www.amazon.jobs/en/")[0], "amazon")

    def test_state_only_location_is_kept(self):
        locs = boards.split_terms("columbus, dublin oh")
        job = {"job_title": "IT Business Analyst", "job_location": "OH"}
        self.assertTrue(boards.location_matches(job, locs, allow_unknown=True))
        self.assertTrue(boards.in_area(job, locs))
        self.assertFalse(boards.location_matches({**job, "job_location": "Portland, OR"}, locs, allow_unknown=True))

    def test_avature_page_parsing_and_paging(self):
        def page(ids):
            return "".join(
                f'<h3 class="article__header__text__title"><a href="https://x.example/en_US/careers/JobDetail/BA/{i}">'
                f' Business Analyst {i} </a></h3> <div class="article__header__text__subtitle"><span>Deloitte</span> | '
                f'<span>Columbus, Ohio, United States</span></div>' for i in ids)
        pages = [page(range(1, 11)), page(range(11, 16)), page(range(11, 16))]
        resp = [mock.MagicMock(text=p) for p in pages]
        with mock.patch.object(boards, "_request", side_effect=resp):
            jobs = boards.fetch_avature("x.example/en_US/careers", "Deloitte", "business analyst")
        self.assertEqual(len(jobs), 15)
        self.assertEqual((jobs[0]["job_title"], jobs[0]["job_location"], jobs[0]["job_req_id"]),
                         ("Business Analyst 1", "Columbus, Ohio, United States", "1"))

    def test_existing_db_upgraded_to_new_connectors(self):
        db.add_company({"name": "Pearson", "ats": "search"})
        db.add_company({"name": "Deloitte", "ats": "lever", "ats_token": "mine"})  # set by hand: leave alone
        db.upgrade_seed_connectors()
        by_name = {c["name"]: c for c in db.list_companies()}
        self.assertEqual((by_name["Pearson"]["ats"], by_name["Pearson"]["ats_token"]), ("jobsyn", "pearson.jobs"))
        self.assertEqual(by_name["Deloitte"]["ats"], "lever")


class ScoreCalculationTests(FreshDbTest):
    def calc(self, work_mode=None, **figures):
        from jobsearch.benefits import calculate_scores, _clean_figures
        return calculate_scores(_clean_figures(figures), work_mode=work_mode)[0]

    def test_retirement(self):
        self.assertEqual(self.calc(employer_match_pct=6)["retirement"], 4)
        self.assertEqual(self.calc(employer_match_pct="5%")["retirement"], 3)
        self.assertEqual(self.calc(employer_match_pct=4.5)["retirement"], 1)
        self.assertEqual(self.calc(employer_match_pct=3, pension=True)["retirement"], 4)
        self.assertEqual(self.calc()["retirement"], 2)

    def test_pto_counts_sick_days(self):
        self.assertEqual(self.calc(unlimited_pto=True)["pto"], 4)
        self.assertEqual(self.calc(pto_days=20, sick_days=10)["pto"], 4)
        self.assertEqual(self.calc(pto_days=25)["pto"], 3)
        self.assertEqual(self.calc(pto_days=15, sick_days=5)["pto"], 1)
        self.assertEqual(self.calc()["pto"], 2)

    def test_bonus(self):
        self.assertEqual(self.calc(bonus=True, bonus_target_pct=15)["bonus"], 4)
        self.assertEqual(self.calc(bonus="yes")["bonus"], 3)
        self.assertEqual(self.calc(bonus_target_pct=5)["bonus"], 3)
        self.assertEqual(self.calc(bonus=False)["bonus"], 1)
        self.assertEqual(self.calc()["bonus"], 2)

    def test_commute_only_when_clear(self):
        self.assertEqual(self.calc(work_mode="remote")["commute"], 4)
        self.assertEqual(self.calc(work_mode=None)["commute"], 3)
        self.assertNotIn("commute", self.calc(work_mode="hybrid"))
        self.assertNotIn("commute", self.calc(work_mode="company"))

    def test_lookup_keeps_her_scores(self):
        from jobsearch import benefits
        db.save_benefits("Acme", {"retirement_score": 2.75})
        result = {"facts": [], "figures": {}, "scores": {"retirement": 4, "pto": 3, "bonus": 2},
                  "score_reasons": {}, "method": "pages", "sources": []}
        with mock.patch.object(benefits, "lookup", return_value=result):
            body = self.client.post("/api/benefits/lookup", json={"company": "Acme"}).get_json()
        self.assertEqual((body["retirement_score"], body["pto_score"], body["bonus_score"]), (2.75, 3, 2))
        self.assertEqual(sorted(body["filled"]), ["bonus", "pto"])


class FigureQuoteTests(unittest.TestCase):
    def test_figures_need_real_quotes(self):
        from jobsearch import benefits
        page = "We offer a 401(k) 6% match and four weeks paid time off to start."
        answer = {"facts": [], "figures": {"employer_match_pct": 6, "pension": False, "pto_days": 20, "bonus": True},
                  "figure_quotes": {"employer_match_pct": "401(k) 6% match", "pension": "Not provided",
                                    "pto_days": "four weeks paid time off to start", "bonus": "annual bonus up to 15%"}}
        with mock.patch.object(benefits.ai, "json_answer", return_value=json.dumps(answer)):
            data = benefits._from_pages("Acme", [("https://acme.com/benefits", page)])
        # pension had no real quote; the bonus quote isn't on the page
        self.assertEqual(data["figures"], {"employer_match_pct": 6, "pto_days": 20})


class BonusQuoteTests(unittest.TestCase):
    def test_referral_and_401k_not_bonus(self):
        from jobsearch.benefits import _quoted_figures
        for quote in ("Employee referral bonus program", "A discretionary 401k award is determined each year",
                      "$5,000 sign-on bonus"):
            figs, _ = _quoted_figures({"figures": {"bonus": True}, "figure_quotes": {"bonus": quote}}, lambda q: True)
            self.assertEqual(figs, {}, quote)
        figs, _ = _quoted_figures({"figures": {"bonus": True, "bonus_target_pct": 10},
                                   "figure_quotes": {"bonus": "annual incentive plan", "bonus_target_pct": "target of 10%"}},
                                  lambda q: True)
        self.assertEqual(figs, {"bonus": True, "bonus_target_pct": 10})


class SaveCompanyFromJobTests(FreshDbTest):
    def add_job(self, job_id, company, link, title="Business Analyst"):
        db.save_results([{"job_id": job_id, "job_title": title, "employer_name": company,
                          "job_apply_link": link, "job_publisher": "LinkedIn"}])
        return next(j["id"] for j in db.list_jobs() if j["company"] == company)

    def test_save_from_job_board_listing(self):
        jid = self.add_job("a", "Startup Corp", "https://www.linkedin.com/jobs/view/123")
        self.add_job("b", "Startup Corp.", "https://www.linkedin.com/jobs/view/456", "Product Owner")  # same employer, other spelling
        self.assertIsNone(self.client.get("/api/jobs").get_json()[0]["saved_company_id"])

        r = self.client.post(f"/api/jobs/{jid}/save_company")
        self.assertEqual(r.status_code, 201, r.get_json())
        company = r.get_json()
        self.assertEqual((company["name"], company["ats"], company["careers_url"]), ("Startup Corp", "search", None))
        self.assertFalse(company["keywords"])  # no extras; the default titles apply
        # Both of that employer's jobs now belong to the new company, and no longer offer the button.
        jobs = self.client.get(f"/api/jobs?company_id={company['id']}").get_json()
        self.assertEqual(len(jobs), 2)
        self.assertTrue(all(j["saved_company_id"] == company["id"] for j in jobs))
        self.assertEqual(self.client.post(f"/api/jobs/{jid}/save_company").status_code, 400)

    def test_save_uses_direct_board_when_link_is_one(self):
        jid = self.add_job("c", "Acme", "https://acme.wd5.myworkdayjobs.com/en-US/Careers/job/Columbus/BA_R1")
        company = self.client.post(f"/api/jobs/{jid}/save_company").get_json()
        self.assertEqual((company["ats"], company["ats_token"]), ("workday", "acme.wd5.myworkdayjobs.com/acme/Careers"))

    def test_already_saved_company_flagged(self):
        db.add_company({"name": "JPMorgan Chase", "ats": "search"})
        self.add_job("d", "JPMorgan Chase & Co.", "https://www.indeed.com/viewjob?jk=1")
        self.assertIsNotNone(self.client.get("/api/jobs").get_json()[0]["saved_company_id"])


class RenameLinksJobsTests(FreshDbTest):
    def test_rename_picks_up_matching_jobs(self):
        cid = db.add_company({"name": "Huntington Bank", "ats": "search"})
        db.save_results([{"job_id": "h1", "job_title": "Business Analyst", "employer_name": "Huntington"}])
        self.assertIsNone(self.client.get("/api/jobs").get_json()[0]["saved_company_id"])
        r = self.client.patch(f"/api/companies/{cid}", json={"name": "Huntington"})
        self.assertEqual(r.get_json()["name"], "Huntington")
        self.assertEqual(self.client.get("/api/jobs").get_json()[0]["saved_company_id"], cid)
        self.assertEqual(len(self.client.get(f"/api/jobs?company_id={cid}").get_json()), 1)


class DefaultTitlesTests(FreshDbTest):
    def test_title_terms_merge(self):
        self.assertEqual(boards.title_terms("business analyst", "product owner, Business Analyst"),
                         ["business analyst", "product owner"])
        self.assertEqual(boards.title_terms("", None), [])

    def test_searches_each_title_and_filters(self):
        calls = []

        def fake_fetch(token, name, query, locations):
            calls.append(query)
            return [{"job_id": f"x:{query}", "job_title": f"Senior {query.title()}", "job_location": "Columbus, OH"},
                    {"job_id": "x:other", "job_title": "Sales Lead", "job_location": "Columbus, OH"}]

        company = {"name": "Pearson", "ats": "jobsyn", "ats_token": "pearson.jobs", "keywords": "product owner",
                   "locations": "columbus"}
        with mock.patch.object(boards, "fetch_jobsyn", side_effect=fake_fetch):
            jobs, _ = boards.check_company(company, default_keywords="business analyst")
        self.assertEqual(calls, ["business analyst", "product owner"])
        self.assertEqual(sorted(j["job_title"] for j in jobs), ["Senior Business Analyst", "Senior Product Owner"])

    def test_list_all_boards_fetch_once(self):
        calls = []
        with mock.patch.object(boards, "fetch_greenhouse", side_effect=lambda *a: calls.append(a[2]) or []):
            boards.check_company({"name": "Acme", "ats": "greenhouse", "ats_token": "acme", "keywords": "product owner"},
                                 default_keywords="business analyst")
        self.assertEqual(len(calls), 1)

    def test_setting_endpoint_and_upgrade(self):
        r = self.client.put("/api/settings/default_keywords", json={"default_keywords": " business analyst ,systems analyst "})
        self.assertEqual(r.get_json()["default_keywords"], "business analyst, systems analyst")
        self.assertEqual(self.client.get("/api/meta").get_json()["default_keywords"], "business analyst, systems analyst")
        self.assertEqual(self.client.put("/api/settings/default_keywords", json={"default_keywords": " "}).status_code, 400)

    def test_old_company_titles_split_into_extras(self):
        with db.connect() as conn:
            conn.execute("DELETE FROM meta WHERE key IN ('keywords_split', 'default_keywords')")
        db.add_company({"name": "Pearson", "ats": "search", "keywords": "business analyst, product owner"})
        db.add_company({"name": "OSU", "ats": "search", "keywords": "business analyst"})
        db.split_default_keywords()
        by_name = {c["name"]: c["keywords"] for c in db.list_companies()}
        self.assertEqual(by_name, {"Pearson": "product owner", "OSU": None})


class DistanceAreaTests(FreshDbTest):
    def area(self, extra=None):
        from jobsearch import geo
        home = geo.lookup("Hanover", "OH")
        return boards.Area(home, 50, extra_centers=[geo.parse_points(extra)[0]] if extra else [])

    def job(self, loc, remote=False, desc=""):
        return {"job_title": "Business Analyst", "job_location": loc, "job_is_remote": remote, "job_description": desc}

    def test_geo_parsing_and_distance(self):
        from jobsearch import geo
        home = geo.lookup("Hanover", "OH")
        self.assertAlmostEqual(geo.miles(home, geo.lookup("Columbus", "OH")), 38, delta=2)
        for text, verdict in [("Columbus, OH", "near"), ("Dublin, Ohio, United States", "near"), ("Dublin, OHOH", "near"),
                              ("OH-Columbus-Easton", "near"), ("New Albany, OH", "near"), ("Zanesville, OH", "near"),
                              ("Marysville, OH", "far"), ("Cincinnati, OH", "far"), ("Hoboken, NJ", "far"),
                              ("Bengaluru, Karnataka, IND", "far"), ("Dublin, IRL", "far"), ("Columbus", "near"),
                              ("Seattle", "far"), ("Multiple Locations", "unknown"), ("OH", "unknown"),
                              ("Columbus, Ohio; Chicago, IL", "near")]:
            self.assertEqual(geo.classify(text, home, 50), verdict, text)

    def test_company_filter_uses_distance_and_keeps_remote(self):
        a = self.area()
        self.assertTrue(boards.location_matches(self.job("Mount Vernon, OH"), a))
        self.assertFalse(boards.location_matches(self.job("Cleveland, OH"), a))
        self.assertTrue(boards.location_matches(self.job("Remote - USA", True), a))
        self.assertTrue(boards.location_matches(self.job("3 Locations"), a, allow_unknown=True))
        self.assertFalse(boards.location_matches(self.job("UK Remote", True), a))

    def test_job_board_filter(self):
        a = self.area()
        self.assertTrue(boards.in_area(self.job("Lancaster, OH"), a))
        self.assertFalse(boards.in_area(self.job("Austin, TX"), a))
        self.assertFalse(boards.in_area(self.job("Austin, TX", True, "This is a hybrid role, 3 days a week in the office."), a))
        self.assertTrue(boards.in_area(self.job("Austin, TX", True), a))      # real remote
        self.assertTrue(boards.in_area({**self.job(""), "job_latitude": 40.07, "job_longitude": -82.42}, a))  # Newark by coords
        chi = self.area("Chicago, IL")
        self.assertTrue(boards.in_area(self.job("Evanston, IL"), chi))

    def test_area_setting_endpoint(self):
        self.assertEqual(self.client.get("/api/meta").get_json()["area"], {"home": "Hanover, OH", "radius_miles": 50.0})
        self.assertEqual(self.client.put("/api/settings/area", json={"home": "Nowhereville, ZZ", "radius_miles": 50}).status_code, 400)
        self.assertEqual(self.client.put("/api/settings/area", json={"home": "Newark, OH", "radius_miles": 2}).status_code, 400)
        r = self.client.put("/api/settings/area", json={"home": "Newark, OH", "radius_miles": "40"})
        self.assertEqual(r.get_json(), {"home": "Newark, OH", "radius_miles": 40.0})


class DetailPageTests(unittest.TestCase):
    def test_talentegy_description_with_nested_divs(self):
        page = ('<div class="Band Title">x</div><div class="Description"><p>Intro</p>'
                '<div class="inner"><ul><li>401(k) with 6% match</li></ul></div><p>End</p></div><div class="footer">no</div>')
        job = {"job_description": None}
        with mock.patch.object(boards, "_request", return_value=mock.MagicMock(text=page)):
            boards.talentegy_details(job, "https://x.example/job")
        self.assertIn("Intro", job["job_description"])
        self.assertIn("401(k) with 6% match", job["job_description"])
        self.assertIn("End", job["job_description"])
        self.assertNotIn("no", job["job_description"].split())


class DuplicateJobTests(FreshDbTest):
    def job(self, job_id, publisher, link, title="Business Analyst", location="Columbus, OH", req=None, company="Acme Corp"):
        return {"job_id": job_id, "job_title": title, "employer_name": company, "job_location": location,
                "job_publisher": publisher, "job_apply_link": link, "job_req_id": req}

    def test_same_job_on_two_sites_is_one_card(self):
        new, seen = db.save_results([self.job("lad1", "Ladders", "https://ladders.example/1"),
                                     self.job("zip1", "ZipRecruiter", "https://zip.example/1", location="Columbus, Ohio",
                                              company="Acme Corp.")])
        self.assertEqual((new, seen), (1, 1))
        jobs = db.list_jobs()
        self.assertEqual(len(jobs), 1)
        full = db.get_job(jobs[0]["id"])
        self.assertEqual(json.loads(full["also_found"]), [{"publisher": "ZipRecruiter", "link": "https://zip.example/1"}])
        # Running the search again doesn't bring the second listing back, or add it twice.
        self.assertEqual(db.save_results([self.job("zip1", "ZipRecruiter", "https://zip.example/1")]), (0, 1))
        self.assertEqual(len(json.loads(db.get_job(jobs[0]["id"])["also_found"])), 1)

    def test_different_openings_stay_separate(self):
        db.save_results([self.job("e1", "ADP", "l1", location=None, req="5396"),
                         self.job("e2", "ADP", "l2", location=None, req="5391"),   # different req: another opening
                         self.job("e3", "ADP", "l3", location="Austin, TX"),        # different place
                         self.job("e4", "ADP", "l4", title="Business Analyst II")]) # different title
        self.assertEqual(len(db.list_jobs()), 4)

    def test_status_kept_and_search_linked_on_merge(self):
        db.save_results([self.job("a", "LinkedIn", "https://li.example/a")])
        jid = db.list_jobs()[0]["id"]
        db.update_job(jid, {"status": "applied"})
        sid = db.add_search({"name": "s", "query": "business analyst"})
        db.save_results([self.job("b", "Indeed", "https://in.example/b")], search_id=sid)
        self.assertEqual(db.get_job(jid)["status"], "applied")
        self.assertEqual([j["id"] for j in db.list_jobs(search_id=sid)], [jid])


class InterestLevelTests(FreshDbTest):
    def setUp(self):
        super().setUp()
        db.save_results([{"job_id": f"j{i}", "job_title": f"BA {i}", "employer_name": "Acme"} for i in range(4)])
        self.ids = sorted(j["id"] for j in db.list_jobs())

    def patch(self, jid, **body):
        return self.client.patch(f"/api/jobs/{jid}", json=body)

    def test_set_keep_and_clear(self):
        a = self.ids[0]
        self.patch(a, status="interested")
        self.assertEqual(self.patch(a, interest_level="high").get_json()["interest_level"], "high")
        self.patch(a, status="applied")                       # Interested -> Applied keeps the level
        self.assertEqual(db.get_job(a)["interest_level"], "high")
        self.assertEqual(self.patch(a, interest_level="Low").get_json()["interest_level"], "low")  # change on Applied
        self.patch(a, status="new")                           # back to New clears it
        self.assertIsNone(db.get_job(a)["interest_level"])
        self.assertEqual(self.patch(a, interest_level="urgent").status_code, 400)

    def test_filter_and_sort(self):
        levels = ["low", "high", None, "medium"]
        for jid, level in zip(self.ids, levels):
            self.patch(jid, status="interested", interest_level=level)
        got = lambda q: [j["interest_level"] for j in self.client.get(f"/api/jobs?{q}").get_json()]
        self.assertEqual(got("status=interested&sort=interest"), ["high", "medium", "low", None])
        self.assertEqual(got("status=interested&interest=high"), ["high"])
        self.assertEqual(got("status=interested&interest=unset"), [None])


class EditSavedSearchTests(FreshDbTest):
    def test_edit_saved_search(self):
        sid = self.client.post("/api/searches", json={"query": "business analyst", "location": "Columbus, OH",
                                                     "date_posted": "week", "name": "BA Columbus"}).get_json()["id"]
        r = self.client.patch(f"/api/searches/{sid}", json={"query": "business systems analyst", "location": "",
                                                           "remote_only": True, "date_posted": "month",
                                                           "employment_types": "FULLTIME", "num_pages": 2, "name": "BSA remote"})
        s = r.get_json()
        self.assertEqual((s["name"], s["query"], s["location"], s["remote_only"], s["date_posted"], s["employment_types"], s["num_pages"]),
                         ("BSA remote", "business systems analyst", None, 1, "month", "FULLTIME", 2))
        self.assertEqual(self.client.patch(f"/api/searches/{sid}", json={"query": " "}).status_code, 400)
        self.assertEqual(self.client.patch("/api/searches/999", json={"query": "x"}).status_code, 404)


class ImportJobTests(FreshDbTest):
    PAGE = ('<html><script type="application/ld+json">' + json.dumps({
        "@type": "JobPosting", "title": "Senior Business Analyst", "hiringOrganization": {"name": "Acme Health"},
        "jobLocation": {"address": {"addressLocality": "Columbus", "addressRegion": "OH", "addressCountry": "US"}},
        "description": "<p>Lead requirements work. 401(k) with 6% match. This is a hybrid role.</p>",
        "datePosted": "2026-11-20", "identifier": {"value": "R-555"},
        "baseSalary": {"value": {"minValue": 90000, "maxValue": 110000, "unitText": "YEAR"}}}) + '</script></html>')

    def test_reads_structured_job_page(self):
        from jobsearch import importer
        with mock.patch.object(boards, "_request", return_value=mock.MagicMock(text=self.PAGE)):
            job = importer.read_job("https://careers.acmehealth.example/job/555")
        v = db._job_values(job)
        self.assertEqual((v["title"], v["company"], v["location"], v["req_number"]),
                         ("Senior Business Analyst", "Acme Health", "Columbus, OH, US", "R-555"))
        self.assertEqual((v["salary_min"], v["salary_max"], v["work_mode"]), (90000, 110000, "hybrid"))
        self.assertTrue(v["job_id"].startswith("url:careers.acmehealth.example/job/555"))

    def test_big_job_boards_ask_for_pasted_text(self):
        from jobsearch import importer
        with self.assertRaises(importer.ImportFailed) as ctx:
            importer.read_job("https://www.indeed.com/viewjob?jk=abc")
        self.assertIn("paste", str(ctx.exception).lower())

    def test_pasted_text_uses_chatgpt_fields_and_keeps_text(self):
        from jobsearch import importer
        text = "Business Analyst II\nBigCo\nDublin, OH (Hybrid)\n" + "Gather requirements. " * 30
        answer = {"title": "Business Analyst II", "company": "BigCo", "location": "Dublin, OH", "work_style": "hybrid",
                  "pay": "$80,000 - $95,000", "req_number": None, "posted_date": None, "closing_date": None, "employment_type": None}
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "test", "AI_LISTINGS": "openai"}), mock.patch.object(importer.ai, "json_answer", return_value=json.dumps(answer)):
            job = importer.read_job("https://www.glassdoor.com/job-listing/x", text=text)
        v = db._job_values(job)
        self.assertEqual((v["title"], v["company"], v["work_mode"], v["salary_min"]), ("Business Analyst II", "BigCo", "hybrid", 80000))
        self.assertIn("Gather requirements", v["description"])
        self.assertEqual(v["publisher"], "Glassdoor")

    def test_preview_then_save_with_edits_links_saved_company(self):
        cid = db.add_company({"name": "Acme Health", "ats": "search"})
        with mock.patch.object(boards, "_request", return_value=mock.MagicMock(text=self.PAGE)):
            r = self.client.post("/api/jobs/import/preview", json={"url": "https://careers.acmehealth.example/job/555"})
        self.assertEqual(r.status_code, 200, r.get_json())
        body = r.get_json()
        self.assertEqual(body["preview"]["company"], "Acme Health")
        saved = self.client.post("/api/jobs/import/save", json={"job": body["job"], "edits": {"title": "Sr. Business Analyst"}}).get_json()
        job = db.get_job(saved["id"])
        self.assertEqual((job["title"], job["company_id"], saved["new"]), ("Sr. Business Analyst", cid, True))
        # Adding the same listing again is refused, and nothing new is saved.
        again = self.client.post("/api/jobs/import/save", json={"job": body["job"]})
        self.assertEqual(again.status_code, 409)
        self.assertEqual(again.get_json()["duplicate"]["id"], saved["id"])
        self.assertEqual(len(db.list_jobs()), 1)


class AddJobDuplicateTests(FreshDbTest):
    """Adding a job she already has: refused, unless the earlier one was Rejected and she confirms."""

    def job(self, job_id="url:x.example/1", title="Business Analyst", req=None, location="Columbus, OH"):
        return {"job_id": job_id, "job_title": title, "employer_name": "Acme", "job_location": location,
                "job_apply_link": f"https://{job_id[4:]}", "job_publisher": "X", "job_req_id": req}

    def add(self, job, **extra):
        return self.client.post("/api/jobs/import/save", json={"job": job, **extra})

    def preview_dups(self, job):
        from jobsearch import importer
        with mock.patch.object(importer, "read_job", return_value=job):
            return self.client.post("/api/jobs/import/preview", json={"url": job["job_apply_link"]}).get_json()["duplicates"]

    def test_same_job_on_another_site_is_refused(self):
        first = self.add(self.job()).get_json()
        self.client.patch(f"/api/jobs/{first['id']}", json={"status": "interested"})
        other_site = self.job("url:y.example/9")
        dups = self.preview_dups(other_site)
        self.assertEqual(([d["id"] for d in dups["blocking"]], dups["rejected"]), ([first["id"]], []))
        r = self.add(other_site)
        self.assertEqual(r.status_code, 409)
        self.assertIn("Interested", r.get_json()["error"])
        self.assertEqual(len(db.list_jobs()), 1)

    def test_same_reference_number_with_reworded_title_is_refused(self):
        self.add(self.job(req="R-100"))
        r = self.add(self.job("url:y.example/2", title="Sr. Business Analyst (Hybrid)", req="R-100"))
        self.assertEqual(r.status_code, 409)

    def test_different_opening_is_not_a_duplicate(self):
        self.add(self.job(req="R-100"))
        self.assertEqual(self.add(self.job("url:y.example/2", req="R-200")).status_code, 201)
        self.assertEqual(len(db.list_jobs()), 2)

    def test_copy_of_a_rejected_job_needs_confirming_then_is_a_new_job(self):
        old = self.add(self.job()).get_json()["id"]
        self.client.patch(f"/api/jobs/{old}", json={"status": "rejected", "notes": "Rejected in October"})
        dups = self.preview_dups(self.job())
        self.assertEqual((dups["blocking"], [d["id"] for d in dups["rejected"]]), ([], [old]))
        r = self.add(self.job())
        self.assertEqual((r.status_code, r.get_json()["needs_confirm"]), (409, True))
        self.assertEqual(len(db.list_jobs()), 1)
        r = self.add(self.job(), save_relisted=True)
        self.assertEqual(r.status_code, 201)
        new = r.get_json()["id"]
        self.assertNotEqual(new, old)
        self.assertEqual(db.get_job(new)["status"], "new")
        self.assertEqual((db.get_job(old)["status"], db.get_job(old)["notes"]), ("rejected", "Rejected in October"))
        # Now there's an open copy, so a third attempt is a plain duplicate.
        self.assertIn("duplicate", self.add(self.job(), save_relisted=True).get_json())


class AddJobFollowUpTests(FreshDbTest):
    def job(self, job_id="url:x.example/1"):
        return {"job_id": job_id, "job_title": "Business Analyst", "employer_name": "Acme",
                "job_location": "Columbus, OH", "job_apply_link": "https://x.example/1", "job_publisher": "X"}

    def test_saved_as_new_with_level_and_where_found(self):
        r = self.client.post("/api/jobs/import/save", json={"job": self.job(), "interest_level": "high",
                                                           "found_where": "LinkedIn alert " + "x" * 60}).get_json()
        j = next(x for x in self.client.get("/api/jobs").get_json() if x["id"] == r["id"])
        self.assertEqual((j["status"], j["interest_level"], j["added_by_hand"]), ("new", "high", 1))
        # Marking it Interested later keeps the level she picked when adding it.
        self.client.patch(f"/api/jobs/{r['id']}", json={"status": "interested"})
        self.assertEqual(db.get_job(r["id"])["interest_level"], "high")
        self.assertEqual(len(j["found_where"]), 50)
        self.assertTrue(j["found_where"].startswith("LinkedIn alert"))

    def test_interest_can_be_skipped(self):
        r = self.client.post("/api/jobs/import/save", json={"job": self.job(), "interest_level": None}).get_json()
        j = db.get_job(r["id"])
        self.assertEqual((j["status"], j["interest_level"], j["found_where"]), ("new", None, None))

    def test_job_already_applied_to_is_refused_and_left_alone(self):
        db.save_results([{**self.job("linkedin:9"), "job_publisher": "LinkedIn"}])
        jid = db.list_jobs()[0]["id"]
        db.update_job(jid, {"status": "applied"})
        r = self.client.post("/api/jobs/import/save", json={"job": self.job(), "found_where": "Careers page"})
        self.assertEqual((r.status_code, r.get_json()["duplicate"]["id"]), (409, jid))
        j = db.get_job(jid)
        self.assertEqual((j["status"], j["found_where"], j["added_by_hand"]), ("applied", None, 0))
        self.assertEqual(len(db.list_jobs()), 1)

    def test_bad_interest_level(self):
        self.assertEqual(self.client.post("/api/jobs/import/save", json={"job": self.job(), "interest_level": "urgent"}).status_code, 400)
        self.assertEqual(db.list_jobs(), [])  # nothing saved when the input is bad


class EditJobDetailsTests(FreshDbTest):
    SITE = {"job_id": "wd:1", "job_title": "Business Analyst", "employer_name": "Acme", "job_req_id": "R-77",
            "job_location": "Columbus, OH", "job_posted_at_datetime_utc": "2026-11-01T00:00:00+00:00",
            "job_description": "Pay range $70,000 - $80,000 annually. This is a hybrid role."}

    def setUp(self):
        super().setUp()
        db.save_results([self.SITE])
        self.jid = db.list_jobs()[0]["id"]

    def patch(self, **body):
        return self.client.patch(f"/api/jobs/{self.jid}", json=body)

    def test_her_fields_save_and_validate(self):
        r = self.patch(req_number="SRC-12", resume_submitted="2026-11-20", cover_letter=True,
                       resume_link="careers.acme.example/me", callback_chances="Good fit", response_date="",
                       interviewed=False, listing_copy_location="OneDrive/Job Hunt/Acme.pdf")
        j = r.get_json()
        self.assertEqual((j["req_number"], j["resume_submitted"], j["cover_letter"], j["interviewed"], j["response_date"]),
                         ("SRC-12", "2026-11-20", 1, 0, None))
        self.assertEqual(j["resume_link"], "https://careers.acme.example/me")
        self.assertEqual(self.patch(resume_submitted="11/20/2026").status_code, 400)
        self.assertEqual(self.patch(callback_chances="x" * 251).status_code, 400)
        self.assertEqual(self.patch(work_mode="sometimes").status_code, 400)

    def test_listing_edits_survive_recheck_others_refresh(self):
        j = self.patch(pay="$90k - $100k", work_mode="remote", req_number="R-78").get_json()
        self.assertEqual((j["salary_min"], j["salary_max"], j["salary_period"]), (90000, 100000, "YEAR"))
        self.assertEqual((j["work_mode"], j["work_mode_source"], j["is_remote"]), ("remote", "manual", 1))
        self.assertEqual(json.loads(j["edited_fields"]), ["pay", "req_number", "work_mode"])
        # The site changes its listing; her edits stay, untouched fields follow the site.
        db.save_results([{**self.SITE, "job_req_id": "R-99", "job_location": "Dublin, OH",
                          "job_posted_at_datetime_utc": "2026-11-15T00:00:00+00:00"}])
        j = db.get_job(self.jid)
        self.assertEqual((j["req_number"], j["salary_min"], j["work_mode"]), ("R-78", 90000, "remote"))
        self.assertEqual((j["location"], j["posted_at"][:10]), ("Dublin, OH", "2026-11-15"))

    def test_clearing_pay_and_unknown_work_style(self):
        j = self.patch(pay="", work_mode="").get_json()
        self.assertEqual((j["salary_min"], j["salary_text"], j["work_mode"], j["is_remote"]), (None, None, None, 0))

    def test_applied_fills_resume_date_but_keeps_hers(self):
        self.patch(status="applied")
        self.assertRegex(db.get_job(self.jid)["resume_submitted"], r"^\d{4}-\d{2}-\d{2}$")
        self.patch(resume_submitted="2026-10-01", status="interested")
        self.patch(status="applied")
        self.assertEqual(db.get_job(self.jid)["resume_submitted"], "2026-10-01")


class ReloadJobTests(FreshDbTest):
    def test_reload_fills_missing_and_keeps_edits(self):
        db.save_results([{"job_id": "avature:x:1", "job_title": "Business Analyst", "employer_name": "Deloitte",
                          "job_location": "Multiple Locations", "job_apply_link": "https://apply.example/JobDetail/BA/1"}])
        jid = db.list_jobs()[0]["id"]
        db.update_job(jid, {"location": "Columbus, OH"})   # she corrected the location
        fresh = {"job_id": "url:apply.example/JobDetail/BA/1", "job_title": "BA (long internal title)", "employer_name": "Deloitte",
                 "job_location": "Philadelphia, PA", "job_description": "Full description. 401(k) with 5% match.",
                 "job_posted_at_datetime_utc": "2026-10-27"}
        with mock.patch("jobsearch.importer.read_job", return_value=fresh):
            r = self.client.post(f"/api/jobs/{jid}/reload").get_json()
        self.assertEqual(sorted(r["changed"]), ["description", "posted_at"])
        j = db.get_job(jid)
        self.assertEqual((j["title"], j["location"]), ("Business Analyst", "Columbus, OH"))  # title kept, edit kept
        self.assertIn("Full description", j["description"])

    def test_reload_never_blanks_fields(self):
        db.save_results([{"job_id": "a", "job_title": "BA", "employer_name": "Acme", "job_apply_link": "https://a.example/1",
                          "job_description": "Existing text"}])
        jid = db.list_jobs()[0]["id"]
        self.assertEqual(db.refresh_job(jid, {"job_id": "x", "job_title": "BA", "employer_name": "Acme"}), [])
        self.assertEqual(db.get_job(jid)["description"], "Existing text")

    def test_jsonld_takes_longest_description(self):
        page = ('<script type="application/ld+json">{"@type": "JobPosting", "title": "BA", "description": "short"}</script>'
                '<script type="application/ld+json">{"@type": "JobPosting", "datePosted": "2026-10-27", '
                '"description": "' + "a much longer description " * 20 + '"}</script>')
        item = boards.jsonld_posting(page)
        self.assertTrue(item["description"].startswith("a much longer"))
        self.assertEqual((item["title"], item["datePosted"]), ("BA", "2026-10-27"))


class FillGapsTests(unittest.TestCase):
    TEXT = ("We are hiring a Business Analyst to join our Dublin, OH team. This is a hybrid role with three days "
            "in the office. Compensation: $85,000 - $100,000 per year plus bonus. Requisition R-4521. " * 3)

    def run_fill(self, job, answer):
        from jobsearch import importer
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "test", "AI_LISTINGS": "openai"}), mock.patch.object(importer.ai, "json_answer", return_value=json.dumps(answer)):
            return importer.fill_gaps(job)

    def test_fills_only_missing_and_checked_values(self):
        job = {"job_title": "Business Analyst", "job_location": "Multiple Locations", "job_description": self.TEXT,
               "job_posted_at_datetime_utc": "2026-11-01"}
        answer = {"pay": "$85,000 - $100,000 per year", "work_style": "hybrid", "location": "Dublin, OH",
                  "req_number": "R-4521", "posted_date": "2026-10-01", "closing_date": None, "employment_type": "Full time"}
        filled = self.run_fill(job, answer)
        self.assertEqual(sorted(filled), ["job type", "location", "pay", "reference number", "work style"])
        self.assertEqual(job["job_posted_at_datetime_utc"], "2026-11-01")   # site's own value kept
        v = db._job_values({**job, "job_id": "x", "employer_name": "Acme"})
        self.assertEqual((v["salary_min"], v["salary_max"], v["work_mode"], v["work_mode_source"], v["location"]),
                         (85000, 100000, "hybrid", "description", "Dublin, OH"))

    def test_invented_values_are_rejected(self):
        job = {"job_title": "BA", "job_location": "", "job_description": self.TEXT}
        answer = {"pay": "$120,000 - $140,000", "work_style": "remote", "location": "Seattle, WA", "req_number": "X-999"}
        self.assertEqual(self.run_fill(job, answer), [])

    def test_remote_is_not_a_location(self):
        job = {"job_title": "BA", "job_location": None, "job_description": self.TEXT + " Fully remote."}
        filled = self.run_fill(job, {"location": "Remote", "work_style": "remote"})
        self.assertNotIn("location", filled)
        self.assertIsNone(job["job_location"])

    def test_skips_jobs_already_read_by_chatgpt(self):
        self.assertEqual(self.run_fill({"job_description": self.TEXT, "job_read_by_ai": True}, {"pay": "$85,000"}), [])


def make_xlsx(rows, links=None):
    """A minimal .xlsx: rows = {row number: {column letter: text}}, links = {"G6": url}."""
    import io, zipfile
    from xml.sax.saxutils import escape
    links = links or {}
    cells = "".join(
        f'<row r="{n}">' + "".join(f'<c r="{c}{n}" t="inlineStr"><is><t>{escape(str(v))}</t></is></c>'
                                   for c, v in sorted(cols.items())) + "</row>"
        for n, cols in sorted(rows.items()))
    hl = "".join(f'<hyperlink ref="{ref}" r:id="rId{i}"/>' for i, ref in enumerate(links, 1))
    sheet = ('<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
             'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
             f'<sheetData>{cells}</sheetData>' + (f"<hyperlinks>{hl}</hyperlinks>" if hl else "") + "</worksheet>")
    rels = ('<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">' +
            "".join(f'<Relationship Id="rId{i}" Type="hyperlink" Target="{escape(u)}" TargetMode="External"/>'
                    for i, u in enumerate(links.values(), 1)) + "</Relationships>")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("xl/workbook.xml", '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                   'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                   '<sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets></workbook>')
        z.writestr("xl/_rels/workbook.xml.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Id="rId1" Type="worksheet" Target="worksheets/sheet1.xml"/></Relationships>')
        z.writestr("xl/worksheets/sheet1.xml", sheet)
        z.writestr("xl/worksheets/_rels/sheet1.xml.rels", rels)
    return buf.getvalue()


class JobHuntImportTests(FreshDbTest):
    HEADER = {"B": "Status", "C": "Interest", "E": "Company", "F": "Source #", "G": "Job Title - Link to job listing",
              "H": "Salary", "I": "Location", "J": "Remote/Hybrid", "K": "Cov Ltr", "L": "Resume", "N": "Will they contact me & Why?",
              "O": "Response to Resume", "P": "Interview", "Q": "Posted", "S": "Retirement Ranking", "W": "My Notes", "X": "Source"}

    def sheet(self):
        return make_xlsx({
            1: {"B": "Job Applications"},
            4: self.HEADER,
            5: {"B": "Rejected", "C": "Medium", "E": "Acme Ins.", "F": "ID: R20025", "G": "Tech Business Analyst Sr",
                "H": "88-109k", "I": "New Albany OH", "J": "On-Site", "K": "No", "L": "46337", "O": "46346", "P": "N/A",
                "Q": "46330", "S": "2.75", "W": "Bad pay", "X": "Acme Site"},
            6: {"B": "Won't apply", "C": "Med-High", "E": "Beta Co", "F": "?", "G": "Sr BA", "H": "???", "I": "Remote",
                "J": "Remote ok", "Q": "11/05/2026 (Closing 1/05/2027)", "N": "Maybe - strong fit"},
            7: {"B": "Applied", "C": "High", "E": "Gamma", "G": "Product Owner", "H": "105k", "I": "Columbus, OH", "J": "???",
                "K": "Yes", "L": "46343", "X": "LinkedIn"},
        }, links={"G5": "https://acme.example/jobs/R20025", "G7": "https://gamma.example/po"})

    def test_parse_maps_columns(self):
        from jobsearch import jobhunt
        a, b, c = jobhunt.parse(self.sheet())
        self.assertEqual((a["status"], a["interest_level"], a["req_number"], a["salary_min"], a["salary_max"]),
                         ("rejected", "medium", "R20025", 88000, 109000))
        self.assertEqual((a["work_mode"], a["cover_letter"], a["resume_submitted"], a["response_date"], a["posted_at"]),
                         ("onsite", 0, "2026-11-11", "2026-11-20", "2026-11-04"))
        self.assertEqual(a["apply_link"], "https://acme.example/jobs/R20025")
        self.assertIn("Spreadsheet rankings: Retirement 2.75", a["notes"])
        self.assertEqual((b["status"], b["interest_level"], b["req_number"], b["location"], b["work_mode"]),
                         ("not_a_fit", "high", None, None, "remote"))
        self.assertEqual((b["posted_at"], b["closes_at"], b["salary_min"]), ("2026-11-05", "2027-01-05", None))
        self.assertEqual((c["salary_min"], c["work_mode"], c["cover_letter"]), (105000, None, 1))

    def test_import_adds_then_updates_and_links_companies(self):
        cid = db.add_company({"name": "Acme Ins", "ats": "search"})
        r = self.client.post("/api/jobs/import/spreadsheet",
                             data={"file": (io.BytesIO(self.sheet()), "Job Hunt.xlsx")}, content_type="multipart/form-data")
        self.assertEqual(r.get_json(), {"added": 3, "updated": 0, "rows": 3})
        jobs = {j["title"]: j for j in db.list_jobs()}
        acme = db.get_job(jobs["Tech Business Analyst Sr"]["id"])
        self.assertEqual((acme["status"], acme["company_id"], acme["req_number"], acme["found_where"], acme["added_by_hand"]),
                         ("rejected", cid, "R20025", "Acme Site", 1))
        self.assertEqual(sorted(json.loads(acme["edited_fields"])), ["location", "pay", "posted_at", "req_number", "work_mode"])
        gamma = db.get_job(jobs["Product Owner"]["id"])
        self.assertEqual((gamma["status"], gamma["applied_at"], gamma["interest_level"]), ("applied", "2026-11-17", "high"))
        # A second import updates the same jobs instead of adding copies.
        again = self.client.post("/api/jobs/import/spreadsheet",
                                 data={"file": (io.BytesIO(self.sheet()), "Job Hunt.xlsx")}, content_type="multipart/form-data")
        self.assertEqual(again.get_json(), {"added": 0, "updated": 3, "rows": 3})
        self.assertEqual(len(db.list_jobs()), 3)
        self.assertEqual(db.get_job(acme["id"])["notes"].count("Bad pay"), 1)

    def test_rejects_non_spreadsheets(self):
        r = self.client.post("/api/jobs/import/spreadsheet", data={"file": (io.BytesIO(b"hello"), "notes.txt")},
                             content_type="multipart/form-data")
        self.assertEqual(r.status_code, 400)


class AIProviderTests(unittest.TestCase):
    """The ChatGPT / Claude switch in jobsearch/ai.py (no real calls are made)."""

    def block(self, type_, **kw):
        return mock.MagicMock(type=type_, **kw)

    BOTH = {"OPENAI_API_KEY": "o", "ANTHROPIC_API_KEY": "a", "AI_LISTINGS": "", "AI_BENEFITS": ""}

    def test_each_task_uses_its_own_ai(self):
        from jobsearch import ai
        with mock.patch.dict(os.environ, self.BOTH):
            # Defaults: ChatGPT reads listings, Claude does benefits.
            self.assertEqual((ai.name("listings"), ai.name("benefits")), ("ChatGPT", "Claude"))
            with mock.patch.dict(os.environ, {"AI_LISTINGS": "claude", "AI_BENEFITS": "openai"}):
                self.assertEqual((ai.provider("listings"), ai.provider("benefits")), ("claude", "openai"))
            with ai.using("openai"):
                self.assertEqual(ai.provider("benefits"), "openai")
            self.assertEqual(ai.provider("benefits"), "claude")

    def test_falls_back_to_the_key_that_is_there(self):
        from jobsearch import ai
        with mock.patch.dict(os.environ, {**self.BOTH, "ANTHROPIC_API_KEY": ""}):
            self.assertEqual((ai.name("benefits"), ai.has_key("benefits")), ("ChatGPT", True))

    def test_missing_key_is_a_clear_error(self):
        from jobsearch import ai
        with mock.patch.dict(os.environ, {**self.BOTH, "OPENAI_API_KEY": "", "ANTHROPIC_API_KEY": ""}):
            with self.assertRaisesRegex(ai.AIError, "OPENAI_API_KEY"):
                ai.json_answer("x", "y")

    def test_claude_json_answer(self):
        from jobsearch import ai
        client = mock.MagicMock()
        client.messages.create.return_value = mock.MagicMock(
            stop_reason="end_turn",
            content=[self.block("thinking"), self.block("text", text='```json\n{"title": "BA"}\n```')])
        with mock.patch.dict(os.environ, {"AI_LISTINGS": "claude", "AI_BENEFITS": "claude", "ANTHROPIC_API_KEY": "k"}), \
                mock.patch.object(ai, "_anthropic", return_value=client):
            reply = ai.json_answer("Read it", "listing text")
        self.assertEqual(json.loads(ai.first_json_object(reply)), {"title": "BA"})
        kwargs = client.messages.create.call_args.kwargs
        self.assertEqual(kwargs["model"], ai.claude_model())
        self.assertEqual(kwargs["messages"], [{"role": "user", "content": "listing text"}])

    def test_claude_refusal_raises(self):
        from jobsearch import ai
        client = mock.MagicMock()
        client.messages.create.return_value = mock.MagicMock(stop_reason="refusal", content=[])
        with mock.patch.dict(os.environ, {"AI_LISTINGS": "claude", "AI_BENEFITS": "claude", "ANTHROPIC_API_KEY": "k"}), \
                mock.patch.object(ai, "_anthropic", return_value=client):
            with self.assertRaises(ai.AIError):
                ai.json_answer("Read it", "listing text")

    def test_claude_web_answer_continues_after_pause_and_collects_citations(self):
        from jobsearch import ai
        cite = mock.MagicMock(url="https://acme.com/benefits")
        paused = mock.MagicMock(stop_reason="pause_turn", content=[self.block("server_tool_use")])
        done = mock.MagicMock(stop_reason="end_turn", content=[self.block("text", text='{"facts": []}', citations=[cite])])
        client = mock.MagicMock()
        client.beta.messages.create.side_effect = [paused, done]
        with mock.patch.dict(os.environ, {"AI_LISTINGS": "claude", "AI_BENEFITS": "claude", "ANTHROPIC_API_KEY": "k"}), \
                mock.patch.object(ai, "_anthropic", return_value=client):
            text, cited = ai.web_answer("Find benefits", "Acme benefits?")
        self.assertEqual((text, cited), ('{"facts": []}', {"https://acme.com/benefits"}))
        second = client.beta.messages.create.call_args_list[1].kwargs["messages"]
        self.assertEqual([m["role"] for m in second], ["user", "assistant"])

    def test_benefits_web_lookup_uses_the_switch(self):
        from jobsearch import benefits
        answer = {"facts": [{"topic": "pto", "summary": "20 days", "quote": "20 days PTO"}],
                  "figures": {"pto_days": 20}, "figure_quotes": {"pto_days": "20 days PTO"}}
        with mock.patch.object(benefits.ai, "web_answer", return_value=(json.dumps(answer), {"https://acme.com/b"})):
            data = benefits._from_web("Acme")
        self.assertEqual(data["facts"][0]["source_url"], "https://acme.com/b")
        self.assertEqual(data["figures"], {"pto_days": 20})



class SkillsTests(FreshDbTest):
    """Certifications and tools read from listings (the AI is faked)."""

    ANSWERS = {
        "one": {"certs": [{"name": "CBAP certification", "need": "preferred"}],
                "tools": [{"name": "Microsoft Power BI", "need": "required"}, {"name": "Jira", "need": "nice"}]},
        "two": {"certs": [{"name": "Certified Business Analysis Professional", "need": "required"}],
                "tools": [{"name": "PowerBI", "need": "preferred"}, {"name": "SQL", "need": "required"}]},
        "three": {"certs": [], "tools": [{"name": "Excel", "need": "required"}]},
    }

    def fake_answer(self, instructions, text):
        word = text.split()[0]
        return json.dumps(self.ANSWERS[word])

    def add_jobs(self):
        jobs = [board_job(i, f"Analyst {word}") for i, word in enumerate(self.ANSWERS)]
        for j, word in zip(jobs, self.ANSWERS):
            j["job_description"] = f"{word} " + "We need a business analyst. " * 12
        jobs.append(board_job(9, "No text job"))  # description too short to read
        db.save_results(jobs)
        return {j["title"]: j["id"] for j in db.list_jobs()}

    def analyze(self):
        from jobsearch import skills
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "test", "AI_LISTINGS": "openai"}), \
                mock.patch.object(skills.ai, "json_answer", side_effect=self.fake_answer) as fake:
            r = self.client.post("/api/skills/analyze", json={"limit": 20}).get_json()
        return r, fake.call_count

    def test_names_are_grouped_and_need_is_cleaned(self):
        from jobsearch import skills
        items = skills.clean({"certs": [{"name": "CBAP certification", "need": "Preferred"}, "junk"],
                              "tools": [{"name": "Microsoft Power BI", "need": "preferred"},
                                        {"name": "Power BI", "need": "required"}, {"name": "Tableau"}]})
        by_name = {i["name"]: i["need"] for i in items}
        self.assertEqual(by_name, {"CBAP": "preferred", "Power BI": "required", "Tableau": "mentioned"})

    def test_counts_across_all_jobs_and_filter(self):
        ids = self.add_jobs()
        self.client.patch(f"/api/jobs/{ids['Analyst three']}", json={"status": "not_a_fit"})
        r, calls = self.analyze()
        self.assertEqual((r["read"], r["remaining"], calls), (3, 0, 3))
        s = r["summary"]
        self.assertEqual((s["total_jobs"], s["read"], s["no_description"], s["unread"]), (4, 3, 1, 0))
        tools = {g["name"]: g for g in s["tools"]}
        self.assertEqual((tools["Power BI"]["jobs"], tools["Power BI"]["pct"]), (2, 67))
        self.assertEqual((tools["Power BI"]["required"], tools["Power BI"]["preferred"]), (1, 1))
        self.assertEqual(tools["Excel"]["jobs"], 1)                # from the Not a fit job
        self.assertEqual(tools["Jira"]["mentioned"], 1)
        self.assertEqual([(g["name"], g["jobs"]) for g in s["certs"]], [("CBAP", 2)])
        shown = self.client.get("/api/jobs?skill=powerbi").get_json()
        self.assertEqual(sorted(j["title"] for j in shown), ["Analyst one", "Analyst two"])

    def test_listings_are_read_once_until_the_description_changes(self):
        ids = self.add_jobs()
        self.analyze()
        r, calls = self.analyze()
        self.assertEqual((r["read"], calls), (0, 0))
        with db.connect() as conn:
            conn.execute("UPDATE jobs SET description = ? WHERE id = ?",
                         ("three " + "Now Excel and more. " * 15, ids["Analyst three"]))
        r, calls = self.analyze()
        self.assertEqual((r["read"], calls), (1, 1))

    def test_needs_the_listings_key(self):
        self.add_jobs()
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "", "ANTHROPIC_API_KEY": ""}):
            r = self.client.post("/api/skills/analyze", json={})
        self.assertEqual(r.status_code, 400)
        self.assertIn("OPENAI_API_KEY", r.get_json()["error"])


class RankingTests(FreshDbTest):
    """Rank Jobs: every job except Rejected / Not a fit (the AI is faked)."""

    def add_jobs(self):
        long = " Requirements: SQL, Jira, stakeholder interviews." * 10
        jobs = [board_job(1, "BA remote", "Columbus, OH"), board_job(2, "BA hybrid", "Dublin, OH"),
                board_job(3, "BA no pay", "Westerville, OH"), board_job(4, "BA rejected", "Columbus, OH"),
                board_job(5, "BA not a fit", "Columbus, OH")]
        for j in jobs:
            j["job_description"] = j["job_title"] + long
        db.save_results(jobs)
        ids = {j["title"]: j["id"] for j in db.list_jobs()}
        with db.connect() as conn:
            conn.execute("UPDATE jobs SET status='interested', salary_min=110000, salary_max=140000, salary_period='YEAR', "
                         "work_mode='remote' WHERE id=?", (ids["BA remote"],))
            conn.execute("UPDATE jobs SET status='applied', salary_min=55, salary_max=60, salary_period='HOUR', "
                         "work_mode='hybrid' WHERE id=?", (ids["BA hybrid"],))
            conn.execute("UPDATE jobs SET status='rejected' WHERE id=?", (ids["BA rejected"],))
            conn.execute("UPDATE jobs SET status='not_a_fit' WHERE id=?", (ids["BA not a fit"],))
        return ids

    def fake_ai(self, instructions, text, task="listings"):
        self.assertEqual(task, "matching")
        skills = 80 if "BA remote" in text else 60 if "BA hybrid" in text else 40
        return json.dumps({"skills": {"score": skills, "matched": ["SQL"], "partial": [], "missing": ["Jira"], "reason": "ok"},
                           "interests": {"likes": 60, "ok": 0, "minimal": 0, "other": 40, "reason": "fits"}})

    def rank_jobs(self):
        from jobsearch import ranking
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "test", "AI_MATCHING": "openai"}), \
                mock.patch.object(ranking.ai, "json_answer", side_effect=self.fake_ai) as fake:
            r = self.client.post("/api/ranking/score")
        return r, fake.call_count

    def test_salary_rating_uses_her_thresholds(self):
        from jobsearch import ranking
        rate = lambda top, period="YEAR": ranking.salary_rating({"salary_max": top, "salary_period": period})[0]
        self.assertEqual([rate(135000), rate(134999), rate(125000), rate(124999), rate(120000), rate(119999)],
                         [5, 4, 4, 2, 2, 1])
        self.assertEqual(rate(65, "HOUR"), 5)                                  # 65 x 2080 = 135,200
        self.assertEqual(ranking.salary_rating({})[0], 3)                       # no salary listed
        self.assertEqual(ranking.salary_rating({"salary_min": 130000, "salary_period": "YEAR"})[0], 4)

    def test_commute_and_benefit_ratings(self):
        from jobsearch import ranking
        home = (39.96, -82.99)  # Columbus
        rate = lambda job, record=None: ranking.commute_rating(job, home, record)[0]
        self.assertEqual(rate({"work_mode": "remote"}), 4)
        self.assertEqual(rate({"work_mode": "hybrid", "location": "Dublin, OH"}), 2)
        self.assertEqual(rate({"work_mode": "hybrid", "location": "Cleveland, OH"}), 1)
        self.assertEqual(rate({"work_mode": "onsite", "location": "Dublin, OH"}), 1)
        self.assertEqual(rate({"work_mode": "onsite", "location": "Cleveland, OH"}), 0)
        self.assertEqual(rate({"location": "Somewhere"}), 3)                     # no info
        self.assertEqual(rate({"location": "Somewhere"}, {"commute_score": 2.5}), 2.5)
        self.assertEqual(ranking.benefit_rating(None, "retirement_score", "retirement")[0], 2)
        self.assertEqual(ranking.benefit_rating({"retirement_score": 4}, "retirement_score", "retirement")[0], 4)

    def test_rank_jobs_needs_a_resume_and_nothing_scores_on_its_own(self):
        self.add_jobs()
        from jobsearch import ranking
        with mock.patch.object(ranking.ai, "json_answer") as fake:
            self.client.post("/api/profile", json={"resume": "Business analyst, 8 years, SQL", "likes": "Requirements"})
            self.client.get("/api/jobs")
            self.client.get("/api/ranking")
        fake.assert_not_called()                                                # only the button reads listings
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "test", "AI_MATCHING": "openai"}):
            self.client.post("/api/profile", json={"resume": ""})
            r = self.client.post("/api/ranking/score")
        self.assertEqual(r.status_code, 400)
        self.assertIn("resume", r.get_json()["error"])

    def test_ranks_ratings_and_overall(self):
        from jobsearch import ranking
        ids = self.add_jobs()
        self.client.post("/api/profile", json={"resume": "Business analyst, 8 years, SQL", "likes": "Requirements gathering"})
        info = self.client.get("/api/ranking").get_json()
        self.assertEqual((info["count"], info["needs_ai"]), (3, 3))            # Rejected / Not a fit left out
        r, calls = self.rank_jobs()
        self.assertEqual((r.status_code, r.get_json()["read"], calls), (200, 3, 3))

        jobs = {j["title"]: j for j in self.client.get("/api/jobs?sort=rank").get_json()}
        self.assertNotIn("rank", jobs["BA rejected"])
        self.assertNotIn("rank", jobs["BA not a fit"])
        remote, hybrid, no_pay = jobs["BA remote"]["rank"], jobs["BA hybrid"]["rank"], jobs["BA no pay"]["rank"]
        self.assertEqual((remote["qualify"], hybrid["qualify"], no_pay["qualify"]), (1, 2, 3))
        self.assertEqual((remote["interest"], hybrid["interest"]), (1, 1))     # same interest score: tied
        self.assertEqual((remote["salary"], hybrid["salary"], no_pay["salary"]), (5, 2, 3))   # 60/hr = 124,800
        self.assertEqual(remote["overall"], 1)
        ordered = self.client.get("/api/jobs?sort=rank").get_json()
        self.assertEqual(ordered[0]["title"], "BA remote")
        # "Hide not a fit & rejected" leaves both out; their own tabs still show them.
        shown = {j["title"] for j in self.client.get("/api/jobs?sort=rank&hide_not_fit=1").get_json()}
        self.assertEqual(shown, {"BA remote", "BA hybrid", "BA no pay"})
        tab = self.client.get("/api/jobs?status=rejected&hide_not_fit=1").get_json()
        self.assertEqual([j["title"] for j in tab], ["BA rejected"])
        self.assertIn("BA rejected", {j["title"] for j in self.client.get("/api/jobs").get_json()})

        m = self.client.get(f"/api/jobs/{ids['BA remote']}/match").get_json()
        # Qualify and Interest count by rank: ranked 1st of 3 = 1.0 (Interest is a three-way tie for 1st).
        expected = (3 * 1.0 + 3 * 1.0 + 2 * 1.0 + 2 * (1 / 3) + 1 * (1 / 3) + 1 * 1.0 + 1 * (1 / 3)) / 13
        self.assertEqual(m["overall"], round(100 * expected))
        self.assertEqual(m["parts"]["commute"]["score"], 4)
        self.assertEqual(m["missing"], ["Jira"])
        self.assertEqual(m["weights_order"][:2], ["qualify", "interest"])

        # Her own Qualify score wins; out-of-range is refused; nothing to redo until the resume changes.
        m = self.client.patch(f"/api/jobs/{ids['BA no pay']}/match", json={"qualify": 100}).get_json()
        self.assertEqual((m["parts"]["qualify"]["score"], m["parts"]["qualify"]["source"], m["parts"]["qualify"]["rank"]),
                         (100, "yours", 1))
        self.assertEqual(self.client.patch(f"/api/jobs/{ids['BA no pay']}/match", json={"qualify": 101}).status_code, 400)
        self.assertEqual(self.rank_jobs()[1], 0)
        self.client.post("/api/profile", json={"resume": "Business analyst, 9 years, SQL, Jira"})
        self.assertEqual(self.client.get("/api/ranking").get_json()["needs_ai"], 3)

    def test_resume_from_word_file(self):
        from jobsearch import ranking_files
        body = ('<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
                '<w:p><w:r><w:t>Jane Doe</w:t></w:r></w:p><w:p><w:r><w:t>Business Analyst</w:t></w:r></w:p>'
                '</w:body></w:document>')
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("word/document.xml", body)
        self.assertEqual(ranking_files.resume_text("r.docx", buf.getvalue()), "Jane Doe\nBusiness Analyst")
        with self.assertRaisesRegex(ValueError, "paste"):
            ranking_files.resume_text("r.pdf", b"%PDF")


class DescriptionTests(FreshDbTest):
    """Jobs with no description: filter, paste it in by hand, and keep it."""

    TEXT = "We are hiring a business analyst to gather requirements and run UAT. " * 6

    def setUp(self):
        super().setUp()
        empty, full = board_job(1, "No text"), board_job(2, "Has text")
        empty["job_description"], full["job_description"] = None, self.TEXT
        db.save_results([empty, full])
        self.ids = {j["title"]: j["id"] for j in db.list_jobs()}

    def test_filter_and_flag(self):
        listed = {j["title"]: j["has_description"] for j in self.client.get("/api/jobs").get_json()}
        self.assertEqual(listed, {"No text": 0, "Has text": 1})
        only = self.client.get("/api/jobs?no_description=1").get_json()
        self.assertEqual([j["title"] for j in only], ["No text"])

    def test_pasted_description_is_kept(self):
        jid = self.ids["No text"]
        r = self.client.patch(f"/api/jobs/{jid}", json={"description": "Pasted:\r\n\r\n\r\n" + self.TEXT})
        self.assertEqual(r.status_code, 200)
        job = db.get_job(jid)
        self.assertTrue(job["description"].startswith("Pasted:\n\n"))
        self.assertIn("description", json.loads(job["edited_fields"]))
        # Reload from listing doesn't replace what she pasted...
        fresh = board_job(1, "No text")
        fresh["job_description"] = "From the site " * 30
        db.refresh_job(jid, fresh)
        self.assertTrue(db.get_job(jid)["description"].startswith("Pasted:"))
        # ...and neither does a search that finds the job again.
        again = board_job(1, "No text")
        again["job_description"] = "Search result text " * 30
        db.save_results([again])
        self.assertTrue(db.get_job(jid)["description"].startswith("Pasted:"))
        self.assertEqual(self.client.get("/api/jobs?no_description=1").get_json(), [])

    def test_search_without_description_keeps_the_saved_one(self):
        jid = self.ids["Has text"]
        again = board_job(2, "Has text")
        again["job_description"] = ""
        db.save_results([again])
        self.assertEqual(db.get_job(jid)["description"], self.TEXT)

    def test_too_long_is_refused(self):
        r = self.client.patch(f"/api/jobs/{self.ids['No text']}", json={"description": "x" * 50001})
        self.assertEqual(r.status_code, 400)


class AIPackageTests(unittest.TestCase):
    """A laptop with the key but not the package (before requirements are reinstalled)."""

    BOTH = {"OPENAI_API_KEY": "o", "ANTHROPIC_API_KEY": "a", "AI_LISTINGS": "", "AI_BENEFITS": "", "AI_MATCHING": ""}

    def test_missing_package_falls_back_to_the_other_ai(self):
        from jobsearch import ai
        with mock.patch.dict(os.environ, self.BOTH), \
                mock.patch.object(ai, "package_installed", side_effect=lambda p: p != "claude"):
            self.assertEqual(ai.name("benefits"), "ChatGPT")

    def test_clear_message_when_nothing_is_usable(self):
        from jobsearch import ai
        env = {**self.BOTH, "OPENAI_API_KEY": ""}
        with mock.patch.dict(os.environ, env), mock.patch.object(ai, "package_installed", return_value=False):
            with self.assertRaisesRegex(ai.AIError, "setup.bat"):
                ai.web_answer("x", "y")


class InterestsMixTests(unittest.TestCase):
    """Interests score = the job's mix of work, weighted by which of her lists it falls in."""

    def test_mix_is_weighted_by_list(self):
        from jobsearch import ranking
        p = {"likes": "Analysis", "ok": "KPIs", "minimal": "Managing employees", "interests": ""}
        self.assertEqual(ranking.interests_from_mix({"likes": 100}, p)[0], 100)
        self.assertEqual(ranking.interests_from_mix({"minimal": 100}, p)[0], 0)
        score, mix = ranking.interests_from_mix({"likes": 2, "ok": 1, "minimal": 1, "other": 0}, p)  # not out of 100
        self.assertEqual(mix, {"likes": 50, "ok": 25, "minimal": 25, "other": 0})
        self.assertAlmostEqual(score, 50 + 0.25 * 55)
        # A mostly product-owner / manager job scores well below a mostly-analysis one.
        po = ranking.interests_from_mix({"likes": 20, "ok": 50, "minimal": 30}, p)[0]
        ba = ranking.interests_from_mix({"likes": 80, "ok": 20}, p)[0]
        self.assertLess(po, 50)
        self.assertGreater(ba, 85)

    def test_other_preferences_count_a_fifth(self):
        from jobsearch import ranking
        p = {"likes": "Analysis", "ok": "", "minimal": "", "interests": "Healthcare"}
        score, _ = ranking.interests_from_mix({"likes": 100, "prefs_score": 0}, p)
        self.assertAlmostEqual(score, 80)
        self.assertEqual(ranking.interests_from_mix({}, p), (None, None))


class MatchingModelTests(unittest.TestCase):
    """Job scoring uses Claude Sonnet (with server-side fallback) unless .env says otherwise."""

    def test_matching_defaults_to_sonnet_with_fallback(self):
        from jobsearch import ai
        client = mock.MagicMock()
        client.beta.messages.create.return_value = mock.MagicMock(
            stop_reason="end_turn", content=[mock.MagicMock(type="text", text='{"skills": {}}')])
        env = {"OPENAI_API_KEY": "o", "ANTHROPIC_API_KEY": "a", "AI_MATCHING": "", "CLAUDE_MATCHING_MODEL": ""}
        with mock.patch.dict(os.environ, env), mock.patch.object(ai, "package_installed", return_value=True), \
                mock.patch.object(ai, "_anthropic", return_value=client):
            self.assertEqual(ai.name("matching"), "Claude")
            ai.json_answer("x", "y", task="matching")
        kwargs = client.beta.messages.create.call_args.kwargs
        self.assertEqual((kwargs["model"], kwargs["fallbacks"]), ("claude-sonnet-5-5", "default"))
        client.messages.create.assert_not_called()


class ProfileFilesTests(FreshDbTest):
    """profile/Resume.docx and profile/interests.txt fill the Resume & interests card."""

    INTERESTS = """# notes are ignored
[Work she likes] 90
Requirements gathering
Root cause analysis

[Fine in small doses] 50
KPI analysis

[Keep to a minimum] 10
Managing employees

[Not on her lists] 30

[Anything else]
Healthcare

[Rank Jobs weights]
Qualify: 4
Interest = 2
Sick & PTO: 0
"""

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        with open(os.path.join(self.tmp.name, "interests.txt"), "w", encoding="utf-8") as f:
            f.write(self.INTERESTS)
        with open(os.path.join(self.tmp.name, "resume.txt"), "w", encoding="utf-8") as f:
            f.write("Jane Doe\nNewark, OH | Mobile: 555-123-4567 | Email: jane@example.com\nBusiness Analyst, SQL")
        self.env = mock.patch.dict(os.environ, {"JOBSEARCH_PROFILE_DIR": self.tmp.name})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_file_is_parsed(self):
        from jobsearch import profile_files
        d = profile_files.read_interests()
        self.assertEqual(d["likes"], "Requirements gathering\nRoot cause analysis")
        self.assertEqual((d["likes_weight"], d["ok_weight"], d["minimal_weight"], d["other_weight"]), ("90", "50", "10", "30"))
        self.assertEqual(d["interests"], "Healthcare")
        self.assertEqual((d["weight_qualify"], d["weight_interest"], d["weight_pto"]), ("4", "2", "0"))
        self.assertNotIn("weight_salary", d)                                   # not listed: default stays

    def test_empty_card_fills_from_files_and_load_replaces(self):
        p = self.client.get("/api/profile").get_json()
        self.assertTrue(p["resume"].startswith("Jane Doe"))
        self.assertEqual((p["likes_weight"], p["weight_qualify"]), ("90", "4"))
        self.assertEqual(p["weight_defaults"]["weight_salary"], 2)
        # Her own edits aren't overwritten by a later visit...
        self.client.post("/api/profile", json={"likes": "Edited by hand"})
        self.assertEqual(self.client.get("/api/profile").get_json()["likes"], "Edited by hand")
        # ...until she clicks Load from profile files.
        p = self.client.post("/api/profile/load_files").get_json()
        self.assertEqual(p["likes"], "Requirements gathering\nRoot cause analysis")
        self.assertIn("resume", p["copied"])

    def test_weights_change_scores_without_reading_listings_again(self):
        from jobsearch import ranking
        profile = {"likes": "x", "ok": "", "minimal": "", "interests": ""}
        mix = {"likes": 50, "ok": 0, "minimal": 0, "other": 50}
        self.assertEqual(ranking.interest_score(mix, None, profile), 50 + 50 * 0.35)
        self.assertEqual(ranking.interest_score(mix, None, {**profile, "other_weight": "0"}), 50)
        weights = dict((n, w) for n, _, w in ranking.criteria_weights({"weight_qualify": "5", "weight_bonus": ""}))
        self.assertEqual((weights["qualify"], weights["interest"], weights["bonus"]), (5, 3, 1))

    def test_contact_details_are_not_sent_to_the_ai(self):
        from jobsearch import ranking
        text = ranking.resume_for_ai("Jane Doe\nNewark, OH | Mobile: 555-123-4567 | Email: jane@example.com\nSQL")
        self.assertEqual(text, "Jane Doe\nNewark, OH\nSQL")

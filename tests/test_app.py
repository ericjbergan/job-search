"""Tests for storage, company boards, and the Flask API. Run: python -m unittest

No network: JSearch and job-board calls are replaced with fixtures.
"""

import json
import shutil
import sqlite3
import subprocess
import os
import tempfile
import unittest
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


@unittest.skipUnless(shutil.which("git"), "git not installed")
class BackupTests(unittest.TestCase):
    """Back up commits jobs.db and pushes it, using a throwaway local remote."""

    def test_backup_commits_and_pushes(self):
        with tempfile.TemporaryDirectory() as tmp:
            remote, clone = os.path.join(tmp, "remote.git"), os.path.join(tmp, "clone")
            run = lambda *a, cwd=tmp: subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True)
            run("init", "--bare", "-b", "main", remote)
            run("clone", remote, clone)
            run("config", "user.email", "test@example.com", cwd=clone)
            run("config", "user.name", "Test", cwd=clone)
            run("commit", "--allow-empty", "-m", "init", cwd=clone)
            run("push", "-u", "origin", "main", cwd=clone)

            db_file = os.path.join(clone, "jobs.db")
            with mock.patch.object(db, "REPO_DIR", clone), mock.patch.dict(os.environ, {"JOBSEARCH_DB": db_file}):
                db.init_db(seed_path=None)
                db.save_results(sample_results()[:2])
                client = app_module.app.test_client()
                first = client.post("/api/backup").get_json()
                second = client.post("/api/backup").get_json()

            self.assertEqual(first, {"ok": True, "committed": True})
            self.assertEqual(second, {"ok": True, "committed": False})
            log = subprocess.run(["git", "log", "--oneline", "main"], cwd=remote, capture_output=True, text=True).stdout
            self.assertIn("Back up job search data", log)


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
            {**base, "job_id": "1", "job_min_salary": 60000, "job_max_salary": 70000, "job_salary_period": "YEAR"},
            {**base, "job_id": "2", "job_min_salary": 50, "job_max_salary": 60, "job_salary_period": "HOUR"},  # ~$125k
            {**base, "job_id": "3"},
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
            {**base, "job_id": "1", "job_work_mode": "Hybrid"},
            {**base, "job_id": "2", "job_location": "Ohio Remote"},
            {**base, "job_id": "3", "job_description": "This role is onsite in Columbus. Must work onsite daily."},
            {**base, "job_id": "4"},
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
        fake = mock.MagicMock()
        fake.chat.completions.create.return_value.choices = [mock.MagicMock(message=mock.MagicMock(content=json.dumps(answer)))]
        with mock.patch.object(benefits, "_client", return_value=fake):
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
        fake = mock.MagicMock()
        fake.chat.completions.create.return_value.choices = [mock.MagicMock(message=mock.MagicMock(content=json.dumps(answer)))]
        with mock.patch.object(benefits, "_client", return_value=fake):
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
    def add_job(self, job_id, company, link):
        db.save_results([{"job_id": job_id, "job_title": "Business Analyst", "employer_name": company,
                          "job_apply_link": link, "job_publisher": "LinkedIn"}])
        return next(j["id"] for j in db.list_jobs() if j["company"] == company)

    def test_save_from_job_board_listing(self):
        jid = self.add_job("a", "Startup Corp", "https://www.linkedin.com/jobs/view/123")
        self.add_job("b", "Startup Corp.", "https://www.linkedin.com/jobs/view/456")  # same employer, other spelling
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

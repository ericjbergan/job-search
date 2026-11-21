"""Tests for storage, company boards, and the Flask API. Run: python -m unittest

No network: JSearch and job-board calls are replaced with fixtures.
"""

import json
import shutil
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

        feed = [board_job(1, "Java Developer"), board_job(2, "Office Manager")]
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
        self.assertEqual(osu["keywords"], "business analyst")
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

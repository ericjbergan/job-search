# Job Search

A small Flask app for running job searches on demand and tracking the results in SQLite.

- **Searches** go through the [JSearch API](https://www.openwebninja.com/api/jsearch) (Google for Jobs results, which include LinkedIn, ZipRecruiter, Monster, company sites and more). Save a search and re-run it with one click; jobs already saved are skipped and new ones are flagged.
- **Companies** you're watching are searched directly on their own careers site (free, no API quota) using the same search their careers page uses. Supported platforms: Workday, Oracle, UKG, ADP, Eightfold, iCIMS (Jibe), Radancy, Phenom, Rippling, Amazon, Greenhouse, Lever and Ashby. Results are filtered by title keywords and locations. Companies on other platforms fall back to a JSearch "*keywords* at *company*" query, which uses API quota.
- **Starter data:** a brand-new database is loaded once from `seed/seed.json` — 56 Columbus-area companies (priority, Glassdoor rating, notes) and two saved Business Analyst searches.
- **Benefits:** each job shows the benefits its posting mentions (401(k) match, PTO days, pension, bonus, tuition, parental leave...). A **Benefits** panel — from any job, even if its company isn't saved, or from the company list — holds Retirement / Sick & PTO / Commute / Bonus scores on the spreadsheet's 1-4 scales plus notes. **Look up benefits** (needs `OPENAI_API_KEY`) follows "benefits" links from the listing and careers site and has ChatGPT pull out facts, keeping each one's quote and source; quotes are checked against the page. When no page can be found it uses ChatGPT web search instead (marked "verify"). Suggested scores never overwrite the ones you've entered.
- **Tracking:** every job gets a status (new, interested, applied, interviewing, offer, rejected, not a fit) and notes. Re-running a search never overwrites them.
- **Back up** commits `jobs.db` and pushes it to GitHub.

## Setup (Windows)

1. Install Python 3 (with "Add to PATH" checked).
2. Clone the repo and double-click `setup.bat`. It installs the requirements, creates `.env`, and puts a **Job Search** shortcut on the desktop.
3. Open `.env` and paste your JSearch key after `JSEARCH_API_KEY=`.
4. Double-click **Job Search** on the desktop. The app opens at http://localhost:5001.

The database (`jobs.db`) is created on first run and loaded with the starter companies and searches. The server reloads itself when Python files change, so refreshing the page is enough to pick up edits.

## ChatGPT or Claude

Each AI step uses the service that did it best in a side-by-side test:

- **Reading listings** (pasted text, filling in missing details): ChatGPT (`OPENAI_API_KEY`). The two were about even.
- **Benefits lookups** (benefits pages and web search): Claude (`ANTHROPIC_API_KEY`). It found real figures with quotes.

Change either in `.env` with `AI_LISTINGS=openai|claude` and `AI_BENEFITS=openai|claude`. If the chosen
service's key isn't in `.env`, the other one is used, so a laptop with only an OpenAI key still works.
Optional model settings: `OPENAI_MODEL` (default gpt-4o-mini), `CLAUDE_MODEL` (default claude-haiku-5-5,
reads text) and `CLAUDE_WEB_MODEL` (default claude-sonnet-5-5, benefits web search).

To compare the two on the same saved jobs (nothing saved is changed), with both keys in `.env`:

    python compare_ai.py --jobs 10 --benefits "Cardinal Health"

This writes `ai-comparison.html` (git-ignored, since it holds job data) and opens it.

## API quota

JSearch's free plan has a small monthly allowance with a hard cap (no overage charges). Each search page (~10 jobs) uses one request; the header shows how many pages you've used this month. Direct careers-site checks don't use quota. **Check all** always checks the direct companies and asks before including the JSearch ones (yellow tag).

## Adding a company

Click **+ Add** under Companies and paste the careers page URL. These are recognized automatically:

| Platform | Example URL |
|---|---|
| Workday | `https://osu.wd1.myworkdayjobs.com/OSUCareers` |
| Oracle | `https://jpmc.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001/jobs` |
| UKG | `https://recruiting.ultipro.com/PAR1025PNATB/JobBoard/<board-id>/` |
| ADP Workforce Now | `https://workforcenow.adp.com/...recruitment.html?cid=<cid>` |
| Eightfold | `https://<company>.eightfold.ai/careers?domain=<company>.com` |
| Rippling | `https://ats.rippling.com/joinroot/jobs` |
| Greenhouse / Lever / Ashby | `boards.greenhouse.io/stripe`, `jobs.lever.co/spotify`, `jobs.ashbyhq.com/ramp` |

Many companies put their own domain in front (e.g. `careers.nationwide.com` is Workday underneath). For those, and for iCIMS/Jibe, Radancy and Phenom sites, pick the **Job board** type by hand and fill in the **Board ID** (the form shows the expected format). Anything else uses JSearch.

**Title keywords** and **locations** are comma-separated. A job must have one of the keywords in its title; locations match anywhere in the job's location text ("dublin oh" matches "Dublin, Ohio" but not Dublin, Ireland). US-remote jobs always pass, and jobs listed only as "Multiple locations" are kept so you can look.

These connectors call the same endpoints the companies' own search pages use; they aren't official APIs, so a redesign can break one. A failed check shows "couldn't check *company*" — switch that company to JSearch or fix its board ID.

## Command line

```
python app.py                 # start the web UI on port 5001
python -m unittest            # run the tests (no network needed)
```

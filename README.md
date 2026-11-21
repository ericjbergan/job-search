# Job Search

A small Flask app for running job searches on demand and tracking the results in SQLite.

- **Searches** go through the [JSearch API](https://www.openwebninja.com/api/jsearch) (Google for Jobs results, which include LinkedIn, ZipRecruiter, Monster, company sites and more). Save a search and re-run it with one click; jobs already saved are skipped and new ones are flagged.
- **Companies** you're watching are checked directly on their Greenhouse, Lever or Ashby job board (free, no API quota), filtered by title keywords and locations. Companies on other platforms fall back to a JSearch "jobs at *company*" query.
- **Tracking:** every job gets a status (new, interested, applied, interviewing, offer, rejected, not a fit) and notes. Re-running a search never overwrites them.
- **Back up** commits `jobs.db` and pushes it to GitHub.

## Setup (Windows)

1. Install Python 3 (with "Add to PATH" checked).
2. Clone the repo and double-click `setup.bat`. It installs the requirements, creates `.env`, and puts a **Job Search** shortcut on the desktop.
3. Open `.env` and paste your JSearch key after `JSEARCH_API_KEY=`.
4. Double-click **Job Search** on the desktop. The app opens at http://localhost:5001.

The database (`jobs.db`) is created empty on first run. The server reloads itself when Python files change, so refreshing the page is enough to pick up edits.

## API quota

JSearch's free plan has a small monthly allowance with a hard cap (no overage charges). Each search page (~10 jobs) uses one request; the header shows how many pages you've used this month. Company board checks on Greenhouse, Lever and Ashby don't use quota.

## Adding a company

Click **+ Add** under Companies and paste the careers page URL. Board URLs are recognized automatically:

| Platform | Example URL |
|---|---|
| Greenhouse | `https://boards.greenhouse.io/stripe` or `https://job-boards.greenhouse.io/stripe` |
| Lever | `https://jobs.lever.co/spotify` |
| Ashby | `https://jobs.ashbyhq.com/ramp` |

Anything else (or no URL) uses JSearch. Title keywords and locations are comma-separated; remote jobs always pass the location filter.

## Command line

```
python app.py                 # start the web UI on port 5001
python -m unittest            # run the tests (no network needed)
```

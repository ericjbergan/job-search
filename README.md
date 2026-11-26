# Job Search

A small Flask app for running job searches on demand and tracking the results in SQLite.

- **Searches** go through the [JSearch API](https://www.openwebninja.com/api/jsearch) (Google for Jobs results, which include LinkedIn, ZipRecruiter, Monster, company sites and more). Save a search and re-run it with one click; jobs already saved are skipped and new ones are flagged.
- **Companies** you're watching are searched directly on their own careers site (free, no API quota) using the same search their careers page uses. Supported platforms: Workday, Oracle, UKG, ADP, Eightfold, iCIMS (Jibe), Radancy, Phenom, Rippling, Amazon, Greenhouse, Lever and Ashby. Results are filtered by title keywords and locations. Companies on other platforms fall back to a JSearch "*keywords* at *company*" query, which uses API quota.
- **Starter data:** a brand-new database is loaded once from `seed/seed.json` — 56 Columbus-area companies (priority, Glassdoor rating, notes) and two saved Business Analyst searches.
- **Benefits:** each job shows the benefits its posting mentions (401(k) match, PTO days, pension, bonus, tuition, parental leave...). A **Benefits** panel — from any job, even if its company isn't saved, or from the company list — holds Retirement / Sick & PTO / Commute / Bonus scores on the spreadsheet's 1-4 scales plus notes. **Look up benefits** (needs `OPENAI_API_KEY`) follows "benefits" links from the listing and careers site and has ChatGPT pull out facts, keeping each one's quote and source; quotes are checked against the page. When no page can be found it uses ChatGPT web search instead (marked "verify"). Suggested scores never overwrite the ones you've entered.
- **Tracking:** every job gets a status (new, interested, applied, interviewing, offer, rejected, not a fit) and notes. Re-running a search never overwrites them.
- **Back up** saves a dated copy of `jobs.db` (`jobs-YYYY-MM-DD.db`, one per day) to a **Job Search backups** folder in OneDrive, which syncs it off the computer. Set `BACKUP_DIR` in `.env` to use a different folder. The database is never pushed to GitHub: the repo is public.

## Setup (Windows)

1. Install Python 3 (with "Add to PATH" checked).
2. Clone the repo and double-click `setup.bat`. It installs the requirements, creates `.env`, and puts a **Job Search** shortcut on the desktop.
3. Open `.env` and paste your JSearch key after `JSEARCH_API_KEY=`.
4. Double-click **Job Search** on the desktop. The app opens at http://localhost:5001.

The database (`jobs.db`) is created on first run and loaded with the starter companies and searches. The server reloads itself when Python files change, so refreshing the page is enough to pick up edits.

## Ranking jobs

Paste her resume (or load a .docx/.txt) and her interests (three lists: work she likes, fine in small doses,
keep to a minimum) in the **Resume & interests** card, then click **Rank Jobs** above the job list. Every
job except Applied, Rejected and Not a fit gets rank numbers, shown in the middle of each job card:

- **Qualify** and **Interest**: Claude Sonnet compares each listing with her resume and interest lists
  (only when she clicks Rank Jobs, never on its own), and the jobs are ranked 1 = best. Click the label
  for the Qualify / Interest Rank Summary (what she meets or is missing; the job's mix of work).
- **Salary Rating** on the top of the pay range: 5 = $135k+, 4 = $125-135k, 3 = not listed,
  2 = $120-125k, 1 = under $120k.
- **Retirement, Sick & PTO, Bonus**: the company's 1-4 ratings from the Benefits panel (2 = no info);
  **Commute**: the spreadsheet's 0-4 scale from work style and distance.
- **Overall**: Qualify ×3, Interest ×3, Salary ×2, Retirement ×2, Sick & PTO ×1, Commute ×1, Bonus ×1.
  Qualify and Interest count by their rank, so they carry their full weight. Sort by **Overall rank**.

Opening a job shows the full breakdown, where she can also set her own Qualify / Interest score.
The resume and interests stay in her local database.

## Filling in applications

Fill in the **Application info** card once (name, contact details, work authorization, salary answer,
voluntary EEO answers). Then open a job and click **Fill application**: the app opens the job's link in
its own Google Chrome window (Edge if Chrome is missing). Click Apply there and sign in if the site asks (sign-ins are remembered in
a git-ignored `.browser-jobs` folder). On each page of the form, click **Fill this page** in the panel at the
bottom right of the app:

- her saved info goes in directly (outlined green), and `profile/Resume.docx` is uploaded where a resume is asked for;
- the other questions are answered by Claude Sonnet from her resume and the listing (outlined orange: check these);
- passwords, Social Security numbers, birth dates, agreement boxes and text-message opt-ins are left for her;
- it never submits. She checks the page, goes on to the next, and submits herself.

Phone, email, street and ZIP are typed in directly and never sent to the AI. Sites differ a lot, so some
fields (especially Workday's) may be missed; the panel lists what still needs her.

## Tailoring her resume to a job

Open a job and click **Tailor resume**. Claude Sonnet suggests changes to `profile/Resume.docx` for that
listing: a summary that leads with what the job cares about, bullets reworded and reordered within each job,
and skills lines reordered. Each change shows before (gray) and after (green); she ticks the ones she wants
and clicks **Save tailored resume**. That saves a Word copy, same layout, in OneDrive's
`Job Search tailored resumes` folder (or `TAILORED_DIR`), named `Resume - Company - Title.docx`, and
**Fill application** uploads that copy for that job. Her own resume file is never changed.

It stays within her real experience: names, contact details, employers, titles and dates are never
changed, a skills line keeps exactly the same items, and any tool, name or number in a suggestion that isn't
in her resume is flagged in red and starts unticked. What the listing asks for that her resume doesn't show
is listed as gaps. Suggestions are saved per job, so reopening costs nothing until the resume or listing changes.

## ChatGPT or Claude

Each AI step uses the service that did it best in a side-by-side test:

- **Reading listings** (pasted text, filling in missing details): ChatGPT (`OPENAI_API_KEY`). The two were about even.
- **Benefits lookups** (benefits pages and web search): Claude (`ANTHROPIC_API_KEY`). It found real figures with quotes.
- **Scoring Interested jobs** (resume and interests vs the listing): Claude Sonnet. It read her resume accurately and judged each job's mix of work; ChatGPT missed tools on her resume.
- **Application questions and Tailor resume**: Claude Sonnet, writing from her resume.

Change any of them in `.env` with `AI_LISTINGS`, `AI_BENEFITS`, `AI_MATCHING` or `AI_APPLICATIONS` (`openai` or `claude`). If the chosen
service's key isn't in `.env`, the other one is used, so a laptop with only an OpenAI key still works.
Optional model settings: `OPENAI_MODEL` (default gpt-4o-mini), `CLAUDE_MODEL` (default claude-haiku-5-5,
reads text) and `CLAUDE_WEB_MODEL` (default claude-sonnet-5-5, benefits web search) and `CLAUDE_MATCHING_MODEL` (default claude-sonnet-5-5, job scoring and application answers).
`APPLY_BROWSER` picks the Fill application browser: `chrome` (default), `msedge` or `chromium`.

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

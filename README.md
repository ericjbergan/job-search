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

Her name and contact details never go to the AI. The name and contact lines at the top of her resume are left
out (name, address, phone, email, LinkedIn), and her name, emails, phone numbers and web addresses are taken out
of the rest. Tailor resume does the same. Her own resume file keeps them all.

Opening a job shows the full breakdown, where she can also set her own Qualify / Interest score.
The resume and interests stay in her local database.

## Tailoring her resume to a job

Open a job and click **Tailor resume**. It starts from the Word resume she last loaded with
**Load resume file…** (kept in the database), or `profile/Resume.docx` if she hasn't loaded one. Claude Sonnet
reads it against the listing, and the window goes through her resume section by section:

- **Professional Summary** (gray): current, suggested (new words in bold) and a summary of the changes.
- **Skills sections** (green: Key Skills, Common Artifacts Produced, Recent Tools & Technology): current,
  suggested, what was added and removed, and a summary of the other changes (such as why the order changed).
- **Experience**: one colored part per job (by its order on her resume: orange, teal, blue, purple, then more
  colors). It shows only the bullets worth changing, numbered as on her resume. Each has the ATS keywords it adds
  and the listing's qualifications it addresses.

Each has a box that starts with the suggestion. She edits it, then clicks **Save to New Resume**. A saved box turns
black on light gray; if she edits it again, it goes back to gray on white until she saves again. Saved boxes are
kept per job, so she can close the window and pick up later.

The header stays at the top while she scrolls. It has **go to listing** (opens the listing in a new window),
**ask again** (new suggestions; her saved boxes stay) and **Preview New Resume**. The preview shows the whole
new resume as text with her saved boxes highlighted, then **Still Missing**: the ATS keywords that aren't in the
new resume, and the listing's qualifications that her resume doesn't show and no saved bullet covers yet.
**Save** downloads `<Company>-<Job Title> - Suggested Resume.docx` (same layout as hers) to her Downloads
folder; **Continue editing** goes back with everything kept. Her own resume file is never changed.

The AI's answer is checked before it's shown. Keywords must appear in the listing and be missing from her
resume. Qualifications must be copied from the listing. A bullet only claims the keywords its new text really
adds. Words new to her resume are flagged in red, so she keeps only what's true. Names, contact details,
headings, employers, titles and dates can't be changed. Suggestions are kept per job, so reopening costs
nothing until the resume or listing changes.

## ChatGPT or Claude

Each AI step uses the service that did it best in a side-by-side test:

- **Reading listings** (pasted text, filling in missing details): ChatGPT (`OPENAI_API_KEY`). The two were about even.
- **Benefits lookups** (benefits pages and web search): Claude (`ANTHROPIC_API_KEY`). It found real figures with quotes.
- **Scoring Interested jobs** (resume and interests vs the listing): Claude Sonnet. It read her resume accurately and judged each job's mix of work; ChatGPT missed tools on her resume.
- **Tailor resume**: Claude Sonnet, suggesting summary, skills and bullet changes for a listing.

Change any of them in `.env` with `AI_LISTINGS`, `AI_BENEFITS`, `AI_MATCHING` or `AI_APPLICATIONS` (Tailor resume) (`openai` or `claude`). If the chosen
service's key isn't in `.env`, the other one is used, so a laptop with only an OpenAI key still works.
Optional model settings: `OPENAI_MODEL` (default gpt-4o-mini), `CLAUDE_MODEL` (default claude-haiku-5-5,
reads text) and `CLAUDE_WEB_MODEL` (default claude-sonnet-5-5, benefits web search) and `CLAUDE_MATCHING_MODEL` (default claude-sonnet-5-5, job scoring and Tailor resume).

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

In the company list, the company's **name** is a link that opens its careers page in a new window; clicking anywhere else on the row still filters the jobs to that company. A JSearch company keeps its careers page URL just for this link. The careers links Jen listed (`careers_links` in `seed/seed.json`) are filled in once on each database, only where a company's URL was blank.

Above the company list, **Filter:** narrows it by how each company is searched:
- **JSearch:** shows only the companies searched through JSearch. Those are worth checking by hand through their name link.
- **Career page:** shows only the companies whose careers site the app searches directly, and ticks them for **Check selected**, which is free.
- **none:** shows them all again and clears the ticks.

**Reset** also turns the filter off, but leaves the ticks alone.

## Missing information

The **Missing information** checkbox above the job list shows New, Interested and Applied jobs that have no
description, or whose company is missing any of its benefit ratings (Retirement, PTO, Commute, Bonus). Other
statuses never show while it's checked. Badges say what's missing ("No description", "No Commute rating",
"No benefits ratings"). Fill descriptions with **Reload these from their listings**, and ratings with the
company's **Benefits** button.

**Title keywords** and **locations** are comma-separated. A job must have one of the keywords in its title; locations match anywhere in the job's location text ("dublin oh" matches "Dublin, Ohio" but not Dublin, Ireland). US-remote jobs always pass, and jobs listed only as "Multiple locations" are kept so you can look.

These connectors call the same endpoints the companies' own search pages use; they aren't official APIs, so a redesign can break one. A failed check shows "couldn't check *company*" — switch that company to JSearch or fix its board ID.

## Command line

```
python app.py                 # start the web UI on port 5001
python -m unittest            # run the tests (no network needed)
```

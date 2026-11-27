# Job Search app: notes for Claude

A Flask + SQLite app Eric built for his wife Jen's job search. Changes usually arrive as "jiras" from Jen.
Plain JS/CSS front end (`static/app.js`, `static/style.css`, `templates/index.html`), Python back end
(`app.py`, `jobsearch/*.py`), stdlib `unittest` tests in `tests/test_app.py`.

## The repo is public

GitHub `ericjbergan/job-search` is public. Never commit:
- any database (`jobs.db`, `demo.db`, `*.db`): they hold Jen's statuses, notes and application details
- `.env` (API keys), `ai-comparison.html`, her spreadsheets, or other personal data of hers

Exception, by Eric's decision (2026-11-26): `profile/Resume.docx` and `profile/interests.txt` are
committed on purpose (her resume is already public), so a fresh install fills the Resume & interests
card. Rank Jobs strips phone numbers and emails before sending the resume to the AI.
- never import or store the spreadsheet's "Log in Info" column (passwords)
- the `.browser-*` folders left by the removed Fill application: they hold her sign-ins to job sites

Commit only files named explicitly (`git add path1 path2`, never `git add -A` / `.`), and run
`git status --short` first. The pre-commit hook in `hooks/` blocks databases, `.env` and key values;
if it fires, fix what's staged. Don't bypass it with `--no-verify`.

The **Back up** button copies the database to OneDrive (`Job Search backups`, or `BACKUP_DIR`).
It used to push `jobs.db` to GitHub; never bring that back while the repo is public.

## Checks after every change

- Tests: `python -m unittest tests.test_app` (all must pass; no network, AI and job boards are mocked)
- After editing `static/app.js`: `node -e "new Function(require('fs').readFileSync('static/app.js','utf8'))"`
  (catches syntax errors such as an apostrophe inside a single-quoted string)
- Tests must never reach a real service. Patch `ai.json_answer` / `ai.web_answer` / `ai._anthropic`, and set
  the AI env vars explicitly in `mock.patch.dict(os.environ, ...)`, since `.env` is loaded.

## The demo

Eric looks at changes in a demo copy, not in Jen's data: `JOBSEARCH_DB=demo.db` on port 5001
(use the `demo` skill to start or restart it). The server reloads on Python changes, so after an
edit tell Eric to **refresh the page**; mention a restart only when one is really needed
(new package, `.env` change, server not running).

## Editing

- Prefer the Edit tool. Shell heredocs have mangled `\n` and line continuations in this repo; for
  scripted multi-file edits, write a Python script to the scratchpad and run it.
- Don't let a quick `python -c "import app"` create a stray `jobs.db`: set `JOBSEARCH_DB` first.

## Conventions

- AI calls go through `jobsearch/ai.py`. Per task: listings use ChatGPT (`AI_LISTINGS`), benefits use
  Claude (`AI_BENEFITS`), each falling back to whichever key is present.
- Values read by AI are checked against the source text before they're saved (see `importer.fill_gaps`,
  `benefits._quoted_figures`). Keep that pattern for anything new.
- Tailor resume (`jobsearch/tailor.py`) never edits `profile/Resume.docx`; it writes a copy. Only the summary,
  bullets (within one job) and skills-line order may change, and `check_changes` enforces that and flags
  anything not in her resume. Keep those limits.
- UI text is plain language for Jen (not developer terms).
- Commit and push only when Eric asks. Let changes pile up until then.

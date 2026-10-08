# jobagent: project context for AI coding assistants

A condensed description of the project's design, constraints and guardrails. The README is the
user-facing overview; this file is what a coding assistant should know before changing the code.
Personal preferences live in a git-ignored `CLAUDE.local.md`.

## What this is
A multi-agent system that (1) automates job applications up to a human-approved submit and
(2) strengthens a resume, per job and over time, for Senior Manager, Director and VP engineering and
AI leadership roles. It is also a portfolio project, so design choices must be defensible: prefer
measured claims to impressions and keep trade-offs explicit.

## Architecture
LLM agents for judgment work; plain Python for everything else. A deterministic orchestrator (not an
LLM) moves each job through states:

`new → filtered → scored → shortlisted → approved → tailored → verified → ready → submitted → interview | rejected | closed`

Only the orchestrator changes state, and only a human can move a job to `approved` or `submitted`.
(`ready → approved` exists for a human re-tailoring; a submitted job never returns to `approved`.)

| Agent | Job | Model tier | Status |
|---|---|---|---|
| Company Onboarding | Company name → careers site, ATS type, board ID; validate by fetching a real posting | Small (Gemini Flash-Lite, free tier, public data only) | built |
| Level + Fit Scorer | Classify real level from scope signals, not title; judge fit; structured output | Small (Haiku 4.5) | built |
| Tailor | Select and reword master-resume bullets by id | Strong (Sonnet) | built |
| Verifier | Trace every claim to the master resume; flag inflation and wrong level framing; may send back to the Tailor once | Strong (Sonnet) | built |
| Application | Map form fields to approved answers, pre-fill with Playwright, pause before Submit | Strong | planned |
| Career Gap Analyst | Weekly: recurring gaps across scored postings | Strong | planned |

Model IDs live in config, never in code. LLM access goes through a provider interface (Claude and
Gemini adapters) so models can be compared by evals.

## Extensibility requirements (non-negotiable)
- **Adding a company = one row** in the Companies tab (status `pending`) or
  `jobagent add-company "Name" --tier A --levels director,vp`. No code change. Onboarding sets
  `active` or `needs_review` with a reason.
- **ATS adapters**: one class per ATS behind a common interface (Ashby, Greenhouse, Lever and Workday
  are built; SmartRecruiters and an aggregator fallback are not).
- **Role levels** are config (`role_profiles.yaml`). Adding a level is a config edit only.
- **Filters are data** (`criteria.*.yaml` rules). A new filter is a rule, not code.
- **No personal data in code**: it must be open-sourceable unchanged. Personal content lives in
  git-ignored files or in GitHub secrets.

## Config and data
- `config/master_resume.yaml`: every true bullet, with ids (git-ignored; an example is committed)
- `config/criteria.local.yaml` (git-ignored) over `config/criteria.example.yaml`; the scheduled run reads
  it from the `CRITERIA_YAML` secret
- `config/role_profiles.yaml`, `scoring.yaml`, `tailoring.yaml`, `onboarding.yaml`, `fetch.yaml`, `evals.yaml`
- `config/answers.yaml`: application answers the user approved (git-ignored)
- `prompts/<agent>.md`: versioned prompts (the version is logged with every score and eval)
- Google Sheet tabs: Jobs, Companies, Runs, Evals, behind a storage interface (Sheets now, CSV
  fallback, Postgres later). Google Drive holds one folder per job with candidate subfolders.

## Stack
Python 3.12, Pydantic models for every agent's input and output, httpx, gspread, Google Drive API,
python-docx, reportlab, Typer + rich, pytest with recorded responses and fakes, uv, and GitHub Actions
for the scheduled run and the tests. Playwright is planned for the Application agent (local only).

Layout: `src/jobagent/{adapters/ats, agents, demo, documents, evals, llm, models, onboarding,
orchestrator, storage}`, with `config/`, `prompts/`, `evals/data/`, `tests/` and `.github/workflows/`
at the top level.

## Constraints discovered
- **Google auth is split.** Sheets uses the service account. Drive uses OAuth as the user, because
  service accounts have no Drive storage quota (uploads to a personal folder fail with 403
  `storageQuotaExceeded`; Shared Drives need Workspace).
- **Drive scope is `drive.file`**, so the app only sees files it created. It creates its own root
  folder via `jobagent auth-drive`; `DRIVE_FOLDER_ID` must be that folder, not one made by hand.
- **The OAuth app is published ("In production")**, so the refresh token does not expire after 7
  days. It can still die if access is revoked or unused for about 6 months; re-run `jobagent auth-drive`.
- **Free API tiers cannot serve agents that see the resume.** This is enforced in code
  (`check_provider_policy`), not just by convention.
- **Changing a prompt, profile or relevant config invalidates the demo recording.** Refresh it with
  `jobagent demo --record` (a few cents); a test fails until you do, on purpose.

## Guardrails (never relax without asking the project owner)
- The Tailor may only select and reword master-resume bullets. Anything the Verifier cannot trace blocks the job.
- Knock-out answers (salary, work authorization, notice period) come only from `answers.yaml`. New
  questions are drafted for human review, never guessed.
- No LinkedIn automation. No CAPTCHA bypassing. No auto-submit until an "earned autonomy" gate (20 clean
  supervised runs, Greenhouse/Lever only).
- Dedupe on company + normalized title + location. A submitted job never returns to `approved`.
- Hard filters run before any LLM call. Log tokens and cost per run.
- API keys only in env vars and GitHub secrets. Paid API tiers for anything that sees the resume.
  Free tiers are allowed only for agents that handle public data.
- Logs of the scheduled run are public: counts and costs only, never company names, job titles or resume text.
- Workflows that use secrets are never triggered by pull requests; actions are pinned to commit SHAs.

## Evals
Scorer level accuracy (90%+), Verifier fabrication (0 escapes), Onboarding first-try success (80%+),
shortlist precision (70%+, not yet measured), Tailor keyword coverage vs untailored (not yet built), and
cost per approved job. Run `jobagent eval scorer|verifier|all`. Keep the Scorer's output structured so a
trained classifier on Apply/Skip labels can use it as features later.

## Build status
Blocks 1 to 8 are built: setup, pipeline, Scorer, Tailor and Verifier, Onboarding, the scheduled run,
evals, and the README with a demo. Not built: the Application agent, SmartRecruiters, the aggregator
fallback, the Career Gap Analyst, and any web UI.

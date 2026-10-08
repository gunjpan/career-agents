# Job Search Agent — project context for Claude Code

Full plan (living doc): https://claude.ai/code/artifact/922d1ea5-c3f2-4458-a421-b2e8ae829af4
This file is the condensed version. When they disagree, ask me.

## What we're building
A multi-agent system that (1) automates job applications up to a human-approved submit and
(2) strengthens my resume, per job and over time. Targets: Senior Manager, Director and VP
engineering / AI leadership roles. Portfolio project for an AI leadership job search — design
choices must be defensible in interviews.

## Architecture
Six LLM agents for judgment work; plain Python for everything else. A deterministic
orchestrator (not an LLM) moves each job through states:

`new → filtered → scored → shortlisted → approved → tailored → verified → ready → submitted → interview | rejected | closed`

Only the orchestrator changes state. Only I can move a job to `approved` or `submitted`.

| Agent | Job | Model tier | Build |
|---|---|---|---|
| Company Onboarding | Company name → careers site, ATS type, board ID; validate by fetching one posting | Small + web search | Day 1 |
| Level + Fit Scorer | Classify real level (Sr Mgr / Director / VP) from scope signals, not title; score fit; return score, rationale, gaps | Small | Day 1 |
| Tailor | Select and reword master-resume bullets for the posting and level; draft cover letter | Strong | Day 1 |
| Verifier | Trace every claim to the master resume; flag inflation, wrong level framing, missing must-haves; may send back to Tailor once | Strong | Day 1 |
| Application | Map form fields to approved answers, draft custom answers, pre-fill with Playwright, pause before Submit | Strong | Week 2 |
| Career Gap Analyst | Weekly: recurring gaps across scored postings | Strong | Later |

Small = Haiku 4.5 / Gemini Flash-Lite. Strong = Sonnet / Gemini Flash. Model IDs live in
config, never hardcoded. LLM access goes through a provider interface (Claude and Gemini
adapters) so models can be compared by evals.

## Extensibility requirements (non-negotiable)
- **Adding a company = one row** in the Sheet's Companies tab (status `pending`) or
  `jobagent add-company "Name" --tier A --levels director,vp`. No code change.
  Onboarding sets `active` or `needs_review` with a reason.
- **ATS adapters**: one class per ATS behind a common interface. Day 1: Greenhouse, Lever,
  Ashby (public APIs). Week 2: SmartRecruiters, Workday (unofficial endpoint — fragile),
  aggregator fallback (Adzuna / JSearch).
- **Role levels** are config (`role_profiles.yaml`): title variants, scope signals, resume
  emphasis, cover-letter tone, salary floor per level. Adding a level = config edit only.
- **No personal data in code** — it must be open-sourceable unchanged.

## Config and data
- `config/master_resume.yaml` — every true bullet, tagged by level and theme (git-ignored)
- `config/role_profiles.yaml`, `config/criteria.yaml`
- `config/answers.yaml` — application answers I approved (git-ignored)
- `prompts/<agent>.md` — versioned prompts (version logged with every eval)
- Google Sheet tabs: Jobs, Companies, Runs, Evals. Behind a storage interface
  (Sheets now, Postgres later). Fallback for day 1: local CSV if Google auth blocks.
- Google Drive: one folder per approved job (resume .docx + PDF, cover letter, Verifier report)

## Stack
Python 3.12, Pydantic models for every agent's input/output, httpx, gspread, Google Drive API,
python-docx, Playwright (local only), Typer + rich (CLI and a watchable demo mode),
pytest with recorded API responses. Scheduled runs on GitHub Actions cron; secrets in repo
settings.

Repo layout: `agents/`, `adapters/ats/`, `storage/`, `orchestrator/`, `prompts/`, `config/`,
`evals/`, `tests/`.

## Constraints discovered (Block 1)
- **Google auth is split.** Sheets uses the service account. Drive uses OAuth as me, because
  service accounts have no Drive storage quota (uploads to a personal folder fail with 403
  `storageQuotaExceeded`; Shared Drives need Workspace).
- **Drive scope is `drive.file`**, so the app only sees files it created. It creates its own root
  folder via `jobagent auth-drive`; `DRIVE_FOLDER_ID` must be that folder, not one made by hand.
- **OAuth app is published ("In production") on the Google Cloud project**, so the refresh token
  does not expire after 7 days (Testing status would). It can still die if I revoke access,
  change my password or leave it unused ~6 months; fix by re-running `jobagent auth-drive`.
  CI gets the token from the `GOOGLE_OAUTH_TOKEN_JSON` secret.

## Day-1 build order (each block has a "done when")
1. Setup — repo, keys, Sheet + Drive shared with service account → test writes one row
2. Core pipeline — Companies tab, Greenhouse + Lever adapters, dedupe, hard filters, states → Jobs tab fills
3. Level + Fit Scorer → every posting has level, score, rationale, gaps
4. Tailor + Verifier, .docx/PDF to Drive → 3 approved jobs, zero untraceable claims
5. Company Onboarding + Ashby adapter + `add-company` → a company typed in the Sheet is active next run
6. GitHub Actions daily run + Runs tab → scheduled run completes without my laptop
7. Light evals (15 labelled cases: Scorer level accuracy, Verifier fabrication) → pass rates printed
8. README with architecture diagram, demo recording

## Guardrails (never relax without asking me)
- Tailor may only select and reword master-resume bullets. Anything the Verifier can't trace blocks the job.
- Knock-out answers (salary, work authorization, notice period) come only from `answers.yaml`. New questions are drafted for my review, never guessed.
- No LinkedIn automation. No CAPTCHA bypassing. No auto-submit until the "earned autonomy" gate (20 clean supervised runs, Greenhouse/Lever only).
- Dedupe on company + normalized title + location. A submitted job never returns to `approved`.
- Hard filters run before any LLM call. Log tokens and cost per run.
- API keys only in env vars / GitHub secrets. Paid API tiers only (no training on my resume).
- Free API tiers are allowed for public-data only and for agents accessing public-data.

## Evals to support
Scorer level accuracy (90%+), shortlist precision (70%+), Tailor keyword coverage vs untailored,
Verifier fabrication rate (0%), Onboarding first-try success (80%+), cost per approved job.
Later: a trained classifier (e.g. XGBoost) on my Apply/Skip labels once ~150 exist — keep Scorer output structured so features are captured from day one.

## How to work with me
- I'm a former Director of Software Engineering (13 years; strong in Node.js, REST/OpenAPI, microservices, Spring Boot, CI/CD, cloud). Newer to Python and LLM engineering.
- Be direct and brief. No flattery. If information is insufficient, say so instead of guessing.
- For any non-trivial change or decision: explain the approach and trade-off first, then wait for my go-ahead — unless I say "do it".
- Define LLM/AI and Python terms before using them; tie new concepts to API design, microservices or CI/CD where it helps.
- When a block or concept lands, quiz me with 2–3 interview-style questions and correct me if I'm wrong.

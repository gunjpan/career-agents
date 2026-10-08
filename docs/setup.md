# Setup for real use

`uv run jobagent demo` needs none of this. Follow these steps only to run the pipeline on real job boards.

## 1. Install

```bash
# Python 3.12+ and uv (https://docs.astral.sh/uv/) are required
uv sync
cp .env.example .env          # then fill it in as you go; .env is git-ignored
```

## 2. Model keys

| Key | Used by | Notes |
|---|---|---|
| `ANTHROPIC_API_KEY` | Scorer, Tailor, Verifier | Use a **paid** API tier. These agents receive your resume. |
| `GEMINI_API_KEY` | Onboarding agent only | Optional. The free tier is acceptable *only* here, because only a public company name is sent. The code refuses to give a free-tier provider to any agent that sees your resume. |

## 3. Storage: a Google Sheet (or local CSV)

For a quick trial without Google, set `STORAGE_BACKEND=csv` in `.env` (files go to `data/`). Otherwise:

1. In the Google Cloud Console, create a project and enable the **Google Sheets API** and **Google Drive API**.
2. Create a **service account**, add a JSON key, and save it as `secrets/service-account.json` (git-ignored).
3. Create a Google Sheet with four tabs named exactly `Jobs`, `Companies`, `Runs` and `Evals`. Headers are written automatically.
4. Share the Sheet with the service account's email as **Editor**, and put the Sheet ID (the long string in its URL) in `.env` as `SHEET_ID`.
5. Check it: `uv run jobagent check-setup`.

## 4. Drive, for tailored documents (optional until you run `tailor`)

Service accounts have no Drive storage quota on a personal account, so uploads use OAuth as you:

1. In the same Cloud project, configure the OAuth consent screen (External) and create an **OAuth client of type Desktop app**. Save the JSON as `secrets/oauth-client.json`.
2. `uv run jobagent auth-drive` opens a browser for one-time consent (scope `drive.file`, so the app only sees files it creates), saves `secrets/drive-token.json`, and prints a `DRIVE_FOLDER_ID` for `.env`.
3. Check it: `uv run jobagent check-drive`.

## 5. Your content

```bash
cp config/master_resume.example.yaml config/master_resume.yaml   # then replace with your TRUE experience
cp config/criteria.example.yaml config/criteria.local.yaml       # your locations, title rules, company rules
```

Both files are git-ignored. Nothing the Tailor writes can come from anywhere except `master_resume.yaml`.
Other settings are committed and safe to edit: `role_profiles.yaml` (what each level looks like), `scoring.yaml`
(models, prices, shortlist thresholds, spend cap), `tailoring.yaml` (what the Tailor must keep, cover letter
off by default), `fetch.yaml` (per-platform cooldowns) and `onboarding.yaml`.

## 6. First real run

```bash
uv run jobagent add-company "Some Company" --tier A     # one pending row; onboarding finds its job platform
uv run jobagent run                                      # onboard pending, fetch, filter, dedupe (free)
uv run jobagent score --limit 10                         # model scoring, a few cents
uv run jobagent shortlist                                # review
uv run jobagent approve <job_id>                         # only you can
uv run jobagent tailor                                   # tailor + verify, saves to Drive
uv run jobagent diff <job_id>                            # what changed versus your master resume
uv run jobagent pick <job_id> 1                          # when there are two candidates
```

## 7. Daily automation (GitHub Actions)

`.github/workflows/daily.yml` runs `jobagent daily` at 11:00 UTC and on demand. It needs these repository secrets:

```bash
gh secret set ANTHROPIC_API_KEY
gh secret set GEMINI_API_KEY
gh secret set SHEET_ID
gh secret set GOOGLE_SERVICE_ACCOUNT_JSON < secrets/service-account.json
gh secret set MASTER_RESUME_YAML < config/master_resume.yaml
gh secret set CRITERIA_YAML < config/criteria.local.yaml
gh workflow run daily && gh run watch
```

The run prints counts and costs only (never company names, titles or resume text), because logs on a public
repository are public. It never approves, tailors or submits anything.

# Job Monitor (daily + anti-scam + email)

Monitors fresh vacancies every day, filters them by your profile prompt, applies anti-scam scoring, keeps an always-updated active list, and emails you the results.

## What it does

- pulls fresh vacancies from **Superjob API** by default (no HeadHunter employer account needed);
- optional provider **`hh`** (HeadHunter) if you can satisfy their API access rules;
- calculates `AI fit %` against your keyword profile;
- calculates scam risk score and removes risky items;
- stores current active list in `data/jobs.json`;
- removes vacancies from active list when archived or stale;
- sends daily email digest with:
  - vacancy title and company;
  - salary;
  - schedule/employment type;
  - link;
  - AI fit percent;
  - scam risk score + reasons.

## Files

- `monitor.py` - main script
- `config.json` - working config (edit this)
- `config.example.json` - template config
- `.env.example` - SMTP variable template
- `data/jobs.json` - persistent state
- `logs/monitor.log` - run logs

## Quick start

1. Create virtual env and install deps:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

2. Fill SMTP credentials:

```powershell
copy .env.example .env
```

Then edit `.env` values.

3. Edit filters in `config.json` (query, keywords, risk thresholds).

4. Run once:

```powershell
python monitor.py
```

## Useful run modes

- Test without email:

```powershell
python monitor.py --disable-email
```

- Dry run (full logic, no email):

```powershell
python monitor.py --dry-run
```

## Daily auto-run on GitHub (PC can be off)

Workflow file is already included: `.github/workflows/job-monitor.yml`.

It runs:

- every day by cron (`06:00 UTC`, edit if needed);
- manually from GitHub UI (`Run workflow`);
- saves updated `data/jobs.json` back into the repo.

Required GitHub repository secrets:

- `SMTP_HOST`
- `SMTP_PORT`
- `SMTP_USER`
- `SMTP_PASS`
- `SMTP_FROM`
- `SMTP_TO`
- `SUPERJOB_APP_ID` — secret key from [Superjob API registration](https://api.superjob.ru/register) (same value as header `X-Api-App-Id`)

Optional (only if `provider` is `hh` in `config.json`):

- `HH_USER_AGENT`, `HH_ACCESS_TOKEN` — see HeadHunter section below.

## Daily auto-run on Windows (alternative)

Example Task Scheduler command (runs at 09:00 every day):

```powershell
schtasks /create /tn "JobMonitorDaily" /sc daily /st 09:00 /tr "powershell -NoProfile -ExecutionPolicy Bypass -Command \"cd C:\Users\Alexandr\job_monitor; .\.venv\Scripts\python.exe monitor.py\"" /f
```

## Notes

- For Gmail, use App Password (not your normal password).
- `lookback_hours` controls freshness (default 24h).
- `max_age_days` controls how long active vacancies can remain before forced cleanup.

## Superjob setup (default)

1. Register an application: [https://api.superjob.ru/register](https://api.superjob.ru/register).
2. Copy the **secret key** (used as `X-Api-App-Id`).
3. Put it into GitHub secret **`SUPERJOB_APP_ID`** (or into `superjob.app_id` in `config.json` if you accept storing it in the repo).
4. Tune search in `config.json`: `search.superjob_keyword` (plain text, not HH query syntax).

If you previously used HeadHunter IDs in `data/jobs.json`, delete or reset that file when switching providers (or old rows will linger).

## HeadHunter (`provider: hh`) — `403` in CI

HeadHunter often blocks cloud runners and/or requires employer-side app registration. If you set `provider` to `hh`, you may need:

1. `HH_USER_AGENT` like `MyApp/1.0 (you@email.com)` — see [hh API docs](https://github.com/hhru/api).
2. `HH_ACCESS_TOKEN` from [dev.hh.ru/admin](https://dev.hh.ru/admin) if anonymous requests fail.

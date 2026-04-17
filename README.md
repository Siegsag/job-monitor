# Job Monitor (daily + anti-scam + email)

Monitors fresh vacancies every day, filters them by your profile prompt, applies anti-scam scoring, keeps an always-updated active list, and emails you the results.

## What it does

- pulls fresh vacancies from HH API;
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

## Daily auto-run on Windows (alternative)

Example Task Scheduler command (runs at 09:00 every day):

```powershell
schtasks /create /tn "JobMonitorDaily" /sc daily /st 09:00 /tr "powershell -NoProfile -ExecutionPolicy Bypass -Command \"cd C:\Users\Alexandr\job_monitor; .\.venv\Scripts\python.exe monitor.py\"" /f
```

## Notes

- For Gmail, use App Password (not your normal password).
- `lookback_hours` controls freshness (default 24h).
- `max_age_days` controls how long active vacancies can remain before forced cleanup.

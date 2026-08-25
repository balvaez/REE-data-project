# Daily Spain Electricity Generation Report

Sends you a daily email (around 12:00 Madrid local time) with:
- A pie chart of Spain's electricity generation mix (yesterday, full day)
- The renewable vs. non-renewable share
- A table with the breakdown by technology (GWh and %)

Data comes from Red Eléctrica de España's free, public **REData API**
(`apidatos.ree.es`) — no API key or registration needed.

## How it works

- `.github/workflows/daily-ree-report.yml` — GitHub Actions workflow, scheduled daily.
- `scripts/ree_report.py` — fetches the data, builds the chart, sends the email.
- `requirements.txt` — Python dependencies (`requests`, `matplotlib`).

### About the schedule (important)

GitHub Actions `cron` schedules always run in **UTC**, but Spain's clock
offset from UTC changes twice a year (CET = UTC+1 in winter, CEST = UTC+2
in summer). To reliably fire at 12:00 Madrid time year-round without you
having to remember to edit the cron expression at each DST change, the
workflow schedules **two** triggers:

```yaml
- cron: '0 10 * * *'   # 12:00 in Madrid during summer (CEST)
- cron: '0 11 * * *'   # 12:00 in Madrid during winter (CET)
```

Both run every day, but `ree_report.py` checks the *actual* current local
time in `Europe/Madrid` at the start and immediately exits (without
sending an email) if it isn't ~12:00. So on any given day, one of the two
triggers does the real work and the other is a silent no-op. You don't
need to touch this across DST changes.

(GitHub's scheduler is best-effort and can run a few minutes late,
especially at busy times — this is normal and out of your control.)

## Setup

### 1. Create the repository

Create a new GitHub repository and add these three files/folders to it,
preserving the paths:

```
.github/workflows/daily-ree-report.yml
scripts/ree_report.py
requirements.txt
```

### 2. Set up an email account to send from

You need SMTP credentials for *some* account to send the email through.
The easiest options:

- **Gmail** — use your Gmail address as the "from", and create an
  [App Password](https://myaccount.google.com/apppasswords) (requires
  2-Step Verification enabled on the account). Regular Gmail passwords
  will **not** work with SMTP due to Google's security policies.
  - `SMTP_HOST`: `smtp.gmail.com`
  - `SMTP_PORT`: `587`
- **Outlook/Hotmail**:
  - `SMTP_HOST`: `smtp.office365.com`
  - `SMTP_PORT`: `587`
- Any other provider that offers SMTP (Fastmail, Zoho, a transactional
  service like SendGrid/Mailgun, your own domain's email, etc.) works
  the same way — just use their SMTP host/port and credentials.

### 3. Add GitHub Actions secrets

In your repository: **Settings → Secrets and variables → Actions → New
repository secret**. Add each of these:

| Secret name      | Example value              | Notes                                   |
|-------------------|-----------------------------|------------------------------------------|
| `SMTP_HOST`       | `smtp.gmail.com`            |                                          |
| `SMTP_PORT`       | `587`                       |                                          |
| `SMTP_USERNAME`   | `you@gmail.com`             | Your login for the SMTP account         |
| `SMTP_PASSWORD`   | `xxxxxxxxxxxxxxxx`          | App password (not your normal password) |
| `EMAIL_FROM`      | `you@gmail.com`             | Can be same as `SMTP_USERNAME`          |
| `EMAIL_TO`        | `you@example.com`           | Where the report should be delivered    |

### 4. Enable the workflow and test it

1. Push the files to GitHub (the workflow schedule activates automatically
   once the file is on the default branch — GitHub disables scheduled
   workflows only after **60 days of repository inactivity**, so keep the
   repo lightly active, e.g. by occasionally committing, or re-enable it
   manually in the Actions tab if that happens).
2. Go to the **Actions** tab → **Daily REE Generation Report** → **Run
   workflow**. The `force` input defaults to `true`, so a manual run
   sends an email immediately regardless of the current time — use this
   to test your setup.
3. Check your inbox. If it fails, open the failed run's logs in the
   Actions tab — the script also tries to send you a short "the job
   failed" email so failures aren't silent.

That's it — from then on it'll run automatically every day.

## Customizing

Some easy tweaks, all in `scripts/ree_report.py`:

- **Different local time / timezone**: change `MADRID_TZ` and the
  `now_madrid.hour == 12` check, and adjust the two cron lines in the
  workflow to match your desired UTC offsets.
- **Today's partial data instead of yesterday's full day**: change
  `report_date = date.today() - timedelta(days=1)` to `date.today()`,
  and consider fetching `start_date` as `00:00` up to the current time
  instead of `23:59` (the API will just return what's available so far).
- **Add more figures** (e.g. peak/lowest demand, import/export balance,
  CO₂ emissions): REE's REData API has other endpoints under
  `demanda`, `intercambios`, and `balance` categories
  (see https://www.ree.es/en/apidatos for the full list) — fetch them
  similarly to `fetch_generation_mix()` and add to the summary/email.
- **Chart style / small-slice grouping threshold**: see
  `build_pie_chart()`'s `small_slice_threshold` parameter.

## Local testing

You can run the script locally without waiting for the schedule:

```bash
pip install -r requirements.txt
export SMTP_HOST=smtp.gmail.com
export SMTP_PORT=587
export SMTP_USERNAME=you@gmail.com
export SMTP_PASSWORD=your-app-password
export EMAIL_FROM=you@gmail.com
export EMAIL_TO=you@example.com
export FORCE_RUN=true   # bypasses the 12:00 local-time check
python scripts/ree_report.py
```

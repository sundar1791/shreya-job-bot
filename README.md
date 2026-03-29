# London Job Bot — Shreya's Weekly Digest

A Python script that scans Adzuna every Monday morning for London operations roles matching Shreya's profile, then sends a curated HTML email digest with the top 20 results. Runs in the cloud via GitHub Actions — no laptop required.

---

## What It Does

- Queries **7 search terms** across Adzuna
- Deduplicates results
- Scores each job for relevance (title match, keywords, salary range, location)
- Selects the **top 20** most relevant roles
- Sends a **clean HTML email** with job title, company, salary, location, description snippet, and a direct Apply link
- Runs **automatically every Monday at 8:00 AM** via GitHub Actions (no laptop needed)

---

## Setup (One-Time)

### Step 1 — Get your API keys (all free)

**Adzuna**
1. Go to https://developer.adzuna.com/
2. Click **Register** and create a free account
3. Create an application → you'll get an **App ID** and **App Key**

**Gmail App Password** (for sending email)
1. Make sure your Gmail has 2-Step Verification enabled:
   Google Account → Security → 2-Step Verification
2. Then go to: Google Account → Security → 2-Step Verification → **App passwords** (scroll to bottom)
3. Choose "Other (custom name)", enter "Job Bot", click **Generate**
4. Copy the 16-character password — you'll only see it once

---

### Step 2 — Create a GitHub repo and push the code

```bash
cd ~/Workspace/job-bot

# Initialise git (if not already done)
git init
git add .
git commit -m "Initial commit: Shreya's London job bot"

# Create a new repo on GitHub, then push:
git remote add origin https://github.com/sundar1791/shreya-job-bot.git
git branch -M main
git push -u origin main
```

> You can create the repo at https://github.com/new — name it `shreya-job-bot`, set it to **Private**.

---

### Step 3 — Add secrets to GitHub

Your API keys must be stored as GitHub Secrets (never in code).

1. Go to your repo on GitHub
2. Click **Settings** → **Secrets and variables** → **Actions** → **New repository secret**
3. Add each of these secrets one by one:

| Secret name | Value |
|---|---|
| `ADZUNA_APP_ID` | Your Adzuna App ID |
| `ADZUNA_APP_KEY` | Your Adzuna App Key |
| `GMAIL_USER` | Your Gmail address |
| `GMAIL_APP_PASS` | Your 16-character Gmail App Password |
| `FROM_EMAIL` | Your Gmail address (same as above) |
| `TO_EMAIL` | `shreyaa1693@gmail.com` |

---

### Step 4 — Test it manually on GitHub

1. Go to your repo → **Actions** tab
2. Click **Weekly London Job Digest** in the left sidebar
3. Click **Run workflow** → **Run workflow**
4. Watch the run complete — check Shreya's inbox within a minute

If it fails, click the failed run to see the logs. A `debug-digest` HTML file is uploaded as an artifact so you can inspect the email content.

---

## Automated Schedule

The workflow runs **every Monday at 8:00 AM UTC** (8:00 AM London time in winter / 9:00 AM BST in summer).

GitHub Actions runs on GitHub's servers — your laptop does not need to be on.

To trigger manually at any time: Actions tab → **Run workflow**.

---

## Running locally (optional)

```bash
cd ~/Workspace/job-bot
cp .env.example .env
# Fill in .env with your keys
pip install -r requirements.txt
python job_bot.py --test    # sends immediately
python job_bot.py           # normal run
```

---

## Customisation

All search behaviour is at the top of `job_bot.py`:

| Variable | What it controls |
|---|---|
| `ADZUNA_QUERIES` | Search terms sent to Adzuna |
| `POSITIVE_KEYWORDS` | Keywords that boost a job's relevance score |
| `NEGATIVE_KEYWORDS` | Keywords that lower a job's score (engineering, junior, etc.) |
| `TITLE_BOOST_KEYWORDS` | Job title matches get extra points |
| `MAX_JOBS` | Number of jobs in the digest (default: 20) |

---

## Troubleshooting

**Workflow not triggering** — GitHub Actions schedules can be delayed by up to 15 minutes. Also check the repo is not archived.

**"Adzuna credentials missing"** — Check the GitHub Secrets are named exactly as shown above (case-sensitive).

**"SMTP authentication failed"** — Re-generate your Gmail App Password and update the `GMAIL_APP_PASS` secret.

**Email goes to spam** — Add the sender address to Shreya's contacts.

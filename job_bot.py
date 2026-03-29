#!/usr/bin/env python3
"""
Job Bot for Shreya Anantha Subramaniyam
Weekly London job scanner using the Adzuna API.
Sends a curated HTML email digest every Monday morning.

Usage:
    python job_bot.py           # Normal run (respects schedule)
    python job_bot.py --test    # Runs immediately, sends one email
"""

import os
import sys
import smtplib
import logging
import argparse
import re
from datetime import datetime, date
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import Optional

import requests
from dotenv import load_dotenv

# ─────────────────────────────────────────────
# SETUP
# ─────────────────────────────────────────────
load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("job_bot")


# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────
ADZUNA_APP_ID  = os.getenv("ADZUNA_APP_ID", "")
ADZUNA_APP_KEY = os.getenv("ADZUNA_APP_KEY", "")

GMAIL_USER     = os.getenv("GMAIL_USER", "")
GMAIL_APP_PASS = os.getenv("GMAIL_APP_PASS", "")

FROM_EMAIL     = os.getenv("FROM_EMAIL", GMAIL_USER)
TO_EMAIL       = os.getenv("TO_EMAIL", "shreyaa1693@gmail.com")

MAX_JOBS       = 20
ADZUNA_BASE    = "https://api.adzuna.com/v1/api/jobs/gb/search"

# ─────────────────────────────────────────────
# SEARCH QUERIES
# ─────────────────────────────────────────────
ADZUNA_QUERIES = [
    "operations manager",
    "operations lead",
    "e-commerce operations",
    "vendor operations",
    "merchandising operations",
    "platform operations",
    "business operations manager",
]

# ─────────────────────────────────────────────
# RELEVANCE SCORING
# ─────────────────────────────────────────────
POSITIVE_KEYWORDS = [
    "operations", "e-commerce", "ecommerce", "retail", "vendor", "merchandising",
    "platform", "data governance", "onboarding", "partner", "marketplace",
    "catalogue", "catalog", "supply chain", "continuous improvement", "sla",
    "stakeholder", "workflow", "process improvement", "agile", "cross-functional",
    "migration", "data quality", "service operations", "team lead",
]

NEGATIVE_KEYWORDS = [
    "software engineer", "developer", "coding", "devops", "data engineer",
    "machine learning", "ml engineer", "junior", "graduate", "intern",
    "accountant", "finance manager", "financial analyst", "actuary",
    "recruitment consultant", "sales executive", "field sales",
]

TITLE_BOOST_KEYWORDS = [
    "operations manager", "operations lead", "e-commerce operations",
    "vendor operations", "merchandising", "platform operations",
    "business operations", "data operations",
]

def score_job(job: dict) -> int:
    """Returns a relevance score. Higher = more relevant."""
    score = 0
    title       = (job.get("title") or "").lower()
    description = (job.get("description") or "").lower()
    location    = (job.get("location") or "").lower()
    salary_min  = job.get("salary_min") or 0
    salary_max  = job.get("salary_max") or 0

    combined_text = f"{title} {description}"

    # Hard disqualifiers
    for neg in NEGATIVE_KEYWORDS:
        if neg in combined_text:
            score -= 20

    # Title boosts
    for kw in TITLE_BOOST_KEYWORDS:
        if kw in title:
            score += 15

    # Positive keyword hits
    for kw in POSITIVE_KEYWORDS:
        if kw in combined_text:
            score += 2

    # London / hybrid / remote preference
    if "london" in location:
        score += 10
    if "hybrid" in combined_text or "remote" in combined_text:
        score += 5

    # Salary range £50k–£90k sweet spot
    avg_salary = (salary_min + salary_max) / 2 if salary_max else salary_min
    if 45_000 <= avg_salary <= 95_000:
        score += 8
    elif avg_salary > 95_000:
        score += 3    # still good, just above range

    return score


# ─────────────────────────────────────────────
# ADZUNA API
# ─────────────────────────────────────────────
def fetch_adzuna_jobs(query: str, page: int = 1, results_per_page: int = 20) -> list[dict]:
    """Fetch jobs from Adzuna for a single query."""
    if not ADZUNA_APP_ID or not ADZUNA_APP_KEY:
        log.warning("Adzuna credentials missing – skipping Adzuna.")
        return []

    params = {
        "app_id":           ADZUNA_APP_ID,
        "app_key":          ADZUNA_APP_KEY,
        "results_per_page": results_per_page,
        "what":             query,
        "where":            "London",
        "distance":         15,
        "content-type":     "application/json",
        "sort_by":          "date",
    }

    try:
        resp = requests.get(f"{ADZUNA_BASE}/{page}", params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        raw_jobs = data.get("results", [])
        jobs = []
        for j in raw_jobs:
            loc = j.get("location", {})
            location_str = loc.get("display_name", "London")
            sal = j.get("salary_min"), j.get("salary_max")
            jobs.append({
                "source":      "Adzuna",
                "id":          f"adzuna_{j.get('id', '')}",
                "title":       j.get("title", ""),
                "company":     j.get("company", {}).get("display_name", "Unknown"),
                "location":    location_str,
                "salary_min":  sal[0],
                "salary_max":  sal[1],
                "description": j.get("description", "")[:500],
                "url":         j.get("redirect_url", ""),
                "date_posted": j.get("created", ""),
            })
        log.info(f"  Adzuna [{query!r}]: fetched {len(jobs)} jobs")
        return jobs
    except requests.RequestException as e:
        log.error(f"  Adzuna [{query!r}] error: {e}")
        return []


def fetch_all_adzuna() -> list[dict]:
    all_jobs = []
    for q in ADZUNA_QUERIES:
        jobs = fetch_adzuna_jobs(q)
        all_jobs.extend(jobs)
    return all_jobs


# ─────────────────────────────────────────────
# DEDUPLICATION & RANKING
# ─────────────────────────────────────────────
def deduplicate(jobs: list[dict]) -> list[dict]:
    """Remove duplicate jobs by URL and fuzzy title+company match."""
    seen_urls = set()
    seen_titles = {}
    unique = []

    for job in jobs:
        url = job.get("url", "")
        title_key = re.sub(r"[^a-z0-9]", "", (job.get("title", "") + job.get("company", "")).lower())

        if url and url in seen_urls:
            continue
        if title_key and title_key in seen_titles:
            continue

        if url:
            seen_urls.add(url)
        if title_key:
            seen_titles[title_key] = True

        unique.append(job)

    log.info(f"After dedup: {len(unique)} unique jobs (from {len(jobs)} total)")
    return unique


def rank_and_select(jobs: list[dict], top_n: int = MAX_JOBS) -> list[dict]:
    """Score, sort, and return the top N jobs."""
    for job in jobs:
        job["score"] = score_job(job)

    ranked = sorted(jobs, key=lambda j: j["score"], reverse=True)
    selected = ranked[:top_n]
    log.info(f"Selected top {len(selected)} jobs (highest score: {selected[0]['score'] if selected else 0})")
    return selected


# ─────────────────────────────────────────────
# EMAIL FORMATTING
# ─────────────────────────────────────────────
def format_salary(job: dict) -> str:
    lo = job.get("salary_min")
    hi = job.get("salary_max")
    if lo and hi:
        return f"£{lo:,.0f} – £{hi:,.0f}"
    elif lo:
        return f"£{lo:,.0f}+"
    elif hi:
        return f"up to £{hi:,.0f}"
    return "Not specified"


def truncate(text: str, length: int = 200) -> str:
    text = re.sub(r"<[^>]+>", "", text or "")   # strip HTML tags
    text = re.sub(r"\s+", " ", text).strip()
    return text[:length].rstrip() + "…" if len(text) > length else text


def build_html_email(jobs: list[dict], week_of: str) -> str:
    """Generate a clean, professional HTML email body."""

    job_cards = ""
    for i, job in enumerate(jobs, start=1):
        salary_str  = format_salary(job)
        desc_short  = truncate(job.get("description", ""), 220)
        source_badge = (
            '<span style="background:#e8f4fd;color:#1a73e8;padding:2px 8px;'
            'border-radius:12px;font-size:11px;font-weight:600;">'
            f'{job["source"]}</span>'
        )
        salary_icon = "💰" if salary_str != "Not specified" else "💼"
        date_str = job.get("date_posted", "")[:10] if job.get("date_posted") else ""
        date_html = f'<span style="color:#888;font-size:12px;">Posted: {date_str}</span>' if date_str else ""

        job_cards += f"""
        <tr>
          <td style="padding:0 0 20px 0;">
            <table width="100%" cellpadding="0" cellspacing="0" style="
              background:#ffffff;
              border:1px solid #e0e0e0;
              border-radius:10px;
              overflow:hidden;
              border-left:4px solid #4f46e5;
            ">
              <tr>
                <td style="padding:18px 20px 6px 20px;">
                  <table width="100%" cellpadding="0" cellspacing="0">
                    <tr>
                      <td>
                        <span style="color:#888;font-size:12px;font-weight:600;letter-spacing:1px;">#{i}</span>
                        &nbsp; {source_badge}
                        &nbsp; {date_html}
                      </td>
                    </tr>
                    <tr>
                      <td style="padding-top:6px;">
                        <a href="{job['url']}" style="
                          color:#1a1a2e;
                          font-size:17px;
                          font-weight:700;
                          text-decoration:none;
                          line-height:1.3;
                        ">{job['title']}</a>
                      </td>
                    </tr>
                    <tr>
                      <td style="padding-top:4px;color:#4f46e5;font-size:14px;font-weight:600;">
                        🏢 {job['company']}
                      </td>
                    </tr>
                  </table>
                </td>
              </tr>
              <tr>
                <td style="padding:4px 20px 8px 20px;">
                  <table cellpadding="0" cellspacing="0">
                    <tr>
                      <td style="padding-right:20px;color:#555;font-size:13px;">
                        📍 {job['location']}
                      </td>
                      <td style="color:#555;font-size:13px;">
                        {salary_icon} {salary_str}
                      </td>
                    </tr>
                  </table>
                </td>
              </tr>
              <tr>
                <td style="padding:0 20px 14px 20px;color:#444;font-size:13px;line-height:1.6;">
                  {desc_short}
                </td>
              </tr>
              <tr>
                <td style="padding:0 20px 18px 20px;">
                  <a href="{job['url']}" style="
                    display:inline-block;
                    background:#4f46e5;
                    color:#ffffff;
                    padding:8px 18px;
                    border-radius:6px;
                    font-size:13px;
                    font-weight:600;
                    text-decoration:none;
                  ">View & Apply →</a>
                </td>
              </tr>
            </table>
          </td>
        </tr>
        """

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1.0">
  <title>Your London Job Digest</title>
</head>
<body style="margin:0;padding:0;background:#f4f4f8;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;">

  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f4f4f8;padding:30px 0;">
    <tr>
      <td align="center">
        <table width="620" cellpadding="0" cellspacing="0" style="max-width:620px;width:100%;">

          <!-- HEADER -->
          <tr>
            <td style="
              background:linear-gradient(135deg,#4f46e5 0%,#7c3aed 100%);
              border-radius:12px 12px 0 0;
              padding:32px 32px 28px 32px;
              text-align:center;
            ">
              <div style="font-size:32px;margin-bottom:8px;">🔍</div>
              <h1 style="margin:0;color:#ffffff;font-size:24px;font-weight:700;letter-spacing:-0.5px;">
                Your London Job Digest
              </h1>
              <p style="margin:8px 0 0 0;color:rgba(255,255,255,0.85);font-size:15px;">
                Week of {week_of} &nbsp;·&nbsp; {len(jobs)} curated roles
              </p>
            </td>
          </tr>

          <!-- INTRO CARD -->
          <tr>
            <td style="background:#fff;padding:22px 32px 18px 32px;border-bottom:1px solid #eee;">
              <p style="margin:0;color:#333;font-size:14px;line-height:1.7;">
                Hi <strong>Shreya</strong> 👋 — here are this week's top
                <strong>{len(jobs)} London operations roles</strong> matched to your profile.
                Roles are ranked by relevance to your background in
                <em>e-commerce operations, vendor management, and data governance</em>.
              </p>
            </td>
          </tr>

          <!-- JOB CARDS -->
          <tr>
            <td style="padding:24px 20px 0 20px;">
              <table width="100%" cellpadding="0" cellspacing="0">
                {job_cards}
              </table>
            </td>
          </tr>

          <!-- FOOTER -->
          <tr>
            <td style="
              background:#2d2d44;
              border-radius:0 0 12px 12px;
              padding:24px 32px;
              text-align:center;
            ">
              <p style="margin:0 0 6px 0;color:rgba(255,255,255,0.6);font-size:12px;">
                Source: Adzuna &nbsp;|&nbsp; London &amp; surrounding areas
              </p>
              <p style="margin:0;color:rgba(255,255,255,0.4);font-size:11px;">
                This digest is generated automatically every Monday morning.
                Job availability may change — always verify directly with the employer.
              </p>
            </td>
          </tr>

        </table>
      </td>
    </tr>
  </table>

</body>
</html>"""
    return html


# ─────────────────────────────────────────────
# EMAIL SENDING
# ─────────────────────────────────────────────
def send_email(subject: str, html_body: str) -> bool:
    """Send the HTML email via Gmail SMTP."""
    if not GMAIL_USER or not GMAIL_APP_PASS:
        log.error("Gmail credentials missing. Set GMAIL_USER and GMAIL_APP_PASS in .env")
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = FROM_EMAIL
    msg["To"]      = TO_EMAIL
    msg.attach(MIMEText(html_body, "html"))

    try:
        log.info(f"Connecting to Gmail SMTP...")
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as server:
            server.login(GMAIL_USER, GMAIL_APP_PASS)
            server.sendmail(FROM_EMAIL, TO_EMAIL, msg.as_string())
        log.info(f"✅ Email sent successfully to {TO_EMAIL}")
        return True
    except smtplib.SMTPAuthenticationError:
        log.error("SMTP authentication failed. Check your Gmail App Password.")
        return False
    except Exception as e:
        log.error(f"Failed to send email: {e}")
        return False


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────
def run(test_mode: bool = False):
    today = date.today()
    week_of = today.strftime("%B %d, %Y")
    subject = f"🔍 Your London Job Digest – Week of {week_of} ({MAX_JOBS} roles)"

    log.info("=" * 60)
    log.info(f"Job Bot starting — {'TEST MODE' if test_mode else 'SCHEDULED RUN'}")
    log.info(f"Week of: {week_of}")
    log.info("=" * 60)

    log.info("Fetching from Adzuna...")
    all_jobs = fetch_all_adzuna()
    log.info(f"Total raw jobs fetched: {len(all_jobs)}")

    if not all_jobs:
        log.error("No jobs fetched. Check your ADZUNA_APP_ID and ADZUNA_APP_KEY in .env")
        return

    # Dedup → Score → Select
    unique_jobs = deduplicate(all_jobs)
    top_jobs    = rank_and_select(unique_jobs, top_n=MAX_JOBS)

    # Build & send
    html_body = build_html_email(top_jobs, week_of)
    sent = send_email(subject, html_body)

    if sent:
        log.info(f"Done! Sent digest with {len(top_jobs)} jobs.")
    else:
        log.warning("Email failed. Check credentials.")
        # Optionally save HTML locally for debugging
        debug_path = os.path.join(os.path.dirname(__file__), "last_digest.html")
        with open(debug_path, "w", encoding="utf-8") as f:
            f.write(html_body)
        log.info(f"Saved debug HTML to: {debug_path}")


def main():
    parser = argparse.ArgumentParser(description="Shreya's London Job Bot")
    parser.add_argument(
        "--test",
        action="store_true",
        help="Run immediately and send a test email (ignores schedule).",
    )
    args = parser.parse_args()
    run(test_mode=args.test)


if __name__ == "__main__":
    main()

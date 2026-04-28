#!/usr/bin/env python3
"""
Job Bot for Shreya Anantha Subramaniyam
Weekly London job scanner using the Adzuna API + Claude LLM ranking.
Fetches ~100 unique jobs via expanded keyword search, then uses Claude
to select the top 20 best matches based on resume.txt.

Usage:
    python job_bot.py           # Normal run (respects schedule)
    python job_bot.py --test    # Runs immediately, sends one email
"""

import os
import json
import smtplib
import logging
import argparse
import re
from datetime import date
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

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
ADZUNA_APP_ID     = os.getenv("ADZUNA_APP_ID", "")
ADZUNA_APP_KEY    = os.getenv("ADZUNA_APP_KEY", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

GMAIL_USER     = os.getenv("GMAIL_USER", "")
GMAIL_APP_PASS = os.getenv("GMAIL_APP_PASS", "")
FROM_EMAIL     = os.getenv("FROM_EMAIL", GMAIL_USER)
TO_EMAIL       = os.getenv("TO_EMAIL", "shreyaa1693@gmail.com")

TARGET_FETCH = 100   # unique jobs to gather before LLM filtering
OUTPUT_JOBS  = 20    # jobs to include in the digest
ADZUNA_BASE  = "https://api.adzuna.com/v1/api/jobs/gb/search"

LLM_MODEL = "claude-haiku-4-5-20251001"

# ─────────────────────────────────────────────
# SEARCH QUERIES  (derived from resume keywords)
# ─────────────────────────────────────────────
ADZUNA_QUERIES = [
    "operations manager",
    "operations lead",
    "e-commerce operations",
    "vendor operations",
    "merchandising operations",
    "platform operations",
    "business operations manager",
    "data operations manager",
    "marketplace operations",
    "supply chain operations manager",
    "commercial operations manager",
    "retail operations manager",
    "data governance manager",
    "partner operations manager",
]


# ─────────────────────────────────────────────
# RESUME
# ─────────────────────────────────────────────
def load_resume() -> str:
    resume_path = os.path.join(os.path.dirname(__file__), "resume.txt")
    if os.path.exists(resume_path):
        with open(resume_path, encoding="utf-8") as f:
            return f.read().strip()
    # Minimal fallback if resume.txt is missing
    return (
        "Shreya Anantha Subramaniyam — Operations professional, London. "
        "Background: e-commerce ops, vendor management, data governance, "
        "catalogue management, platform ops, supply chain, process improvement. "
        "Target: Operations Manager/Lead, London, £50k–£90k."
    )


# ─────────────────────────────────────────────
# ADZUNA API
# ─────────────────────────────────────────────
def fetch_adzuna_jobs(query: str, page: int = 1, results_per_page: int = 20) -> list[dict]:
    if not ADZUNA_APP_ID or not ADZUNA_APP_KEY:
        log.warning("Adzuna credentials missing – skipping.")
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
        raw_jobs = resp.json().get("results", [])
        jobs = []
        for j in raw_jobs:
            jobs.append({
                "source":       "Adzuna",
                "id":           f"adzuna_{j.get('id', '')}",
                "title":        j.get("title", ""),
                "company":      j.get("company", {}).get("display_name", "Unknown"),
                "location":     j.get("location", {}).get("display_name", "London"),
                "salary_min":   j.get("salary_min"),
                "salary_max":   j.get("salary_max"),
                "description":  j.get("description", "")[:500],
                "url":          j.get("redirect_url", ""),
                "date_posted":  j.get("created", ""),
                "match_reason": "",
                "score":        0,
            })
        log.info(f"  Adzuna [{query!r} p{page}]: {len(jobs)} jobs")
        return jobs
    except requests.RequestException as e:
        log.error(f"  Adzuna [{query!r}] error: {e}")
        return []


def fetch_all_adzuna(target: int = TARGET_FETCH) -> list[dict]:
    """Fetch across all queries (2 pages each) until we have `target` unique jobs by ID."""
    all_jobs: list[dict] = []
    seen_ids: set[str] = set()

    for query in ADZUNA_QUERIES:
        for page in (1, 2):
            jobs = fetch_adzuna_jobs(query, page=page)
            new = [j for j in jobs if j["id"] not in seen_ids]
            seen_ids.update(j["id"] for j in new)
            all_jobs.extend(new)
            log.info(f"  Unique so far: {len(all_jobs)}")
            if len(all_jobs) >= target:
                break
        if len(all_jobs) >= target:
            break

    return all_jobs


# ─────────────────────────────────────────────
# DEDUPLICATION
# ─────────────────────────────────────────────
def deduplicate(jobs: list[dict]) -> list[dict]:
    """Remove duplicates by URL and fuzzy title+company key."""
    seen_urls: set[str] = set()
    seen_keys: set[str] = set()
    unique = []

    for job in jobs:
        url = job.get("url", "")
        key = re.sub(r"[^a-z0-9]", "", (job.get("title", "") + job.get("company", "")).lower())

        if (url and url in seen_urls) or (key and key in seen_keys):
            continue

        if url:
            seen_urls.add(url)
        if key:
            seen_keys.add(key)
        unique.append(job)

    log.info(f"After dedup: {len(unique)} unique (from {len(jobs)} raw)")
    return unique


# ─────────────────────────────────────────────
# LLM RANKING  (primary)
# ─────────────────────────────────────────────
def llm_rank_jobs(jobs: list[dict], resume: str, top_n: int = OUTPUT_JOBS) -> list[dict] | None:
    """
    Ask Claude to pick the top `top_n` jobs from `jobs` based on the resume.
    Each selected job gets a `match_reason` and a `score`.
    Returns None if LLM is unavailable (triggers keyword fallback).
    """
    if not ANTHROPIC_API_KEY:
        log.warning("ANTHROPIC_API_KEY not set — will use keyword fallback.")
        return None

    try:
        import anthropic
    except ImportError:
        log.warning("anthropic package not installed — will use keyword fallback.")
        return None

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    # Build compact job listing for the prompt
    lines = []
    for i, job in enumerate(jobs):
        salary = format_salary(job)
        desc = re.sub(r"<[^>]+>", "", job.get("description", "")).strip()[:300]
        lines.append(
            f"[{i}] {job['title']} | {job['company']} | {job['location']} | {salary}\n{desc}"
        )

    prompt = f"""You are a specialist careers advisor helping a candidate find their next role.

CANDIDATE PROFILE:
{resume}

Below are {len(jobs)} job listings. Select the top {top_n} best matches for this candidate.

Return ONLY valid JSON — no markdown fences, no extra text:
{{
  "selections": [
    {{"index": 0, "score": 9, "match_reason": "One concise sentence (max 120 chars) specific to this candidate."}},
    ...
  ]
}}

Rules:
- Return exactly {top_n} selections (fewer only if strong matches are exhausted).
- Sort by score descending (10 = perfect, 1 = weak).
- Exclude: pure software/engineering roles, graduate/junior roles, unrelated finance/accounting, field sales.
- match_reason must be personalised — reference the candidate's actual background.

JOB LISTINGS:
{chr(10).join(lines)}"""

    log.info(f"Calling Claude ({LLM_MODEL}) to rank {len(jobs)} jobs...")
    try:
        message = client.messages.create(
            model=LLM_MODEL,
            max_tokens=2048,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = message.content[0].text.strip()
        # Strip any accidental markdown fences
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.MULTILINE)
        raw = re.sub(r"\s*```$", "", raw, flags=re.MULTILINE)

        selections = json.loads(raw).get("selections", [])
        ranked: list[dict] = []
        for sel in selections[:top_n]:
            idx = sel.get("index")
            if idx is None or not (0 <= idx < len(jobs)):
                continue
            job = dict(jobs[idx])
            job["score"] = sel.get("score", 5)
            job["match_reason"] = sel.get("match_reason", "")
            ranked.append(job)

        ranked.sort(key=lambda j: j["score"], reverse=True)
        log.info(f"LLM selected {len(ranked)} jobs (top score: {ranked[0]['score'] if ranked else 0})")
        return ranked

    except Exception as e:
        log.error(f"LLM ranking failed ({e}) — falling back to keyword scoring.")
        return None


# ─────────────────────────────────────────────
# KEYWORD FALLBACK SCORING
# ─────────────────────────────────────────────
_POSITIVE = [
    "operations", "e-commerce", "ecommerce", "retail", "vendor", "merchandising",
    "platform", "data governance", "onboarding", "partner", "marketplace",
    "catalogue", "catalog", "supply chain", "continuous improvement", "sla",
    "stakeholder", "workflow", "process improvement", "agile", "cross-functional",
    "migration", "data quality", "service operations", "team lead",
]
_NEGATIVE = [
    "software engineer", "developer", "coding", "devops", "data engineer",
    "machine learning", "ml engineer", "junior", "graduate", "intern",
    "accountant", "finance manager", "financial analyst", "actuary",
    "recruitment consultant", "sales executive", "field sales",
]
_TITLE_BOOST = [
    "operations manager", "operations lead", "e-commerce operations",
    "vendor operations", "merchandising", "platform operations",
    "business operations", "data operations",
]


def _score_job(job: dict) -> int:
    score = 0
    title = (job.get("title") or "").lower()
    text = f"{title} {(job.get('description') or '').lower()}"
    loc = (job.get("location") or "").lower()
    lo, hi = job.get("salary_min") or 0, job.get("salary_max") or 0

    for kw in _NEGATIVE:
        if kw in text:
            score -= 20
    for kw in _TITLE_BOOST:
        if kw in title:
            score += 15
    for kw in _POSITIVE:
        if kw in text:
            score += 2
    if "london" in loc:
        score += 10
    if "hybrid" in text or "remote" in text:
        score += 5
    avg = (lo + hi) / 2 if hi else lo
    if 45_000 <= avg <= 95_000:
        score += 8
    elif avg > 95_000:
        score += 3
    return score


def keyword_rank_and_select(jobs: list[dict], top_n: int = OUTPUT_JOBS) -> list[dict]:
    for job in jobs:
        job["score"] = _score_job(job)
        job["match_reason"] = ""
    ranked = sorted(jobs, key=lambda j: j["score"], reverse=True)
    selected = ranked[:top_n]
    log.info(f"Keyword fallback: top {len(selected)} (highest score: {selected[0]['score'] if selected else 0})")
    return selected


# ─────────────────────────────────────────────
# EMAIL FORMATTING
# ─────────────────────────────────────────────
def format_salary(job: dict) -> str:
    lo, hi = job.get("salary_min"), job.get("salary_max")
    if lo and hi:
        return f"£{lo:,.0f} – £{hi:,.0f}"
    if lo:
        return f"£{lo:,.0f}+"
    if hi:
        return f"up to £{hi:,.0f}"
    return "Not specified"


def _truncate(text: str, length: int = 220) -> str:
    text = re.sub(r"<[^>]+>", "", text or "")
    text = re.sub(r"\s+", " ", text).strip()
    return text[:length].rstrip() + "…" if len(text) > length else text


def build_html_email(jobs: list[dict], week_of: str, llm_powered: bool = True) -> str:
    job_cards = ""
    for i, job in enumerate(jobs, start=1):
        salary_str   = format_salary(job)
        desc_short   = _truncate(job.get("description", ""))
        match_reason = job.get("match_reason", "")

        source_badge = (
            '<span style="background:#e8f4fd;color:#1a73e8;padding:2px 8px;'
            'border-radius:12px;font-size:11px;font-weight:600;">'
            f'{job["source"]}</span>'
        )
        date_str  = job.get("date_posted", "")[:10]
        date_html = f'<span style="color:#888;font-size:12px;">Posted: {date_str}</span>' if date_str else ""

        match_html = ""
        if match_reason:
            match_html = f"""
              <tr>
                <td style="padding:0 20px 12px 20px;">
                  <div style="background:#f0fdf4;border-left:3px solid #22c55e;border-radius:0 6px 6px 0;
                              padding:8px 12px;font-size:12px;color:#166534;line-height:1.5;">
                    ✨ <strong>Why it matches:</strong> {match_reason}
                  </div>
                </td>
              </tr>"""

        job_cards += f"""
        <tr>
          <td style="padding:0 0 20px 0;">
            <table width="100%" cellpadding="0" cellspacing="0" style="
              background:#ffffff;border:1px solid #e0e0e0;border-radius:10px;
              overflow:hidden;border-left:4px solid #4f46e5;">
              <tr>
                <td style="padding:18px 20px 6px 20px;">
                  <table width="100%" cellpadding="0" cellspacing="0">
                    <tr>
                      <td>
                        <span style="color:#888;font-size:12px;font-weight:600;letter-spacing:1px;">#{i}</span>
                        &nbsp; {source_badge} &nbsp; {date_html}
                      </td>
                    </tr>
                    <tr>
                      <td style="padding-top:6px;">
                        <a href="{job['url']}" style="color:#1a1a2e;font-size:17px;font-weight:700;
                                                      text-decoration:none;line-height:1.3;">{job['title']}</a>
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
                      <td style="padding-right:20px;color:#555;font-size:13px;">📍 {job['location']}</td>
                      <td style="color:#555;font-size:13px;">{"💰" if salary_str != "Not specified" else "💼"} {salary_str}</td>
                    </tr>
                  </table>
                </td>
              </tr>
              <tr>
                <td style="padding:0 20px 14px 20px;color:#444;font-size:13px;line-height:1.6;">
                  {desc_short}
                </td>
              </tr>
              {match_html}
              <tr>
                <td style="padding:0 20px 18px 20px;">
                  <a href="{job['url']}" style="display:inline-block;background:#4f46e5;color:#ffffff;
                    padding:8px 18px;border-radius:6px;font-size:13px;font-weight:600;text-decoration:none;">
                    View &amp; Apply →
                  </a>
                </td>
              </tr>
            </table>
          </td>
        </tr>"""

    method_label = "AI-curated" if llm_powered else "keyword-matched"
    screened_note = (
        "Screened by Claude AI from 100+ listings based on your resume."
        if llm_powered else
        "Ranked by keyword matching against your profile."
    )
    footer_note = "Ranked by Claude AI (Haiku) &nbsp;·&nbsp; " if llm_powered else ""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1.0">
  <title>Your London Job Digest</title>
</head>
<body style="margin:0;padding:0;background:#f4f4f8;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f4f4f8;padding:30px 0;">
    <tr><td align="center">
      <table width="620" cellpadding="0" cellspacing="0" style="max-width:620px;width:100%;">

        <tr>
          <td style="background:linear-gradient(135deg,#4f46e5 0%,#7c3aed 100%);
                     border-radius:12px 12px 0 0;padding:32px 32px 28px 32px;text-align:center;">
            <div style="font-size:32px;margin-bottom:8px;">🔍</div>
            <h1 style="margin:0;color:#ffffff;font-size:24px;font-weight:700;letter-spacing:-0.5px;">
              Your London Job Digest
            </h1>
            <p style="margin:8px 0 0 0;color:rgba(255,255,255,0.85);font-size:15px;">
              Week of {week_of} &nbsp;·&nbsp; {len(jobs)} {method_label} roles
            </p>
          </td>
        </tr>

        <tr>
          <td style="background:#fff;padding:22px 32px 18px 32px;border-bottom:1px solid #eee;">
            <p style="margin:0;color:#333;font-size:14px;line-height:1.7;">
              Hi <strong>Shreya</strong> 👋 — here are this week's top
              <strong>{len(jobs)} London operations roles</strong> curated for you.
              {screened_note}
              Each card shows a personalised reason why it matches your background.
            </p>
          </td>
        </tr>

        <tr>
          <td style="padding:24px 20px 0 20px;">
            <table width="100%" cellpadding="0" cellspacing="0">{job_cards}</table>
          </td>
        </tr>

        <tr>
          <td style="background:#2d2d44;border-radius:0 0 12px 12px;padding:24px 32px;text-align:center;">
            <p style="margin:0 0 6px 0;color:rgba(255,255,255,0.6);font-size:12px;">
              {footer_note}Source: Adzuna &nbsp;|&nbsp; London &amp; surrounding areas
            </p>
            <p style="margin:0;color:rgba(255,255,255,0.4);font-size:11px;">
              Generated automatically every Monday morning.
              Job availability may change — always verify directly with the employer.
            </p>
          </td>
        </tr>

      </table>
    </td></tr>
  </table>
</body>
</html>"""


# ─────────────────────────────────────────────
# EMAIL SENDING
# ─────────────────────────────────────────────
def send_email(subject: str, html_body: str) -> bool:
    if not GMAIL_USER or not GMAIL_APP_PASS:
        log.error("Gmail credentials missing. Set GMAIL_USER and GMAIL_APP_PASS in .env")
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = FROM_EMAIL
    msg["To"]      = TO_EMAIL
    msg.attach(MIMEText(html_body, "html"))

    try:
        log.info("Connecting to Gmail SMTP...")
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as server:
            server.login(GMAIL_USER, GMAIL_APP_PASS)
            server.sendmail(FROM_EMAIL, TO_EMAIL, msg.as_string())
        log.info(f"✅ Email sent to {TO_EMAIL}")
        return True
    except smtplib.SMTPAuthenticationError:
        log.error("SMTP auth failed. Check your Gmail App Password.")
        return False
    except Exception as e:
        log.error(f"Failed to send email: {e}")
        return False


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────
def run(test_mode: bool = False):
    today    = date.today()
    week_of  = today.strftime("%B %d, %Y")

    log.info("=" * 60)
    log.info(f"Job Bot starting — {'TEST MODE' if test_mode else 'SCHEDULED RUN'}")
    log.info(f"Week of: {week_of}")
    log.info("=" * 60)

    resume = load_resume()
    log.info(f"Loaded resume ({len(resume)} chars)")

    log.info(f"Fetching up to {TARGET_FETCH} unique jobs from Adzuna...")
    raw_jobs = fetch_all_adzuna(target=TARGET_FETCH)
    log.info(f"Raw fetch: {len(raw_jobs)} jobs")

    if not raw_jobs:
        log.error("No jobs fetched. Check ADZUNA_APP_ID and ADZUNA_APP_KEY in .env")
        return

    unique_jobs = deduplicate(raw_jobs)

    top_jobs = llm_rank_jobs(unique_jobs, resume, top_n=OUTPUT_JOBS)
    llm_powered = top_jobs is not None
    if not llm_powered:
        top_jobs = keyword_rank_and_select(unique_jobs, top_n=OUTPUT_JOBS)

    method  = "AI-ranked" if llm_powered else "keyword-ranked"
    subject = f"🔍 Your London Job Digest – Week of {week_of} ({OUTPUT_JOBS} {method} roles)"

    html_body = build_html_email(top_jobs, week_of, llm_powered=llm_powered)
    sent = send_email(subject, html_body)

    if sent:
        log.info(f"Done! Sent digest with {len(top_jobs)} jobs ({method}).")
    else:
        log.warning("Email failed. Saving debug HTML locally.")
        debug_path = os.path.join(os.path.dirname(__file__), "last_digest.html")
        with open(debug_path, "w", encoding="utf-8") as f:
            f.write(html_body)
        log.info(f"Saved to: {debug_path}")


def main():
    parser = argparse.ArgumentParser(description="Shreya's London Job Bot")
    parser.add_argument("--test", action="store_true",
                        help="Run immediately and send a test email.")
    run(test_mode=parser.parse_args().test)


if __name__ == "__main__":
    main()

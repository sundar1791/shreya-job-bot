#!/usr/bin/env python3
"""
Job Bot for Shreya Anantha Subramaniyam
Weekly London job scanner via JSearch + Active Jobs DB + LinkedIn, ranked by Claude Sonnet.
Sends an HTML email digest and writes output/jobs.json for the GitHub Pages frontend.

Usage:
    python job_bot.py           # Normal run
    python job_bot.py --test    # Runs immediately, sends one email
"""

import os
import json
import smtplib
import logging
import argparse
import re
from datetime import date, datetime, timezone
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from dotenv import load_dotenv

# ─────────────────────────────────────────────────────────────────
# SETUP
# ─────────────────────────────────────────────────────────────────
load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("job_bot")


# ─────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────
JSEARCH_API_KEY   = os.getenv("JSEARCH_API_KEY", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

GMAIL_USER     = os.getenv("GMAIL_USER", "")
GMAIL_APP_PASS = os.getenv("GMAIL_APP_PASS", "")
FROM_EMAIL     = os.getenv("FROM_EMAIL", GMAIL_USER)
TO_EMAIL       = os.getenv("TO_EMAIL", "shreyaa1693@gmail.com")

OUTPUT_JOBS      = 20
PRE_FILTER_TOP_N = 150   # candidates forwarded to LLM after keyword pre-filter
OUTPUT_DIR       = os.path.join(os.path.dirname(__file__), "output")

JSEARCH_BASE    = "https://jsearch.p.rapidapi.com/search"
JSEARCH_HOST    = "jsearch.p.rapidapi.com"
ACTIVEJOBS_BASE = "https://active-jobs-db.p.rapidapi.com/active-ats-7d"
ACTIVEJOBS_HOST = "active-jobs-db.p.rapidapi.com"
LINKEDIN_BASE   = "https://linkedin-job-search-api.p.rapidapi.com/active-jb-7d"
LINKEDIN_HOST   = "linkedin-job-search-api.p.rapidapi.com"

LLM_MODEL = "claude-sonnet-4-6"

# JSearch — 10 queries, page 1 only (10 req/run × 4 = 40/month; free tier = 200/month).
SEARCH_QUERIES = [
    "ecommerce operations manager London",
    "marketplace operations manager London",
    "vendor operations manager London",
    "seller operations manager London",
    "platform operations manager London",
    "catalogue manager ecommerce London",
    "vendor onboarding manager London",
    "customer success manager ecommerce London",
    "data governance manager London",
    "merchandising operations manager London",
]

# LinkedIn — 4 queries, limit=15 each (max 60 jobs/run × 4 = 240/month; free tier = 250 jobs + 25 req/month).
# Uses advanced_title_filter PostgreSQL operators: & (AND), | (OR), ! (NOT),
# single-quoted phrases (exact order), :* (prefix wildcard matches manager/management/managing).
# Unquoted single words match anywhere in the title; avoid rare multi-word phrases as standalone filters.
LINKEDIN_QUERIES = [
    # Core ecommerce / marketplace / vendor operations roles
    "(ecommerce | 'e-commerce' | marketplace | vendor | seller | partner) & (operations | onboarding | manag:*)",
    # Catalogue / merchandising / data governance / data quality
    "(catalogue | catalog | merchandising | taxonomy | 'data governance' | 'data quality') & (manag:* | lead | head)",
    # Customer success scoped to ecommerce / marketplace platforms
    "'customer success' & (ecommerce | 'e-commerce' | marketplace | platform | retail | digital)",
    # Platform / digital / online retail operations management
    "(platform | digital | 'online retail') & operations & (manag:* | lead | head)",
]

# Active Jobs DB — title_filter is Google-like natural language (no AND/OR syntax).
ACTIVEJOBS_QUERIES = [
    "ecommerce operations manager",
    "marketplace operations manager",
    "vendor operations manager",
    "seller operations manager",
    "platform operations manager",
    "digital operations manager",
    "catalogue operations manager",
    "vendor onboarding manager",
    "customer success manager ecommerce",
    "customer success manager marketplace",
    "data governance manager",
    "merchandising operations manager",
    "online retail operations manager",
]


# ─────────────────────────────────────────────────────────────────
# RESUME
# ─────────────────────────────────────────────────────────────────
def load_resume() -> str:
    resume_path = os.path.join(os.path.dirname(__file__), "resume.txt")
    if os.path.exists(resume_path):
        with open(resume_path, encoding="utf-8") as f:
            return f.read().strip()
    return (
        "Shreya Anantha Subramaniyam — Operations professional, London. "
        "Background: e-commerce ops, vendor management, data governance, "
        "catalogue management, platform ops, supply chain, process improvement. "
        "Target: Operations Manager/Lead, London, £50k–£90k."
    )


# ─────────────────────────────────────────────────────────────────
# API FETCHING
# ─────────────────────────────────────────────────────────────────
def _normalize_salary(raw: float | None, period: str | None) -> float | None:
    if raw is None:
        return None
    period = (period or "").upper()
    if period == "MONTH":
        return raw * 12
    if period == "WEEK":
        return raw * 52
    if period == "HOUR":
        return raw * 40 * 52
    return raw


def fetch_jsearch_jobs(query: str, page: int = 1) -> list[dict]:
    if not JSEARCH_API_KEY:
        log.warning("JSEARCH_API_KEY missing – skipping.")
        return []
    params = {
        "query":       query,
        "page":        page,
        "num_pages":   1,
        "date_posted": "month",
    }
    headers = {
        "X-RapidAPI-Key":  JSEARCH_API_KEY,
        "X-RapidAPI-Host": JSEARCH_HOST,
    }
    try:
        resp = requests.get(JSEARCH_BASE, params=params, headers=headers, timeout=30)
        resp.raise_for_status()
        raw_jobs = resp.json().get("data", [])
        jobs = []
        for j in raw_jobs:
            period = j.get("job_salary_period")
            location_parts = [p for p in [
                j.get("job_city"), j.get("job_state"),
                (j.get("job_country") or "").upper() or None,
            ] if p]
            location = ", ".join(location_parts) if location_parts else "London, UK"
            jobs.append({
                "source":       "JSearch",
                "id":           f"jsearch_{j.get('job_id', '')}",
                "title":        j.get("job_title", ""),
                "company":      j.get("employer_name", "Unknown"),
                "location":     location,
                "salary_min":   _normalize_salary(j.get("job_min_salary"), period),
                "salary_max":   _normalize_salary(j.get("job_max_salary"), period),
                "salary_str":   "",
                "description":  j.get("job_description", "")[:500],
                "url":          j.get("job_apply_link", ""),
                "date_posted":  j.get("job_posted_at_datetime_utc", ""),
                "match_reason": "",
                "score":        0,
            })
        log.info(f"  JSearch [{query!r} p{page}]: {len(jobs)} jobs")
        return jobs
    except requests.RequestException as e:
        log.error(f"  JSearch [{query!r}] error: {e}")
        return []


def fetch_activejobs_jobs(query: str) -> list[dict]:
    """Fetch up to 100 jobs in a single request (max allowed by the API)."""
    if not JSEARCH_API_KEY:
        log.warning("JSEARCH_API_KEY missing – skipping Active Jobs DB.")
        return []
    params = {
        "title_filter":               query,
        "location_filter":            "London OR United Kingdom",
        "description_type":           "text",
        "ai_employment_type_filter":  "FULL_TIME",
        "ai_experience_level_filter": "2-5,5-10,10+",
        "ai_taxonomies_a_exclusion_filter": "Logistics,Transportation",
        "offset":                     0,
        "limit":                      100,
    }
    headers = {
        "X-RapidAPI-Key":  JSEARCH_API_KEY,
        "X-RapidAPI-Host": ACTIVEJOBS_HOST,
    }
    try:
        resp = requests.get(ACTIVEJOBS_BASE, params=params, headers=headers, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        raw_jobs = data if isinstance(data, list) else data.get("data", [])
        jobs = []
        for j in raw_jobs:
            url = (j.get("url") or j.get("apply_url") or j.get("job_url")
                   or j.get("apply_link") or j.get("source_url") or "")
            uid = (j.get("id") or j.get("job_id")
                   or f"{j.get('title', '')}{j.get('organization', '')}")
            jobs.append({
                "source":       "ActiveJobsDB",
                "id":           f"activejobs_{uid}",
                "title":        j.get("title", ""),
                "company":      j.get("organization", "Unknown"),
                "location":     j.get("location", "London, UK"),
                "salary_min":   None,
                "salary_max":   None,
                "salary_str":   "",
                "description":  (j.get("description_text") or j.get("description") or "")[:500],
                "url":          url,
                "date_posted":  j.get("date_posted", ""),
                "match_reason": "",
                "score":        0,
            })
        log.info(f"  ActiveJobsDB [{query!r}]: {len(jobs)} jobs")
        return jobs
    except requests.RequestException as e:
        log.error(f"  ActiveJobsDB [{query!r}] error: {e}")
        return []


def fetch_linkedin_jobs(query: str) -> list[dict]:
    """Fetch up to 15 LinkedIn jobs per query (4 queries × 15 = 60 jobs/run max; budget-safe)."""
    if not JSEARCH_API_KEY:
        log.warning("JSEARCH_API_KEY missing – skipping LinkedIn.")
        return []
    params = {
        "advanced_title_filter": query,
        "location_filter":       "London OR United Kingdom",
        "type_filter":           "FULL_TIME",
        "seniority_filter":      "Mid-Senior level,Associate,Director",
        "description_type":      "text",
        "exclude_ats_duplicate": "true",
        "offset":                0,
        "limit":                 15,
    }
    headers = {
        "X-RapidAPI-Key":  JSEARCH_API_KEY,
        "X-RapidAPI-Host": LINKEDIN_HOST,
    }
    try:
        resp = requests.get(LINKEDIN_BASE, params=params, headers=headers, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        raw_jobs = data if isinstance(data, list) else data.get("data", [])
        jobs = []
        for j in raw_jobs:
            locs = j.get("locations_derived") or []
            location = locs[0] if isinstance(locs, list) and locs else j.get("location", "London, UK")
            salary_raw = j.get("salary_raw") or ""
            jobs.append({
                "source":       "LinkedIn",
                "id":           f"linkedin_{j.get('id') or j.get('job_id') or j.get('url', '')}",
                "title":        j.get("title", ""),
                "company":      j.get("organization", "Unknown"),
                "location":     location,
                "salary_min":   None,
                "salary_max":   None,
                "salary_str":   str(salary_raw) if salary_raw else "",
                "description":  (j.get("description_text") or j.get("description") or "")[:500],
                "url":          j.get("url", ""),
                "date_posted":  j.get("date_posted", ""),
                "match_reason": "",
                "score":        0,
            })
        log.info(f"  LinkedIn [{query[:60]!r}]: {len(jobs)} jobs")
        return jobs
    except requests.RequestException as e:
        log.error(f"  LinkedIn [{query[:60]!r}] error: {e}")
        return []


def fetch_all_jobs() -> list[dict]:
    """Fetch from JSearch (parallel page-1 only), Active Jobs DB, and LinkedIn (both sequential)."""
    if not JSEARCH_API_KEY:
        log.error("JSEARCH_API_KEY not set.")
        return []

    all_jobs: list[dict] = []
    seen_ids: set[str] = set()

    # JSearch: page 1 only for all queries, fired concurrently.
    # Pages 2-3 skipped — they rarely add unique results and burn monthly quota.
    log.info(f"JSearch: firing {len(SEARCH_QUERIES)} page-1 requests concurrently...")

    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = {pool.submit(fetch_jsearch_jobs, q, 1): q for q in SEARCH_QUERIES}
        for future in as_completed(futures):
            query = futures[future]
            try:
                jobs = future.result()
            except Exception as e:
                log.error(f"JSearch task failed: {e}")
                continue
            new = [j for j in jobs if j["id"] not in seen_ids]
            seen_ids.update(j["id"] for j in new)
            all_jobs.extend(new)

    jsearch_count = len(all_jobs)
    log.info(f"JSearch total: {jsearch_count} unique jobs")

    # Active Jobs DB: sequential to respect rate limits.
    log.info(f"ActiveJobsDB: fetching {len(ACTIVEJOBS_QUERIES)} queries sequentially...")
    for query in ACTIVEJOBS_QUERIES:
        jobs = fetch_activejobs_jobs(query)
        new = [j for j in jobs if j["id"] not in seen_ids]
        seen_ids.update(j["id"] for j in new)
        all_jobs.extend(new)

    activejobs_count = len(all_jobs) - jsearch_count
    log.info(f"ActiveJobsDB total: {activejobs_count} unique new jobs")

    # LinkedIn: sequential to respect rate limits.
    log.info(f"LinkedIn: fetching {len(LINKEDIN_QUERIES)} queries sequentially...")
    for query in LINKEDIN_QUERIES:
        jobs = fetch_linkedin_jobs(query)
        new = [j for j in jobs if j["id"] not in seen_ids]
        seen_ids.update(j["id"] for j in new)
        all_jobs.extend(new)

    linkedin_count = len(all_jobs) - jsearch_count - activejobs_count
    log.info(
        f"Total fetched: {len(all_jobs)} unique jobs "
        f"({jsearch_count} JSearch + {activejobs_count} ActiveJobsDB + {linkedin_count} LinkedIn)"
    )
    return all_jobs


# ─────────────────────────────────────────────────────────────────
# DEDUPLICATION
# ─────────────────────────────────────────────────────────────────
def deduplicate(jobs: list[dict]) -> list[dict]:
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


# ─────────────────────────────────────────────────────────────────
# KEYWORD SCORING  (used for pre-filter and fallback ranking)
# ─────────────────────────────────────────────────────────────────
_POSITIVE = [
    "e-commerce", "ecommerce", "marketplace", "vendor", "seller", "onboarding",
    "catalogue", "catalog", "merchandising", "platform operations", "data governance",
    "data quality", "partner", "sla", "workflow", "process improvement",
    "continuous improvement", "agile", "cross-functional", "product and engineering",
    "migration", "service operations", "team lead", "digital platform",
]
_NEGATIVE = [
    "software engineer", "developer", "coding", "devops", "data engineer",
    "machine learning", "ml engineer", "junior", "graduate", "intern",
    "accountant", "finance manager", "financial analyst", "field sales",
    "revenue strategy", "commercial strategy", "demand planning", "inventory planning",
    "supply chain", "procurement", "m&a", "mergers", "acquisitions",
    "transformation programme", "strategic initiatives", "divestment",
    "recruitment consultant", "freight", "forwarder", "forwarding", "haulage",
    "shipping manager", "logistics manager", "warehouse", "distribution",
    "transport manager", "product owner", "product manager",
    "general manager", "managing director",
]
_TITLE_BOOST = [
    "vendor operations", "seller operations", "e-commerce operations",
    "platform operations", "marketplace operations", "catalogue operations",
    "data governance", "onboarding manager", "vendor onboarding",
    "operations lead", "operations manager",
]

# Title-level hard disqualifiers — removed before the LLM sees anything.
_TITLE_DISQUALIFIERS = [
    "freight", "forwarder", "forwarding", "haulage", "shipping manager",
    "logistics manager", "warehouse", "distribution manager", "transport manager",
    "software engineer", "data engineer", "machine learning", "devops",
    "recruitment consultant", "talent acquisition", "hr manager",
    "financial analyst", "finance manager", "accountant",
    "managing director",
]


def _score_job_keyword(job: dict) -> int:
    score = 0
    title = (job.get("title") or "").lower()
    text  = f"{title} {(job.get('description') or '').lower()}"
    loc   = (job.get("location") or "").lower()
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


def pre_filter_jobs(jobs: list[dict], top_n: int = PRE_FILTER_TOP_N) -> list[dict]:
    """Remove obvious title-level disqualifiers, then return top_n by keyword score."""
    filtered = []
    removed = 0
    for job in jobs:
        title = (job.get("title") or "").lower()
        if any(kw in title for kw in _TITLE_DISQUALIFIERS):
            removed += 1
            continue
        filtered.append(job)

    log.info(f"Pre-filter: removed {removed} disqualified by title, {len(filtered)} remaining")

    for job in filtered:
        job["_pre_score"] = _score_job_keyword(job)
    filtered.sort(key=lambda j: j["_pre_score"], reverse=True)

    selected = filtered[:top_n]
    log.info(f"Pre-filter: forwarding top {len(selected)} candidates to LLM")
    return selected


def keyword_rank_and_select(jobs: list[dict], top_n: int = OUTPUT_JOBS) -> list[dict]:
    for job in jobs:
        raw = _score_job_keyword(job)
        job["score"] = max(1, min(10, round((raw + 40) / 12)))
        job["match_reason"] = ""
    ranked = sorted(jobs, key=lambda j: j["score"], reverse=True)
    selected = ranked[:top_n]
    log.info(f"Keyword fallback: top {len(selected)}, scores {[j['score'] for j in selected]}")
    return selected


# ─────────────────────────────────────────────────────────────────
# LLM RANKING
# ─────────────────────────────────────────────────────────────────
def llm_rank_jobs(jobs: list[dict], resume: str, top_n: int = OUTPUT_JOBS) -> list[dict] | None:
    """Ask Claude Sonnet to pick the top `top_n` jobs with 1–10 scores."""
    if not ANTHROPIC_API_KEY:
        log.warning("ANTHROPIC_API_KEY not set — keyword fallback.")
        return None
    try:
        import anthropic
    except ImportError:
        log.warning("anthropic not installed — keyword fallback.")
        return None

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    lines = []
    for i, job in enumerate(jobs):
        salary = format_salary(job)
        desc = re.sub(r"<[^>]+>", "", job.get("description", "")).strip()[:300]
        lines.append(
            f"[{i}] {job['title']} | {job['company']} | {job['location']} | {salary}\n{desc}"
        )

    prompt = f"""You are a specialist careers advisor. Screen these job listings against the candidate's full profile below.

CANDIDATE PROFILE:
{resume}

HARD DISQUALIFIERS — score 1–3 and exclude from selections if the role is primarily:
- Freight forwarding, shipping, haulage, or physical logistics/transport operations
- Supply chain logistics, demand planning, inventory forecasting, or warehouse management
- Revenue strategy, financial strategy, commercial strategy, or pricing strategy
- M&A, corporate transformation, divestments, or strategic consulting
- Technical Product Manager or Product Owner (software delivery, roadmap ownership, agile ceremonies)
- Software/data engineering, machine learning, or DevOps
- Recruitment, HR operations, or talent acquisition
- Field sales, account management, or business development
- Finance, accounting, or roles requiring CPA/CIMA qualifications
- Graduate, junior, or entry-level positions
- General Manager of a logistics or distribution company

A job scores 7–10 ONLY if it directly involves one or more of:
- E-commerce or marketplace operations management (managing platform workflows, seller/vendor lifecycle)
- Vendor or seller onboarding, activation, enablement, and support
- Product catalogue management, taxonomy governance, or data quality for an online retail platform
- Customer success / account management for an e-commerce SaaS or marketplace platform (NOT field sales)
- Digital platform operations collaborating with Product & Engineering
- Operations team leadership (people management) in a tech/retail/marketplace/e-commerce company

IMPORTANT: The candidate is a specialist in e-commerce marketplace / vendor / catalogue operations.
Do NOT give a passing score (≥6) to:
- Any role in freight, logistics, or physical goods movement, even if titled “Operations Manager”
- Any “General Manager” role for a logistics, freight, or supply-chain company
- Any “Product Owner” or “Technical Product Manager” role focused on software delivery
- Generic “Operations Manager” roles in financial services, consulting, or professional services

Below are {len(jobs)} pre-filtered job listings. Select the top {top_n} best matches.

Return ONLY valid JSON — no markdown fences, no extra text:
{{
  "selections": [
    {{"index": 0, "score": 9, "match_reason": "One concise sentence (max 120 chars) personalised to this candidate."}},
    ...
  ]
}}

Rules:
- Return EXACTLY {top_n} selections, ranked by score. Only return fewer if there are genuinely fewer than {top_n} jobs in the input.
- score 1–10: 10 = direct match on e-commerce/vendor/catalogue ops, 1 = hard disqualifier.
- Spread scores — do not cluster everything at 7–8. A 9–10 should be rare and only for an almost perfect match.
- Sort by score descending.
- match_reason must reference the candidate's specific background (e.g. vendor onboarding at scale, catalogue taxonomy governance, 10K+ partner migration at Lowe's, ICF coaching qualification, etc.).

JOB LISTINGS:
{chr(10).join(lines)}"""

    log.info(f"Calling Claude ({LLM_MODEL}) to rank {len(jobs)} jobs...")
    try:
        message = client.messages.create(
            model=LLM_MODEL,
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = message.content[0].text.strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.MULTILINE)
        raw = re.sub(r"\s*```$", "", raw, flags=re.MULTILINE)

        selections = json.loads(raw).get("selections", [])
        ranked: list[dict] = []
        for sel in selections[:top_n]:
            idx = sel.get("index")
            if idx is None or not (0 <= idx < len(jobs)):
                continue
            job = dict(jobs[idx])
            job["score"] = max(1, min(10, int(sel.get("score", 5))))
            job["match_reason"] = sel.get("match_reason", "")
            ranked.append(job)

        ranked.sort(key=lambda j: j["score"], reverse=True)
        log.info(f"LLM selected {len(ranked)} jobs, scores {[j['score'] for j in ranked]}")
        return ranked

    except Exception as e:
        log.error(f"LLM ranking failed ({e}) — keyword fallback.")
        return None


# ─────────────────────────────────────────────────────────────────
# SALARY FORMATTING
# ─────────────────────────────────────────────────────────────────
def format_salary(job: dict) -> str:
    if job.get("salary_str"):
        return job["salary_str"]
    lo, hi = job.get("salary_min"), job.get("salary_max")
    if lo and hi:
        return f"£{lo:,.0f} – £{hi:,.0f}"
    if lo:
        return f"£{lo:,.0f}+"
    if hi:
        return f"up to £{hi:,.0f}"
    return "Not specified"


# ─────────────────────────────────────────────────────────────────
# JSON OUTPUT  (for frontend)
# ─────────────────────────────────────────────────────────────────
def save_jobs_json(jobs: list[dict], week_of: str, llm_powered: bool) -> None:
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "week_of":      week_of,
        "llm_powered":  llm_powered,
        "jobs": [
            {
                "rank":         i + 1,
                "title":        j["title"],
                "company":      j["company"],
                "location":     j["location"],
                "salary":       format_salary(j),
                "description":  j.get("description", ""),
                "url":          j["url"],
                "date_posted":  j.get("date_posted", ""),
                "score":        j["score"],
                "match_reason": j.get("match_reason", ""),
                "source":       j["source"],
            }
            for i, j in enumerate(jobs)
        ],
    }
    path = os.path.join(OUTPUT_DIR, "jobs.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    log.info(f"Saved {len(jobs)} jobs to {path}")


# ─────────────────────────────────────────────────────────────────
# EMAIL FORMATTING
# ─────────────────────────────────────────────────────────────────
def _truncate(text: str, length: int = 220) -> str:
    text = re.sub(r"<[^>]+>", "", text or "")
    text = re.sub(r"\s+", " ", text).strip()
    return text[:length].rstrip() + "…" if len(text) > length else text


def _score_color(score: int) -> tuple[str, str]:
    if score >= 8:
        return "#16a34a", "#ffffff"
    if score >= 6:
        return "#ca8a04", "#ffffff"
    return "#dc2626", "#ffffff"


def build_html_email(jobs: list[dict], week_of: str, llm_powered: bool = True) -> str:
    job_cards = ""
    for i, job in enumerate(jobs, start=1):
        salary_str   = format_salary(job)
        desc_short   = _truncate(job.get("description", ""))
        match_reason = job.get("match_reason", "")
        score        = job.get("score", 0)
        score_bg, score_fg = _score_color(score)

        date_str  = job.get("date_posted", "")[:10]
        date_html = (
            f'<span style="color:#888;font-size:12px;">Posted: {date_str}</span>'
            if date_str else ""
        )

        match_html = ""
        if match_reason:
            match_html = f"""
              <tr>
                <td style="padding:0 20px 12px 20px;">
                  <div style="background:#f0fdf4;border-left:3px solid #22c55e;
                              border-radius:0 6px 6px 0;padding:8px 12px;
                              font-size:12px;color:#166534;line-height:1.5;">
                    ✨ <strong>Why it matches:</strong> {match_reason}
                  </div>
                </td>
              </tr>"""

        score_badge = (
            f'<span style="background:{score_bg};color:{score_fg};padding:3px 10px;'
            f'border-radius:12px;font-size:12px;font-weight:700;">'
            f'Score: {score}/10</span>'
        )
        money_icon = "💰" if salary_str != "Not specified" else "💼"

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
                        <span style="color:#888;font-size:12px;font-weight:600;">#{i}</span>
                        &nbsp; {score_badge} &nbsp; {date_html}
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
                      <td style="color:#555;font-size:13px;">{money_icon} {salary_str}</td>
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
        "Screened by Claude AI from hundreds of listings based on your full resume."
        if llm_powered else
        "Ranked by keyword matching against your profile."
    )
    ai_credit = "Ranked by Claude Sonnet &nbsp;·&nbsp; " if llm_powered else ""

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
            <h1 style="margin:0;color:#ffffff;font-size:24px;font-weight:700;">Your London Job Digest</h1>
            <p style="margin:8px 0 0 0;color:rgba(255,255,255,0.85);font-size:15px;">
              Week of {week_of} &nbsp;·&nbsp; {len(jobs)} {method_label} roles
            </p>
          </td>
        </tr>
        <tr>
          <td style="background:#fff;padding:22px 32px 18px 32px;border-bottom:1px solid #eee;">
            <p style="margin:0;color:#333;font-size:14px;line-height:1.7;">
              Hi <strong>Shreya</strong> 👋 — here are this week's top
              <strong>{len(jobs)} London operations roles</strong>. {screened_note}
              Each card shows a score (1–10) and a personalised match reason.
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
              {ai_credit}Sources: JSearch, Active Jobs DB &amp; LinkedIn &nbsp;|&nbsp; London &amp; surrounding areas
            </p>
            <p style="margin:0;color:rgba(255,255,255,0.4);font-size:11px;">
              Generated every Monday morning. Always verify directly with the employer.
            </p>
          </td>
        </tr>
      </table>
    </td></tr>
  </table>
</body>
</html>"""


# ─────────────────────────────────────────────────────────────────
# EMAIL SENDING
# ─────────────────────────────────────────────────────────────────
def send_email(subject: str, html_body: str) -> bool:
    if not GMAIL_USER or not GMAIL_APP_PASS:
        log.error("Gmail credentials missing.")
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
        log.error("SMTP auth failed — check Gmail App Password.")
        return False
    except Exception as e:
        log.error(f"Email failed: {e}")
        return False


# ─────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────
def run(test_mode: bool = False):
    today   = date.today()
    week_of = today.strftime("%B %d, %Y")

    log.info("=" * 60)
    log.info(f"Job Bot starting — {'TEST MODE' if test_mode else 'SCHEDULED RUN'}")
    log.info(f"Week of: {week_of}")
    log.info("=" * 60)

    resume = load_resume()
    log.info(f"Loaded resume ({len(resume)} chars)")

    log.info("Fetching jobs from JSearch + Active Jobs DB + LinkedIn...")
    raw_jobs = fetch_all_jobs()
    log.info(f"Raw fetch: {len(raw_jobs)} jobs")

    if not raw_jobs:
        log.error("No jobs fetched. Check JSEARCH_API_KEY.")
        return

    unique_jobs = deduplicate(raw_jobs)
    candidates  = pre_filter_jobs(unique_jobs, top_n=PRE_FILTER_TOP_N)

    top_jobs = llm_rank_jobs(candidates, resume, top_n=OUTPUT_JOBS)
    llm_powered = top_jobs is not None
    if not llm_powered:
        top_jobs = keyword_rank_and_select(unique_jobs, top_n=OUTPUT_JOBS)

    method  = "AI-ranked" if llm_powered else "keyword-ranked"
    subject = f"🔍 Your London Job Digest – Week of {week_of} ({len(top_jobs)} {method} roles)"

    save_jobs_json(top_jobs, week_of, llm_powered)

    html_body = build_html_email(top_jobs, week_of, llm_powered=llm_powered)
    sent = send_email(subject, html_body)

    if sent:
        log.info(f"Done! Sent {len(top_jobs)} jobs ({method}).")
    else:
        log.warning("Email failed — saving debug HTML.")
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        debug_path = os.path.join(OUTPUT_DIR, "last_digest.html")
        with open(debug_path, "w", encoding="utf-8") as f:
            f.write(html_body)
        log.info(f"Saved to: {debug_path}")


def main():
    parser = argparse.ArgumentParser(description="Shreya's London Job Bot")
    parser.add_argument("--test", action="store_true", help="Run immediately, send test email.")
    run(test_mode=parser.parse_args().test)


if __name__ == "__main__":
    main()

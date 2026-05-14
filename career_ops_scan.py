"""
career_ops_scan.py

Scans career portals for London e-commerce / marketplace operations roles.
Triggered by GitHub Actions (career_ops_scan.yml) on Monday 8 AM UTC
or via workflow_dispatch (on-demand from frontend).

Sources per company (config/portals.yml):
  greenhouse  — Greenhouse public JSON API
  ashby       — Ashby public JSON API
  lever       — Lever public JSON API
  jsearch     — JSearch RapidAPI (same key as job_bot.py)

All sources dedup against data/scan_history.json to avoid re-surfacing old roles.

Outputs:
  output/scan_results.json   — new jobs found this run (served by GitHub Pages)
  data/scan_history.json     — persistent seen-URL log
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
import yaml
from playwright.sync_api import sync_playwright


# ── Config ────────────────────────────────────────────────────────────────────

PORTALS_PATH = Path("config/portals.yml")
HISTORY_PATH = Path("data/scan_history.json")
OUTPUT_PATH = Path("output/scan_results.json")

JSEARCH_API_KEY = os.environ.get("JSEARCH_API_KEY", "")
REQUEST_TIMEOUT = 15  # seconds per HTTP request
MAX_JSEARCH_RESULTS = 10  # per query


# ── Helpers ───────────────────────────────────────────────────────────────────

def load_portals() -> dict:
    with open(PORTALS_PATH) as f:
        return yaml.safe_load(f)


def load_history() -> dict:
    if HISTORY_PATH.exists():
        try:
            return json.loads(HISTORY_PATH.read_text())
        except Exception:
            pass
    return {"last_scan": None, "seen_urls": []}


def save_history(history: dict):
    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    HISTORY_PATH.write_text(json.dumps(history, indent=2))


def title_matches(title: str, filters: dict) -> bool:
    """Return True if job title passes include/exclude filters."""
    t = title.lower()
    include = filters.get("include", [])
    exclude = filters.get("exclude", [])
    if not any(kw.lower() in t for kw in include):
        return False
    if any(kw.lower() in t for kw in exclude):
        return False
    return True


def location_matches(location: str, filters: dict) -> bool:
    """Return True if location is acceptable (empty location passes through)."""
    if not location:
        return True  # avoid penalising missing data
    loc = location.lower()
    include = filters.get("include", [])
    return any(kw.lower() in loc for kw in include)


def normalise_job(title: str, url: str, company: str, location: str = "", source: str = "") -> dict:
    return {
        "title": title.strip(),
        "url": url.strip(),
        "company": company.strip(),
        "location": location.strip(),
        "source": source,
        "found_at": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
    }


# ── Greenhouse ────────────────────────────────────────────────────────────────

def scan_greenhouse(company: dict, title_filter: dict, location_filter: dict) -> list[dict]:
    gid = company.get("greenhouse_id", "")
    url = f"https://boards-api.greenhouse.io/v1/boards/{gid}/jobs"
    try:
        resp = requests.get(url, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        jobs_data = resp.json().get("jobs", [])
    except Exception as e:
        print(f"  Greenhouse {gid}: {e}")
        return []

    results = []
    for j in jobs_data:
        title = j.get("title", "")
        job_url = j.get("absolute_url", "")
        location = j.get("location", {}).get("name", "")
        if title_matches(title, title_filter) and location_matches(location, location_filter):
            results.append(normalise_job(title, job_url, company["name"], location, "Greenhouse"))
    return results


# ── Ashby ─────────────────────────────────────────────────────────────────────

def scan_ashby(company: dict, title_filter: dict, location_filter: dict) -> list[dict]:
    aid = company.get("ashby_id", "")
    url = f"https://api.ashbyhq.com/posting-api/job-board/{aid}"
    try:
        resp = requests.get(url, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        postings = resp.json().get("jobPostings", [])
    except Exception as e:
        print(f"  Ashby {aid}: {e}")
        return []

    results = []
    for j in postings:
        title = j.get("title", "")
        job_url = j.get("jobUrl", "") or j.get("externalLink", "")
        location = j.get("location", "")
        if title_matches(title, title_filter) and location_matches(location, location_filter):
            results.append(normalise_job(title, job_url, company["name"], location, "Ashby"))
    return results


# ── Lever ─────────────────────────────────────────────────────────────────────

def scan_lever(company: dict, title_filter: dict, location_filter: dict) -> list[dict]:
    lid = company.get("lever_id", "")
    url = f"https://api.lever.co/v0/postings/{lid}?mode=json"
    try:
        resp = requests.get(url, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        postings = resp.json()
        if not isinstance(postings, list):
            postings = postings.get("data", [])
    except Exception as e:
        print(f"  Lever {lid}: {e}")
        return []

    results = []
    for j in postings:
        title = j.get("text", "")
        job_url = j.get("hostedUrl", "")
        location = j.get("categories", {}).get("location", "")
        if title_matches(title, title_filter) and location_matches(location, location_filter):
            results.append(normalise_job(title, job_url, company["name"], location, "Lever"))
    return results


# ── JSearch (RapidAPI) ────────────────────────────────────────────────────────

def scan_jsearch(company: dict, title_filter: dict, location_filter: dict) -> list[dict]:
    if not JSEARCH_API_KEY:
        print(f"  JSearch: no API key — skipping {company['name']}")
        return []

    query = company.get("jsearch_query", company["name"] + " operations London")
    headers = {
        "X-RapidAPI-Key": JSEARCH_API_KEY,
        "X-RapidAPI-Host": "jsearch.p.rapidapi.com",
    }
    params = {
        "query": query,
        "num_pages": "1",
        "date_posted": "month",
        "country": "gb",
    }
    try:
        resp = requests.get(
            "https://jsearch.p.rapidapi.com/search",
            headers=headers,
            params=params,
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json().get("data", [])[:MAX_JSEARCH_RESULTS]
    except Exception as e:
        print(f"  JSearch {company['name']}: {e}")
        return []

    results = []
    for j in data:
        title = j.get("job_title", "")
        job_url = j.get("job_apply_link", "") or j.get("job_url", "")
        location = j.get("job_city", "") or j.get("job_state", "")
        if title_matches(title, title_filter) and location_matches(location, location_filter):
            results.append(normalise_job(title, job_url, company["name"], location, "JSearch"))
    return results


# ── Playwright fallback ───────────────────────────────────────────────────────

def scan_playwright(company: dict, title_filter: dict, location_filter: dict) -> list[dict]:
    """
    Browse the company's career page with headless Chromium and extract
    (title, href) pairs from job listing links. Used as a supplementary
    check for all companies to catch anything the JSON APIs miss.
    """
    career_url = company.get("career_url", "")
    if not career_url:
        return []

    results = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(
                user_agent=(
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                )
            )
            page.goto(career_url, wait_until="networkidle", timeout=30_000)
            page.wait_for_timeout(2000)

            # Extract all links that look like job postings
            links = page.evaluate("""
                () => {
                    const anchors = Array.from(document.querySelectorAll('a[href]'));
                    return anchors.map(a => ({
                        text: a.innerText.trim(),
                        href: a.href
                    })).filter(l => l.text.length > 5 && l.text.length < 200);
                }
            """)
            browser.close()

            for link in links:
                title = link.get("text", "")
                href = link.get("href", "")
                if title_matches(title, title_filter):
                    location = ""  # can't always detect from links alone
                    results.append(normalise_job(
                        title, href, company["name"], location, "Playwright"
                    ))
    except Exception as e:
        print(f"  Playwright {company['name']}: {e}")

    return results


# ── Main scan ─────────────────────────────────────────────────────────────────

def main():
    config = load_portals()
    title_filter = config.get("title_filter", {})
    location_filter = config.get("location_filter", {})
    companies = config.get("companies", [])

    history = load_history()
    seen_urls = set(history.get("seen_urls", []))

    all_new_jobs = []
    total_found = 0
    total_new = 0

    print(f"Scanning {len(companies)} companies...")

    for company in companies:
        name = company.get("name", "?")
        ats = company.get("ats", "jsearch")
        print(f"\n[{name}] ({ats})")

        raw_jobs = []

        if ats == "greenhouse":
            raw_jobs = scan_greenhouse(company, title_filter, location_filter)
        elif ats == "ashby":
            raw_jobs = scan_ashby(company, title_filter, location_filter)
        elif ats == "lever":
            raw_jobs = scan_lever(company, title_filter, location_filter)
        elif ats == "jsearch":
            raw_jobs = scan_jsearch(company, title_filter, location_filter)

        # Playwright supplement for all companies (catches SPA-rendered jobs)
        # Only run if primary scan found 0 results or career_url is set
        if len(raw_jobs) == 0 or company.get("career_url"):
            pw_jobs = scan_playwright(company, title_filter, location_filter)
            # Merge without duplicating by URL
            existing_pw_urls = {j["url"] for j in raw_jobs}
            for j in pw_jobs:
                if j["url"] not in existing_pw_urls:
                    raw_jobs.append(j)

        total_found += len(raw_jobs)

        # Dedup against history
        new_jobs = [j for j in raw_jobs if j["url"] not in seen_urls and j["url"]]
        for j in new_jobs:
            seen_urls.add(j["url"])

        total_new += len(new_jobs)
        all_new_jobs.extend(new_jobs)
        print(f"  Found {len(raw_jobs)} matching, {len(new_jobs)} new")

        time.sleep(0.5)  # polite delay between companies

    # Sort: alphabetical by company, then title
    all_new_jobs.sort(key=lambda j: (j["company"], j["title"]))

    # Save outputs
    now = datetime.now(timezone.utc).isoformat()
    scan_result = {
        "last_scan": now,
        "companies_scanned": len(companies),
        "total_found": total_found,
        "new_this_run": total_new,
        "jobs": all_new_jobs,
    }

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(scan_result, indent=2))
    print(f"\nScan complete: {total_new} new roles → {OUTPUT_PATH}")

    history["last_scan"] = now
    history["seen_urls"] = list(seen_urls)
    save_history(history)


if __name__ == "__main__":
    main()

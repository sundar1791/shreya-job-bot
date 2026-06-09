"""
career_ops_scan.py

Uses Claude's built-in web_search tool to find London e-commerce / marketplace
operations roles. Claude reads the candidate CV, decides what to search for,
and returns a curated list of matching jobs (up to MAX_WEB_SEARCHES searches per run).

Deduplicates against:
  - data/scan_history.json  (jobs surfaced by previous scanner runs)
  - output/jobs.json        (jobs already in the weekly email digest)

Outputs:
  output/scan_results.json  — jobs found this run (served by GitHub Pages)
  data/scan_history.json    — persistent seen-URL log
"""

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import anthropic
import yaml

PORTALS_PATH = Path("config/portals.yml")
HISTORY_PATH = Path("data/scan_history.json")
OUTPUT_PATH  = Path("output/scan_results.json")
DIGEST_PATH  = Path("output/jobs.json")
CV_PATH      = Path("cv.md")
RESUME_PATH  = Path("resume.txt")

MODEL            = "claude-sonnet-4-6"
MAX_WEB_SEARCHES = 10


def load_cv() -> str:
    for path in (CV_PATH, RESUME_PATH):
        if path.exists():
            return path.read_text()
    return ""


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


def load_digest_urls() -> set:
    if not DIGEST_PATH.exists():
        return set()
    try:
        data = json.loads(DIGEST_PATH.read_text())
        return {j.get("url", "") for j in data.get("jobs", []) if j.get("url")}
    except Exception:
        return set()


def extract_json_array(text: str) -> list:
    """Pull the first JSON array out of Claude's response text."""
    match = re.search(r"\[[\s\S]*\]", text)
    if match:
        try:
            return json.loads(match.group())
        except Exception:
            pass
    return []


def scan_with_claude(cv: str, companies: list, already_seen: set) -> list:
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    companies_str = ", ".join(companies)
    seen_sample   = "\n".join(sorted(already_seen)[:100]) or "None yet"

    system = """\
You are a specialist job scout for a senior e-commerce and marketplace operations \
professional based in London. Your job is to search the web for relevant open roles \
and return a clean JSON array.

Target roles: operations specialist / associate / coordinator / analyst / executive, \
vendor operations, catalogue operations, marketplace operations, platform operations, \
seller onboarding, content operations, data governance. \
NOT director / head / VP — individual contributor or team-lead level only.

Location: London (on-site, hybrid, or remote UK).

Output format — return ONLY a valid JSON array, no markdown fences, no explanation:
[
  {
    "title": "exact job title",
    "company": "company name",
    "url": "direct link to the job posting",
    "location": "city or Remote",
    "date_posted": "YYYY-MM-DD or best estimate",
    "why_relevant": "one sentence"
  }
]

If you find no suitable roles, return an empty array: []"""

    user = f"""\
Candidate CV:
{cv}

Companies of particular interest (search these specifically, but also search broadly \
across all London e-commerce / marketplace employers):
{companies_str}

Already-seen URLs — do NOT include these in your results:
{seen_sample}

Make up to {MAX_WEB_SEARCHES} targeted web searches covering the roles described above \
posted in the last 4 weeks, then return the JSON array of all unique matching roles you found."""

    print(f"Running Claude web search (max {MAX_WEB_SEARCHES} searches)...")
    response = client.messages.create(
        model=MODEL,
        max_tokens=4096,
        tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": MAX_WEB_SEARCHES}],
        system=system,
        messages=[{"role": "user", "content": user}],
    )

    text = "".join(block.text for block in response.content if hasattr(block, "text"))
    jobs = extract_json_array(text)
    print(f"Claude returned {len(jobs)} candidates")
    return jobs


def main():
    cv = load_cv()
    if not cv:
        print("ERROR: no CV found (cv.md or resume.txt)")
        return

    config   = load_portals()
    companies = config.get("companies", [])

    history   = load_history()
    seen_urls = set(history.get("seen_urls", []))
    digest_urls = load_digest_urls()
    all_seen  = seen_urls | digest_urls

    print(f"Seen: {len(seen_urls)} history + {len(digest_urls)} digest = {len(all_seen)} total")

    raw_jobs = scan_with_claude(cv, companies, all_seen)

    now_str  = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    new_jobs = []
    for j in raw_jobs:
        url = (j.get("url") or "").strip()
        if not url or url in seen_urls:
            continue
        j["url"]       = url
        j["found_at"]  = now_str
        j["source"]    = "Claude Web Search"
        new_jobs.append(j)
        seen_urls.add(url)

    new_jobs.sort(key=lambda j: (j.get("company", ""), j.get("title", "")))

    now = datetime.now(timezone.utc).isoformat()
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps({
        "last_scan":        now,
        "companies_scanned": len(companies),
        "total_found":      len(new_jobs),
        "new_this_run":     len(new_jobs),
        "jobs":             new_jobs,
    }, indent=2))
    print(f"Scan complete: {len(new_jobs)} new roles → {OUTPUT_PATH}")

    history["last_scan"] = now
    history["seen_urls"] = list(seen_urls)
    save_history(history)


if __name__ == "__main__":
    main()

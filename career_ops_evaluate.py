"""
career_ops_evaluate.py

Evaluates a job posting against Shreya's CV using Claude Sonnet.
Triggered by GitHub Actions (career_ops_evaluate.yml) with inputs:
  JOB_URL  — URL to fetch and evaluate
  JOB_TEXT — raw job description text (alternative to URL)

Outputs:
  output/latest_evaluation.json  — full A-G evaluation
  output/applications.json       — updated tracker (copy of data/applications.json)
  data/applications.json         — persistent tracker
"""

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import anthropic
from playwright.sync_api import sync_playwright


# ── Config ────────────────────────────────────────────────────────────────────

CV_PATH = Path("cv.md")
APPLICATIONS_PATH = Path("data/applications.json")
OUTPUT_EVAL_PATH = Path("output/latest_evaluation.json")
OUTPUT_APPS_PATH = Path("output/applications.json")
MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 4096
DEDUP_HOURS = 24  # skip Claude call if same URL evaluated within this window


# ── Job description fetching ──────────────────────────────────────────────────

def fetch_job_description(url: str) -> str:
    """Fetch visible text from a job posting URL using headless Chromium."""
    print(f"Fetching job description from: {url}")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
        )
        try:
            page.goto(url, wait_until="networkidle", timeout=30_000)
            page.wait_for_timeout(2000)  # allow JS to render

            # Remove nav, footer, cookie banners before extracting text
            page.evaluate("""
                ['header','footer','nav','[role=banner]','[role=navigation]',
                 '.cookie-banner','.consent','#onetrust-banner-sdk']
                .forEach(sel => document.querySelectorAll(sel)
                .forEach(el => el.remove()));
            """)

            text = page.evaluate("document.body.innerText")
        except Exception as e:
            print(f"Playwright fetch failed: {e}", file=sys.stderr)
            text = ""
        finally:
            browser.close()

    # Collapse excess whitespace
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    return "\n".join(lines)[:12_000]  # cap at 12k chars to stay within token budget


# ── Evaluation prompt ─────────────────────────────────────────────────────────

SYSTEM_PROMPT_TEMPLATE = """\
You are an expert career advisor specialising in UK e-commerce and marketplace operations roles.
Your job is to evaluate job postings against a candidate's CV and produce a structured assessment.

CANDIDATE CV:
{cv}

OUTPUT RULES:
- Return ONLY valid JSON. No markdown fences, no prose before or after.
- All string values must be plain text (no markdown inside JSON strings).
- Use British English spelling throughout.
- Never invent experience or metrics not present in the CV.
- Score global on a 1.0–5.0 scale (one decimal place).
- Score block_b and block_d on a 1.0–5.0 scale as well.
- For legitimacy, use exactly one of: "High Confidence", "Proceed with Caution", "Suspicious".
- For recommendation, use exactly one of:
    "Strong match — recommend applying" (global >= 4.0)
    "Decent match — apply if interested" (global >= 3.0)
    "Weak match — consider skipping" (global < 3.0)

ARCHETYPES (detect the best fit):
- Vendor/Seller Operations: vendor onboarding, seller activation, partner management
- Catalogue/Content Operations: catalogue, product data, taxonomy, merchandising, SEO
- Platform/Marketplace Operations: platform management, marketplace, digital operations
- Data & Process Operations: data governance, data quality, RCA, SOPs, process improvement
- Operations Leadership: team leadership, SLA management, cross-functional, ops strategy
"""

USER_PROMPT_TEMPLATE = """\
Evaluate this job posting. Return the result as a single JSON object with exactly this structure:

{{
  "job_title": "<extracted title>",
  "company": "<extracted company name>",
  "archetype": "<one of the 5 archetypes>",
  "global_score": <1.0–5.0>,
  "legitimacy": "<High Confidence | Proceed with Caution | Suspicious>",
  "recommendation": "<one of the 3 recommendation strings>",
  "blocks": {{
    "A": {{
      "title": "Role Summary",
      "archetype": "<detected archetype>",
      "seniority": "<e.g. Lead / Manager / Senior>",
      "remote_policy": "<Remote / Hybrid / On-site>",
      "location": "<city or region>",
      "team_size": "<if mentioned, else null>",
      "tldr": "<one sentence summary of the role>"
    }},
    "B": {{
      "title": "CV Match",
      "score": <1.0–5.0>,
      "matches": [
        {{"requirement": "<JD requirement>", "cv_evidence": "<exact line from CV>"}}
      ],
      "gaps": [
        {{"gap": "<missing requirement>", "severity": "blocker|nice-to-have", "mitigation": "<how to address>"}}
      ]
    }},
    "C": {{
      "title": "Level & Strategy",
      "level_detected": "<seniority in JD>",
      "candidate_level": "<natural level for this archetype>",
      "positioning": "<how to frame 10+ years of experience for this role>",
      "key_angles": ["<angle 1>", "<angle 2>", "<angle 3>"]
    }},
    "D": {{
      "title": "Comp & Market",
      "score": <1.0–5.0>,
      "salary_in_jd": "<stated range or null>",
      "market_estimate": "<estimated range from training data for this role in London>",
      "vs_target": "<above target / within target / below target (target: £55k–£90k)>",
      "notes": "<brief comp commentary>"
    }},
    "E": {{
      "title": "Personalisation Plan",
      "cv_changes": [
        {{"section": "<e.g. Summary>", "change": "<what to adjust and why>"}}
      ],
      "keywords_to_inject": ["<keyword 1>", "<keyword 2>"]
    }},
    "F": {{
      "title": "Interview Plan",
      "stories": [
        {{
          "jd_requirement": "<requirement from JD>",
          "story_title": "<brief story name>",
          "situation": "<S from CV>",
          "task": "<T>",
          "action": "<A>",
          "result": "<R — use real metrics from CV>",
          "reflection": "<what this shows about seniority>"
        }}
      ]
    }},
    "G": {{
      "title": "Posting Legitimacy",
      "tier": "<High Confidence | Proceed with Caution | Suspicious>",
      "signals": [
        {{"signal": "<observation>", "weight": "positive|neutral|concerning"}}
      ],
      "notes": "<any caveats or context>"
    }}
  }},
  "keywords": ["<ATS keyword 1>", "<ATS keyword 2>"]
}}

JOB POSTING:
{job_text}
"""


# ── Claude call ───────────────────────────────────────────────────────────────

def evaluate_with_claude(cv_text: str, job_text: str) -> dict:
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    system_prompt = SYSTEM_PROMPT_TEMPLATE.format(cv=cv_text)
    user_prompt = USER_PROMPT_TEMPLATE.format(job_text=job_text[:8000])  # hard cap

    message = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=[
            {
                "type": "text",
                "text": system_prompt,
                # Prompt caching: cv.md + instructions cached for 5 min window
                # (~90% cheaper on repeated evaluations within same hour)
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[{"role": "user", "content": user_prompt}],
    )

    raw = message.content[0].text.strip()

    # Strip markdown fences if Claude adds them despite instructions
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1]
        raw = raw.rsplit("```", 1)[0]

    return json.loads(raw)


# ── Dedup check ───────────────────────────────────────────────────────────────

def already_evaluated_today(url: str) -> dict | None:
    """Return cached result if the same URL was evaluated within DEDUP_HOURS."""
    if not OUTPUT_EVAL_PATH.exists():
        return None
    try:
        cached = json.loads(OUTPUT_EVAL_PATH.read_text())
        if cached.get("job_url") != url:
            return None
        evaluated_at = datetime.fromisoformat(cached.get("evaluated_at", ""))
        age_hours = (datetime.now(timezone.utc) - evaluated_at).total_seconds() / 3600
        if age_hours < DEDUP_HOURS:
            print(f"Same URL evaluated {age_hours:.1f}h ago — returning cached result.")
            return cached
    except Exception:
        pass
    return None


# ── Tracker update ────────────────────────────────────────────────────────────

def update_tracker(result: dict, job_url: str):
    tracker = {"last_updated": None, "applications": []}
    if APPLICATIONS_PATH.exists():
        try:
            tracker = json.loads(APPLICATIONS_PATH.read_text())
        except Exception:
            pass

    # Avoid adding duplicate entries for the same URL
    existing_urls = {a.get("url") for a in tracker["applications"]}
    if job_url in existing_urls:
        return

    entry = {
        "id": len(tracker["applications"]) + 1,
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "company": result.get("company", "Unknown"),
        "role": result.get("job_title", "Unknown"),
        "score": result.get("global_score"),
        "status": "Evaluated",
        "url": job_url,
        "archetype": result.get("archetype"),
        "legitimacy": result.get("legitimacy"),
        "recommendation": result.get("recommendation"),
    }
    tracker["applications"].insert(0, entry)
    tracker["last_updated"] = datetime.now(timezone.utc).isoformat()

    APPLICATIONS_PATH.write_text(json.dumps(tracker, indent=2))
    OUTPUT_APPS_PATH.write_text(json.dumps(tracker, indent=2))
    print(f"Tracker updated: {entry['company']} — {entry['role']} (score {entry['score']})")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    job_url = os.environ.get("JOB_URL", "").strip()
    job_text = os.environ.get("JOB_TEXT", "").strip()

    if not job_url and not job_text:
        print("Error: provide JOB_URL or JOB_TEXT", file=sys.stderr)
        sys.exit(1)

    if not CV_PATH.exists():
        print(f"Error: {CV_PATH} not found", file=sys.stderr)
        sys.exit(1)

    cv_text = CV_PATH.read_text()

    # Dedup check (skip Claude call if recently evaluated)
    if job_url:
        cached = already_evaluated_today(job_url)
        if cached:
            print("Returning cached evaluation (no API call made).")
            OUTPUT_EVAL_PATH.write_text(json.dumps(cached, indent=2))
            return

    # Fetch job description
    if job_url and not job_text:
        job_text = fetch_job_description(job_url)
        if not job_text:
            print("Warning: could not fetch job description — text will be empty", file=sys.stderr)

    if not job_text:
        print("Error: no job text available to evaluate", file=sys.stderr)
        sys.exit(1)

    print(f"Evaluating: {len(job_text)} chars of job text")
    print(f"Calling Claude {MODEL}...")

    result = evaluate_with_claude(cv_text, job_text)

    # Enrich with metadata
    result["evaluated_at"] = datetime.now(timezone.utc).isoformat()
    result["job_url"] = job_url or ""
    result["status"] = "complete"

    # Write output
    OUTPUT_EVAL_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_EVAL_PATH.write_text(json.dumps(result, indent=2))
    print(f"Evaluation written to {OUTPUT_EVAL_PATH}")
    print(f"Score: {result.get('global_score')} — {result.get('recommendation')}")

    # Update tracker
    if job_url:
        update_tracker(result, job_url)


if __name__ == "__main__":
    main()

/**
 * Cloudflare Worker — Career Ops Trigger Proxy
 *
 * Receives POST requests from the frontend and dispatches
 * GitHub Actions workflows without exposing the GitHub PAT.
 *
 * Secrets (set in Cloudflare dashboard):
 *   GITHUB_PAT  — fine-grained PAT with "Actions: write" scope for this repo only
 *
 * Supported actions:
 *   { "action": "evaluate", "jobUrl": "https://...", "jobText": "..." }
 *   { "action": "scan" }
 */

const REPO = "sundar1791/shreya-job-bot";
const BRANCH = "main";
const ALLOWED_ORIGIN = "https://sundar1791.github.io";

const WORKFLOWS = {
  evaluate: "career_ops_evaluate.yml",
  scan: "career_ops_scan.yml",
};

export default {
  async fetch(request, env) {
    const origin = request.headers.get("Origin") || "";

    // CORS preflight
    if (request.method === "OPTIONS") {
      return corsResponse(null, 204, origin);
    }

    if (request.method !== "POST") {
      return corsResponse("Method Not Allowed", 405, origin);
    }

    let body;
    try {
      body = await request.json();
    } catch {
      return corsResponse("Invalid JSON", 400, origin);
    }

    const { action, jobUrl = "", jobText = "" } = body;

    if (!WORKFLOWS[action]) {
      return corsResponse(`Unknown action: ${action}`, 400, origin);
    }

    const inputs =
      action === "evaluate"
        ? { job_url: jobUrl, job_text: jobText }
        : {};

    const ghResp = await dispatchWorkflow(env.GITHUB_PAT, WORKFLOWS[action], inputs);

    if (!ghResp.ok) {
      const text = await ghResp.text();
      console.error(`GitHub dispatch failed (${ghResp.status}): ${text}`);
      return corsResponse("Failed to trigger workflow", 502, origin);
    }

    return corsResponse(
      JSON.stringify({ status: "queued", action, workflow: WORKFLOWS[action] }),
      202,
      origin,
      { "Content-Type": "application/json" }
    );
  },
};

async function dispatchWorkflow(pat, workflow, inputs) {
  return fetch(
    `https://api.github.com/repos/${REPO}/actions/workflows/${workflow}/dispatches`,
    {
      method: "POST",
      headers: {
        Authorization: `Bearer ${pat}`,
        Accept: "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ ref: BRANCH, inputs }),
    }
  );
}

function corsResponse(body, status, origin, extraHeaders = {}) {
  const allowed = origin === ALLOWED_ORIGIN || origin === "";
  return new Response(
    typeof body === "string" || body === null ? body : JSON.stringify(body),
    {
      status,
      headers: {
        "Access-Control-Allow-Origin": allowed ? origin || "*" : "null",
        "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
        ...extraHeaders,
      },
    }
  );
}

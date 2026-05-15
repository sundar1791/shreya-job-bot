# Cloudflare Worker — One-Time Setup

This Worker acts as a secure proxy between the frontend ("Evaluate" / "Scan Now" buttons)
and GitHub Actions. It holds the GitHub token as a secret so it never appears in the browser.

**Estimated time: 5–10 minutes. Done once, never touched again.**

---

## Step 1 — Create a GitHub Fine-Grained PAT

1. Go to **GitHub → Settings → Developer settings → Personal access tokens → Fine-grained tokens**
2. Click **Generate new token**
3. Set:
   - **Token name**: `shreya-career-ops-trigger`
   - **Expiration**: 1 year (set a calendar reminder to renew)
   - **Repository access**: Only `sundar1791/shreya-job-bot`
   - **Permissions → Actions**: `Read and write`
   - All other permissions: `No access`
4. Click **Generate token** and copy it — you won't see it again.

---

## Step 2 — Create a Cloudflare Account

Go to [cloudflare.com](https://cloudflare.com) and sign up for a free account.
No credit card needed.

---

## Step 3 — Create the Worker

1. In the Cloudflare dashboard, click **Workers & Pages** → **Create application** → **Create Worker**
2. Name it `shreya-career-ops` (or anything you like)
3. Click **Deploy** (deploys the default "Hello World" first)
4. On the next page, click **Edit code**
5. Delete the existing code and paste the full contents of `trigger.js` (this file's sibling)
6. Click **Save and deploy**

---

## Step 4 — Add the GitHub PAT as a Secret

1. In your Worker's settings, click **Settings → Variables**
2. Under **Environment Variables**, click **Add variable**
3. Set:
   - **Variable name**: `GITHUB_PAT`
   - **Value**: paste the token from Step 1
   - Click the **Encrypt** toggle (makes it a secret — good)
4. Click **Save**

---

## Step 5 — Copy the Worker URL into the Frontend

1. Your Worker URL is shown at the top of the Worker page, e.g.:
   `https://shreya-career-ops.YOUR-SUBDOMAIN.workers.dev`
2. Open `frontend/index.html` in this repo
3. Find the line near the top of the `<script>` section:
   ```js
   const WORKER_URL = "REPLACE_WITH_YOUR_WORKER_URL";
   ```
4. Replace `REPLACE_WITH_YOUR_WORKER_URL` with your Worker URL
5. Commit and push — the site will redeploy automatically

---

## That's it!

The "Evaluate" button and "Scan Now" button on the Career Ops tab will now trigger
GitHub Actions in the background. Results appear on the dashboard ~2–4 minutes later.

---

## Troubleshooting

| Problem | Fix |
|---|---|
| "Failed to trigger workflow" error on button click | Check the Worker logs in Cloudflare dashboard; likely a bad PAT |
| PAT expired | Generate a new one in GitHub, update the Worker secret |
| Wrong Worker URL | Re-check step 5; URL must match exactly |
| CORS error in browser | Check `ALLOWED_ORIGIN` in `trigger.js` matches your GitHub Pages URL |

# LinkedIn Job Seen Tracker

Chrome extension that talks to the local healthtech-jobs server.

## What it does
- On `linkedin.com/jobs/*`, scans each job card and extracts
  `(linkedin_id, company, title, location)`.
- Hashes `company|title|location` (sha1) as a stable fallback key.
- Queries `POST /api/seen/lookup` and paints a yellow badge
  ("dismissed 2026-06-12 ×2") on cards you've seen before.
- When you click the **X / Dismiss** button on a card, posts
  `POST /api/seen { action: "dismissed" }` to the server.

## Install (unpacked)
1. Start the server: `cd healthtech-jobs && python3 server.py`.
2. Open `chrome://extensions`, toggle **Developer mode**.
3. Click **Load unpacked** and pick this directory
   (`healthtech-jobs/linkedin-extension/`).
4. Open `https://www.linkedin.com/jobs/` and search. Check the
   DevTools console for `[hjs] LinkedIn Job Seen Tracker loaded`.

## Verify
- Dismiss a card. Inspect: `sqlite3 healthtech-jobs/state.db
  "SELECT * FROM linkedin_seen;"` — you should see a row.
- Reload the search page. The same card (or a re-post with a
  different LinkedIn id but same company+title+location) gets a
  yellow "dismissed …" badge.

## Notes
- LinkedIn's class names rotate (`zKVPvETH…`) so selectors target
  stable attributes: `data-occludable-job-id`, `data-job-id`,
  `aria-label^="Dismiss"`.
- The badge slots into `.job-card-list__footer-wrapper` alongside
  the native "Viewed"/"Promoted" pills.

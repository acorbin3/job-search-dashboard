# Job Application Field Capture (Learn Mode)

Chrome extension that records the fields you type into job applications so
your autofill config builds itself from real behavior instead of hand-editing
`profile.json`.

## What it does

- A popup toggle turns **Learn mode** on/off. Default is **off** — nothing is
  recorded while you browse normally.
- When on, a `change` listener on every page derives a normalized *question
  key* for each field you fill (from its `<label>`, `aria-label`, `name`, or
  placeholder), grabs the value, and POSTs to `127.0.0.1:5050/api/answers`.
- The server keeps one row per distinct question, with every value you've
  given and how many times — so the review page shows your *usual* answer.
- A small green "● Learning fields — N captured" badge shows in the corner
  while Learn mode is on, so you always know it's recording.

Passwords, file uploads, hidden fields, and search/OTP/captcha boxes are never
captured (filtered on both the client and the server).

## Install (unpacked)

1. Start the server: `cd healthtech-jobs && python3 server.py`.
2. Open `chrome://extensions`, toggle **Developer mode**.
3. **Load unpacked** → pick this directory (`field-capture-extension/`).
4. Click the extension icon, flip **Learn mode** on, and fill out a job app.

## Review

Open <http://127.0.0.1:5050/answers> (linked from the popup, and from the
Applied/Leads nav). Filter by question or value; the "usual value" column is
the most-frequent answer for each question.

## Next step (Fill mode)

Once enough real answers accumulate, a fill path can read
`GET /api/answers`, fuzzy-match each field's question key, and prefill new
applications — generalizing the per-ATS `greenhouse_autofill.js` /
`gem_autofill.js` scripts into one label-driven filler.

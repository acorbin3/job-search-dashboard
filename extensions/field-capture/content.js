// Job Application Field Capture — Learn mode.
//
// When Learn mode is ON (toggled from the extension popup), listens for
// `change` events on form fields anywhere on the page, derives a normalized
// "question key" for each, and POSTs {question_key, label, field_type, value,
// domain} to the local server. Nothing is captured while Learn mode is OFF,
// so ordinary browsing is never recorded.

const API_BASE = "http://127.0.0.1:5050";
const BADGE_ID = "jfc-learn-badge";

// All server calls go through the background worker: MV3 content-script
// fetches follow the page's CSP, and strict sites (Greenhouse) block
// connections to 127.0.0.1. Falls back to a direct fetch if the background
// worker is unavailable (e.g. extension just updated).
async function api(path, init) {
  try {
    const res = await chrome.runtime.sendMessage({ type: "api", path, init });
    if (res && !res.error) {
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      return res.body ? JSON.parse(res.body) : null;
    }
    throw new Error(res ? res.error : "no response from background");
  } catch (e) {
    const r = await fetch(API_BASE + path, init);
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    return r.json();
  }
}

let learnMode = false;
let captureCount = 0;

// ---- Question-key derivation --------------------------------------------

function normKey(s) {
  return (s || "")
    .replace(/[∗*]+/g, " ")          // asterisks / required markers
    .replace(/\(required\)|\(optional\)/gi, " ")
    .replace(/[\r\n\t]+/g, " ")
    .replace(/\s+/g, " ")
    .replace(/[:*]+\s*$/, "")             // trailing colons/stars
    .trim()
    .toLowerCase()
    .slice(0, 120);
}

// Fields can live inside open shadow roots (SmartRecruiters renders every
// input inside an spl-* web component). Label/id lookups must be scoped to
// the element's own root — document-level queries can't see across the
// boundary.
function rootOf(el) {
  const r = el.getRootNode ? el.getRootNode() : document;
  return r.querySelectorAll ? r : document;
}

// Component hosts often carry the field label as an attribute
// (<spl-input label="First name">). Walk up the shadow-host chain.
function hostLabel(el) {
  let root = el.getRootNode && el.getRootNode();
  for (let i = 0; i < 4 && root && root.host; i++) {
    const h = root.host;
    const t = h.getAttribute("label") || h.getAttribute("aria-label");
    if (t && t.trim()) return t;
    root = h.getRootNode && h.getRootNode();
  }
  return "";
}

function idsToText(idString, root = document) {
  if (!idString) return "";
  const parts = idString.split(/\s+/)
    .map((id) => (root.getElementById ? root.getElementById(id) : null))
    .filter(Boolean)
    .map((n) => n.textContent.trim());
  return parts.join(" ").trim();
}

function labelText(el) {
  const root = rootOf(el);
  // 1) explicit <label for=id> in the same root
  if (el.id) {
    const forLabel = root.querySelector(`label[for="${CSS.escape(el.id)}"]`);
    if (forLabel && forLabel.textContent.trim()) return forLabel.textContent;
  }
  // 2) wrapping <label> — minus the control's own text: a <select> wrapped in
  // its label (Lever's EEO fields) would otherwise dump every option into
  // the question key.
  const wrap = el.closest("label");
  if (wrap) {
    let t = "";
    for (const node of wrap.childNodes) {
      if (node === el || (node.nodeType === 1 && node.contains(el))) continue;
      t += " " + (node.textContent || "");
    }
    if (t.trim()) return t;
    if (wrap.textContent.trim() && !/^select$/i.test(el.tagName)) return wrap.textContent;
  }
  // 3) aria-labelledby
  const byId = idsToText(el.getAttribute("aria-labelledby"), root);
  if (byId) return byId;
  // 4) aria-label / shadow-host label attr / container question / placeholder
  //    / name. The container question outranks the placeholder: Lever gives
  //    every custom-question textarea the same "Type your response" hint.
  return el.getAttribute("aria-label") ||
         hostLabel(el) ||
         fieldEntryQuestion(el) ||
         el.getAttribute("placeholder") ||
         el.getAttribute("name") ||
         "";
}

// The question text for a field whose own labels only name the *option*
// (radios, yes/no pills). Ashby wraps each question in a field-entry
// container whose first <label> (class ...question-title) is the question;
// generic fieldsets put it in a <legend> or a leading <label>.
// Walk up from a field looking for its question text in the surrounding
// container (Ashby field entries, Lever .application-question blocks,
// legend-less fieldsets). At each level, accept only when exactly ONE
// plausible question label survives the filters — several surviving labels
// means we've climbed past the field's own container into a shared section,
// where picking the first would steal a sibling question's text.
function fieldEntryQuestion(el) {
  let node = el.parentElement;
  for (let depth = 0; depth < 8 && node; depth++, node = node.parentElement) {
    const cands = node.querySelectorAll(
      '.ashby-application-form-question-title, [class*="application-label"], ' +
      '[class*="question-title"], legend, label'
    );
    const good = [];
    for (const q of cands) {
      if (q.contains(el) || !q.textContent.trim()) continue;
      // Option labels wrap their own control; question labels never do.
      if (q.querySelector("input, select, textarea")) continue;
      // A label whose `for` resolves to a *different* control names that
      // control's question, not this one (Ashby nests the SMS-consent radios
      // inside the Phone entry). Question-title labels point `for` at a
      // field UUID that is no element's id, so they pass this check.
      const f = q.getAttribute && q.getAttribute("for");
      if (f) {
        const root = rootOf(el);
        const target = root.getElementById ? root.getElementById(f) : null;
        if (target && target !== el &&
            target.getAttribute("name") !== el.getAttribute("name")) continue;
      }
      good.push(q);
    }
    if (good.length === 1) return good[0].textContent;
    if (good.length > 1) return "";
    if (/^(form|section|main|body)$/i.test(node.tagName)) break;
  }
  return "";
}

// For radios/checkboxes the field's own text is the *option*, not the
// question. Walk up to the group's label (fieldset legend or a labelledby).
function groupLabel(el) {
  const group = el.getAttribute("name");
  const fieldset = el.closest("fieldset");
  if (fieldset) {
    const legend = fieldset.querySelector("legend");
    if (legend && legend.textContent.trim()) return legend.textContent;
  }
  const byId = idsToText(el.getAttribute("aria-labelledby"), rootOf(el));
  if (byId) return byId;
  // Fall back to the closest group container's first text.
  const grp = el.closest('[role="radiogroup"], [role="group"]');
  if (grp && grp.getAttribute("aria-label")) return grp.getAttribute("aria-label");
  // Question label rendered as plain text in the field container (Ashby:
  // fieldset with a <label> question title but no <legend>).
  const feq = fieldEntryQuestion(el);
  if (feq) return feq;
  return group || "";
}

// Ashby-style yes/no pill: sibling <button>Yes</button><button>No</button>
// next to a hidden bookkeeping checkbox. Returns the button group's buttons
// if `el` (a button or the checkbox) is part of one, else null.
function yesNoGroup(el) {
  const wrap = el.parentElement;
  if (!wrap) return null;
  const kids = [...wrap.children];
  const btns = kids.filter((c) => c.tagName === "BUTTON");
  const hasCb = kids.some(
    (c) => c.tagName === "INPUT" && (c.getAttribute("type") || "").toLowerCase() === "checkbox"
  );
  if (btns.length >= 2 && hasCb) return btns;
  return null;
}

// The question for a custom dropdown: the open combobox's label. When you
// click an option, the combobox that owns it is the one still expanded.
function openComboLabel(optionEl) {
  // Prefer a listbox that names its question via aria-labelledby.
  const listbox = optionEl.closest('[role="listbox"]');
  if (listbox) {
    const byId = idsToText(listbox.getAttribute("aria-labelledby"));
    if (byId) return byId;
    if (listbox.getAttribute("aria-label")) return listbox.getAttribute("aria-label");
  }
  // Else the currently-expanded combobox on the page.
  const combo = document.querySelector(
    '[role="combobox"][aria-expanded="true"], [aria-haspopup="listbox"][aria-expanded="true"]'
  );
  if (combo) {
    const t = (labelText(combo) || groupLabel(combo)).trim();
    if (t) return t;
  }
  return "";
}

// ---- Value / type extraction --------------------------------------------

function extract(el) {
  const tag = el.tagName.toLowerCase();
  const type = (el.getAttribute("type") || tag).toLowerCase();

  if (type === "password" || type === "hidden" || type === "file") return null;

  if (tag === "select") {
    const opt = el.options[el.selectedIndex];
    const value = opt ? opt.textContent.trim() : el.value;
    if (!value) return null;
    return { key: normKey(labelText(el)), label: labelText(el).trim(), type: "select", value };
  }

  if (type === "radio") {
    if (!el.checked) return null;
    // Value = this option's visible label; key = the group's question.
    const optLabel = labelText(el).trim() || el.value;
    return { key: normKey(groupLabel(el)), label: groupLabel(el).trim(), type: "radio", value: optLabel };
  }

  if (type === "checkbox") {
    // Only capture when checked. Key each box by its own label and store a
    // plain "checked" — this handles a lone consent box AND "select all that
    // apply" uniformly (each ticked box is an independent boolean to replay).
    if (!el.checked) return null;
    const lbl = (labelText(el).trim() || el.value || "").trim();
    if (!lbl) return null;
    return { key: normKey(lbl), label: lbl, type: "checkbox", value: "checked" };
  }

  // text-like inputs + textarea
  const value = (el.value || "").trim();
  if (!value) return null;
  return { key: normKey(labelText(el)), label: labelText(el).trim(), type, value };
}

// Like extract(), but derives only the question key + type for an *empty*
// field we want to fill (extract() bails on empty values). Uses the exact
// same label logic so a field's fill-key matches its learned key.
function deriveKey(el) {
  const tag = el.tagName.toLowerCase();
  const type = (el.getAttribute("type") || tag).toLowerCase();
  if (type === "password" || type === "hidden" || type === "file") return null;
  if (type === "submit" || type === "button" || type === "reset") return null;
  if (tag === "select") return { key: normKey(labelText(el)), type: "select" };
  if (type === "radio") {
    return { key: normKey(groupLabel(el)), type: "radio",
             optLabel: (labelText(el).trim() || el.value || "") };
  }
  if (type === "checkbox") {
    return { key: normKey((labelText(el).trim() || el.value || "")), type: "checkbox" };
  }
  if (type === "search" || type === "otp") return null;
  // react-select style: an <input role=combobox> — typing into it never
  // commits a selection, it must be driven as a dropdown.
  if (tag === "input" && el.getAttribute("role") === "combobox") {
    return { key: normKey(labelText(el)), type: "combobox" };
  }
  return { key: normKey(labelText(el)), type };
}

// A native `change` and a custom-widget `click` can describe the same
// selection. Suppress a repeat of the same (key, value) within this window.
const recent = new Map();
const DEDUPE_MS = 1500;

async function send(rec) {
  const now = Date.now();
  const prev = recent.get(rec.key);
  if (prev && prev.value === rec.value && now - prev.ts < DEDUPE_MS) return;
  recent.set(rec.key, { value: rec.value, ts: now });
  try {
    await api("/api/answers", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        question_key: rec.key,
        label: rec.label,
        field_type: rec.type,
        value: rec.value,
        domain: location.hostname,
      }),
    });
    captureCount += 1;
    updateBadge();
  } catch (e) {
    console.warn("[jfc] failed to POST /api/answers — is the server running?", e);
  }
}

function onChange(ev) {
  if (!learnMode) return;
  const el = ev.target;
  if (!el || !el.tagName) return;
  if (!/^(input|select|textarea)$/i.test(el.tagName)) return;
  const rec = extract(el);
  if (!rec || !rec.key || rec.key.length < 2 || !rec.value) return;
  send(rec);
}

// Custom dropdowns (Greenhouse/Workday react-select etc.) render options as
// clickable divs and never fire a native `change`. Capture the selection when
// an option is clicked, keyed by the open combobox's question.
function onClick(ev) {
  if (!learnMode) return;
  // Yes/No pill buttons (Ashby): plain <button>s that never fire a change
  // event; the question lives on the surrounding field entry.
  const btn = ev.target.closest && ev.target.closest("button");
  if (btn && yesNoGroup(btn)) {
    const value = (btn.textContent || "").replace(/\s+/g, " ").trim();
    const qlabel = fieldEntryQuestion(btn);
    const key = normKey(qlabel);
    if (value && key && key.length >= 2) {
      send({ key, label: qlabel.trim(), type: "yesno", value });
    }
    return;
  }
  const opt = ev.target.closest && ev.target.closest('[role="option"]');
  if (!opt) return;
  const value = (opt.textContent || "").replace(/\s+/g, " ").trim();
  if (!value) return;
  const qlabel = openComboLabel(opt);
  const key = normKey(qlabel);
  if (!key || key.length < 2) return;
  send({ key, label: qlabel.trim(), type: "combobox", value });
}

document.addEventListener("change", onChange, { capture: true });
document.addEventListener("click", onClick, { capture: true });

// `change` is not a composed event: it never crosses a shadow boundary, so
// the document-level listeners above miss fields rendered inside web
// components (SmartRecruiters' spl-* elements). Attach capture listeners
// inside every open shadow root, and re-sweep as the app renders more.
const hookedRoots = new WeakSet();
function hookShadowRoots(scope) {
  for (const el of scope.querySelectorAll("*")) {
    const sr = el.shadowRoot;
    if (!sr) continue;
    if (!hookedRoots.has(sr)) {
      hookedRoots.add(sr);
      sr.addEventListener("change", onChange, { capture: true });
      sr.addEventListener("click", onClick, { capture: true });
    }
    hookShadowRoots(sr);
  }
}
let sweepTimer = null;
function scheduleSweep() {
  if (sweepTimer) return;
  sweepTimer = setTimeout(() => { sweepTimer = null; hookShadowRoots(document); }, 500);
}
hookShadowRoots(document);
new MutationObserver(scheduleSweep)
  .observe(document.documentElement, { childList: true, subtree: true });

// ---- On-page indicator ---------------------------------------------------

function updateBadge() {
  const badge = document.getElementById(BADGE_ID);
  if (!badge) return;
  badge.textContent = `● Learning fields — ${captureCount} captured`;
}

function showBadge() {
  // The content script now runs in every frame (application forms often live
  // in cross-origin iframes, e.g. Comeet). Draw chrome only in the top frame;
  // iframes still capture and fill silently.
  if (window !== window.top) return;
  if (document.getElementById(BADGE_ID)) return;
  const badge = document.createElement("div");
  badge.id = BADGE_ID;
  badge.textContent = "● Learning fields — 0 captured";
  Object.assign(badge.style, {
    position: "fixed", bottom: "14px", right: "14px", zIndex: "2147483647",
    background: "#161a23", color: "#4ade80", font: "12px/1.4 -apple-system, sans-serif",
    padding: "6px 10px", border: "1px solid #2a3142", borderRadius: "6px",
    boxShadow: "0 2px 8px rgba(0,0,0,0.4)", pointerEvents: "none",
  });
  document.body.appendChild(badge);
  updateBadge();
}

function hideBadge() {
  const badge = document.getElementById(BADGE_ID);
  if (badge) badge.remove();
}

function applyMode() {
  if (learnMode) showBadge();
  else hideBadge();
}

// ---- Wire up storage state ----------------------------------------------

chrome.storage.local.get(["learnMode"], (res) => {
  learnMode = !!res.learnMode;
  applyMode();
});

chrome.storage.onChanged.addListener((changes, area) => {
  if (area === "local" && changes.learnMode) {
    learnMode = !!changes.learnMode.newValue;
    if (!learnMode) captureCount = 0;
    applyMode();
  }
});

// ---- Fill mode -----------------------------------------------------------
// On demand (from the popup's "Fill this page" button) fetch every learned
// answer, key each field on the page the same way Learn mode did, and prefill
// the ones we recognize. Never overwrites a field you've already filled.

function usualValue(answer) {
  // The most-frequent variant is your "usual" answer; fall back to last value.
  const variants = answer.variants || {};
  let best = answer.value, bestN = -1;
  for (const [v, n] of Object.entries(variants)) {
    if (n > bestN) { bestN = n; best = v; }
  }
  return best;
}

// Token-Jaccard similarity for fuzzy key matching (label wording drifts
// slightly between sites: "work authorization" vs "authorized to work").
function tokens(s) {
  return new Set((s || "").split(/[^a-z0-9]+/i).filter((t) => t.length > 2));
}
function sim(a, b) {
  const A = tokens(a), B = tokens(b);
  if (!A.size || !B.size) return 0;
  let inter = 0;
  for (const t of A) if (B.has(t)) inter++;
  return inter / (A.size + B.size - inter);
}

// strict=true: exact key only. Use for constrained choices (radio/select/
// checkbox) where a fuzzy near-miss would set the wrong Yes/No on a *different*
// question. Free-text fields pass strict=false and tolerate wording drift.
function bestAnswer(key, byKey, strict) {
  if (!key) return null;
  if (byKey.has(key)) return byKey.get(key);
  if (strict) return null;
  let best = null, score = 0;
  for (const [k, a] of byKey) {
    const s = sim(key, k);
    if (s > score) { score = s; best = a; }
  }
  return score >= 0.6 ? best : null;
}

// querySelectorAll that descends into open shadow roots (fields on
// SmartRecruiters etc. are invisible to a document-level query).
function deepQueryAll(selector, scope = document) {
  const out = [...scope.querySelectorAll(selector)];
  for (const el of scope.querySelectorAll("*")) {
    if (el.shadowRoot) out.push(...deepQueryAll(selector, el.shadowRoot));
  }
  return out;
}

// Simulate real typing: focus, insert via execCommand (fires trusted-shaped
// beforeinput/input with inputType, which React/Vue/controlled forms all
// honor). Falls back to the native-setter trick when execCommand is
// unavailable (e.g. number inputs). Does NOT blur — combobox typing needs
// the field to stay focused so the dropdown stays open.
function typeInto(el, value) {
  const proto = el.tagName === "TEXTAREA"
    ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
  const setter = Object.getOwnPropertyDescriptor(proto, "value").set;
  el.focus();
  let ok = false;
  try {
    setter.call(el, "");
    if (el.select) el.select();
    ok = document.execCommand("insertText", false, value) && el.value === value;
  } catch (e) { /* fall through */ }
  if (!ok) {
    setter.call(el, value);
    el.dispatchEvent(new Event("input", { bubbles: true }));
  }
}

function setNativeValue(el, value) {
  typeInto(el, value);
  el.dispatchEvent(new Event("change", { bubbles: true }));
  // Blur to mark the field "touched" so framework validation runs.
  el.blur();
  el.dispatchEvent(new Event("blur", { bubbles: false }));
  el.dispatchEvent(new Event("focusout", { bubbles: true }));
}

function fillOutline(el, ok) {
  el.style.outline = ok ? "2px solid #4ade80" : "2px dashed #f59e0b";
  el.style.outlineOffset = "1px";
}

function fillSelect(el, value) {
  const want = normKey(value);
  for (const opt of el.options) {
    if (normKey(opt.textContent) === want || normKey(opt.value) === want) {
      el.value = opt.value;
      el.dispatchEvent(new Event("change", { bubbles: true }));
      return true;
    }
  }
  return false;
}

// ---- Structured group fill (education / employment history) --------------
// Greenhouse-style repeating blocks use index-suffixed ids (school--0,
// degree--0, discipline--0, then school--1 after "Add another"). Entries come
// from the server's profile.json (education[] / workHistory[]), not from
// learned answers — a block is an ordered record, not independent fields.

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function groupField(base, i) {
  for (const id of [`${base}--${i}`, `${base}-${i}`, `${base}_${i}`]) {
    const el = deepQueryAll(`input[id="${id}"], select[id="${id}"]`)[0];
    if (el) return el;
  }
  return null;
}

function nearestAddButton(anchorEl) {
  const btns = deepQueryAll('button, [role="button"], a').filter((b) =>
    /add (another|more|education|experience|employment|position)/i.test((b.textContent || "").trim()));
  let node = anchorEl && anchorEl.parentElement;
  for (let i = 0; i < 8 && node; i++, node = node.parentElement) {
    const hit = btns.find((b) => node.contains(b));
    if (hit) return hit;
  }
  return btns[0] || null;
}

// react-select (Greenhouse) ignores bare .click(): it opens menus on
// mousedown and selects options on mousedown/up. Send the full sequence.
function mouseSeq(el) {
  for (const t of ["pointerdown", "mousedown", "pointerup", "mouseup", "click"]) {
    el.dispatchEvent(new MouseEvent(t, { bubbles: true, cancelable: true, view: window }));
  }
}

function visibleOptions() {
  // Options portal is light DOM on GH/Ashby; only deep-walk when needed.
  let opts = [...document.querySelectorAll('[role="option"]')];
  if (!opts.length) opts = deepQueryAll('[role="option"]');
  return opts.filter((o) => o.offsetParent !== null);
}

// A react-select that already holds a selection shows it in a "single-value"
// element next to the (empty) input.
function comboHasValue(el) {
  const shell = el.closest('[class*="select"], [class*="control"]') || el.parentElement;
  if (!shell) return false;
  const sv = shell.querySelector('[class*="single-value"], [class*="singleValue"]');
  return !!(sv && sv.textContent.trim());
}

function matchOption(opts, want) {
  let target =
    opts.find((o) => normKey(o.textContent) === want) ||
    opts.find((o) => normKey(o.textContent).includes(want) ||
                     want.includes(normKey(o.textContent)));
  if (!target) {
    // Punctuation drifts ("PhD" vs "Ph.D."): token-similarity fallback.
    let best = null, score = 0;
    for (const o of opts) {
      const s = sim(normKey(o.textContent), want);
      if (s > score) { score = s; best = o; }
    }
    if (score >= 0.5) target = best;
  }
  if (!target) {
    // Degree taxonomies vary per site: "Master of Science (MS)" must land on
    // "Master's Degree" when that's all the list offers. Match on the leading
    // degree word, but only when exactly one option stems to it.
    const stem = (s) => normKey(s).replace(/'s\b/g, "").split(/\s+/)[0];
    const wstem = stem(want);
    if (/^(associate|bachelor|master|doctor|phd|mba|high)/.test(wstem)) {
      const hits = opts.filter((o) => stem(o.textContent) === wstem);
      if (hits.length === 1) target = hits[0];
      else if (hits.length > 1) {
        // "Master of Science" vs {"Master's Degree", "Master of Business
        // Administration"}: take the most token-similar stem hit.
        let best = null, score = -1;
        for (const o of hits) {
          const s = sim(normKey(o.textContent), want);
          if (s > score) { score = s; best = o; }
        }
        target = best;
      }
    }
  }
  return target || null;
}

// Wait for dropdown options via MutationObserver rather than timer polling:
// setTimeout is heavily throttled in background tabs, but DOM mutations
// (menu render, async search results) fire observers regardless.
function awaitOptions(maxMs) {
  return new Promise((resolve) => {
    let mo = null, timer = null;
    const done = (opts) => {
      if (mo) mo.disconnect();
      if (timer) clearTimeout(timer);
      resolve(opts);
    };
    const check = () => {
      const o = visibleOptions();
      if (o.length) { done(o); return true; }
      return false;
    };
    if (check()) return;
    mo = new MutationObserver(check);
    mo.observe(document.body, { childList: true, subtree: true });
    timer = setTimeout(() => done(visibleOptions()), maxMs);
  });
}

// Drive a searchable combobox (react-select and friends). Static lists are
// matched by reading the open menu directly — typing first is harmful there
// because the substring filter hides near-matches ("(MS)" vs "(M.S.)").
// Async/long lists (school search) get type-to-filter with progressively
// looser text; the full wanted value still decides which option wins.
async function pickCombo(input, text) {
  if (!text) return false;
  const want = normKey(text);
  input.focus();
  if (input.getAttribute("aria-expanded") !== "true") mouseSeq(input);
  let opts = await awaitOptions(1500);
  let target = matchOption(opts, want);
  if (!target) {
    const tries = [...new Set([
      text,
      text.replace(/\(.*?\)/g, " ").replace(/\s+/g, " ").trim(),
      text.split(/\s+/).slice(0, 2).join(" "),
    ])].filter(Boolean);
    for (const t of tries) {
      typeInto(input, t);
      opts = await awaitOptions(2000);
      target = matchOption(opts, want);
      if (target) break;
    }
  }
  if (target) {
    mouseSeq(target);
    await sleep(200);
    fillOutline(input, true);
    return true;
  }
  input.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
  input.blur();
  fillOutline(input, false);
  return false;
}

// Ensure block i exists (clicking "Add another" if needed) and return its
// lead field, or null when the page can't grow further.
async function ensureBlock(base, i) {
  let lead = groupField(base, i);
  if (lead) return lead;
  const btn = nearestAddButton(groupField(base, 0));
  if (!btn) return null;
  btn.click();
  await sleep(500);
  return groupField(base, i);
}

async function putGroupValue(el, value) {
  if (!el || !value) return;
  if (el.getAttribute("role") === "combobox") await pickCombo(el, value);
  else setNativeValue(el, value);
}

async function fillEducationGroups(list) {
  if (!Array.isArray(list) || !list.length || !groupField("school", 0)) return 0;
  let groups = 0;
  for (let i = 0; i < list.length; i++) {
    const school = await ensureBlock("school", i);
    if (!school) break;
    const e = list[i];
    if (school.value.trim()) { groups++; continue; }  // entry already filled
    await putGroupValue(school, e.school);
    await putGroupValue(groupField("degree", i), e.degree);
    await putGroupValue(groupField("discipline", i), e.discipline);
    await putGroupValue(groupField("start-year", i) || groupField("start_year", i), e.startYear);
    await putGroupValue(groupField("end-year", i) || groupField("end_year", i), e.endYear);
    groups++;
  }
  return groups;
}

async function fillWorkHistoryGroups(list) {
  if (!Array.isArray(list) || !list.length) return 0;
  const base = ["company", "employer", "organization"].find((b) => groupField(b, 0));
  if (!base) return 0;
  let groups = 0;
  for (let i = 0; i < list.length; i++) {
    const company = await ensureBlock(base, i);
    if (!company) break;
    const w = list[i];
    if (company.value.trim()) { groups++; continue; }
    await putGroupValue(company, w.company);
    await putGroupValue(groupField("title", i) || groupField("job_title", i), w.title);
    await putGroupValue(groupField("start-month", i) || groupField("start_month", i), w.startMonth);
    await putGroupValue(groupField("start-year", i) || groupField("start_year", i), w.startYear);
    if (!w.current) {
      await putGroupValue(groupField("end-month", i) || groupField("end_month", i), w.endMonth);
      await putGroupValue(groupField("end-year", i) || groupField("end_year", i), w.endYear);
    }
    groups++;
  }
  return groups;
}

async function runFill() {
  let answers, profile = {};
  try {
    answers = (await api("/api/answers")).answers || [];
  } catch (e) {
    console.warn("[jfc] fill: cannot reach server", e);
    flashBadge("Fill failed — is the server running?", "#f87171");
    return;
  }
  try {
    profile = (await api("/api/profile")) || {};
  } catch (e) { /* structured groups just won't fill */ }
  const byKey = new Map(answers.map((a) => [a.question_key, a]));

  // Seed profile.json values for common questions never captured in Learn
  // mode (only where no learned answer exists — learned answers win).
  const loc = profile.location || {};
  const eeo = profile.eeo || {};
  const seeds = {
    "location (city)": loc.city,
    "city": loc.city,
    "state": loc.state,
    "country": loc.country,
    "website": profile.website || profile.github,
    "github url": profile.github,
    "disability status": eeo.disability,
    "veteran status": eeo.veteran,
    "gender": eeo.gender,
    "will you now or in the future require sponsorship to work within the united states?": profile.sponsorship,
    "will you now or in the future require sponsorship for employment visa status (e.g., h-1b visa status)?": profile.sponsorship,
    "are you legally authorized to work in the united states?": profile.workAuth,
  };
  for (const [k, v] of Object.entries(seeds)) {
    if (v && !byKey.has(k)) byKey.set(k, { question_key: k, value: v, variants: { [v]: 1 } });
  }

  let filled = 0, unmatched = 0;
  const seenRadioGroups = new Set();
  const textFills = [];
  const els = deepQueryAll("input, select, textarea");

  for (const el of els) {
    if (el.disabled || el.readOnly || el.offsetParent === null) continue;
    // react-select renders an aria-hidden required-validation shim input
    // alongside each combobox — not a real field.
    if (el.getAttribute("aria-hidden") === "true") continue;
    // Index-suffixed ids (school--0, company--1) belong to structured blocks
    // — the group filler below handles those as whole records. (Match only
    // known block bases: GH custom questions also end in digits.)
    if (/^(school|degree|discipline|company|employer|organization|title|job[-_]title|(start|end)[-_]?(month|year|date))[-_]{1,2}\d+$/i.test(el.id || "")) continue;
    const d = deriveKey(el);
    if (!d || !d.key || d.key.length < 2) continue;

    if (d.type === "radio") {
      const group = el.getAttribute("name") || d.key;
      if (seenRadioGroups.has(group)) continue;
      seenRadioGroups.add(group);
      const ans = bestAnswer(d.key, byKey, true);
      if (!ans) { unmatched++; continue; }
      const want = normKey(usualValue(ans));
      const radios = rootOf(el).querySelectorAll(`input[type=radio][name="${CSS.escape(group)}"]`);
      let hit = false;
      for (const r of radios) {
        const rl = normKey(labelText(r).trim() || r.value);
        if (rl === want) {
          r.checked = true;
          r.dispatchEvent(new Event("change", { bubbles: true }));
          fillOutline(r, true); hit = true; break;
        }
      }
      hit ? filled++ : unmatched++;
      continue;
    }

    if (d.type === "checkbox") {
      if (el.checked) continue;
      const ans = bestAnswer(d.key, byKey, true);
      if (ans) {  // stored as a ticked box -> tick it
        el.checked = true;
        el.dispatchEvent(new Event("change", { bubbles: true }));
        fillOutline(el, true); filled++;
      }
      continue;
    }

    if (d.type === "select") {
      if (el.value && el.selectedIndex > 0) continue;
      const ans = bestAnswer(d.key, byKey, true);
      if (ans && fillSelect(el, usualValue(ans))) { fillOutline(el, true); filled++; }
      else if (ans) { fillOutline(el, false); unmatched++; }
      continue;
    }

    if (d.type === "combobox") {
      if (comboHasValue(el)) continue;  // already selected
      const ans = bestAnswer(d.key, byKey, true);
      if (!ans) { unmatched++; continue; }
      (await pickCombo(el, usualValue(ans))) ? filled++ : unmatched++;
      continue;
    }

    // text-like inputs + textarea — never clobber what you've already typed
    if (el.value && el.value.trim()) continue;
    const ans = bestAnswer(d.key, byKey);
    if (ans) {
      const v = usualValue(ans);
      setNativeValue(el, v); fillOutline(el, true); filled++;
      textFills.push({ el, value: v });
    }
    else { unmatched++; }
  }

  // Some SPAs re-render after the fill burst and revert fields whose state
  // didn't stick (seen on Ashby: field looks filled but submit says it's
  // empty). Re-check a moment later and re-apply once.
  await new Promise((r) => setTimeout(r, 600));
  for (const { el, value } of textFills) {
    if (el.isConnected && el.value !== value) {
      setNativeValue(el, value);
      fillOutline(el, el.value === value);
    }
  }

  // Yes/No pill groups (hidden checkbox + sibling buttons): the checkbox is
  // skipped by the loop above (visually hidden, no usable label). Click the
  // button whose text matches the learned answer.
  for (const cbx of deepQueryAll('input[type=checkbox]')) {
    const btns = yesNoGroup(cbx);
    if (!btns) continue;
    // Already answered (Ashby marks the chosen pill with an _active class).
    if (btns.some((b) => /(^|[_\s])(active|selected)/i.test(b.className))) continue;
    const key = normKey(fieldEntryQuestion(cbx));
    if (!key || key.length < 2) continue;
    const ans = bestAnswer(key, byKey, true);
    if (!ans) { unmatched++; continue; }
    const want = normKey(usualValue(ans));
    const target = btns.find((b) => normKey(b.textContent) === want);
    if (target) { target.click(); fillOutline(target, true); filled++; }
    else { unmatched++; }
  }

  // Structured blocks last: the flat pass above skipped their fields.
  let groups = 0;
  try {
    groups += await fillEducationGroups(profile.education);
    groups += await fillWorkHistoryGroups(profile.workHistory);
  } catch (e) {
    console.warn("[jfc] group fill failed", e);
  }

  flashBadge(`Filled ${filled} field${filled === 1 ? "" : "s"}` +
             (groups ? ` · ${groups} history entr${groups === 1 ? "y" : "ies"}` : "") +
             (unmatched ? ` · ${unmatched} unmatched` : ""), "#4ade80");
  console.log(`[jfc] fill: ${filled} filled, ${groups} group entries, ${unmatched} unmatched`);
}

// Transient corner toast reused for fill results (distinct from the learn badge).
function flashBadge(text, color) {
  if (window !== window.top) return;
  const id = "jfc-fill-toast";
  document.getElementById(id)?.remove();
  const t = document.createElement("div");
  t.id = id;
  t.textContent = text;
  Object.assign(t.style, {
    position: "fixed", bottom: "44px", right: "14px", zIndex: "2147483647",
    background: "#161a23", color: color, font: "12px/1.4 -apple-system, sans-serif",
    padding: "6px 10px", border: "1px solid #2a3142", borderRadius: "6px",
    boxShadow: "0 2px 8px rgba(0,0,0,0.4)", pointerEvents: "none",
  });
  document.body.appendChild(t);
  setTimeout(() => t.remove(), 4000);
}

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg && msg.type === "fill") {
    runFill().then(() => sendResponse({ ok: true }));
    return true;  // async response
  }
});

console.log("[jfc] Field Capture content script loaded");

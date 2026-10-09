// LinkedIn Job Seen Tracker
// Scans job cards on linkedin.com/jobs/*, hashes (company|title|location),
// queries the local server for prior sightings, paints a badge, and posts
// a "dismissed" event when the X button is clicked.

const API_BASE = "http://127.0.0.1:5050";
const BADGE_CLASS = "hjs-seen-badge";
const PROCESSED_ATTR = "data-hjs-processed";

function norm(s) {
  return (s || "").trim().toLowerCase().replace(/\s+/g, " ");
}

// SHA-1 via SubtleCrypto — async, but fast.
async function sha1(s) {
  const buf = new TextEncoder().encode(s);
  const digest = await crypto.subtle.digest("SHA-1", buf);
  return Array.from(new Uint8Array(digest))
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}

function textOf(el) {
  return el ? el.textContent.replace(/\s+/g, " ").trim() : "";
}

function extractCard(li) {
  // Prefer the inner div with data-job-id — also present at the li level
  // as data-occludable-job-id.
  const inner = li.querySelector("[data-job-id]");
  const linkedinId =
    (inner && inner.getAttribute("data-job-id")) ||
    li.getAttribute("data-occludable-job-id") ||
    "";
  if (!linkedinId) return null;

  // Title: the strong inside the title link. Fall back to aria-label.
  const titleLink = li.querySelector("a.job-card-list__title--link, a.job-card-container__link");
  let title = textOf(li.querySelector(".job-card-list__title--link strong")) ||
              textOf(li.querySelector(".artdeco-entity-lockup__title"));
  if (!title && titleLink) {
    title = (titleLink.getAttribute("aria-label") || "")
      .replace(/ with verification$/, "")
      .trim();
  }
  const company = textOf(li.querySelector(".artdeco-entity-lockup__subtitle"));
  const location = textOf(li.querySelector(".artdeco-entity-lockup__caption li, .job-card-container__metadata-wrapper li"));

  return { li, linkedinId, title, company, location };
}

function findBadgeTarget(li) {
  // Place badge next to the "Viewed/Promoted" footer pill if present,
  // else into the title block.
  return (
    li.querySelector(".job-card-list__footer-wrapper") ||
    li.querySelector(".artdeco-entity-lockup__title") ||
    li
  );
}

function paintBadge(li, info) {
  // Remove any existing badge first (cards re-render on scroll).
  li.querySelectorAll("." + BADGE_CLASS).forEach((e) => e.remove());

  const target = findBadgeTarget(li);
  if (!target) return;
  const when = (info.last_seen_at || "").slice(0, 10) || "?";
  const action = info.action || "dismissed";
  const count = info.seen_count || 1;
  const badge = document.createElement("span");
  badge.className = `${BADGE_CLASS} hjs-action-${action}`;
  badge.textContent = `${action} ${when}${count > 1 ? ` ×${count}` : ""}`;
  badge.title = `Last ${action}: ${info.last_seen_at}\nFirst seen: ${info.first_seen_at}`;
  target.appendChild(badge);
}

async function postSeen(card, action) {
  const hash = await sha1(`${norm(card.company)}|${norm(card.title)}|${norm(card.location)}`);
  try {
    await fetch(`${API_BASE}/api/seen`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        linkedin_id: card.linkedinId,
        hash,
        company: card.company,
        title: card.title,
        location: card.location,
        action,
      }),
    });
  } catch (e) {
    console.warn("[hjs] failed to POST /api/seen", e);
  }
}

async function lookupAndBadge(cards) {
  if (!cards.length) return;
  const ids = cards.map((c) => c.linkedinId);
  const hashes = await Promise.all(
    cards.map((c) => sha1(`${norm(c.company)}|${norm(c.title)}|${norm(c.location)}`))
  );
  let data;
  try {
    const resp = await fetch(`${API_BASE}/api/seen/lookup`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ids, hashes }),
    });
    data = await resp.json();
  } catch (e) {
    console.warn("[hjs] /api/seen/lookup failed — is the server running?", e);
    return;
  }
  const seen = data.seen || {};
  cards.forEach((card, i) => {
    const hit = seen[card.linkedinId] || seen[hashes[i]];
    if (hit) paintBadge(card.li, hit);
  });
}

function findDismissButton(li) {
  return li.querySelector('button[aria-label^="Dismiss"]');
}

function wireDismissListener(card) {
  const btn = findDismissButton(card.li);
  if (!btn || btn.dataset.hjsBound === "1") return;
  btn.dataset.hjsBound = "1";
  btn.addEventListener("click", () => {
    postSeen(card, "dismissed");
  }, { capture: true });
}

async function processNewCards(root = document) {
  const lis = Array.from(root.querySelectorAll("li[data-occludable-job-id]"))
    .filter((li) => !li.hasAttribute(PROCESSED_ATTR));
  if (!lis.length) return;
  const cards = [];
  for (const li of lis) {
    li.setAttribute(PROCESSED_ATTR, "1");
    const c = extractCard(li);
    if (!c) continue;
    cards.push(c);
    wireDismissListener(c);
  }
  await lookupAndBadge(cards);
}

const observer = new MutationObserver((muts) => {
  // Cheap debounce — coalesce bursts of mutations.
  if (observer._pending) return;
  observer._pending = true;
  setTimeout(() => {
    observer._pending = false;
    processNewCards();
  }, 150);
});

observer.observe(document.documentElement, { childList: true, subtree: true });
processNewCards();
console.log("[hjs] LinkedIn Job Seen Tracker loaded");

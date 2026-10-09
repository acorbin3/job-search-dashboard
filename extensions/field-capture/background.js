// Service worker: proxies all local-server traffic for the content script.
//
// In Manifest V3, content-script fetches follow the *page's* CSP — sites with
// a strict connect-src (Greenhouse job-boards, some Workday tenants) block
// requests to 127.0.0.1, which surfaced as intermittent "Failed to fetch"
// in Learn/Fill. Background fetches are exempt from page CSP and use the
// extension's own host permissions.

const API_BASE = "http://127.0.0.1:5050";

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (!msg || msg.type !== "api") return;
  fetch(API_BASE + msg.path, msg.init || {})
    .then(async (res) => sendResponse({ ok: res.ok, status: res.status, body: await res.text() }))
    .catch((e) => sendResponse({ ok: false, error: String(e) }));
  return true; // keep the message channel open for the async response
});

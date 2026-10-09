const toggle = document.getElementById("toggle");
const status = document.getElementById("status");

function paint(on) {
  toggle.checked = on;
  status.textContent = on
    ? "on — recording the fields you fill"
    : "off — nothing is recorded";
  status.classList.toggle("on", on);
}

chrome.storage.local.get(["learnMode"], (res) => paint(!!res.learnMode));

toggle.addEventListener("change", () => {
  const on = toggle.checked;
  chrome.storage.local.set({ learnMode: on }, () => paint(on));
});

const fill = document.getElementById("fill");
const fillStatus = document.getElementById("fillStatus");

fill.addEventListener("click", () => {
  fill.disabled = true;
  fillStatus.textContent = "filling…";
  chrome.tabs.query({ active: true, currentWindow: true }, ([tab]) => {
    if (!tab) { fillStatus.textContent = "no active tab"; fill.disabled = false; return; }
    chrome.tabs.sendMessage(tab.id, { type: "fill" }, (resp) => {
      if (chrome.runtime.lastError) {
        fillStatus.textContent = "reload the page, then retry";
      } else {
        fillStatus.textContent = "done — see the page";
      }
      fill.disabled = false;
    });
  });
});

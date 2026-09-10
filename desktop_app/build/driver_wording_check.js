// Is the destination really ABOVE the status line the message points at?
await setSelection([SELECTION], true);
await new Promise((r) => setTimeout(r, 300));
const out = document.getElementById("out-box").getBoundingClientRect();
const csv = document.getElementById("csv-box").getBoundingClientRect();
const status = document.getElementById("status-card").getBoundingClientRect();
window.__wording = {
  statusText: document.getElementById("status-card").innerText,
  outputBoxAboveStatus: out.bottom <= status.top,
  csvBoxAboveStatus: csv.bottom <= status.top,
  gapPx: Math.round(status.top - out.bottom),
};

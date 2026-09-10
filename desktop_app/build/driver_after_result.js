// The state the operator is actually left in: a finished batch run. The
// result banner, the per-patient table and the action row all appear AFTER
// processing, so a frame that fits before anonymizing proves nothing.
// Goes through the real flow -- select, then press Anonymize -- so the
// button's own height is part of what gets measured.
await setSelection([SELECTION], true);
await new Promise((r) => setTimeout(r, 900));  // let the reveal scroll settle
window.__selectedFrame = (() => {
  const d = document.documentElement;
  const a = document.querySelector(".anon-actions").getBoundingClientRect();
  const b = document.getElementById("anonymize-btn").getBoundingClientRect();
  return {
    anonymizeOnScreen: b.height > 0 && b.bottom <= d.clientHeight + 1 && b.top >= 0,
    destinationStillVisible: (() => {
      const o = document.getElementById("out-box").getBoundingClientRect();
      return o.top >= 0 && o.bottom <= d.clientHeight + 1;
    })(),
    actionsInView: a.bottom <= d.clientHeight + 1,
    pageScrolls: d.scrollHeight > d.clientHeight + 1,
  };
})();
document.getElementById("anonymize-btn").click();
await new Promise((r) => setTimeout(r, 900));

const doc = document.documentElement;
const card = document.querySelector(".anon-tool-card");
const actions = document.querySelector(".anon-actions");
const table = document.getElementById("batch-results");
const approve = document.getElementById("approve-btn");

// runProcess() scrolls the result into view; give the smooth scroll time
// to land before measuring what the operator can actually see.
await new Promise((r) => setTimeout(r, 900));

const cardRect = card.getBoundingClientRect();
const actRect = actions.getBoundingClientRect();

const bannerRect = document.getElementById("result-banner").getBoundingClientRect();
window.__after = {
  bannerVisibleWithoutTouchingAnything:
    bannerRect.top >= cardRect.top - 1 && bannerRect.bottom <= cardRect.bottom + 1,
  approveVisibleWithoutTouchingAnything: (() => {
    const r = approve.getBoundingClientRect();
    return r.top >= cardRect.top - 1 && r.bottom <= cardRect.bottom + 1;
  })(),
  pageScrolls: doc.scrollHeight > doc.clientHeight + 1,
  viewportH: doc.clientHeight,
  cardOverflowY: getComputedStyle(card).overflowY,
  cardContentH: card.scrollHeight,
  cardVisibleH: Math.round(cardRect.height),
  cardClipsContent: card.scrollHeight > card.clientHeight + 1,
  batchTableVisible: table && !table.classList.contains("hidden"),
  approveEnabled: approve && !approve.disabled,
  actionsBottom: Math.round(actRect.bottom),
  actionsWithinCard: actRect.bottom <= cardRect.bottom + 1,
};

// The real question: can the operator GET to Approve at all?
if (card.scrollHeight > card.clientHeight + 1) {
  card.scrollTop = card.scrollHeight;
  await new Promise((r) => setTimeout(r, 120));
  const r2 = approve.getBoundingClientRect();
  window.__after.approveReachableByScrolling =
    r2.bottom <= doc.clientHeight + 1 && r2.top >= 0 && r2.height > 0;
  window.__after.scrolledCardTop = card.scrollTop;
} else {
  window.__after.approveReachableByScrolling = window.__after.actionsWithinCard;
  window.__after.scrolledCardTop = 0;
}

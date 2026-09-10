// What spacing is actually in force at this viewport?
await new Promise((r) => setTimeout(r, 500));
const cs = (sel, ...props) => {
  const el = document.querySelector(sel);
  if (!el) return null;
  const s = getComputedStyle(el);
  const out = { _height: Math.round(el.getBoundingClientRect().height) };
  props.forEach((p) => { out[p] = s[p]; });
  return out;
};
window.__spacing = {
  viewport: document.documentElement.clientHeight,
  // Which compaction passes are live right now
  passes: {
    "<=860": matchMedia("(max-height: 860px)").matches,
    "<=800": matchMedia("(max-height: 800px)").matches,
    "<=780": matchMedia("(max-height: 780px)").matches,
    "<=740": matchMedia("(max-height: 740px)").matches,
  },
  toolCard: cs(".anon-tool-card", "padding"),
  introCard: cs(".anon-intro-card", "padding"),
  layoutGap: cs(".anon-layout", "gap"),
  sessionInputs: cs(".session-inputs", "gap", "margin"),
  sessionBox: cs(".session-box", "padding"),
  uploadTile: cs(".upload-tile", "padding", "minHeight"),
  cardHead: cs(".anon-tool-card .anon-card-head", "marginBottom"),
  headRow: cs(".workbench-head", "marginBottom"),
  workbench: cs(".anon-workbench", "padding"),
  hiddenNow: {
    toolCardIntro: getComputedStyle(document.querySelector(".anon-tool-card > .anon-card-head p")).display,
    uploadSubtitle: getComputedStyle(document.querySelector(".upload-subtitle")).display,
    uploadIcon: getComputedStyle(document.querySelector(".upload-icon")).display,
    tip: getComputedStyle(document.querySelector(".upload-guidance")).display,
  },
};

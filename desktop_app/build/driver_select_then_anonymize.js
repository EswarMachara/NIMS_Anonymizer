// The new flow: select, review where things will land, then Anonymize.
window.__flow = [];
const SEL = SELECTION;
const snap = (label) => window.__flow.push({
  label,
  csv: document.getElementById("csv-path").innerText,
  out: document.getElementById("out-path").innerText,
  outFull: document.getElementById("out-path").title,
  outState: document.getElementById("out-state").innerText,
  anonymizeVisible: !document.getElementById("anonymize-btn").classList.contains("hidden"),
  status: document.getElementById("status-card").innerText.slice(0, 62),
  resultShown: !document.getElementById("result-banner").classList.contains("hidden"),
  stateOutputDir: state.outputDir,
  stateOutputSource: state.outputSource,
  stateStudy: state.detectedStudy,
});

snap("on open");
await setSelection([SEL], true);
snap("selected sample_NIMS/KFRE");

document.getElementById("anonymize-btn").click();
await new Promise((r) => setTimeout(r, 1200));
snap("clicked Anonymize");

document.getElementById("reset-btn").click();
await new Promise((r) => setTimeout(r, 300));
snap("clicked Clear");

document.getElementById("out-browse-btn").click();
await new Promise((r) => setTimeout(r, 300));
snap("browsed to own folder");

await setSelection([SEL], true);
snap("selected again");

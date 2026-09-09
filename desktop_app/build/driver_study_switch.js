// Reproduces the reported bug: output destination chosen, Auto-detect left
// on, then a KFRE folder handed in. The session panel must stop saying eGFR.
window.__trace = [];
const snap = (label) => window.__trace.push({
  label,
  csv: document.getElementById("csv-path").innerText,
  out: document.getElementById("out-path").innerText,
});

state.outputDir = "C:\\Users\\macha\\Downloads\\KFRE";
await refreshSession();
snap("output chosen, nothing selected yet");

await runProcess(["C:\\Users\\macha\\Desktop\\NIMS\\KFRE"], true);
snap("after handing in a KFRE folder on Auto-detect");

setStudyOverride("egfr");
await refreshSession();
snap("operator forces eGFR");

setStudyOverride("");
await refreshSession();
snap("operator returns to Auto-detect");

els.resetBtn.click();
await new Promise((r) => setTimeout(r, 60));
snap("after Clear");

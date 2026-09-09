"use strict";
/*
  app.js -- TANUH Renal Anonymizer desktop app.

  Replaces the Renal-Data-Collection portal's fake in-browser demo JS
  (public/script.js's anon* functions, which just renamed files to
  "ANON-PATIENT-001_001.ext" in browser memory) with real calls into the
  Python backend (main.py's Api class / desktop_app/backend/engine.py),
  which is itself a thin wrapper around this project's already-validated
  anonymize_eGFR.py / anonymize_KFRE.py / anon_common.py engines.
*/

const els = {
  statusCard: document.getElementById("status-card"),
  resultBanner: document.getElementById("result-banner"),
  previewGrid: document.getElementById("preview-grid"),
  originalList: document.getElementById("original-list"),
  outputList: document.getElementById("output-list"),
  resetBtn: document.getElementById("reset-btn"),
  revealBtn: document.getElementById("reveal-btn"),
  approveBtn: document.getElementById("approve-btn"),
  chooseFolderBtn: document.getElementById("choose-folder-btn"),
  chooseFilesBtn: document.getElementById("choose-files-btn"),
  packageTile: document.getElementById("package-tile"),
  studyButtons: {
    "": document.getElementById("study-auto"),
    egfr: document.getElementById("study-egfr"),
    kfre: document.getElementById("study-kfre"),
  },
  studyDetectedNote: document.getElementById("study-detected-note"),
  hospitalChip: document.getElementById("hospital-chip"),
  footerMappingPath: document.getElementById("footer-mapping-path"),
  footerOutputPath: document.getElementById("footer-output-path"),
  steps: {
    1: document.getElementById("step-1"),
    2: document.getElementById("step-2"),
    3: document.getElementById("step-3"),
  },
};

let state = {
  studyOverride: "", // "" = auto-detect, "egfr", "kfre"
  lastResult: null,
  processing: false,
  appDataDir: null, // shared parent of both mapping CSVs + both output folders (see loadAppInfo())
};

function escapeHTML(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function basename(p) {
  return String(p || "").split(/[\\/]/).pop();
}

function setStep(n) {
  Object.entries(els.steps).forEach(([key, el]) => {
    if (!el) return;
    el.classList.toggle("step-active", Number(key) === n);
    el.classList.toggle("step-done", Number(key) < n);
  });
}

function setStudyOverride(value) {
  state.studyOverride = value;
  Object.entries(els.studyButtons).forEach(([key, btn]) => {
    if (btn) btn.classList.toggle("active", key === value);
  });
}

Object.entries(els.studyButtons).forEach(([key, btn]) => {
  btn?.addEventListener("click", () => setStudyOverride(key));
});

// ---------------------------------------------------------------------
// App info (mapping-csv / output-folder transparency, hospital branding)
// ---------------------------------------------------------------------

async function loadAppInfo() {
  try {
    const info = await window.pywebview.api.get_app_info();
    if (els.hospitalChip) els.hospitalChip.textContent = info.hospital_name;
    if (els.footerMappingPath) {
      els.footerMappingPath.textContent = `${info.egfr_mapping_csv} · ${info.kfre_mapping_csv}`;
    }
    if (els.footerOutputPath) {
      els.footerOutputPath.textContent = `${info.egfr_output_dir} · ${info.kfre_output_dir}`;
    }
    // Both eGFR/KFRE mapping CSVs and both output folders live directly
    // under this one shared app-data root -- opening it (rather than
    // guessing which of the two study-specific paths the operator meant)
    // is the one click target that is always correct regardless of which
    // study's path in the footer text they clicked.
    state.appDataDir = info.app_data_dir || null;
  } catch (err) {
    console.error("get_app_info failed", err);
  }
}

els.footerMappingPath?.addEventListener("click", () => {
  if (state.appDataDir) window.pywebview.api.reveal_in_explorer(state.appDataDir);
});
els.footerOutputPath?.addEventListener("click", () => {
  if (state.appDataDir) window.pywebview.api.reveal_in_explorer(state.appDataDir);
});

// ---------------------------------------------------------------------
// Selection -> processing
// ---------------------------------------------------------------------

els.chooseFolderBtn?.addEventListener("click", async () => {
  const picked = await window.pywebview.api.pick_folder();
  if (picked?.picked) runProcess(picked.paths, true);
});

els.chooseFilesBtn?.addEventListener("click", async () => {
  const picked = await window.pywebview.api.pick_files();
  if (picked?.picked) runProcess(picked.paths, false);
});

// Native drag-and-drop bridge -- see main.py's _setup_drag_and_drop().
// Python calls these two globals directly via evaluate_js(); they are not
// invoked from anywhere else in this file.
window.__anonSetDragOver = function (isOver) {
  els.packageTile?.classList.toggle("drag-over", Boolean(isOver));
};

window.__anonOnDropResolved = function (paths, errorMessage) {
  if (errorMessage) {
    setStatus(errorMessage, false);
    return;
  }
  if (paths && paths.length) runProcess(paths, false);
};

function setStatus(message, hasFile) {
  if (!els.statusCard) return;
  els.statusCard.textContent = message;
  els.statusCard.classList.toggle("has-file", Boolean(hasFile));
  els.statusCard.classList.remove("processing");
}

async function runProcess(paths, isFolder) {
  if (state.processing) return;
  state.processing = true;
  setStep(1);
  els.packageTile?.classList.add("tile-has-file");
  els.statusCard.textContent = `Processing ${paths.length} item(s)…`;
  els.statusCard.classList.add("processing");
  els.resultBanner.classList.add("hidden");
  els.previewGrid.classList.add("hidden");
  [els.resetBtn, els.revealBtn, els.approveBtn].forEach((b) => b && (b.disabled = true));

  try {
    const result = await window.pywebview.api.process_package(paths, isFolder, state.studyOverride || null);
    state.lastResult = result;
    renderResult(result);
  } catch (err) {
    renderResult({ success: false, errors: [`Unexpected error: ${err}`] });
  } finally {
    state.processing = false;
  }
}

function renderResult(result) {
  setStep(2);
  els.statusCard.classList.remove("processing");

  const studyLabel = result.study === "egfr" ? "eGFR" : result.study === "kfre" ? "KFRE" : "—";
  if (els.studyDetectedNote) {
    els.studyDetectedNote.textContent = result.study
      ? `Detected study type for this package: ${studyLabel}${state.studyOverride ? " (manual override)" : " (auto-detected)"}.`
      : " ";
  }

  els.resultBanner.classList.remove("hidden");
  if (result.success) {
    els.statusCard.textContent = `${result.preview_pairs?.length || 0} file(s) anonymized.`;
    els.statusCard.classList.add("has-file");
    els.resultBanner.className = "result-banner success";
    els.resultBanner.innerHTML = `
      <strong>Anonymization complete (${studyLabel})</strong>
      Anonymized ID: <span class="anon-id-pill">${escapeHTML(result.anon_id)}</span>
      ${result.is_new_id ? " — new patient" : " — existing patient, ID reused (follow-up visit)"}
    `;
    els.revealBtn.disabled = false;
    els.approveBtn.disabled = false;
  } else {
    els.statusCard.textContent = "Could not anonymize this package.";
    els.resultBanner.className = "result-banner error";
    const errs = (result.errors || ["Unknown error."]).map((e) => `<li>${escapeHTML(e)}</li>`).join("");
    els.resultBanner.innerHTML = `<strong>Anonymization failed</strong><ul>${errs}</ul>`;
    els.revealBtn.disabled = true;
    els.approveBtn.disabled = true;
  }
  els.resetBtn.disabled = false;

  renderPreview(result.preview_pairs || [], result.failed_pdfs || []);
}

function renderPreview(pairs, failedPdfs) {
  if (!pairs.length && !failedPdfs.length) {
    els.previewGrid.classList.add("hidden");
    return;
  }
  els.previewGrid.classList.remove("hidden");
  els.originalList.innerHTML = "";
  els.outputList.innerHTML = "";

  pairs.forEach((pair) => {
    els.originalList.insertAdjacentHTML("beforeend", `
      <div class="anon-file-row">
        <span class="anon-check" aria-hidden="true">&#10003;</span>
        <div>
          <strong>${escapeHTML(basename(pair.original))}</strong>
          <small>${escapeHTML(pair.category || "")}</small>
        </div>
      </div>`);
    els.outputList.insertAdjacentHTML("beforeend", `
      <div class="anon-file-row anonymized">
        <span class="anon-check" aria-hidden="true">&#10003;</span>
        <div>
          <strong>${escapeHTML(basename(pair.output))}</strong>
          <small>${escapeHTML(pair.category || "")}</small>
        </div>
      </div>`);
  });

  failedPdfs.forEach((f) => {
    els.originalList.insertAdjacentHTML("beforeend", `
      <div class="anon-file-row failed">
        <span class="anon-check fail" aria-hidden="true">!</span>
        <div><strong>${escapeHTML(basename(f.path))}</strong><small>${escapeHTML(f.reason)}</small></div>
      </div>`);
    els.outputList.insertAdjacentHTML("beforeend", `
      <div class="anon-file-row failed">
        <span class="anon-check fail" aria-hidden="true">!</span>
        <div><strong>Not written</strong><small>Redaction failed -- inspect the source file manually.</small></div>
      </div>`);
  });
}

// ---------------------------------------------------------------------
// Actions
// ---------------------------------------------------------------------

els.revealBtn?.addEventListener("click", () => {
  if (state.lastResult?.output_dir) window.pywebview.api.reveal_in_explorer(state.lastResult.output_dir);
});

els.approveBtn?.addEventListener("click", () => {
  // Approving locks this review in (visual confirmation only) -- it does
  // NOT delete the original source files. Deletion is a separate,
  // explicit, confirmed action so a slip of this button can never cause
  // data loss; see the note below and main.py's delete_original_files().
  setStep(3);
  els.approveBtn.textContent = "Reviewed ✓";
  els.approveBtn.disabled = true;

  const originals = (state.lastResult?.preview_pairs || []).map((p) => p.original);
  if (!originals.length) return;
  const note = document.createElement("div");
  note.className = "result-banner";
  note.style.marginTop = "0.6rem";
  note.innerHTML = `
    <strong>Reviewed.</strong> The anonymized copy is saved. Original source file(s) are untouched --
    <button type="button" class="btn-secondary" id="delete-originals-btn" style="margin-left:0.4rem;">
      Permanently delete ${originals.length} original file(s)&hellip;
    </button>`;
  els.resultBanner.insertAdjacentElement("afterend", note);
  document.getElementById("delete-originals-btn")?.addEventListener("click", async () => {
    const ok = window.confirm(
      `Permanently delete ${originals.length} ORIGINAL source file(s) from this computer?\n\n` +
      "This cannot be undone. The anonymized copy already saved will NOT be affected."
    );
    if (!ok) return;
    const res = await window.pywebview.api.delete_original_files(originals);
    note.innerHTML = res.success
      ? `<strong>Done.</strong> ${res.deleted.length} original file(s) permanently deleted.`
      : `<strong>Some deletions failed:</strong><ul>${(res.errors || []).map((e) => `<li>${escapeHTML(e)}</li>`).join("")}</ul>`;
  });
});

els.resetBtn?.addEventListener("click", () => {
  state.lastResult = null;
  els.statusCard.textContent = "No package selected";
  els.statusCard.classList.remove("has-file", "processing");
  els.packageTile?.classList.remove("tile-has-file");
  els.resultBanner.classList.add("hidden");
  els.previewGrid.classList.add("hidden");
  els.previewGrid.parentElement.querySelectorAll(".result-banner:not(#result-banner)").forEach((n) => n.remove());
  [els.resetBtn, els.revealBtn, els.approveBtn].forEach((b) => b && (b.disabled = true));
  els.approveBtn.textContent = "Approve";
  if (els.studyDetectedNote) els.studyDetectedNote.textContent = " ";
  setStep(1);
});

loadAppInfo();

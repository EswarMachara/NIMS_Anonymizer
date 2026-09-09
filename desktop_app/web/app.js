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
  progressTrack: document.getElementById("progress-track"),
  landingStage: document.getElementById("landing-stage"),
  scrollCue: document.getElementById("scroll-cue"),
  csvState: document.getElementById("csv-state"),
  csvHint: document.getElementById("csv-hint"),
  csvPath: document.getElementById("csv-path"),
  csvBrowseBtn: document.getElementById("csv-browse-btn"),
  csvClearBtn: document.getElementById("csv-clear-btn"),
  csvBox: document.getElementById("csv-box"),
  outState: document.getElementById("out-state"),
  outPath: document.getElementById("out-path"),
  outBrowseBtn: document.getElementById("out-browse-btn"),
  outClearBtn: document.getElementById("out-clear-btn"),
  ccProgress: document.getElementById("cc-progress"),
  ccPrev: document.getElementById("cc-prev"),
  ccNext: document.getElementById("cc-next"),
  studyButtons: {
    "": document.getElementById("study-auto"),
    egfr: document.getElementById("study-egfr"),
    kfre: document.getElementById("study-kfre"),
  },
  studyDetectedNote: document.getElementById("study-detected-note"),
  crosscheckBtn: document.getElementById("crosscheck-btn"),
  batchResults: document.getElementById("batch-results"),
  batchSummary: document.getElementById("batch-summary"),
  batchTbody: document.getElementById("batch-tbody"),
  workbench: document.querySelector(".anon-workbench"),
  crosscheckPage: document.getElementById("crosscheck-page"),
  crosscheckBack: document.getElementById("crosscheck-back"),
  crosscheckSubject: document.getElementById("crosscheck-subject"),
  crosscheckHint: document.getElementById("crosscheck-hint"),
  crosscheckBody: document.getElementById("crosscheck-body"),
  crosscheckTabs: Array.from(document.querySelectorAll("[data-cc-tab]")),
  hospitalChip: document.getElementById("hospital-chip"),
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
  mappingCsv: null, // operator-chosen ID-mapping CSV; null = app-data default
  outputDir: null,  // operator-chosen output destination; null = app-data default
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
  btn?.addEventListener("click", () => {
    setStudyOverride(key);
    // Each study has its own default mapping CSV and output subfolder, so
    // the paths on screen must follow the choice.
    refreshSession();
  });
});

// ---------------------------------------------------------------------
// Landing stage
// ---------------------------------------------------------------------

// The three steps fade in one after another on open (delays live in the
// markup as --reveal-delay). Nothing is pre-highlighted: on arrival the
// operator has not done anything yet, so marking step 1 as "active" was
// claiming progress that had not happened.
requestAnimationFrame(() => document.body.classList.add("ready"));

els.scrollCue?.addEventListener("click", () => {
  document.getElementById("workbench")?.scrollIntoView({ behavior: "smooth", block: "start" });
});

// ---------------------------------------------------------------------
// Session inputs: ID-mapping CSV and output destination
// ---------------------------------------------------------------------

function renderSession(info) {
  if (!info) return;
  const csvChosen = !info.mapping_csv_is_default;
  els.csvPath.textContent = info.mapping_csv || "";
  els.outPath.textContent = info.output_dir || "";
  els.csvClearBtn.classList.toggle("hidden", !csvChosen);
  els.outClearBtn.classList.toggle("hidden", info.output_is_default);
  els.outState.textContent = info.output_is_default ? "Using default" : "Custom";
  els.outState.className = `session-state ${info.output_is_default ? "" : "is-custom"}`;

  // The distinction that actually matters: is this a continuing ID space
  // (returning patients keep their existing ID, and new IDs are checked
  // against the ones already issued) or a fresh one?
  if (info.error) {
    els.csvState.textContent = "Unreadable";
    els.csvState.className = "session-state is-bad";
    els.csvHint.textContent = `That file could not be read: ${info.error}`;
    els.csvBox.classList.add("is-bad");
    return;
  }
  els.csvBox.classList.remove("is-bad");
  if (info.continuing) {
    els.csvState.textContent = "Continuing";
    els.csvState.className = "session-state is-good";
    // The file count is the repeat check: a known patient could be a
    // follow-up OR the same documents again, and only that record tells
    // them apart. Say so when it is missing too -- a mapping CSV carried
    // over WITHOUT its companion file looks completely normal otherwise,
    // and the first sign of trouble would be already-done patients being
    // quietly redone.
    const known = info.processed_files
      ? ` ${info.processed_files} file(s) already anonymized are on record, so the same documents handed in again are recognised instead of redone.`
      : " No record of previously anonymized files sits next to this CSV, so re-submitted documents cannot be recognised. Copy the matching _processed_files.json file alongside it if you have one.";
    els.csvHint.textContent =
      `${info.existing_patients} patient(s) already in this mapping. A returning patient keeps their existing ` +
      `Anonymized ID, and any new ID is checked against all ${info.existing_ids} already issued.` + known;
  } else {
    els.csvState.textContent = csvChosen ? "Empty file" : "New mapping";
    els.csvState.className = `session-state ${csvChosen ? "is-warn" : ""}`;
    if (csvChosen) {
      els.csvHint.textContent =
        "This file has no patients in it yet, so nothing can be matched as a follow-up. If you meant to continue an earlier session, pick that session's CSV instead.";
    } else if (info.mapping_csv_beside_output) {
      // Placed next to the anonymized folder rather than inside it: that
      // folder stays safe to hand over, but the one above it now holds
      // real names and CR numbers, so it must not be shared wholesale.
      els.csvHint.textContent =
        "Will be created next to your output folder, not inside it. Keep this file: next time, load it so returning " +
        "patients keep the same ID. Share only the Anonymized_ subfolder, never the folder holding this CSV.";
    } else {
      els.csvHint.textContent =
        "No previous mapping loaded, so every patient here counts as new. Continuing an earlier session? Load its CSV first.";
    }
  }
}

function syncHintTitles() {
  // The hint is line-clamped on short windows, so mirror it into the title
  // where the full wording stays reachable on hover.
  if (els.csvHint) els.csvHint.title = els.csvHint.textContent || "";
}

async function refreshSession() {
  try {
    const info = await window.pywebview.api.describe_session(
      state.mappingCsv, state.outputDir, state.studyOverride || "egfr");
    if (info && info.success !== false) { renderSession(info); syncHintTitles(); }
  } catch (err) {
    console.error("describe_session failed", err);
  }
}

els.csvBrowseBtn?.addEventListener("click", async () => {
  const picked = await window.pywebview.api.pick_mapping_csv();
  if (picked?.picked) {
    state.mappingCsv = picked.path;
    refreshSession();
  }
});
els.csvClearBtn?.addEventListener("click", () => {
  state.mappingCsv = null;
  refreshSession();
});
els.outBrowseBtn?.addEventListener("click", async () => {
  const picked = await window.pywebview.api.pick_output_folder();
  if (picked?.picked) {
    state.outputDir = picked.path;
    refreshSession();
  }
});
els.outClearBtn?.addEventListener("click", () => {
  state.outputDir = null;
  refreshSession();
});

// ---------------------------------------------------------------------
// Expected-layout explainer (eGFR / KFRE)
// ---------------------------------------------------------------------
// The two studies arrive in genuinely different shapes: eGFR as one folder
// per patient holding kidney subfolders plus loose report PDFs, KFRE as a
// single flat folder of every patient's PDFs. Showing one generic tree for
// both misdescribed both, so each is drawn as it really is.

document.querySelectorAll("[data-layout]").forEach((btn) => {
  btn.addEventListener("click", () => {
    const wanted = btn.dataset.layout;
    document.querySelectorAll("[data-layout]").forEach((b) => {
      const active = b.dataset.layout === wanted;
      b.classList.toggle("active", active);
      b.setAttribute("aria-selected", String(active));
    });
    document.querySelectorAll("[data-layout-view]").forEach((view) => {
      view.classList.toggle("hidden", view.dataset.layoutView !== wanted);
    });
  });
});

// ---------------------------------------------------------------------
// App info (mapping-csv / output-folder transparency, hospital branding)
// ---------------------------------------------------------------------

async function loadAppInfo() {
  try {
    const info = await window.pywebview.api.get_app_info();
    if (els.hospitalChip) els.hospitalChip.textContent = info.hospital_name;
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
  els.statusCard.textContent = isFolder
    ? "Reading the selection and anonymizing. A folder of many patients can take a while."
    : `Anonymizing ${paths.length} file(s).`;
  els.statusCard.classList.add("processing");
  els.progressTrack?.classList.remove("hidden");
  els.resultBanner.classList.add("hidden");
  els.previewGrid.classList.add("hidden");
  [els.resetBtn, els.revealBtn, els.approveBtn].forEach((b) => b && (b.disabled = true));

  try {
    const result = await window.pywebview.api.process_package(
      paths, isFolder, state.studyOverride || null, state.mappingCsv, state.outputDir);
    state.lastResult = result;
    renderResult(result);
  } catch (err) {
    renderResult({ success: false, errors: [`Unexpected error: ${err}`] });
  } finally {
    state.processing = false;
    els.progressTrack?.classList.add("hidden");
  }
}

function renderResult(result) {
  setStep(2);
  els.statusCard.classList.remove("processing");

  const studyLabel = result.study === "egfr" ? "eGFR" : result.study === "kfre" ? "KFRE" : "not detected";
  if (els.studyDetectedNote) {
    els.studyDetectedNote.textContent = result.study
      ? `Detected study type for this package: ${studyLabel}${state.studyOverride ? " (manual override)" : " (auto-detected)"}.`
      : " ";
  }

  if (result.batch) {
    renderBatchResult(result, studyLabel);
    return;
  }

  els.resultBanner.classList.remove("hidden");
  if (result.success) {
    const repeat = result.repeat || {};
    const isDuplicate = repeat.verdict === "duplicate";
    els.statusCard.textContent = isDuplicate
      ? `${result.preview_pairs?.length || 0} file(s) already anonymized earlier.`
      : `${result.preview_pairs?.length || 0} file(s) anonymized.`;
    els.statusCard.classList.add("has-file");
    els.resultBanner.className = `result-banner ${isDuplicate ? "warn" : "success"}`;
    // A reused ID means a known patient, which is not the same as a new
    // visit. Say which one this actually is rather than assuming follow-up.
    const idNote = result.is_new_id
      ? " (new patient)"
      : " (already-known patient, existing ID reused)";
    const notes = (result.warnings || []).map((w) => `<li>${escapeHTML(w)}</li>`).join("");
    els.resultBanner.innerHTML = `
      <strong>${isDuplicate ? "Nothing to do (" + studyLabel + ")" : "Anonymization complete (" + studyLabel + ")"}</strong>
      Anonymized ID: <span class="anon-id-pill">${escapeHTML(result.anon_id)}</span>${idNote}
      ${repeat.message ? `<br>${escapeHTML(repeat.message)}` : ""}
      ${notes ? `<ul>${notes}</ul>` : ""}
    `;
    els.revealBtn.disabled = false;
    els.approveBtn.disabled = false;
    els.crosscheckBtn.disabled = !(result.preview_pairs || []).length;
  } else {
    els.statusCard.textContent = "Could not anonymize this package.";
    els.resultBanner.className = "result-banner error";
    const errs = (result.errors || ["Unknown error."]).map((e) => `<li>${escapeHTML(e)}</li>`).join("");
    els.resultBanner.innerHTML = `<strong>Anonymization failed</strong><ul>${errs}</ul>`;
    els.revealBtn.disabled = true;
    els.approveBtn.disabled = true;
    els.crosscheckBtn.disabled = true;
  }
  els.batchResults.classList.add("hidden");
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
// Batch mode (a folder of many patients)
// ---------------------------------------------------------------------

function renderBatchResult(result, studyLabel) {
  const s = result.summary || {};
  const patients = result.patients || [];

  els.statusCard.textContent = `${s.succeeded || 0} of ${s.total || 0} patient(s) anonymized.`;
  els.statusCard.classList.add("has-file");

  els.resultBanner.classList.remove("hidden");
  els.resultBanner.className = `result-banner ${result.success ? "success" : s.succeeded ? "warn" : "error"}`;
  const batchNotes = (result.errors || []).map((e) => `<li>${escapeHTML(e)}</li>`).join("") +
    (result.patients || [])
      .flatMap((p) => p.warnings || [])
      .map((w) => `<li>${escapeHTML(w)}</li>`)
      .join("");

  // A reused ID alone does not distinguish a follow-up visit from the same
  // documents being handed in twice, so the two are counted separately --
  // otherwise "10 of 10 succeeded" quietly covers "and 7 of those were
  // files you already gave me".
  // Plain text, no <strong>: .result-banner strong is display:block, so a
  // second one here would break this sentence across two lines mid-clause.
  const dup = s.skipped_duplicates || 0;
  const repeats = dup
    ? ` ${dup} of them were the same files as before and were left exactly as they are.`
    : "";
  const partial = s.partial_repeats
    ? ` ${s.partial_repeats} had some files already done and some new.`
    : "";
  els.resultBanner.innerHTML = `
    <strong>Batch anonymization (${studyLabel}): ${escapeHTML(String(s.succeeded || 0))} of
      ${escapeHTML(String(s.total || 0))} patient(s) succeeded</strong>
    ${s.new_ids || 0} new ID(s), ${s.reused_ids || 0} reused (already-known patients).${repeats}${partial}
    ${batchNotes ? `<ul>${batchNotes}</ul>` : ""}
  `;

  els.batchSummary.textContent =
    `Each patient folder was resolved independently and given its own Anonymized ID, ` +
    `so no two patients can share one ID. Files already anonymized in an earlier session are ` +
    `recognised by their content and left untouched. Review any patient with "Cross-check".`;

  els.batchTbody.innerHTML = "";
  patients.forEach((p, index) => {
    const fileCount = (p.preview_pairs || []).length;
    const repeat = p.repeat || {};
    // `&& p.success` is belt and braces: the engine only ever returns a
    // skipped duplicate as a success, and a row that failed must never be
    // labelled "Already done" whatever else it carries.
    const isDuplicate = repeat.verdict === "duplicate" && p.success;
    // "Already done" is a success, but it must never read as "Anonymized"
    // just now -- that is the whole confusion this check exists to remove.
    const statusLabel = isDuplicate
      ? "Already done"
      : p.success
        ? (repeat.verdict === "partial" ? "Anonymized (+repeats)" : "Anonymized")
        : (p.anon_id ? "Partly written" : "Not processed");
    const statusClass = isDuplicate ? "dup" : p.success ? "ok" : (p.anon_id ? "warn" : "fail");
    const canReview = fileCount > 0;
    const idNote = p.anon_id ? (p.is_new_id ? "new" : "already known") : "";

    const tr = document.createElement("tr");
    tr.className = p.success ? (isDuplicate ? "batch-row-duplicate" : "") : "batch-row-problem";
    tr.innerHTML = `
      <td><strong>${escapeHTML(p.source || "(unnamed)")}</strong></td>
      <td>${p.anon_id ? `<span class="anon-id-pill">${escapeHTML(p.anon_id)}</span>
            <small>${escapeHTML(idNote)}</small>` : "<small>none assigned</small>"}</td>
      <td>${fileCount}${isDuplicate
            ? "<small>unchanged</small>"
            : (p.dcm_count != null ? `<small>${escapeHTML(String(p.dcm_count))} image(s)</small>` : "")}</td>
      <td><span class="batch-status ${statusClass}">${escapeHTML(statusLabel)}</span></td>
      <td class="batch-row-actions"></td>
    `;

    const actions = tr.querySelector(".batch-row-actions");
    const reviewBtn = document.createElement("button");
    reviewBtn.type = "button";
    reviewBtn.className = "package-upload-action";
    reviewBtn.textContent = "Cross-check";
    reviewBtn.disabled = !canReview;
    reviewBtn.addEventListener("click", () => openCrosscheck(p, index));
    actions.appendChild(reviewBtn);

    els.batchTbody.appendChild(tr);

    // One extra row carries whatever this patient still needs explained:
    // why nothing was rewritten, which files were repeats, or an ID clash.
    const notes = [];
    if (repeat.message) notes.push(escapeHTML(repeat.message));
    (p.warnings || []).forEach((w) => notes.push(escapeHTML(w)));
    if (!p.success) (p.errors || []).forEach((e) => notes.push(escapeHTML(e)));
    if (notes.length) {
      const noteTr = document.createElement("tr");
      noteTr.className = p.success ? "batch-row-note" : "batch-row-errors";
      const td = document.createElement("td");
      td.colSpan = 5;
      td.innerHTML = `<ul>${notes.map((n) => `<li>${n}</li>`).join("")}</ul>`;
      noteTr.appendChild(td);
      els.batchTbody.appendChild(noteTr);
    }
  });

  els.batchResults.classList.remove("hidden");
  els.previewGrid.classList.add("hidden");
  els.resetBtn.disabled = false;
  els.revealBtn.disabled = false;
  // Cross-checking is optional, so Approve is enabled as soon as anything
  // was written. In practice an operator verifies the first few packages
  // closely and then trusts the pipeline; blocking Approve behind a review
  // they have chosen to skip just leaves them with no way to finish.
  els.approveBtn.disabled = (s.succeeded || 0) === 0;
  // Per-patient review is reached from each row's own Cross-check button,
  // so the toolbar one stays off in batch: it has no single subject.
  els.crosscheckBtn.disabled = true;
}

// ---------------------------------------------------------------------
// Cross-check review (original vs anonymized)
// ---------------------------------------------------------------------

// Rows are paged rather than all mounted at once. Ten frames is already a
// long scroll; a future patient with fifty would make the tab unusable and
// hold fifty decoded previews in memory. PAGE_SIZE bounds both.
const CC_PAGE_SIZE = 4;
let ccState = { tab: "images", manifest: null, subject: "", observer: null, page: 0, items: [] };

function openWorkbench() {
  ccState.observer?.disconnect();
  ccState.observer = null;
  els.crosscheckPage.classList.add("hidden");
  els.workbench.classList.remove("hidden");
  window.scrollTo(0, 0);
}

async function openCrosscheck(patientResult, index) {
  const pairs = patientResult.preview_pairs || [];
  const res = await window.pywebview.api.get_crosscheck_manifest(pairs);
  if (!res || res.success === false) {
    setStatus((res && res.errors && res.errors[0]) || "Could not open cross-check.", false);
    return;
  }

  ccState.manifest = res;
  ccState.subject = patientResult.source
    ? `${patientResult.source} · ${patientResult.anon_id || "no ID"}`
    : (patientResult.anon_id || "");

  els.crosscheckSubject.textContent = ccState.subject;
  els.crosscheckTabs.forEach((btn) => {
    const key = btn.dataset.ccTab;
    const n = (res.counts && res.counts[key]) || 0;
    btn.textContent = `${{ images: "DCM Images", metadata: "DCM Metadata", reports: "Reports" }[key]} (${n})`;
    btn.disabled = n === 0;
  });

  const firstAvailable = ["images", "metadata", "reports"].find((k) => (res.counts || {})[k] > 0) || "images";
  els.workbench.classList.add("hidden");
  els.crosscheckPage.classList.remove("hidden");
  window.scrollTo(0, 0);
  selectCrosscheckTab(firstAvailable);
}

function selectCrosscheckTab(tab) {
  ccState.tab = tab;
  els.crosscheckTabs.forEach((btn) => btn.classList.toggle("active", btn.dataset.ccTab === tab));

  ccState.items = (ccState.manifest && ccState.manifest[tab]) || [];
  ccState.page = 0;
  els.crosscheckHint.textContent = {
    images: "Scroll through every ultrasound frame. Check that the burned-in banner across the top of each image is blanked in the anonymized copy. Clearing tags does not remove text printed into the pixels.",
    metadata: "Tag-by-tag comparison. Changed and removed tags are listed first; unchanged tags are collapsed.",
    reports: "Each report page, original beside anonymized. Check that every patient identifier is blacked out.",
  }[tab] || "";

  renderCrosscheckPage();
}

function renderCrosscheckPage() {
  const items = ccState.items;
  const pages = Math.max(1, Math.ceil(items.length / CC_PAGE_SIZE));
  ccState.page = Math.min(Math.max(0, ccState.page), pages - 1);
  const start = ccState.page * CC_PAGE_SIZE;
  const slice = items.slice(start, start + CC_PAGE_SIZE);

  els.ccProgress.textContent = items.length
    ? `Showing ${start + 1} to ${start + slice.length} of ${items.length}`
    : "Nothing to compare";
  els.ccPrev.disabled = ccState.page === 0;
  els.ccNext.disabled = ccState.page >= pages - 1;

  renderCrosscheckRows(slice, ccState.tab, start);
  els.crosscheckBody.scrollTop = 0;
}

els.ccPrev?.addEventListener("click", () => { ccState.page -= 1; renderCrosscheckPage(); });
els.ccNext?.addEventListener("click", () => { ccState.page += 1; renderCrosscheckPage(); });

function renderCrosscheckRows(items, tab, offset = 0) {
  ccState.observer?.disconnect();
  els.crosscheckBody.innerHTML = "";

  if (!items.length) {
    els.crosscheckBody.innerHTML = `<p class="cc-empty">Nothing to compare in this category for this patient.</p>`;
    return;
  }

  items.forEach((item, i) => {
    const row = document.createElement("article");
    row.className = "cc-row";
    row.dataset.index = String(i);
    row.innerHTML = `
      <h4><span class="cc-row-num">${offset + i + 1} / ${ccState.items.length}</span> ${escapeHTML(item.label)}
        ${item.category ? `<small>${escapeHTML(item.category)}</small>` : ""}</h4>
      <div class="cc-panes" data-cc-content><p class="cc-loading">Loading…</p></div>
    `;
    els.crosscheckBody.appendChild(row);
  });

  // Rows are decoded on demand: a patient's ten frames are tens of
  // megabytes once decoded, so loading them all up front would freeze the
  // window. rootMargin starts the fetch just before a row is scrolled to.
  ccState.observer = new IntersectionObserver((entries, obs) => {
    entries.forEach((entry) => {
      if (!entry.isIntersecting) return;
      obs.unobserve(entry.target);
      const item = items[Number(entry.target.dataset.index)];
      const holder = entry.target.querySelector("[data-cc-content]");
      if (tab === "metadata") loadMetadataRow(item, holder);
      else loadImageRow(item, holder, tab);
    });
  }, { rootMargin: "300px 0px" });

  els.crosscheckBody.querySelectorAll(".cc-row").forEach((r) => ccState.observer.observe(r));
}

function comparePaneHTML(side, label, inner) {
  return `
    <figure class="cc-pane cc-pane-${side}">
      <figcaption>${label}<small>${escapeHTML(inner.label || "")}</small></figcaption>
      ${inner.ok
        ? `<img src="${inner.data_uri}" alt="${escapeHTML(label)} preview of ${escapeHTML(inner.label || "")}" loading="lazy" />`
        : `<p class="cc-error">${escapeHTML(inner.error || "Could not render.")}</p>`}
    </figure>`;
}

async function loadImageRow(item, holder, tab) {
  const render = tab === "reports"
    ? (p) => window.pywebview.api.render_pdf_preview(p, 0)
    : (p) => window.pywebview.api.render_dicom_preview(p);
  try {
    const [orig, anon] = await Promise.all([render(item.original), render(item.output)]);
    holder.innerHTML = comparePaneHTML("original", "Original", orig) +
                       comparePaneHTML("anon", "Anonymized", anon);
    if (tab === "reports" && orig.page_count > 1) {
      const note = document.createElement("p");
      note.className = "cc-note";
      note.textContent = `Page 1 of ${orig.page_count} shown.`;
      holder.appendChild(note);
    }
  } catch (err) {
    holder.innerHTML = `<p class="cc-error">Could not load this comparison: ${escapeHTML(String(err))}</p>`;
  }
}

async function loadMetadataRow(item, holder) {
  try {
    const md = await window.pywebview.api.get_dicom_metadata_pair(item.original, item.output);
    if (!md.ok) {
      holder.innerHTML = `<p class="cc-error">${escapeHTML(md.error || "Could not read metadata.")}</p>`;
      return;
    }
    const interesting = md.rows.filter((r) => r.status !== "unchanged");
    const unchanged = md.rows.filter((r) => r.status === "unchanged");

    const rowHTML = (r) => `
      <tr class="md-${r.status}">
        <td><code>${escapeHTML(r.tag)}</code><small>${escapeHTML(r.keyword || "")}</small></td>
        <td>${r.original === null ? "<em>absent</em>" : escapeHTML(r.original) || "<em>empty</em>"}</td>
        <td>${r.anonymized === null ? "<em>removed</em>" : escapeHTML(r.anonymized) || "<em>cleared</em>"}</td>
        <td><span class="md-badge md-badge-${r.status}">${escapeHTML(r.status)}</span></td>
      </tr>`;

    holder.innerHTML = `
      <div class="cc-md">
        <p class="cc-md-counts">
          <strong>${md.counts.changed}</strong> changed ·
          <strong>${md.counts.removed}</strong> removed ·
          <strong>${md.counts.unchanged}</strong> unchanged
        </p>
        <table class="md-table">
          <thead><tr><th>Tag</th><th>Original</th><th>Anonymized</th><th>Status</th></tr></thead>
          <tbody>${interesting.map(rowHTML).join("")}</tbody>
        </table>
        ${unchanged.length ? `
          <details class="cc-md-unchanged">
            <summary>Show ${unchanged.length} unchanged tag(s)</summary>
            <table class="md-table"><tbody>${unchanged.map(rowHTML).join("")}</tbody></table>
          </details>` : ""}
      </div>`;
  } catch (err) {
    holder.innerHTML = `<p class="cc-error">Could not load metadata: ${escapeHTML(String(err))}</p>`;
  }
}

els.crosscheckTabs.forEach((btn) => {
  btn.addEventListener("click", () => {
    if (!btn.disabled) selectCrosscheckTab(btn.dataset.ccTab);
  });
});
els.crosscheckBack?.addEventListener("click", openWorkbench);
els.crosscheckBtn?.addEventListener("click", () => {
  if (state.lastResult && !state.lastResult.batch) openCrosscheck(state.lastResult, 0);
});

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
  ccState = { tab: "images", manifest: null, subject: "", observer: null };
  els.batchResults.classList.add("hidden");
  els.batchTbody.innerHTML = "";
  els.crosscheckBtn.disabled = true;
  openWorkbench();
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

// pywebview injects window.pywebview.api asynchronously and fires
// `pywebviewready` when it is callable. Calling straight away raced that
// and left the session paths showing their "…" placeholder, so the
// operator could not see which mapping CSV was actually in use. If the
// event already fired before this script ran, the API is present and we
// go immediately.
function startup() {
  loadAppInfo();
  refreshSession();
}

if (window.pywebview && window.pywebview.api) {
  startup();
} else {
  window.addEventListener("pywebviewready", startup, { once: true });
}

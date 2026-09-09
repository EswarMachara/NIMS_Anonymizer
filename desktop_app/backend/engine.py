"""
engine.py -- desktop-app backend adapter.

This module is a thin orchestration layer around the project's existing,
already-validated engines:
    anon_common.py       (shared PII redaction / mapping-CSV toolkit)
    anonymize_eGFR.py    (eGFR study: DICOM + PDF de-identification)
    anonymize_KFRE.py    (KFRE study: PDF-only de-identification)

It deliberately duplicates NO redaction/extraction/ID logic -- every call
here delegates straight into those modules (imported from the project
root, added to sys.path below). The one thing genuinely new here is
adapting their BATCH ("--input-dir holds many numbered patient
subfolders") design to a single interactively-selected package (one
patient's folder, or a flat multi-file selection), which is what the
desktop app's "Select package" step produces.

Nothing in this module talks to pywebview directly -- see ../main.py for
the JS-Python bridge that calls into these functions.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

if getattr(sys, "frozen", False):
    # Running as a PyInstaller-built .exe: anon_common.py / anonymize_eGFR.py
    # / anonymize_KFRE.py are bundled as plain data files directly into the
    # frozen app (see build/app.spec's `datas` list) rather than living at
    # a real "../.." path relative to this file -- sys._MEIPASS is
    # PyInstaller's own extraction directory at runtime.
    PROJECT_ROOT = sys._MEIPASS  # type: ignore[attr-defined]
else:
    PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import anon_common as ac  # noqa: E402
import anonymize_eGFR as eg  # noqa: E402
import anonymize_KFRE as kf  # noqa: E402

# --------------------------------------------------------------------------
# Hospital personalization -- per the data-collection portal's own promise
# ("Personalized Software: Pre-configured specifically for your hospital's
# workflow and IDs"), this build is baked for one specific partner hospital.
# A future multi-hospital build would just change these two constants (and
# the logo file) per hospital, not the rest of the app.
# --------------------------------------------------------------------------
HOSPITAL_CODE = "NIMS"
HOSPITAL_NAME = "Nizam's Institute of Medical Sciences (NIMS), Hyderabad"

APP_VENDOR_DIR_NAME = "TANUH-Renal-Anonymizer"

KIDNEY_SUBFOLDER_RE = re.compile(r"kidney", re.IGNORECASE)


# ==========================================================================
# App-data locations (mapping CSVs + accumulated anonymized output)
# ==========================================================================


def get_app_data_dir() -> str:
    """
    Resolve a per-user, per-machine app-data directory that survives an app
    reinstall/update -- NEVER inside the installed application's own folder
    (which can be read-only under Program Files, and is wiped on
    reinstall). Windows: %LOCALAPPDATA%\\TANUH-Renal-Anonymizer. Graceful
    fallback (macOS/Linux, or a stripped-down Windows environment missing
    the env var) to a dotfolder under the user's home directory, so the
    app still runs correctly during development/testing on this machine.
    """
    base = os.environ.get("LOCALAPPDATA")
    if not base:
        base = os.path.join(os.path.expanduser("~"), ".local", "share")
    path = os.path.join(base, APP_VENDOR_DIR_NAME)
    os.makedirs(path, exist_ok=True)
    return path


def get_mapping_csv_path(study: str) -> str:
    """study is 'egfr' or 'kfre' -- mirrors the CLI scripts' own default
    naming (eGFR_anony_Mapping.csv / KFRE_anony_Mapping.csv), just rooted
    in the app-data directory instead of the script's own folder."""
    name = "eGFR_anony_Mapping.csv" if study == "egfr" else "KFRE_anony_Mapping.csv"
    return os.path.join(get_app_data_dir(), name)


def get_output_root(study: str) -> str:
    """
    Persistent, accumulating output location -- unlike the batch CLI (one
    ranged-numbered folder per whole-corpus run), the desktop app processes
    one patient at a time, so there is no natural "session range" to name
    a folder after. Every processed patient just gets its own <AnonID>/
    subfolder added under here, same as the CLI's own per-patient
    subfolder convention (see write_patient_outputs() / _run_redaction_
    and_save()) -- nothing here ever needs pruning or renaming.
    """
    name = "Anonymized_eGFR" if study == "egfr" else "Anonymized_KFRE"
    path = os.path.join(get_app_data_dir(), name)
    os.makedirs(path, exist_ok=True)
    return path


# ==========================================================================
# Study-type auto-detection + "Choose Files" staging
# ==========================================================================


def detect_study_type(package_dir: str) -> str:
    """
    Auto-detect 'egfr' vs 'kfre' for one selected package. eGFR patients
    always carry DICOM kidney-ultrasound files -- confirmed universal
    across both the CKD and Normal cohorts in this project's own sample
    corpus (every patient has Left/Right-kidney *.DCM files regardless of
    which clinical-report template their PDFs use); KFRE is a PDF-only,
    lab-report-only study with no imaging at all. Presence of ANY *.dcm
    file anywhere under package_dir is therefore a reliable signal.
    """
    for _dirpath, _dirnames, filenames in os.walk(package_dir):
        if any(name.lower().endswith(".dcm") for name in filenames):
            return "egfr"
    return "kfre"


def stage_selected_files(file_paths: List[str]) -> str:
    """
    "Choose Files" mode: the user multi-selected individual files rather
    than one folder (native multi-file dialogs typically only let you
    select from one directory at a time, so a real patient package spread
    across Left_Kidney_Images/ + Right_Kidney_Images/ + reports may need
    several such selections combined here). Builds a temporary staging
    directory that reconstructs enough structure for resolve_patient_
    identity() / classify_input_file() to work correctly:

      - a file whose ORIGINAL parent folder name matches the kidney-
        subfolder pattern (e.g. "Left_Kidney_Images", "Left kidney") is
        placed under that SAME-named subfolder in staging, preserving the
        eGFR left/right-kidney grouping find_kidney_subfolders() expects;
      - every other file (PDF reports, from any subfolder, e.g.
        "Ultrasound_Reports"/"Lab_Reports"/flat) is copied FLAT into the
        staging root, since PDF classification is content-based and does
        not depend on which subfolder it came from.

    Returns the staging directory path -- caller is responsible for
    cleaning it up (see process_package()'s finally block) once
    processing is complete; the ORIGINAL source files are never touched
    or moved, only copied.
    """
    staging = tempfile.mkdtemp(prefix="anon_staging_")
    for src in file_paths:
        parent_name = os.path.basename(os.path.dirname(src))
        if KIDNEY_SUBFOLDER_RE.search(parent_name):
            dest_dir = os.path.join(staging, parent_name)
        else:
            dest_dir = staging
        os.makedirs(dest_dir, exist_ok=True)
        shutil.copy2(src, os.path.join(dest_dir, os.path.basename(src)))
    return staging


# ==========================================================================
# Selection-mistake reporting
# ==========================================================================


def _immediate_subfolders(package_dir: str) -> List[str]:
    """Names of the selected folder's immediate subdirectories. Used only to
    tell the operator what they could have picked instead, once we already
    KNOW the selection held more than one patient."""
    try:
        return sorted(
            name for name in os.listdir(package_dir)
            if os.path.isdir(os.path.join(package_dir, name))
        )
    except OSError:
        return []


def _only_written_pairs(pairs: List[Dict[str, str]]) -> List[Dict[str, str]]:
    """
    Drop preview rows whose anonymized output was not actually written.

    Both studies' preview builders RECOMPUTE each expected output path from
    the same pure naming functions the writers use, rather than observing
    what landed on disk -- which is what keeps them correct when some files
    fail. The cost is that a failed file still yields a plausible-looking
    row: before the DICOM decoder plugins were added to requirements.txt,
    a package could report all 10 ultrasound images as anonymized output
    while zero were on disk. The operator is being asked to eyeball this
    list and then Approve, so a row here must mean a real file -- the
    failures are reported through `errors` / `failed_pdfs` instead.
    Called after the writers have run, so existence is settled by now.
    """
    return [p for p in pairs if p.get("output") and os.path.exists(p["output"])]


def _distinct_identity_values(rec: "eg.PatientRecord") -> tuple:
    """(kind, {values}) re-derived from the PDF fields resolve_patient_
    identity() already parsed -- mirrors its own derivation (see
    anonymize_eGFR.py's identity-key block) rather than string-matching its
    error prose, so this stays correct if that wording ever changes."""
    cr_nos = {f.cr_no for _, f in rec.a_results if f.cr_no}
    if cr_nos:
        return ("CR No", cr_nos)
    lab_nos = {f.lab_no for _, f in rec.b_results if f.lab_no}
    if lab_nos:
        return ("Lab No.", lab_nos)
    return (None, set())


def _multi_patient_errors(package_dir: str, kind: str, values: set) -> List[str]:
    """
    Operator-facing explanation of the single most likely selection mistake:
    pointing the app at a folder holding MANY patients (the batch CLI's
    corpus layout, e.g. eGFR/NP1 (CKD)/, NP2 (CKD)/, ...) instead of one
    patient's package.

    The engines' own message for this ("CONFLICTING CR No values across
    report PDFs: {...} -- refusing to guess") is written for someone running
    the batch CLI and reading a terminal; it tells a clinical user nothing
    about what to do differently. This replaces it, and demotes the raw
    values to a trailing detail line instead of leading with them.
    """
    # Deliberately NOT phrased as "holds N patients": len(values) counts the
    # distinct keys of ONE template family, so a mixed corpus (e.g. CKD
    # patients keyed by CR No alongside Normal-cohort ones keyed by Lab No.)
    # would understate the real patient count. "More than one" is the part
    # that is always true and is all the operator needs to act.
    errors = [
        "This folder contains reports from more than one patient "
        f"({len(values)} distinct {kind} values were found in it), so it was not "
        "processed. This app anonymizes ONE patient's package at a time -- nothing "
        "was written, and no patient data was mixed together."
    ]
    subfolders = _immediate_subfolders(package_dir)
    if subfolders:
        shown = ", ".join(subfolders[:6]) + (", ..." if len(subfolders) > 6 else "")
        errors.append(
            "It looks like each patient has their own subfolder here -- select ONE of "
            f'those instead of the folder above (for example "{subfolders[0]}"). '
            f"Subfolders found: {shown}"
        )
    errors.append(f"Distinct {kind} values seen: {', '.join(sorted(values))}")
    return errors


# ==========================================================================
# eGFR processing
# ==========================================================================


def _build_egfr_preview_pairs(rec: "eg.PatientRecord", out_patient_dir: str) -> List[Dict[str, str]]:
    """Original-vs-output path pairs for the GUI's side-by-side preview.
    Recomputes each output path via the SAME pure functions write_patient_
    outputs() itself used (anon_output_filename() for PDFs, the kidney-
    subfolder + basename convention for DICOM) rather than relying on
    order-preserving parallel lists, so it stays correct even if some
    files failed redaction (see rec.failed_pdfs)."""
    pairs: List[Dict[str, str]] = []
    for src_path, _fields in rec.a_results:
        pairs.append({
            "original": src_path,
            "output": os.path.join(out_patient_dir, eg.anon_output_filename(rec.anon_id, src_path)),
            "category": "Clinical / lab report",
        })
    for src_path, _fields in rec.b_results:
        pairs.append({
            "original": src_path,
            "output": os.path.join(out_patient_dir, eg.anon_output_filename(rec.anon_id, src_path)),
            "category": "Clinical / lab report",
        })
    for src_path in rec.scanned_paths:
        pairs.append({
            "original": src_path,
            "output": os.path.join(out_patient_dir, eg.anon_output_filename(rec.anon_id, src_path)),
            "category": "Case Record Form (scanned, no PII -- copied unchanged)",
        })
    for kidney_dir in eg.find_kidney_subfolders(rec.folder):
        side_name = os.path.basename(kidney_dir.rstrip(os.sep))
        for dcm_path in eg.find_dcm_files(kidney_dir):
            pairs.append({
                "original": dcm_path,
                "output": os.path.join(out_patient_dir, side_name, os.path.basename(dcm_path)),
                "category": f"Ultrasound image ({side_name})",
            })
    return pairs


def process_egfr_package(package_dir: str) -> Dict[str, Any]:
    mapping_csv_path = get_mapping_csv_path("egfr")
    output_root = get_output_root("egfr")
    mapping = ac.MappingStore.load_or_create(mapping_csv_path)
    log: List[str] = []

    rec = eg.resolve_patient_identity(package_dir, mapping, log)
    if rec.identity_key is None:
        # Distinguish the most common selection mistake (a folder holding
        # MANY patients) from every other identity-resolution failure, and
        # explain it in terms the operator can act on. The engine's own
        # errors are kept in `log` either way, so nothing is lost.
        kind, values = _distinct_identity_values(rec)
        if len(values) > 1:
            errors = _multi_patient_errors(package_dir, kind, values)
            log.extend(rec.errors)
        else:
            errors = rec.errors or [
                "Could not establish a stable identity key (CR No / Lab No.) for this package. "
                "Make sure it contains at least one readable clinical-report PDF."
            ]
        return {
            "success": False,
            "study": "egfr",
            "errors": errors,
            "log": log,
        }

    eg.write_patient_outputs(rec, output_root)
    mapping.save()

    out_patient_dir = os.path.join(output_root, rec.anon_id)
    return {
        "success": not rec.errors and not rec.failed_pdfs,
        "study": "egfr",
        "anon_id": rec.anon_id,
        "is_new_id": rec.is_new_id,
        "identity_kind": rec.identity_kind,
        "output_dir": out_patient_dir,
        "mapping_csv_path": mapping_csv_path,
        "dcm_count": rec.dcm_count,
        "failed_pdfs": [{"path": p, "reason": r} for p, r in rec.failed_pdfs],
        "errors": rec.errors,
        "preview_pairs": _only_written_pairs(_build_egfr_preview_pairs(rec, out_patient_dir)),
        "log": log,
    }


# ==========================================================================
# KFRE processing
# ==========================================================================


def process_kfre_package(package_dir: str) -> Dict[str, Any]:
    mapping_csv_path = get_mapping_csv_path("kfre")
    output_root = get_output_root("kfre")

    pdf_paths = ac.find_pdfs_recursive(package_dir)
    if not pdf_paths:
        return {"success": False, "study": "kfre", "errors": ["No PDF files were found in this package."]}

    records = [kf.classify_input_file(p) for p in pdf_paths]
    unrecognized = [r for r in records if r.template == "unrecognized"]
    scanned = [r for r in records if r.template == "scanned_no_pii"]
    a_or_b = [r for r in records if r.template in ("A", "B")]
    missing_key = [r for r in a_or_b if r.identity_key is None]
    groupable = [r for r in a_or_b if r.identity_key is not None]
    groups = kf.group_by_identity(groupable)

    errors = [f"Unrecognized file (not a known report template, not scanned/no-PII): {os.path.basename(r.src_path)}" for r in unrecognized]
    errors += [f"No identity key (CR No / Lab No.) could be extracted from: {os.path.basename(r.src_path)}" for r in missing_key]

    if not groups:
        errors.insert(0, "No file in this package could be matched to a stable identity key (CR No / Lab No.).")
        return {"success": False, "study": "kfre", "errors": errors}

    if len(groups) > 1:
        # Same selection mistake as the eGFR path guards against (a folder
        # of many patients rather than one patient's package) -- reported
        # with the same wording so both studies behave identically.
        kinds = {g.identity_key_kind for g in groups}
        kind = next(iter(kinds)) if len(kinds) == 1 else "CR No / Lab No."
        return {
            "success": False,
            "study": "kfre",
            "errors": _multi_patient_errors(package_dir, kind, {g.identity_key for g in groups}),
        }

    mapping = ac.MappingStore.load_or_create(mapping_csv_path)
    group = groups[0]
    anon_id, is_new = mapping.get_or_assign(original_key=group.identity_key, name=group.patient_name)

    os.makedirs(output_root, exist_ok=True)
    exit_code = kf._run_redaction_and_save([group], mapping, output_root)  # also calls mapping.save()

    pairs: List[Dict[str, str]] = []
    for rec in group.files:
        pairs.append({
            "original": rec.src_path,
            "output": os.path.join(output_root, f"{anon_id}_{rec.filename}"),
            "category": "Clinical / lab report",
        })
    for rec in scanned:
        dst = os.path.join(output_root, rec.filename)
        ac.safe_copy_bytes(rec.src_path, dst)
        pairs.append({
            "original": rec.src_path,
            "output": dst,
            "category": "Scanned, no extractable PII -- copied unchanged",
        })

    return {
        "success": exit_code == 0 and not errors,
        "study": "kfre",
        "anon_id": anon_id,
        "is_new_id": is_new,
        "identity_kind": group.identity_key_kind,
        "output_dir": output_root,
        "mapping_csv_path": mapping_csv_path,
        "errors": errors,
        "preview_pairs": _only_written_pairs(pairs),
    }


# ==========================================================================
# Entry point used by the GUI bridge
# ==========================================================================


def process_package(paths: List[str], is_folder: bool, study_override: Optional[str] = None) -> Dict[str, Any]:
    """
    paths: a single-element list (folder path, is_folder=True) or a flat
    list of individually-selected file paths (is_folder=False).
    study_override: "egfr" | "kfre" | None (None = auto-detect).
    """
    staging_dir: Optional[str] = None
    try:
        if is_folder:
            package_dir = paths[0]
        else:
            staging_dir = stage_selected_files(paths)
            package_dir = staging_dir

        study = study_override or detect_study_type(package_dir)
        if study == "egfr":
            result = process_egfr_package(package_dir)
        else:
            result = process_kfre_package(package_dir)
        result["study_detected"] = study
        return result
    finally:
        if staging_dir and os.path.isdir(staging_dir):
            shutil.rmtree(staging_dir, ignore_errors=True)


def get_app_info() -> Dict[str, Any]:
    return {
        "hospital_code": HOSPITAL_CODE,
        "hospital_name": HOSPITAL_NAME,
        "app_data_dir": get_app_data_dir(),
        "egfr_mapping_csv": get_mapping_csv_path("egfr"),
        "kfre_mapping_csv": get_mapping_csv_path("kfre"),
        "egfr_output_dir": get_output_root("egfr"),
        "kfre_output_dir": get_output_root("kfre"),
    }


def cleanup_stale_staging_dirs(max_age_hours: float = 24.0) -> None:
    """
    Best-effort startup sweep of leftover `anon_staging_*` temp directories
    (see stage_selected_files()) -- normally removed in process_package()'s
    `finally` block, but a force-killed process (crash, Task Manager "End
    Task", OS shutdown mid-run) can leave one behind holding an UNREDACTED
    plaintext copy of real patient PHI in the system temp folder
    indefinitely. Called once at app startup (see ../main.py); deliberately
    silent/non-fatal on any error -- this is defense-in-depth cleanup, never
    something that should block the app from starting.
    """
    try:
        temp_root = tempfile.gettempdir()
        now = time.time()
        for name in os.listdir(temp_root):
            if not name.startswith("anon_staging_"):
                continue
            full = os.path.join(temp_root, name)
            try:
                if not os.path.isdir(full):
                    continue
                age_hours = (now - os.path.getmtime(full)) / 3600.0
                if age_hours >= max_age_hours:
                    shutil.rmtree(full, ignore_errors=True)
            except OSError:
                continue  # another process may hold it open, or a race on deletion -- skip, not fatal
    except OSError:
        pass  # e.g. temp dir unreadable -- never block app startup over this

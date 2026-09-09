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


def _only_written_pairs(
    pairs: List[Dict[str, str]],
    written_after: Optional[float] = None,
) -> List[Dict[str, str]]:
    """
    Drop preview rows whose anonymized output this run did not actually write.

    Both studies' preview builders RECOMPUTE each expected output path from
    the same pure naming functions the writers use, rather than observing
    what landed on disk -- which is what keeps them correct when some files
    fail. The cost is that a failed file still yields a plausible-looking
    row, and the operator is being asked to eyeball this list and then
    Approve, so a row here must mean a real file.

    Existence alone is NOT sufficient, and assuming it was a bug: output
    accumulates permanently per patient under Anonymized_<study>/<AnonID>/,
    so a patient processed successfully once leaves files behind that make
    a LATER failed run look successful -- observed exactly that, a run
    reporting 13 files while writing 0 images, because a previous run's
    images were still sitting at the same paths. `written_after` (captured
    immediately before the writers run) distinguishes "written now" from
    "left over from last time".

    The one-second slack absorbs coarse filesystem mtime granularity; being
    slightly permissive here is the safe direction, since the alternative
    is hiding a file that genuinely was just written.
    """
    kept = []
    for p in pairs:
        output = p.get("output")
        if not output or not os.path.exists(output):
            continue
        if written_after is not None:
            try:
                if os.path.getmtime(output) < written_after - 1.0:
                    continue  # a previous run's leftover, not this run's output
            except OSError:
                continue
        kept.append(p)
    return kept


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

    # Captured before the writers run so the preview can tell this run's
    # output apart from a previous run's leftovers at the same paths (this
    # patient's folder persists across runs) -- see _only_written_pairs().
    write_started = time.time()
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
        "preview_pairs": _only_written_pairs(
            _build_egfr_preview_pairs(rec, out_patient_dir), write_started
        ),
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
    write_started = time.time()  # see _only_written_pairs()
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
        "preview_pairs": _only_written_pairs(pairs, write_started),
    }


# ==========================================================================
# Cross-check review (original vs anonymized, side by side)
# ==========================================================================

# Previews are decoded, downscaled and base64'd across the JS bridge one row
# at a time, on demand. A single patient's ten ultrasound frames are ~44 MB of
# pixel data; inlining that into the page would stall the webview, so the UI
# asks for each row only as it scrolls into view.
PREVIEW_MAX_PX = 900
PDF_PREVIEW_DPI = 110

# Tags worth showing first in the metadata comparison: the ones that carry
# identity, plus the UIDs the pipeline regenerates. Everything else follows,
# so nothing is hidden -- this only controls ordering.
CROSSCHECK_PRIORITY_TAGS = [
    "PatientName", "PatientID", "PatientBirthDate", "PatientSex", "PatientAge",
    "InstitutionName", "InstitutionAddress", "ReferringPhysicianName",
    "PerformingPhysicianName", "OperatorsName", "StationName",
    "StudyDate", "StudyTime", "AccessionNumber", "StudyID",
    "StudyInstanceUID", "SeriesInstanceUID", "SOPInstanceUID",
]


def _png_data_uri(img: "Any") -> str:
    """PIL image -> data: URI. PNG keeps the zeroed banner crisp (a JPEG
    would add ringing along the redaction boundary, which is exactly the
    edge the reviewer is trying to judge)."""
    import base64
    import io

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def render_dicom_preview(path: str, max_px: int = PREVIEW_MAX_PX) -> Dict[str, Any]:
    """
    One DICOM frame as a viewable PNG data URI.

    Ultrasound frames are usually colour-encoded as YBR (that is what JPEG
    Baseline carries); handing those raw bytes to PIL renders them with a
    green/magenta cast, which would make a reviewer distrust a correctly
    anonymized image. So the photometric interpretation is honoured
    explicitly. Multi-frame files show their first frame -- the burned-in
    banner is identical across frames.
    """
    import numpy as np
    import pydicom
    from PIL import Image

    try:
        ds = pydicom.dcmread(path)
        arr = ds.pixel_array
        photometric = str(getattr(ds, "PhotometricInterpretation", "") or "")

        if arr.ndim == 4 or (arr.ndim == 3 and photometric.startswith("YBR") is False and arr.shape[-1] not in (3, 4) and getattr(ds, "NumberOfFrames", 1) not in (1, "1", None)):
            arr = arr[0]  # multi-frame: first frame only

        if photometric.startswith("YBR"):
            from pydicom.pixels import convert_color_space
            arr = convert_color_space(arr, photometric, "RGB")

        if arr.dtype != np.uint8:
            lo, hi = float(np.min(arr)), float(np.max(arr))
            arr = np.zeros_like(arr, dtype=np.uint8) if hi <= lo else \
                (((arr.astype(np.float32) - lo) / (hi - lo)) * 255.0).astype(np.uint8)

        img = Image.fromarray(arr)
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        img.thumbnail((max_px, max_px), Image.LANCZOS)
        return {"ok": True, "data_uri": _png_data_uri(img), "label": os.path.basename(path)}
    except Exception as exc:  # noqa: BLE001 -- a preview failure must never look like a redaction failure
        return {"ok": False, "error": f"Could not render this image: {exc}", "label": os.path.basename(path)}


def render_pdf_preview(path: str, page_index: int = 0, dpi: int = PDF_PREVIEW_DPI) -> Dict[str, Any]:
    """One PDF page as a PNG data URI, via the PyMuPDF already used for
    redaction (no extra dependency). page_count is returned so the UI can
    offer every page rather than assuming single-page reports."""
    try:
        import fitz

        doc = fitz.open(path)
        try:
            page_count = doc.page_count
            if page_count == 0:
                return {"ok": False, "error": "This PDF has no pages.", "label": os.path.basename(path)}
            index = max(0, min(page_index, page_count - 1))
            pix = doc.load_page(index).get_pixmap(dpi=dpi)
            import base64
            return {
                "ok": True,
                "data_uri": "data:image/png;base64," + base64.b64encode(pix.tobytes("png")).decode("ascii"),
                "label": os.path.basename(path),
                "page_index": index,
                "page_count": page_count,
            }
        finally:
            doc.close()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"Could not render this report: {exc}", "label": os.path.basename(path)}


def get_dicom_metadata_pair(original_path: str, anonymized_path: str) -> Dict[str, Any]:
    """
    Tag-by-tag comparison of one DICOM before and after anonymization.

    Reads headers only (stop_before_pixels) so opening the metadata tab is
    instant. Every tag present in EITHER file is listed -- a tag that the
    pipeline removed outright must still be visible as "removed", otherwise
    the reviewer cannot tell removal apart from it never having been there.
    Private tags are included: remove_private_tags() is part of what is
    being verified.
    """
    import pydicom

    try:
        src = pydicom.dcmread(original_path, stop_before_pixels=True)
        out = pydicom.dcmread(anonymized_path, stop_before_pixels=True)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"Could not read DICOM metadata: {exc}"}

    def flatten(ds) -> Dict[str, Dict[str, str]]:
        found: Dict[str, Dict[str, str]] = {}
        for elem in ds:
            if elem.tag.group == 0x7FE0:  # pixel data -- compared visually, not as text
                continue
            key = f"({elem.tag.group:04X},{elem.tag.element:04X})"
            try:
                value = "" if elem.value is None else str(elem.value)
            except Exception:  # noqa: BLE001
                value = "<unreadable>"
            found[key] = {
                "tag": key,
                "keyword": (elem.keyword or elem.name or ""),
                "value": value[:400],
            }
        return found

    src_tags, out_tags = flatten(src), flatten(out)

    rows = []
    for key in set(src_tags) | set(out_tags):
        s = src_tags.get(key)
        o = out_tags.get(key)
        keyword = (s or o or {}).get("keyword", "")
        original_value = s["value"] if s else None
        anon_value = o["value"] if o else None
        if s and not o:
            status = "removed"
        elif o and not s:
            status = "added"
        elif original_value == anon_value:
            status = "unchanged"
        else:
            status = "changed"
        rows.append({
            "tag": key,
            "keyword": keyword,
            "original": original_value,
            "anonymized": anon_value,
            "status": status,
        })

    priority = {name: i for i, name in enumerate(CROSSCHECK_PRIORITY_TAGS)}
    rows.sort(key=lambda r: (priority.get(r["keyword"], len(priority)), r["keyword"] or r["tag"]))

    return {
        "ok": True,
        "rows": rows,
        "counts": {
            "changed": sum(1 for r in rows if r["status"] == "changed"),
            "removed": sum(1 for r in rows if r["status"] == "removed"),
            "unchanged": sum(1 for r in rows if r["status"] == "unchanged"),
            "added": sum(1 for r in rows if r["status"] == "added"),
        },
    }


def get_crosscheck_manifest(preview_pairs: List[Dict[str, str]]) -> Dict[str, Any]:
    """
    Split one patient's original/anonymized pairs into the three review
    categories the cross-check screen offers, keeping each list in a stable
    order so "scroll to the end" means the same thing every time.

    Images and metadata are the SAME .dcm pairs viewed two ways (pixels vs
    header), so both tabs are driven by one list rather than re-deriving it.
    Only pairs whose BOTH sides exist on disk are offered -- a row the
    reviewer cannot actually compare is worse than no row.
    """
    images, reports = [], []
    for pair in preview_pairs:
        original, output = pair.get("original", ""), pair.get("output", "")
        if not (original and output and os.path.exists(original) and os.path.exists(output)):
            continue
        entry = {
            "original": original,
            "output": output,
            "label": os.path.basename(original),
            "output_label": os.path.basename(output),
            "category": pair.get("category", ""),
        }
        if original.lower().endswith(".dcm"):
            images.append(entry)
        elif original.lower().endswith(".pdf"):
            reports.append(entry)

    return {
        "images": images,
        "metadata": list(images),
        "reports": reports,
        "counts": {"images": len(images), "metadata": len(images), "reports": len(reports)},
    }


# ==========================================================================
# Batch processing (a folder holding MANY patients)
# ==========================================================================


def _patient_subfolders(parent_dir: str) -> List[str]:
    """
    Every immediate subdirectory, treated as a candidate patient package.

    Deliberately NOT eg.find_patient_folders(), which filters on
    NP_FOLDER_RE (r"NP\\s*0*(\\d+)") -- that matches this project's own
    research-corpus naming ("NP1 (CKD)") but would silently SKIP a
    hospital's real folders if they are named by MRN, date, or anything
    else. Silently processing 3 of 10 patients is a far worse failure here
    than trying a folder that turns out to hold nothing usable, which is
    reported per-patient anyway.
    """
    try:
        return [
            os.path.join(parent_dir, name)
            for name in sorted(os.listdir(parent_dir))
            if os.path.isdir(os.path.join(parent_dir, name))
        ]
    except OSError:
        return []


def _batch_summary(patients: List[Dict[str, Any]]) -> Dict[str, int]:
    return {
        "total": len(patients),
        "succeeded": sum(1 for p in patients if p.get("success")),
        "failed": sum(1 for p in patients if not p.get("success")),
        "new_ids": sum(1 for p in patients if p.get("is_new_id")),
        "reused_ids": sum(1 for p in patients if p.get("anon_id") and not p.get("is_new_id")),
    }


def process_egfr_batch(parent_dir: str) -> Dict[str, Any]:
    """
    Run every patient subfolder under parent_dir through the SAME
    single-patient path, independently: each resolves its own identity and
    gets its own Anonymized ID, exactly as the batch CLI does. That
    independence is the safety property -- two patients can never end up
    merged under one ID because nothing is ever resolved across subfolder
    boundaries. One patient failing does not abort the others; its errors
    are reported in its own row.
    """
    patients: List[Dict[str, Any]] = []
    for sub in _patient_subfolders(parent_dir):
        try:
            result = process_egfr_package(sub)
        except Exception as exc:  # noqa: BLE001 -- one bad folder must not kill the run
            result = {"success": False, "study": "egfr", "errors": [f"Unexpected error: {exc}"]}
        result["source"] = os.path.basename(sub.rstrip(os.sep))
        result["source_path"] = sub
        patients.append(result)

    return {
        "success": bool(patients) and all(p.get("success") for p in patients),
        "batch": True,
        "study": "egfr",
        "patients": patients,
        "summary": _batch_summary(patients),
        "mapping_csv_path": get_mapping_csv_path("egfr"),
        "output_dir": get_output_root("egfr"),
        "errors": [] if patients else ["No patient subfolders were found in the selected folder."],
    }


def process_kfre_batch(parent_dir: str) -> Dict[str, Any]:
    """
    KFRE needs no subfolder walk: classify_input_file() + group_by_identity()
    already partition every PDF found anywhere under the selection into one
    group per patient (identity comes from PDF CONTENT, never from folder
    layout -- see anonymize_KFRE.find_input_pdfs()'s own note). So the whole
    selection is processed in one pass and reported one row per group.
    """
    mapping_csv_path = get_mapping_csv_path("kfre")
    output_root = get_output_root("kfre")

    pdf_paths = ac.find_pdfs_recursive(parent_dir)
    if not pdf_paths:
        return {
            "success": False,
            "batch": True,
            "study": "kfre",
            "patients": [],
            "summary": _batch_summary([]),
            "errors": ["No PDF files were found anywhere in the selected folder."],
        }

    records = [kf.classify_input_file(p) for p in pdf_paths]
    unrecognized = [r for r in records if r.template == "unrecognized"]
    scanned = [r for r in records if r.template == "scanned_no_pii"]
    a_or_b = [r for r in records if r.template in ("A", "B")]
    missing_key = [r for r in a_or_b if r.identity_key is None]
    groups = kf.group_by_identity([r for r in a_or_b if r.identity_key is not None])

    # Files that belong to NO patient group are reported once at batch level
    # rather than being silently attached to an arbitrary patient's row.
    batch_errors = [
        f"Unrecognized file (not a known report template, not scanned/no-PII): {os.path.basename(r.src_path)}"
        for r in unrecognized
    ] + [
        f"No identity key (CR No / Lab No.) could be extracted from: {os.path.basename(r.src_path)}"
        for r in missing_key
    ]

    if not groups:
        batch_errors.insert(0, "No file in this folder could be matched to a stable identity key (CR No / Lab No.).")
        return {
            "success": False,
            "batch": True,
            "study": "kfre",
            "patients": [],
            "summary": _batch_summary([]),
            "errors": batch_errors,
        }

    mapping = ac.MappingStore.load_or_create(mapping_csv_path)
    assigned = []
    for group in groups:
        anon_id, is_new = mapping.get_or_assign(original_key=group.identity_key, name=group.patient_name)
        assigned.append((group, anon_id, is_new))

    os.makedirs(output_root, exist_ok=True)
    write_started = time.time()  # see _only_written_pairs()
    exit_code = kf._run_redaction_and_save(groups, mapping, output_root)  # also calls mapping.save()

    # Scanned/no-PII files carry no identity, so they cannot be attributed to
    # a patient -- copied once, reported at batch level (same as the CLI).
    scanned_pairs = []
    for rec in scanned:
        dst = os.path.join(output_root, rec.filename)
        ac.safe_copy_bytes(rec.src_path, dst)
        scanned_pairs.append({
            "original": rec.src_path,
            "output": dst,
            "category": "Scanned, no extractable PII -- copied unchanged",
        })

    patients: List[Dict[str, Any]] = []
    for group, anon_id, is_new in assigned:
        pairs = [{
            "original": rec.src_path,
            "output": os.path.join(output_root, f"{anon_id}_{rec.filename}"),
            "category": "Clinical / lab report",
        } for rec in group.files]
        written = _only_written_pairs(pairs, write_started)
        patients.append({
            "success": exit_code == 0 and len(written) == len(pairs),
            "study": "kfre",
            "source": f"{group.identity_key_kind} {group.identity_key}",
            "source_path": os.path.dirname(group.files[0].src_path) if group.files else parent_dir,
            "anon_id": anon_id,
            "is_new_id": is_new,
            "identity_kind": group.identity_key_kind,
            "output_dir": output_root,
            "mapping_csv_path": mapping_csv_path,
            "errors": [] if len(written) == len(pairs) else [
                f"{len(pairs) - len(written)} of {len(pairs)} report(s) were not written for this patient."
            ],
            "failed_pdfs": [],
            "preview_pairs": written,
        })

    return {
        "success": exit_code == 0 and not batch_errors and all(p["success"] for p in patients),
        "batch": True,
        "study": "kfre",
        "patients": patients,
        "summary": _batch_summary(patients),
        "mapping_csv_path": mapping_csv_path,
        "output_dir": output_root,
        "errors": batch_errors,
        "unattributed_pairs": scanned_pairs,
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

        # Single patient or a folder of many? Decided from what the reports
        # actually contain, never from folder names or nesting depth: a
        # single patient's own package legitimately has several subfolders
        # (Left_Kidney_Images/, Lab_Reports/, ...), so any structural
        # heuristic would misclassify it. Identity resolution is PDF-text
        # only -- the expensive DICOM work happens later, on write -- so
        # asking first is cheap.
        #
        # Only ever applies to a folder selection: a "Choose Files" staging
        # directory is one patient's files by construction.
        if is_folder:
            if study == "egfr":
                probe = eg.resolve_patient_identity(package_dir, ac.MappingStore.load_or_create(
                    get_mapping_csv_path("egfr")), [])
                _kind, values = _distinct_identity_values(probe)
                if len(values) > 1:
                    result = process_egfr_batch(package_dir)
                    result["study_detected"] = study
                    return result
            else:
                probe_records = [kf.classify_input_file(p) for p in ac.find_pdfs_recursive(package_dir)]
                probe_groups = kf.group_by_identity(
                    [r for r in probe_records if r.template in ("A", "B") and r.identity_key is not None]
                )
                if len(probe_groups) > 1:
                    result = process_kfre_batch(package_dir)
                    result["study_detected"] = study
                    return result

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

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
    # Running as a PyInstaller-built .exe. anon_common / anonymize_eGFR /
    # anonymize_KFRE are bundled as MODULES (see build/app.spec's
    # `hiddenimports`) and are already importable through PyInstaller's own
    # importer, so nothing needs adding to sys.path here.
    #
    # Deliberately do NOT insert sys._MEIPASS into sys.path. In a onedir
    # build _MEIPASS is the `_internal` directory, which also holds
    # numpy's C extensions (numpy/_core/_multiarray_umath...pyd). Putting
    # it on sys.path gives numpy a SECOND import route -- the ordinary
    # filesystem finder -- competing with the frozen importer that already
    # serves numpy's Python code from the embedded archive. CPython then
    # refuses the extension outright with "cannot load module more than
    # once per process", and pydicom catches that and reports its own
    # generic "NumPy is required when converting pixel data to an ndarray",
    # so every ultrasound image fails for a reason that names the wrong
    # cause. Cost us two misdiagnosed builds.
    PROJECT_ROOT = sys._MEIPASS  # type: ignore[attr-defined]  # data files only, never sys.path
else:
    # Running from source: the three engine scripts really do live two
    # levels up (project root), and that path genuinely must be importable.
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


def mapping_csv_name(study: str) -> str:
    """Same filename the CLI scripts use, so a mapping written by either is
    picked up by the other without renaming."""
    return "eGFR_anony_Mapping.csv" if study == "egfr" else "KFRE_anony_Mapping.csv"


def get_mapping_csv_path(
    study: str,
    override: Optional[str] = None,
    output_override: Optional[str] = None,
) -> str:
    """
    Where this run's confidential ID-mapping CSV lives.

    Resolution order:

    1. `override` -- the operator's explicitly chosen file, always wins.
       This matters for more than convenience: the mapping is the ONLY
       thing that makes a follow-up visit resolve to the patient's existing
       Anonymized ID, and it is also the list of IDs already issued that a
       newly minted one is checked against. Running against the wrong file
       silently issues a second ID for a patient who already has one.

    2. No CSV chosen but an output destination was -- keep the mapping with
       the study it belongs to, as a SIBLING of the anonymized folder:

           <chosen>/eGFR_anony_Mapping.csv      <- mapping
           <chosen>/Anonymized_eGFR/...         <- shareable output

       Deliberately NOT inside Anonymized_*/: that folder is the thing
       meant to be safe to hand over, and this file carries every real
       CR No / Lab No. and patient name. anon_common.validate_mapping_csv_
       location() rejects a mapping path that resolves inside the output
       directory outright, and a sibling satisfies it.

       Note the boundary this creates: `Anonymized_eGFR/` stays safe to
       share, but the folder ABOVE it now contains the mapping, so that one
       must not be zipped up and sent anywhere.

    3. Neither chosen -- the per-user app-data copy, which is the right
       default for one workstation used by one team.
    """
    if override:
        return os.path.abspath(override)
    if output_override:
        return os.path.join(os.path.abspath(output_override), mapping_csv_name(study))
    return os.path.join(get_app_data_dir(), mapping_csv_name(study))


def get_output_root(study: str, override: Optional[str] = None) -> str:
    """
    Where anonymized output is written.

    `override` is the operator's chosen destination and wins when given, so
    output can go straight to the study drive or share rather than being
    buried under the user profile. The per-study subfolder name is still
    appended, so pointing two studies at one destination keeps them apart.

    Otherwise: a persistent, accumulating location under app-data. Unlike
    the batch CLI (one ranged-numbered folder per whole-corpus run) there is
    no natural "session range" to name a folder after here, so every patient
    just gets its own <AnonID>/ subfolder, matching the CLI's own per-patient
    convention -- nothing ever needs pruning or renaming.
    """
    path = output_root_path(study, override)
    os.makedirs(path, exist_ok=True)
    return path


def output_root_path(study: str, override: Optional[str] = None) -> str:
    """Where output WOULD go, without creating anything.

    Separate from get_output_root() because describe_session() only reports
    on a run that has not happened yet, and a function that answers "where
    would this go?" must not answer it by making the folder. It did, and
    left an empty Anonymized_eGFR/ sitting next to a KFRE operator's real
    output -- a directory nobody asked for, named after the wrong study,
    inside a folder they were about to share.
    """
    name = "Anonymized_eGFR" if study == "egfr" else "Anonymized_KFRE"
    base = os.path.abspath(override) if override else get_app_data_dir()
    return os.path.join(base, name)


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


def detect_study_for_selection(paths: List[str], is_folder: bool) -> str:
    """
    The same answer process_package() will reach, obtainable BEFORE any work
    starts, so the UI can show which study's mapping CSV and output folder
    this run will actually use.

    That mattered more than it sounds: the session panel used to assume
    "egfr" whenever the study picker was on Auto-detect, so pointing the app
    at a KFRE folder left it advertising an eGFR_anony_Mapping.csv path that
    the run then never wrote to.

    Mirrors process_package()'s own two branches rather than approximating
    them. A folder selection is walked by detect_study_type(); a flat file
    selection is staged first there, and since staging copies exactly the
    files given, testing those names for a .dcm is equivalent to walking the
    staging directory -- without paying to copy real patient data into temp
    just to answer a question about it.
    """
    if is_folder:
        return detect_study_type(paths[0]) if paths else "egfr"
    if any(str(p).lower().endswith(".dcm") for p in paths or []):
        return "egfr"
    return "kfre"


def _is_writable_dir(path: str) -> bool:
    """Actually try to write, rather than trust os.access -- which on
    Windows reports the read-only ATTRIBUTE, not whether the ACL lets this
    user create a file. A read-only study share would otherwise be
    suggested as a destination and fail halfway through the run."""
    probe = os.path.join(path, f".tanuh_write_probe_{os.getpid()}")
    try:
        with open(probe, "w"):
            pass
        os.remove(probe)
        return True
    except OSError:
        return False


def suggest_output_dir(paths: List[str], is_folder: bool) -> Optional[str]:
    """
    Where output should default to for this selection: the folder that
    CONTAINS it, so the anonymized folder and the mapping CSV land as
    SIBLINGS of the input rather than somewhere under the user profile.

        Downloads/sample_NIMS/            <- suggested destination
        ├── KFRE/                         <- what was selected
        ├── KFRE_anony_Mapping.csv        <- mapping, written here
        └── Anonymized_KFRE/              <- output, written here

    Keeping all three together is what makes a study folder self-contained:
    the operator can see at a glance which mapping belongs to which data,
    and carrying the folder to another machine carries the whole session.

    The sibling layout also satisfies anon_common.validate_mapping_csv_
    location(), which refuses a mapping resolving inside either the input
    tree or the shareable output folder. A sibling is neither.

    Returns None when there is no sensible suggestion -- a drive root, a
    parent that no longer exists, or one this user cannot write to -- and
    the caller then keeps whatever default was already in effect.
    """
    if not paths:
        return None
    first = os.path.abspath(paths[0])
    # For a file selection, the "input" is the folder holding the files, so
    # the destination is that folder's parent -- same result as selecting
    # the folder itself, which is what makes the two selection modes agree.
    base = first if (is_folder and os.path.isdir(first)) else os.path.dirname(first)
    parent = os.path.dirname(base)
    if not parent or parent == base or not os.path.isdir(parent):
        return None  # drive root, or the selection had no containing folder
    if not _is_writable_dir(parent):
        return None
    return parent


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


# ==========================================================================
# Repeat submissions (the same files handed over a second time)
# ==========================================================================
#
# A known CR No / Lab No. means "I have seen this PATIENT", not "I have seen
# these DOCUMENTS". Both a genuine follow-up visit and an already-anonymized
# folder handed over again look identical to the mapping store, so the
# second was reported as a follow-up, silently redone, and its verified
# output overwritten.
#
# The discriminator is a fingerprint of the file bytes, stored per patient
# in the mapping CSV's "Source Fingerprint" column -- see
# anon_common.fingerprint_files() for why it is a content hash rather than
# a report date, and why it is the whole set rather than a list per file.
#
# Both studies drive this off their preview PAIRS list, which already
# enumerates every source file the run would process alongside the exact
# output path it would write, so the check needs no separate inventory that
# could drift from what the writers actually do.


def _check_repeat(
    pairs: List[Dict[str, str]],
    identity_key: str,
    anon_id: str,
    mapping: "ac.MappingStore",
) -> Dict[str, Any]:
    """
    Whether this submission is new material, the same files again, or the
    same files whose output has since gone missing.

    Outputs count as present only when EVERY expected one exists at the
    destination this run would write to, so pointing at a fresh output
    folder correctly rewrites everything instead of reporting it as done.
    """
    sources = [p["original"] for p in pairs if p.get("original")]
    current = ac.fingerprint_files(sources) if sources else ""
    stored = mapping.get_fingerprint(identity_key)

    verdict = "new"
    if current and stored and stored == current:
        outputs_exist = bool(pairs) and all(
            p.get("output") and os.path.exists(p["output"]) for p in pairs
        )
        verdict = "duplicate" if outputs_exist else "recreate"

    # The same set of files already recorded against a DIFFERENT patient is
    # not a repeat to skip: it means one source document sits in the
    # anonymized dataset under two subject IDs, inflating the cohort.
    conflict_key = None
    if verdict == "new" and current:
        other = mapping.find_key_by_fingerprint(current)
        if other and other != identity_key.strip():
            conflict_key = other

    if verdict == "duplicate":
        message = (
            f"Already anonymized. These are the same {len(sources)} file(s), byte for byte, "
            f"and the anonymized output is still in place, so nothing was re-written."
        )
    elif verdict == "recreate":
        message = (
            f"Same {len(sources)} file(s) as a previous session, but their anonymized output "
            f"was not present at this destination, so it has been written again."
        )
    else:
        message = ""

    return {
        "verdict": verdict,
        "skipped": verdict == "duplicate",
        "total": len(sources),
        "fingerprint": current,
        "conflict_with": conflict_key,
        "message": message,
    }


def _conflict_warnings(repeat: Dict[str, Any], anon_id: str) -> List[str]:
    """Operator-facing text for a set of files already recorded against a
    different patient. A warning, not an error: the run itself is sound, but
    the dataset now holds one source document under two subject IDs and only
    the operator can say which is right."""
    other = repeat.get("conflict_with")
    if not other:
        return []
    return [
        f"These exact files are already recorded in this mapping against a different patient "
        f"({other}), yet they resolve to {anon_id} here. The same patient may now appear twice "
        f"in the anonymized dataset under two IDs -- check the mapping before sharing."
    ]


def process_egfr_package(
    package_dir: str,
    mapping_csv: Optional[str] = None,
    output_dir: Optional[str] = None,
    mapping: Optional["ac.MappingStore"] = None,
) -> Dict[str, Any]:
    mapping_csv_path = get_mapping_csv_path("egfr", mapping_csv, output_dir)
    output_root = get_output_root("egfr", output_dir)
    # A batch hands its own store down so the CSV is read once for the whole
    # run rather than re-read per patient; a single-package run owns it here.
    owns_mapping = mapping is None
    if owns_mapping:
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

    out_patient_dir = os.path.join(output_root, rec.anon_id)
    pairs = _build_egfr_preview_pairs(rec, out_patient_dir)

    # The mapping store has just told us this is a KNOWN patient (a reused
    # ID). That alone does not say whether this is a follow-up visit or the
    # same visit handed over twice -- only the file bytes do.
    repeat_report = _check_repeat(pairs, rec.mapping_key, rec.anon_id, mapping)
    warnings = _conflict_warnings(repeat_report, rec.anon_id)

    if repeat_report["skipped"]:
        if owns_mapping:
            mapping.save()
        existing = _only_written_pairs(pairs)  # no mtime gate: these ARE the earlier run's output
        return {
            "success": True,
            "study": "egfr",
            "anon_id": rec.anon_id,
            "is_new_id": False,
            "identity_kind": rec.identity_kind,
            "output_dir": out_patient_dir,
            "mapping_csv_path": mapping_csv_path,
            "dcm_count": sum(1 for p in existing if p.get("original", "").lower().endswith(".dcm")),
            "failed_pdfs": [],
            "errors": [],
            "warnings": warnings,
            "repeat": repeat_report,
            "preview_pairs": existing,
            "log": log,
        }

    # Captured before the writers run so the preview can tell this run's
    # output apart from a previous run's leftovers at the same paths (this
    # patient's folder persists across runs) -- see _only_written_pairs().
    write_started = time.time()
    eg.write_patient_outputs(rec, output_root)

    written = _only_written_pairs(pairs, write_started)
    # Recorded only when EVERY file made it. A partial failure must stay
    # unrecorded, or the retry would be called "already anonymized".
    if len(written) == len(pairs) and repeat_report["fingerprint"]:
        mapping.set_fingerprint(rec.mapping_key, repeat_report["fingerprint"])
    if owns_mapping:
        mapping.save()

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
        "warnings": warnings,
        "repeat": repeat_report,
        "preview_pairs": written,
        "log": log,
    }


# ==========================================================================
# KFRE processing
# ==========================================================================


def _copy_scanned(scanned: List[Any], output_root: str) -> List[Dict[str, str]]:
    """Pass through the scanned/no-extractable-PII files. They carry no
    identity key, so they belong to no patient group and take no part in the
    repeat decision -- they are plain byte copies whether or not this is a
    repeat submission, and copying one again costs a file copy."""
    pairs: List[Dict[str, str]] = []
    for rec in scanned:
        dst = os.path.join(output_root, rec.filename)
        ac.safe_copy_bytes(rec.src_path, dst)
        pairs.append({
            "original": rec.src_path,
            "output": dst,
            "category": "Scanned, no extractable PII -- copied unchanged",
        })
    return pairs


def process_kfre_package(
    package_dir: str,
    mapping_csv: Optional[str] = None,
    output_dir: Optional[str] = None,
    mapping: Optional["ac.MappingStore"] = None,
) -> Dict[str, Any]:
    mapping_csv_path = get_mapping_csv_path("kfre", mapping_csv, output_dir)
    output_root = get_output_root("kfre", output_dir)

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

    owns_mapping = mapping is None
    if owns_mapping:
        mapping = ac.MappingStore.load_or_create(mapping_csv_path)
    group = groups[0]
    anon_id, is_new = mapping.get_or_assign(original_key=group.identity_key, name=group.patient_name)

    os.makedirs(output_root, exist_ok=True)

    report_pairs: List[Dict[str, str]] = [{
        "original": rec.src_path,
        "output": os.path.join(output_root, f"{anon_id}_{rec.filename}"),
        "category": "Clinical / lab report",
    } for rec in group.files]

    # A reused Anonymized ID means a known patient, not necessarily a new
    # visit -- only the file bytes separate a follow-up from the same
    # reports being handed over twice.
    repeat_report = _check_repeat(report_pairs, group.identity_key, anon_id, mapping)
    warnings = _conflict_warnings(repeat_report, anon_id)

    if repeat_report["skipped"]:
        scanned_pairs = _copy_scanned(scanned, output_root)
        if owns_mapping:
            mapping.save()
        return {
            "success": not errors,
            "study": "kfre",
            "anon_id": anon_id,
            "is_new_id": False,
            "identity_kind": group.identity_key_kind,
            "output_dir": output_root,
            "mapping_csv_path": mapping_csv_path,
            "errors": errors,
            "warnings": warnings,
            "repeat": repeat_report,
            # No mtime gate: these ARE the earlier run's output, deliberately
            # left untouched, and the operator can still cross-check them.
            "preview_pairs": _only_written_pairs(report_pairs) + scanned_pairs,
        }

    write_started = time.time()  # see _only_written_pairs()
    exit_code = kf._run_redaction_and_save([group], mapping, output_root)  # also calls mapping.save()

    scanned_pairs = _copy_scanned(scanned, output_root)
    written = _only_written_pairs(report_pairs + scanned_pairs, write_started)
    written_reports = [p for p in written if p in report_pairs]
    if len(written_reports) == len(report_pairs) and repeat_report["fingerprint"]:
        mapping.set_fingerprint(group.identity_key, repeat_report["fingerprint"])
    if owns_mapping:
        mapping.save()

    return {
        "success": exit_code == 0 and not errors,
        "study": "kfre",
        "anon_id": anon_id,
        "is_new_id": is_new,
        "identity_kind": group.identity_key_kind,
        "output_dir": output_root,
        "mapping_csv_path": mapping_csv_path,
        "errors": errors,
        "warnings": warnings,
        "repeat": repeat_report,
        "preview_pairs": written,
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

        # No colour-space conversion here, deliberately. pydicom's
        # pixel_array ALREADY normalises YBR to RGB, even though the
        # dataset's PhotometricInterpretation tag still reads YBR_FULL_422
        # for a JPEG-compressed file. Converting again on the strength of
        # that tag double-converted the data and rendered every ORIGINAL
        # frame bright green with magenta speckle, while the anonymized
        # copy (whose tag the engine had already rewritten to RGB) rendered
        # correctly -- so the two panes disagreed and the tool looked like
        # it was corrupting images. Measured on this corpus: originals come
        # back with R==G==B across 99.8% of pixels, i.e. already correct.
        arr = ds.pixel_array

        frames = int(getattr(ds, "NumberOfFrames", 1) or 1)
        if frames > 1:
            # pydicom returns multi-frame data frame-first. Only the first
            # is shown: the burned-in banner is identical on every frame,
            # which is what this view exists to check.
            arr = arr[0]

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
    def repeat_is(p: Dict[str, Any], *verdicts: str) -> bool:
        return (p.get("repeat") or {}).get("verdict") in verdicts

    return {
        "total": len(patients),
        "succeeded": sum(1 for p in patients if p.get("success")),
        "failed": sum(1 for p in patients if not p.get("success")),
        "new_ids": sum(1 for p in patients if p.get("is_new_id")),
        "reused_ids": sum(1 for p in patients if p.get("anon_id") and not p.get("is_new_id")),
        # Repeat submissions, split by what was actually done about them, so
        # "10 of 10 succeeded" can never quietly mean "and 7 of those were
        # the same files you gave me last week".
        "skipped_duplicates": sum(1 for p in patients if repeat_is(p, "duplicate")),
        "rewritten_repeats": sum(1 for p in patients if repeat_is(p, "recreate")),
        "conflicts": sum(1 for p in patients if (p.get("repeat") or {}).get("conflict_with")),
    }


def process_egfr_batch(
    parent_dir: str,
    mapping_csv: Optional[str] = None,
    output_dir: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Run every patient subfolder under parent_dir through the SAME
    single-patient path, independently: each resolves its own identity and
    gets its own Anonymized ID, exactly as the batch CLI does. That
    independence is the safety property -- two patients can never end up
    merged under one ID because nothing is ever resolved across subfolder
    boundaries. One patient failing does not abort the others; its errors
    are reported in its own row.
    """
    mapping_csv_path = get_mapping_csv_path("egfr", mapping_csv, output_dir)
    # One store for the whole batch: read once, written once. Loading per
    # patient would re-read and re-write the same CSV for every folder.
    mapping = ac.MappingStore.load_or_create(mapping_csv_path)

    patients: List[Dict[str, Any]] = []
    for sub in _patient_subfolders(parent_dir):
        try:
            result = process_egfr_package(sub, mapping_csv, output_dir, mapping=mapping)
        except Exception as exc:  # noqa: BLE001 -- one bad folder must not kill the run
            result = {"success": False, "study": "egfr", "errors": [f"Unexpected error: {exc}"]}
        result["source"] = os.path.basename(sub.rstrip(os.sep))
        result["source_path"] = sub
        patients.append(result)

    # Saved even when some folders failed: every patient that DID get written
    # must be on record, or the next session offers to redo work it already
    # did. A patient whose files failed keeps no fingerprint, so their retry
    # is not skipped.
    mapping.save()

    return {
        "success": bool(patients) and all(p.get("success") for p in patients),
        "batch": True,
        "study": "egfr",
        "patients": patients,
        "summary": _batch_summary(patients),
        "mapping_csv_path": get_mapping_csv_path("egfr", mapping_csv, output_dir),
        "output_dir": get_output_root("egfr", output_dir),
        "errors": [] if patients else ["No patient subfolders were found in the selected folder."],
    }


def process_kfre_batch(
    parent_dir: str,
    mapping_csv: Optional[str] = None,
    output_dir: Optional[str] = None,
) -> Dict[str, Any]:
    """
    KFRE needs no subfolder walk: classify_input_file() + group_by_identity()
    already partition every PDF found anywhere under the selection into one
    group per patient (identity comes from PDF CONTENT, never from folder
    layout -- see anonymize_KFRE.find_input_pdfs()'s own note). So the whole
    selection is processed in one pass and reported one row per group.
    """
    mapping_csv_path = get_mapping_csv_path("kfre", mapping_csv, output_dir)
    output_root = get_output_root("kfre", output_dir)

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

    # Every patient's repeat verdict is decided BEFORE anything is written,
    # so the groups that are pure repeats can be kept out of the redaction
    # pass entirely rather than redone and overwritten.
    prepared = []
    to_write = []
    for group, anon_id, is_new in assigned:
        pairs = [{
            "original": rec.src_path,
            "output": os.path.join(output_root, f"{anon_id}_{rec.filename}"),
            "category": "Clinical / lab report",
        } for rec in group.files]
        repeat_report = _check_repeat(pairs, group.identity_key, anon_id, mapping)
        prepared.append((group, anon_id, is_new, pairs, repeat_report))
        if not repeat_report["skipped"]:
            to_write.append(group)

    write_started = time.time()  # see _only_written_pairs()
    exit_code = kf._run_redaction_and_save(to_write, mapping, output_root) if to_write else 0
    mapping.save()  # _run_redaction_and_save() does this itself, but not when every group was skipped

    # Scanned/no-PII files carry no identity, so they cannot be attributed to
    # a patient -- copied once, reported at batch level (same as the CLI).
    scanned_pairs = _copy_scanned(scanned, output_root)

    patients: List[Dict[str, Any]] = []
    for group, anon_id, is_new, pairs, repeat_report in prepared:
        if repeat_report["skipped"]:
            patients.append({
                "success": True,
                "study": "kfre",
                "source": f"{group.identity_key_kind} {group.identity_key}",
                "source_path": os.path.dirname(group.files[0].src_path) if group.files else parent_dir,
                "anon_id": anon_id,
                "is_new_id": False,
                "identity_kind": group.identity_key_kind,
                "output_dir": output_root,
                "mapping_csv_path": mapping_csv_path,
                "errors": [],
                "warnings": _conflict_warnings(repeat_report, anon_id),
                "repeat": repeat_report,
                "failed_pdfs": [],
                "preview_pairs": _only_written_pairs(pairs),  # the earlier run's output, left as it is
            })
            continue

        written = _only_written_pairs(pairs, write_started)
        if len(written) == len(pairs) and repeat_report["fingerprint"]:
            mapping.set_fingerprint(group.identity_key, repeat_report["fingerprint"])
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
            "warnings": _conflict_warnings(repeat_report, anon_id),
            "repeat": repeat_report,
            "failed_pdfs": [],
            "preview_pairs": written,
        })

    mapping.save()

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


def process_package(
    paths: List[str],
    is_folder: bool,
    study_override: Optional[str] = None,
    mapping_csv: Optional[str] = None,
    output_dir: Optional[str] = None,
) -> Dict[str, Any]:
    """
    paths: a single-element list (folder path, is_folder=True) or a flat
    list of individually-selected file paths (is_folder=False).
    study_override: "egfr" | "kfre" | None (None = auto-detect).
    mapping_csv: the operator's chosen ID-mapping CSV, or None for the
        app-data default. Carrying the right one forward is what makes a
        returning patient resolve to their existing Anonymized ID instead
        of being issued a second one.
    output_dir: destination for anonymized output, or None for app-data.
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
                    get_mapping_csv_path("egfr", mapping_csv, output_dir)), [])
                _kind, values = _distinct_identity_values(probe)
                if len(values) > 1:
                    result = process_egfr_batch(package_dir, mapping_csv, output_dir)
                    result["study_detected"] = study
                    return result
            else:
                probe_records = [kf.classify_input_file(p) for p in ac.find_pdfs_recursive(package_dir)]
                probe_groups = kf.group_by_identity(
                    [r for r in probe_records if r.template in ("A", "B") and r.identity_key is not None]
                )
                if len(probe_groups) > 1:
                    result = process_kfre_batch(package_dir, mapping_csv, output_dir)
                    result["study_detected"] = study
                    return result

        if study == "egfr":
            result = process_egfr_package(package_dir, mapping_csv, output_dir)
        else:
            result = process_kfre_package(package_dir, mapping_csv, output_dir)
        result["study_detected"] = study
        return result
    finally:
        if staging_dir and os.path.isdir(staging_dir):
            shutil.rmtree(staging_dir, ignore_errors=True)


def describe_session(
    mapping_csv: Optional[str] = None,
    output_dir: Optional[str] = None,
    study: str = "egfr",
) -> Dict[str, Any]:
    """
    What this run would actually use, and what the mapping file already
    holds, WITHOUT writing anything.

    Two different situations look identical until you inspect the file, and
    confusing them is the costly mistake:

      * the file already has rows  -> this is a continuing ID space. A
        patient whose CR No / Lab No. is in there gets their EXISTING
        Anonymized ID back (a follow-up visit), and any new ID minted is
        checked against every ID already in the file.
      * the file is absent or empty -> a fresh ID space. Nothing to match
        against, so every patient here is new by definition.

    Reporting the row count up front lets the operator notice that they
    pointed at an empty file when they meant to continue a populated one,
    which would otherwise show up much later as duplicate IDs for one
    patient.
    """
    resolved_csv = get_mapping_csv_path(study, mapping_csv, output_dir)
    exists = os.path.exists(resolved_csv)

    rows = 0
    known_ids = 0
    error = None
    if exists:
        try:
            store = ac.MappingStore.load_or_create(resolved_csv)
            rows = len(store.rows)
            known_ids = len({r.get("Anonymized ID", "").strip() for r in store.rows if r.get("Anonymized ID", "").strip()})
        except Exception as exc:  # noqa: BLE001 -- surfaced, not raised: a bad file is the operator's to fix
            error = str(exc)

    return {
        "study": study,
        "mapping_csv": resolved_csv,
        "mapping_csv_exists": exists,
        "mapping_csv_is_default": not bool(mapping_csv),
        "mapping_csv_beside_output": bool(not mapping_csv and output_dir),
        "existing_patients": rows,
        "existing_ids": known_ids,
        "continuing": bool(exists and rows),
        "output_dir": output_root_path(study, output_dir),
        "output_is_default": not bool(output_dir),
        "error": error,
    }


def get_app_info() -> Dict[str, Any]:
    return {
        "hospital_code": HOSPITAL_CODE,
        "hospital_name": HOSPITAL_NAME,
        "app_data_dir": get_app_data_dir(),
        "egfr_mapping_csv": get_mapping_csv_path("egfr"),
        "kfre_mapping_csv": get_mapping_csv_path("kfre"),
        "egfr_output_dir": output_root_path("egfr"),
        "kfre_output_dir": output_root_path("kfre"),
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

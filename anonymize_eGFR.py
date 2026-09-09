#!/usr/bin/env python3
"""
anonymize_eGFR.py -- PII anonymization for the NIMS "eGFR" hospital
research study.

WHAT THIS SCRIPT DOES
----------------------
Walks every patient folder under --input-dir (default: "eGFR", a sibling of
this script) and produces an anonymized copy of everything under
--output-dir (default: "eGFR_anonymized"):

  * Left/Right-kidney ultrasound *.DCM files:
      - the burned-in top banner (rows [0, RegionLocationMinY0) across the
        full image width) is zeroed out -- Y0 is read PER FILE from DICOM
        tag (0018,6011)[0].RegionLocationMinY0, never hardcoded;
      - identifying DICOM metadata tags are cleared (PatientName/PatientID
        are instead SET to the new Anonymized ID, not blanked);
      - StudyInstanceUID / SeriesInstanceUID / SOPInstanceUID (+ the
        file_meta MediaStorageSOPInstanceUID mirror) are regenerated, with
        every ORIGINAL UID value consistently mapped to the SAME new UID
        everywhere it recurs within that patient's files (so cross-linkage
        back to an identified copy is impossible even if both ever leaked);
      - private tags are stripped.
    Clinically-relevant fields (PatientSex, Modality, StudyDate/SeriesDate/
    ContentDate + their Time counterparts, Rows/Columns, the pixel data
    below the banner, and the ultrasound-region geometry) are preserved.

  * Clinical-report PDFs (two different templates, detected from CONTENT
    only -- never from filename; see anon_common.detect_report_template):
      - Template A ("NIMS in-house / Department of Biochemistry", used by
        the 5 CKD-cohort patients): "Patient Name", "Ward" (or "Ward/OPD"),
        "Room/Bed", "Lab/Study No." (a per-sample accession number -- adds
        no follow-up-tracking value beyond CR No + the preserved dates, so
        erased rather than kept; this also removes its duplicate occurrence
        embedded inside "Sample Type/No", e.g. "Serum/260905R0546"),
        "Validated By" (the validating provider's name+credentials), and
        "Clinician" (only if ever populated with a real value, not the
        "--" placeholder seen in every real sample) are located via
        page.search_for() and TRUELY redacted -- blanked, not just painted
        over (page.add_redact_annot + apply_redactions deletes the
        underlying glyphs). "CR No" is handled differently: it is REPLACED
        with the new Anonymized ID (e.g. "CR No : QXMD7192") rather than
        blanked, via the same true-redaction mechanism (add_redact_annot's
        built-in text= support), so the field still reads as a normal
        (now-pseudonymous) value -- mirroring how the DICOM PatientID/
        PatientName tags are also SET to the Anonymized ID rather than
        cleared.
      - Template B ("Dr Lal PathLabs / SUJATHA DIAGNOSTICS", used by the 5
        Normal-cohort patients): "Name" and "Ref By" (referring physician)
        are blanked the same way on every page (both templates repeat their
        header on every page this study's files use); "Lab No." (this
        template's identity key, no CR No field exists) is REPLACED with
        the new Anonymized ID the same way as Template A's CR No. Every
        text span rendered in the 'IDAutomation2D' / '3of9Barcode'
        specialty fonts (these encode the same Lab No. a second time, as a
        scannable barcode glyph run) is still fully BLANKED, not
        replaced-in-kind -- located by font name via get_text('dict')
        rather than search_for(), because search_for()'s rect for barcode
        glyphs was found to under-cover the rendered ink. Also erased:
        the "Processed at" lab address (text, anchored dynamically off its
        label), the top registration-office address line and bottom
        customer-care contact band (both baked into the page background
        raster, fixed page-relative geometry), a boilerplate contact line
        repeated as real text inside the page-3 disclaimer box, the QR
        code (also baked into the background), and the validating
        pathologist's signature block (a signature image + printed name/
        credential lines, located dynamically by image shape + the "End of
        report" text anchor -- never a hardcoded name, so it keeps working
        if a future batch is validated by someone else).
    Lab values, units, reference ranges, dates, and facility/department
    NAMES (as opposed to full addresses) are preserved untouched. Every
    redacted page is stamped with a small fixed-position "Anonymized -
    Subject ID: <AnonID>" label.

  * The scanned Case Record Form ("<NP> CRF.pdf"): identified purely by
    content (zero/near-zero extractable text AND every page is a full-page
    embedded raster image -- see anon_common.classify_pdf), NEVER by the
    "CRF" filename substring. Confirmed to carry no patient-name field, so
    it is copied through byte-for-byte, just renamed with the new
    Anonymized ID. If a future batch ever includes a "CRF"-shaped file that
    DOES carry extractable text, this script refuses to silently pass it
    through -- see the "unknown template" handling below.

IDENTITY KEY (never trust the filename OR the DICOM tags)
-----------------------------------------------------------
DICOM files are grouped by the physical patient folder they live in (folder
name matched loosely via a NP(\\d+) regex, tolerant of inconsistent spacing/
casing) -- NEVER by the DICOM PatientName/PatientID tag, which is a
per-exam-session value a technologist can mistype (confirmed: NP1's own DCM
files carry PatientName="NP12", an operator typo; no "NP12" patient folder
exists). The reliable identity key is extracted from that folder's
clinical-report PDF content: the "CR No" field for Template A, or -- for
Template B, which has no CR No field at all -- the "Lab No." field
(prefixed "Lab No.:<value>" in the mapping CSV so it is visibly
distinguishable from a real CR No; NOTE this key is a per-visit accession
number, not a guaranteed-stable MRN, unlike CR No).

ANONYMIZED-ID MAPPING CSV -- CONFIDENTIALITY
-----------------------------------------------
The --mapping-csv file (columns: "S. No", "Original CR No", "Anonymized
ID", "Name") is the ONLY artifact in this workflow that still links an
Anonymized ID back to a real patient identity. It is NEVER copied into
--output-dir and must be kept access-restricted at all times. See
anon_common.CONFIDENTIALITY_NOTICE (also printed at the end of every run).

USAGE
-----
    python3 anonymize_eGFR.py \\
        --input-dir eGFR \\
        --output-dir eGFR_anonymized \\
        --mapping-csv /secure/path/eGFR_mapping.csv

If --mapping-csv is omitted, the script prompts for a path interactively.
--input-dir is NEVER written to or mutated by this script.
"""

from __future__ import annotations

import argparse
import copy
import glob
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field as dc_field
from typing import Dict, List, Optional, Tuple

import pydicom
from pydicom.uid import generate_uid

try:
    import pymupdf as fitz
except ImportError:  # pragma: no cover
    import fitz  # type: ignore

import anon_common as ac

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_MAPPING_CSV = os.path.join(SCRIPT_DIR, "eGFR_anony_Mapping.csv")
OUTPUT_DIR_STUDY_PREFIX = "eGFR"

NP_FOLDER_RE = re.compile(r"NP\s*0*(\d+)", re.IGNORECASE)
KIDNEY_SUBFOLDER_RE = re.compile(r"kidney", re.IGNORECASE)

# DICOM tags that must be cleared outright (set to an empty value), keyed by
# pydicom keyword. PatientName / PatientID are handled separately (set to
# the new Anonymized ID, not blanked).
DICOM_CLEAR_KEYWORDS = [
    "PatientBirthDate",
    "InstitutionName",
    "InstitutionalDepartmentName",
    "ReferringPhysicianName",
    "PerformingPhysicianName",
    "OperatorsName",
    "StationName",
    "DeviceSerialNumber",
    "AccessionNumber",
    "OtherPatientIDs",
    "PatientComments",
    "AdditionalPatientHistory",
    "PatientAddress",
    # Addition surfaced during validation: embeds a probe serial number
    # (e.g. 'C5-1s,HP8M32086553'), same rationale as DeviceSerialNumber.
    "TransducerData",
    # FIX (adversarial-audit MINOR finding): these four tags are present
    # (empty-valued in every one of the 101 real source files checked, but
    # free-text-capable) and were previously covered by neither this
    # explicit list nor the Physician/Operator regex sweep below -- a
    # future acquisition where a technologist enters free text into any of
    # them (e.g. a clinical note in ImageComments, a diagnosis string in
    # AdmittingDiagnosesDescription) would have passed through untouched
    # with no warning. AdmissionID/EthnicGroup are identifying-class
    # per DICOM PS3.15 Confidentiality Profile; AdmittingDiagnosesDescription
    # and ImageComments are free-text fields that can carry patient-supplied
    # or clinician-supplied identifying content.
    "AdmissionID",
    "AdmittingDiagnosesDescription",
    "ImageComments",
    "EthnicGroup",
]

# Generic sweep patterns (in addition to the explicit list above) so any
# unforeseen *Physician*Name / *Operator* tag is caught too.
DICOM_CLEAR_KEYWORD_PATTERNS = [
    re.compile(r"Physician.*Name", re.IGNORECASE),
    re.compile(r"Operator", re.IGNORECASE),
]


# ==========================================================================
# Patient / kidney-subfolder discovery
# ==========================================================================


def find_patient_folders(input_dir: str) -> List[str]:
    entries = []
    for name in sorted(os.listdir(input_dir)):
        full = os.path.join(input_dir, name)
        if os.path.isdir(full) and NP_FOLDER_RE.search(name):
            entries.append(full)
    return entries


def find_kidney_subfolders(patient_dir: str) -> List[str]:
    out = []
    for name in sorted(os.listdir(patient_dir)):
        full = os.path.join(patient_dir, name)
        if os.path.isdir(full) and KIDNEY_SUBFOLDER_RE.search(name):
            out.append(full)
    return out


def find_pdfs(patient_dir: str) -> List[str]:
    """Delegates to ac.find_pdfs_recursive() -- walks the whole patient
    folder (not just its top level), so report PDFs nested inside named
    subfolders (e.g. the data-collection portal's "Ultrasound_Reports/" /
    "Lab_Reports/" convention) are found the same as this repo's own flat
    per-patient sample layout. Classification stays content-based either
    way (see anon_common.classify_pdf/detect_report_template)."""
    return ac.find_pdfs_recursive(patient_dir)


def find_dcm_files(kidney_dir: str) -> List[str]:
    out = []
    for name in sorted(os.listdir(kidney_dir)):
        if name.lower().endswith(".dcm"):
            out.append(os.path.join(kidney_dir, name))
    return out


def anon_output_filename(anon_id: str, original_path: str) -> str:
    """Derive a clean, human-readable output filename that carries the
    Anonymized ID instead of the original NP-token. This is a NAMING
    convenience only -- it plays no role in template/PII detection, which
    is always content-based."""
    base = os.path.basename(original_path)
    stripped = NP_FOLDER_RE.sub("", base, count=1).strip()
    stripped = stripped.lstrip("_- ").strip()
    if not stripped:
        stripped = base
    return f"{anon_id}_{stripped}"


# ==========================================================================
# Per-patient-folder record
# ==========================================================================


@dataclass
class PatientRecord:
    folder: str
    folder_label: str
    identity_key: Optional[str] = None
    identity_kind: Optional[str] = None  # "CR No" or "Lab No."
    name: Optional[str] = None
    anon_id: Optional[str] = None
    is_new_id: bool = False
    redacted_pdfs: List[str] = dc_field(default_factory=list)
    passthrough_pdfs: List[str] = dc_field(default_factory=list)
    failed_pdfs: List[Tuple[str, str]] = dc_field(default_factory=list)
    dcm_count: int = 0
    errors: List[str] = dc_field(default_factory=list)
    # Working state carried from resolve_patient_identity() to
    # write_patient_outputs() -- see the "two-pass" note above
    # process_patient_folders_two_pass() for why identity resolution and
    # file writing are two separate passes (the auto-numbered --output-dir
    # name depends on how many NEW mapping-CSV rows this run mints, which
    # is only known once every folder's identity has been resolved).
    a_results: List[Tuple[str, "ac.TemplateAFields"]] = dc_field(default_factory=list)
    b_results: List[Tuple[str, "ac.TemplateBFields"]] = dc_field(default_factory=list)
    scanned_paths: List[str] = dc_field(default_factory=list)


# ==========================================================================
# PDF handling
# ==========================================================================


def _assert_all_targets_redacted(
    src_path: str, per_page_hit_counts: Dict[str, List[int]], page_count: int
) -> None:
    """
    FIX (adversarial-audit CRITICAL finding, second half): redact_exact_text_
    values() returns a per-call hit count, but both callers used to discard
    it entirely -- nothing anywhere verified that every extracted PII target
    string was actually located and queued for redaction. Combined with the
    multi-line field-extraction bug (also fixed), that meant a target value
    could silently fail to be found at all (or be found on fewer pages than
    it appears on) while the run still reported "OK" / 0 errors.

    FIX #2 (adversarial-audit MODERATE finding, 2026-09): the first version
    of this guard took a single SUMMED hit count per target (added up across
    every page) and compared that sum to page_count. That is NOT the
    per-page guarantee this docstring (and the known_limitations write-up)
    claimed: a target found twice on page 0 and zero times on page 1 summed
    to 2 >= page_count=2 and passed silently, even though page 1's
    occurrence was never located/redacted. Reproduced end-to-end with a
    synthetic 2-page Template-A PDF (CR No twice-findable on page 0, once
    unfindable -- spaced-out digits -- on page 1): the old guard did not
    fire and the saved output's page 1 still carried the CR No verbatim.

    This function now takes a PER-PAGE hit-count list for each target (one
    integer per page, in page order -- see callers) and requires EVERY
    single page's count to be >= 1 for every non-empty target. A page with
    zero hits for a target can no longer be masked by a surplus of hits on
    a different page. If any target has one or more zero-hit pages, raise
    -- the caller's except-block records this as a failed_pdfs entry
    (surfacing as "NEEDS REVIEW" in the run summary and a non-zero exit
    code) and NO output file is written for it, so an incompletely-redacted
    PDF can never reach --output-dir.
    """
    deficient = {
        t: [i for i, c in enumerate(counts) if c < 1]
        for t, counts in per_page_hit_counts.items()
    }
    deficient = {t: zero_pages for t, zero_pages in deficient.items() if zero_pages}
    if deficient:
        raise ValueError(
            f"{os.path.basename(src_path)}: the following extracted PII value(s) were NOT "
            f"found on every page (0-indexed page number(s) with ZERO hits, out of "
            f"{page_count} page(s) total: {deficient!r}) and may not be fully redacted on "
            f"those pages. Refusing to save an anonymized copy that could still leak this "
            f"value -- inspect the source PDF manually."
        )


def redact_template_a_pdf(src_path: str, out_path: str, fields: "ac.TemplateAFields", anon_id: str) -> None:
    doc = fitz.open(src_path)
    try:
        blank_targets = fields.blank_redaction_targets()
        identity_value = fields.identity_target()  # CR No -- REPLACED with anon_id, not blanked
        # Per-page hit counts (list, one entry per page, in page order) --
        # NOT summed across pages, see _assert_all_targets_redacted(). Covers
        # every PII target (blank AND replace) so the strict guard still
        # verifies the identity field was actually located+redacted on every
        # page, exactly as it did before the blank-vs-replace split.
        all_targets = list(blank_targets) + ([identity_value] if identity_value else [])
        per_page_hit_counts: Dict[str, List[int]] = {t: [] for t in all_targets}
        for page in doc:
            for t in blank_targets:
                per_page_hit_counts[t].append(ac.redact_exact_text_values(page, [t]))
            if identity_value:
                per_page_hit_counts[identity_value].append(
                    ac.redact_and_replace_value(page, identity_value, anon_id)
                )
            ac.apply_redactions_and_stamp(page, anon_id)
        _assert_all_targets_redacted(src_path, per_page_hit_counts, len(doc))
        ac.save_redacted_pdf(doc, out_path)
    finally:
        doc.close()


def redact_template_b_pdf(src_path: str, out_path: str, fields: "ac.TemplateBFields", anon_id: str) -> None:
    doc = fitz.open(src_path)
    try:
        blank_targets = fields.blank_redaction_targets()
        identity_value = fields.identity_target()  # Lab No. -- REPLACED with anon_id, not blanked
        all_targets = list(blank_targets) + ([identity_value] if identity_value else [])
        per_page_hit_counts: Dict[str, List[int]] = {t: [] for t in all_targets}
        for page in doc:
            for t in blank_targets:
                per_page_hit_counts[t].append(ac.redact_exact_text_values(page, [t]))
            # Boilerplate lab contact line repeated as real text inside the
            # page-3 disclaimer box (harmless to search for on every page --
            # simply won't match on pages that don't carry it).
            ac.redact_exact_text_values(page, [ac.TEMPLATE_B_BOILERPLATE_CONTACT_LINE])
            if identity_value:
                per_page_hit_counts[identity_value].append(
                    ac.redact_and_replace_value(page, identity_value, anon_id)
                )
            # Specialty-font barcode/2D-barcode spans (IDAutomation2D,
            # 3of9Barcode): these render Lab No. a second time as a scannable
            # barcode glyph run, not as plain text -- always fully blanked
            # (never replaced-in-kind; drawing a new barcode isn't attempted)
            # via font-bbox matching, which was validated to give full ink
            # coverage regardless of search_for()'s rect (search_for()
            # undershoots these fonts' true rendered ink on both edges).
            ac.redact_spans_by_font(page, ["IDAutomation", "Barcode", "3of9"])
            # Geometric safety-net over the QR-code-look-alike footer band,
            # on whichever page carries it.
            qr_rect = ac.find_template_b_qr_redaction_rect(page)
            if qr_rect is not None:
                ac.queue_region_redaction(page, qr_rect)
            # Provider addresses (per explicit request -- "any kind of
            # address"): the top registration-office line and bottom
            # customer-care contact band are baked into the background
            # raster on every page (fixed geometry); the "Processed at"
            # lab-address value is real text, anchored dynamically.
            ac.queue_region_redaction(page, ac.TEMPLATE_B_TOP_ADDRESS_RECT)
            ac.queue_region_redaction(page, ac.TEMPLATE_B_FOOTER_CONTACT_RECT)
            processed_at_rect = ac.find_template_b_processed_at_address_rect(page)
            if processed_at_rect is not None:
                ac.queue_region_redaction(page, processed_at_rect)
            # Pathologist signature (image + printed name/credentials), per
            # explicit request -- "any kind of signature" -- on whichever
            # page carries it.
            signature_rect = ac.find_template_b_signature_redaction_rect(page)
            if signature_rect is not None:
                ac.queue_region_redaction(page, signature_rect)
            ac.apply_redactions_and_stamp(page, anon_id)
        _assert_all_targets_redacted(src_path, per_page_hit_counts, len(doc))
        ac.save_redacted_pdf(doc, out_path)
    finally:
        doc.close()


# Parsed-PDF cache, keyed by identity-on-disk rather than path alone so an
# edited or replaced file is never served stale. Process-local and never
# persisted: it exists only to stop the SAME file being parsed repeatedly
# within one run.
#
# The desktop app resolves identity twice over the same PDFs -- once to
# decide whether a selection holds one patient or many, then again per
# patient -- and PDF parsing was measured as the single largest cost of a
# batch (6.7s + 6.4s of a 17.3s run, against 4.2s for all the image work).
# The second pass is now free.
_CLASSIFY_CACHE: Dict[Tuple[str, int, int], Tuple[str, object]] = {}


def classify_and_extract(pdf_path: str):
    """Returns one of:
      ("scanned", None)
      ("A", TemplateAFields)
      ("B", TemplateBFields)
      ("unknown", None)

    Result is cached per file (see _CLASSIFY_CACHE). Callers get a shallow
    COPY of the fields object, never the cached instance, so that a caller
    which mutates what it is handed cannot corrupt what the next caller
    sees.
    """
    key: Optional[Tuple[str, int, int]] = None
    try:
        st = os.stat(pdf_path)
        key = (os.path.abspath(pdf_path), st.st_mtime_ns, st.st_size)
    except OSError:
        key = None  # unreadable: fall through and let the real work report it

    if key is not None:
        cached = _CLASSIFY_CACHE.get(key)
        if cached is not None:
            kind, fields = cached
            return kind, (copy.copy(fields) if fields is not None else None)

    cls = ac.classify_pdf(pdf_path)
    if cls.is_scanned_no_pii:
        result = ("scanned", None)
    else:
        doc = fitz.open(pdf_path)
        try:
            templ = ac.detect_report_template(doc)
            if templ == "A":
                result = ("A", ac.extract_template_a_fields(doc))
            elif templ == "B":
                result = ("B", ac.extract_template_b_fields(doc))
            else:
                result = ("unknown", None)
        finally:
            doc.close()

    if key is not None:
        _CLASSIFY_CACHE[key] = result
    kind, fields = result
    return kind, (copy.copy(fields) if fields is not None else None)


# ==========================================================================
# DICOM handling
# ==========================================================================


def scrub_dicom_dataset(ds: "pydicom.Dataset", anon_id: str) -> None:
    """In-place clear of identifying tags. PatientName/PatientID are SET to
    the new Anonymized ID (not blanked) so the file still carries a
    consistent, non-identifying subject key."""
    if "PatientName" in ds:
        ds.PatientName = anon_id
    if "PatientID" in ds:
        ds.PatientID = anon_id

    for kw in DICOM_CLEAR_KEYWORDS:
        if kw in ds:
            try:
                setattr(ds, kw, "")
            except Exception:
                ds.data_element(kw).value = ""

    for elem in list(ds):
        try:
            keyword = elem.keyword
        except Exception:
            continue
        if not keyword or keyword in ("PatientName", "PatientID"):
            continue
        if keyword in DICOM_CLEAR_KEYWORDS:
            continue
        if any(pat.search(keyword) for pat in DICOM_CLEAR_KEYWORD_PATTERNS):
            # FIX (adversarial-audit MODERATE finding): this used to be a
            # bare `except Exception: pass` with no fallback and no
            # logging -- if clearing ever failed here, the tag's original
            # (possibly identifying) value was silently left untouched and
            # ds.save_as() still ran. Mirror the explicit DICOM_CLEAR_
            # KEYWORDS loop's fail-safe two-step (setattr, then
            # data_element(...).value = "" as a fallback) and, if BOTH
            # fail, raise -- propagating up to abort the save entirely
            # (caught by process_one_dicom_file's caller as a DICOM error)
            # rather than ever writing a file that may still carry this
            # value.
            try:
                setattr(ds, keyword, "")
            except Exception:
                try:
                    ds.data_element(keyword).value = ""
                except Exception as e:
                    raise ValueError(
                        f"Failed to clear identifying DICOM tag {keyword!r} (matched by the "
                        f"Physician/Operator sweep pattern) via both setattr and direct "
                        f"data_element assignment: {e}. Refusing to save a file that may "
                        f"still carry this value."
                    ) from e

    ds.remove_private_tags()


def redact_dicom_banner(ds: "pydicom.Dataset") -> None:
    """Zero out pixel rows [0, RegionLocationMinY0) across the full image
    width. Y0 is read PER FILE from (0018,6011)[0].RegionLocationMinY0,
    never hardcoded. Requires the dataset to already be decompressed
    (uncompressed pixel data) -- see process_one_dicom_file()."""
    seq = ds.get((0x0018, 0x6011))
    if seq is None or len(seq.value) == 0:
        raise ValueError("SequenceOfUltrasoundRegions (0018,6011) missing or empty -- cannot locate banner boundary")
    y0 = int(seq.value[0].RegionLocationMinY0)

    arr = ds.pixel_array
    arr = arr.copy()
    arr[:y0, ...] = 0
    ds.PixelData = arr.tobytes()


def process_one_dicom_file(
    src_path: str,
    out_path: str,
    anon_id: str,
    uid_map: Dict[str, str],
) -> None:
    ds = pydicom.dcmread(src_path)

    # JPEG Baseline (compressed) transfer syntax cannot hold a raw modified
    # pixel array -- decompress first (validated: pydicom hard-fails
    # otherwise with "Pixel Data hasn't been encapsulated..."). Output files
    # are consequently larger than the source (uncompressed vs originally
    # JPEG-compressed), an expected, acceptable tradeoff for correctness.
    #
    # Decoder is named explicitly rather than left to pydicom's own
    # preference order. Measured on this corpus (920x1590x3 JPEG Baseline):
    # pylibjpeg 194 ms/frame, pillow 92 ms/frame -- and pydicom picks
    # pylibjpeg when both are installed, so leaving it to choose costs 2.1x
    # on every single image. Falls back to whatever is available if pillow
    # is not, so a build without it still works, just slower.
    if ds.file_meta.TransferSyntaxUID.is_compressed:
        try:
            ds.decompress(decoding_plugin="pillow")
        except Exception:  # noqa: BLE001 -- plugin absent/unusable, not a data problem
            ds.decompress()

    redact_dicom_banner(ds)
    scrub_dicom_dataset(ds, anon_id)

    # Regenerate Study/Series/SOP UIDs, mapping every ORIGINAL UID value to
    # the SAME new UID everywhere it recurs (data-driven -- does not assume
    # a particular per-side/per-study grouping granularity; whatever the
    # source file actually shares, the output shares identically).
    for uid_tag in ("StudyInstanceUID", "SeriesInstanceUID"):
        old_uid = str(getattr(ds, uid_tag))
        new_uid = uid_map.get(old_uid)
        if new_uid is None:
            new_uid = generate_uid()
            uid_map[old_uid] = new_uid
        setattr(ds, uid_tag, new_uid)

    new_sop_uid = generate_uid()
    ds.SOPInstanceUID = new_sop_uid
    ds.file_meta.MediaStorageSOPInstanceUID = new_sop_uid

    parent = os.path.dirname(os.path.abspath(out_path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    ds.save_as(out_path)


# ==========================================================================
# Main per-patient orchestration
# ==========================================================================


# ---------------------------------------------------------------------------
# TWO-PASS DESIGN: identity resolution is deliberately a separate pass from
# file writing. --output-dir's auto-numbered name ("eGFR_<start>_<end>") is
# derived from how many NEW rows this run adds to the mapping CSV, which is
# only known once every patient folder's identity key has been resolved
# against (or newly minted into) the mapping store -- so ALL folders must be
# resolved before --output-dir can be computed and any file is written.
# main() calls resolve_patient_identity() for every folder first, THEN
# computes --output-dir, THEN calls write_patient_outputs() for every folder.
# ---------------------------------------------------------------------------


def resolve_patient_identity(
    patient_dir: str,
    mapping: "ac.MappingStore",
    log: List[str],
) -> PatientRecord:
    """
    Pass 1: classify every PDF in this patient folder, derive the stable
    identity key (CR No / Lab No.), and look up or mint its Anonymized ID in
    `mapping` (in-memory only -- does not write PDFs/DICOM and does not save
    the mapping CSV to disk). Returns a PatientRecord with identity_key/
    anon_id populated (identity_key is None if this folder must be skipped)
    plus the classified file lists needed by write_patient_outputs().
    """
    folder_label = os.path.basename(patient_dir.rstrip(os.sep))
    rec = PatientRecord(folder=patient_dir, folder_label=folder_label)

    pdfs = find_pdfs(patient_dir)
    unknown_paths: List[str] = []

    # Left sequential deliberately. Classification is expensive (~385 ms per
    # PDF, over half a second for a scanned CRF, since classify_pdf reads
    # every page's text and tests whether each page is a full-page image),
    # so threading it looks obvious -- but it was tried and measured at no
    # gain (18.1s vs 18.3s over the same batch, inside noise). Unlike the
    # JPEG decoder, PyMuPDF appears to hold the GIL through text
    # extraction, so the work serialises anyway. Not worth carrying
    # concurrency here for nothing.
    for pdf_path in pdfs:
        kind, extracted = classify_and_extract(pdf_path)
        if kind == "scanned":
            rec.scanned_paths.append(pdf_path)
        elif kind == "A":
            rec.a_results.append((pdf_path, extracted))
        elif kind == "B":
            rec.b_results.append((pdf_path, extracted))
        else:
            unknown_paths.append(pdf_path)

    # ---- derive the canonical identity key for this folder ----
    identity_key = None
    identity_kind = None
    name = None

    if rec.a_results:
        cr_nos = {f.cr_no for _, f in rec.a_results if f.cr_no}
        names = {f.patient_name for _, f in rec.a_results if f.patient_name}
        if len(cr_nos) > 1:
            msg = f"[{folder_label}] CONFLICTING CR No values across report PDFs: {cr_nos} -- refusing to guess"
            print("ERROR:", msg)
            rec.errors.append(msg)
        elif not cr_nos:
            msg = f"[{folder_label}] Template-A PDF(s) found but no CR No could be extracted"
            print("ERROR:", msg)
            rec.errors.append(msg)
        else:
            identity_key = next(iter(cr_nos))
            identity_kind = "CR No"
            if len(names) > 1:
                log.append(f"[{folder_label}] WARNING: multiple distinct Patient Name values found: {names}")
            name = next(iter(names)) if names else ""
    elif rec.b_results:
        # Exactly one Template-B file is expected per eGFR Normal-cohort
        # patient, but handle >1 defensively the same way.
        lab_nos = {f.lab_no for _, f in rec.b_results if f.lab_no}
        names = {f.name for _, f in rec.b_results if f.name}
        if len(lab_nos) > 1:
            msg = f"[{folder_label}] CONFLICTING Lab No. values across report PDFs: {lab_nos} -- refusing to guess"
            print("ERROR:", msg)
            rec.errors.append(msg)
        elif not lab_nos:
            msg = f"[{folder_label}] Template-B PDF(s) found but no Lab No. could be extracted"
            print("ERROR:", msg)
            rec.errors.append(msg)
        else:
            identity_key = next(iter(lab_nos))
            identity_kind = "Lab No."
            name = next(iter(names)) if names else ""

    if unknown_paths:
        for p in unknown_paths:
            msg = (
                f"[{folder_label}] {os.path.basename(p)}: contains extractable text but matches "
                f"NEITHER known report template (no 'CR No' / 'Department of Biochemistry' anchor, "
                f"no 'Lab No.'+'Name'+Dr-Lal/Sujatha anchor). REFUSING to guess how to redact it or "
                f"to pass it through unredacted -- flagging for MANUAL REVIEW."
            )
            print("!" * 100)
            print("UNKNOWN PDF TEMPLATE -- POSSIBLE UNHANDLED PII SOURCE:", msg)
            print("!" * 100)
            rec.failed_pdfs.append((p, "unknown template, not redacted, not copied"))
            rec.errors.append(msg)

    if identity_key is None:
        msg = f"[{folder_label}] Could not establish a stable identity key for this folder -- SKIPPING folder entirely (no output written)."
        print("ERROR:", msg)
        rec.errors.append(msg)
        log.append(msg)
        return rec

    rec.identity_key = identity_key
    rec.identity_kind = identity_kind
    rec.name = name

    mapping_key = identity_key if identity_kind == "CR No" else f"Lab No.:{identity_key}"
    anon_id, is_new = mapping.get_or_assign(mapping_key, name or "")
    rec.anon_id = anon_id
    rec.is_new_id = is_new

    action = "ASSIGNED NEW" if is_new else "REUSED EXISTING"
    print(f"[{folder_label}] identity={identity_kind}:{identity_key} name={name!r} -> {action} Anonymized ID {anon_id}")
    log.append(f"[{folder_label}] identity={identity_kind}:{identity_key} name={name!r} -> {action} Anonymized ID {anon_id}")

    return rec


def write_patient_outputs(rec: PatientRecord, output_dir: str) -> PatientRecord:
    """
    Pass 2: given an already-resolved PatientRecord (anon_id populated by
    resolve_patient_identity()), actually redact/write the report PDFs,
    pass through scanned files, and anonymize the DICOM files, into
    `output_dir` (now known -- see the two-pass note above). Mutates and
    returns the same `rec`. A record with identity_key is None (skipped in
    pass 1) is returned unchanged -- caller should not invoke this for those.
    """
    if rec.identity_key is None:
        return rec

    patient_dir = rec.folder
    anon_id = rec.anon_id
    out_patient_dir = os.path.join(output_dir, anon_id)

    # ---- redact + write the report PDFs ----
    for src_path, fields in rec.a_results:
        out_name = anon_output_filename(anon_id, src_path)
        out_path = os.path.join(out_patient_dir, out_name)
        try:
            redact_template_a_pdf(src_path, out_path, fields, anon_id)
            rec.redacted_pdfs.append(out_path)
            print(f"    redacted (Template A): {os.path.basename(src_path)} -> {out_path}")
        except Exception as e:
            rec.failed_pdfs.append((src_path, f"redaction error: {e}"))
            print(f"    ERROR redacting {src_path}: {e}")

    for src_path, fields in rec.b_results:
        out_name = anon_output_filename(anon_id, src_path)
        out_path = os.path.join(out_patient_dir, out_name)
        try:
            redact_template_b_pdf(src_path, out_path, fields, anon_id)
            rec.redacted_pdfs.append(out_path)
            print(f"    redacted (Template B): {os.path.basename(src_path)} -> {out_path}")
        except Exception as e:
            rec.failed_pdfs.append((src_path, f"redaction error: {e}"))
            print(f"    ERROR redacting {src_path}: {e}")

    # ---- pass through scanned / no-PII files (e.g. the CRF) unchanged ----
    for src_path in rec.scanned_paths:
        out_name = anon_output_filename(anon_id, src_path)
        out_path = os.path.join(out_patient_dir, out_name)
        ac.safe_copy_bytes(src_path, out_path)
        rec.passthrough_pdfs.append(out_path)
        print(f"    passthrough (scanned, no extractable PII): {os.path.basename(src_path)} -> {out_path}")

    # ---- DICOM files ----
    #
    # Decoding dominates this pass: measured at ~89% of per-patient time,
    # against ~5% for the disk write. That work happens inside the JPEG
    # decoder's C code, which releases the GIL, so plain threads give a
    # real speedup here without the multiprocessing machinery (which would
    # also have to survive being frozen by PyInstaller on Windows).
    #
    # uid_map is built COMPLETELY UP FRONT, single-threaded, and is only
    # read afterwards. Populating it from inside the workers would race:
    # two threads can both find a Study/Series UID absent and mint two
    # different replacements for it, silently splitting one series into
    # two in the output. Reading headers is cheap (~2 ms/file, no pixel
    # data) so this costs almost nothing.
    jobs: List[tuple] = []
    for kidney_dir in find_kidney_subfolders(patient_dir):
        side_name = os.path.basename(kidney_dir.rstrip(os.sep))
        out_kidney_dir = os.path.join(out_patient_dir, side_name)
        for dcm_path in find_dcm_files(kidney_dir):
            jobs.append((dcm_path, os.path.join(out_kidney_dir, os.path.basename(dcm_path))))

    uid_map: Dict[str, str] = {}
    for dcm_path, _out in jobs:
        try:
            header = pydicom.dcmread(dcm_path, stop_before_pixels=True)
        except Exception:  # noqa: BLE001 -- reported per file by the worker below
            continue
        for uid_tag in ("StudyInstanceUID", "SeriesInstanceUID"):
            old_uid = str(getattr(header, uid_tag, "") or "")
            if old_uid and old_uid not in uid_map:
                uid_map[old_uid] = generate_uid()

    def _one(job):
        dcm_path, out_dcm_path = job
        try:
            process_one_dicom_file(dcm_path, out_dcm_path, anon_id, uid_map)
            return (dcm_path, None)
        except Exception as e:  # noqa: BLE001
            return (dcm_path, str(e))

    # Results are collected and applied to `rec` on this thread afterwards,
    # so nothing mutates the record concurrently.
    #
    # Worker count follows the machine. Measured on a 16-core box, 10 frames
    # of this corpus: pillow 0.97s sequential -> 0.33s at 4 threads -> 0.20s
    # at 16 (5.0x). Capped at 16 because each in-flight frame holds a full
    # uncompressed copy (~4.4 MB here) and the returns flatten out, not
    # because more threads hurt.
    max_workers = min(16, (os.cpu_count() or 2), max(1, len(jobs)))
    if len(jobs) > 1 and max_workers > 1:
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            outcomes = list(pool.map(_one, jobs))
    else:
        outcomes = [_one(job) for job in jobs]

    for dcm_path, error in outcomes:
        if error is None:
            rec.dcm_count += 1
        else:
            rec.errors.append(f"DICOM error on {dcm_path}: {error}")
            print(f"    ERROR processing DICOM {dcm_path}: {error}")
    print(f"    {rec.dcm_count} DICOM file(s) anonymized across {len(find_kidney_subfolders(patient_dir))} kidney-side subfolder(s).")

    return rec


# ==========================================================================
# CLI
# ==========================================================================


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "Anonymize the NIMS eGFR study (DICOM ultrasound + clinical-report PDFs + CRF). "
            "Never writes into --input-dir. The mapping CSV is confidential re-identification "
            "material and is never copied into --output-dir."
        )
    )
    p.add_argument(
        "--input-dir",
        default=os.path.join(SCRIPT_DIR, "eGFR"),
        help="Path to the read-only source eGFR study folder (default: %(default)s)",
    )
    p.add_argument(
        "--output-dir",
        default=None,
        help=(
            "Path to write the anonymized dataset to. If omitted (the normal case), it is "
            "created automatically in this script's own directory, auto-named "
            "'eGFR_<start>_<end>' where <start>/<end> is the range of mapping-CSV record "
            "numbers (S. No) this run covers -- e.g. 'eGFR_1_20' the first time (20 patients, "
            "none yet in the mapping CSV), 'eGFR_41_55' on a later run if 40 patients were "
            "already recorded and this run adds 15 more. Pass this flag explicitly to override "
            "with a fixed path instead."
        ),
    )
    p.add_argument(
        "--mapping-csv",
        default=None,
        help=(
            "Path to the confidential Anonymized-ID mapping CSV (existing or new). If omitted "
            f"(the normal case), it defaults automatically to {DEFAULT_MAPPING_CSV!r} -- created "
            "fresh on the first run, loaded and appended to on every later run. This file is "
            "NEVER copied into --output-dir; keep it access-restricted."
        ),
    )
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)

    input_dir = os.path.abspath(args.input_dir)

    if not os.path.isdir(input_dir):
        print(f"ERROR: --input-dir {input_dir!r} is not a directory.", file=sys.stderr)
        return 2

    mapping_csv_path = ac.resolve_mapping_csv_path(args.mapping_csv, default_path=DEFAULT_MAPPING_CSV)
    mapping_csv_path = os.path.abspath(mapping_csv_path)

    mapping = ac.MappingStore.load_or_create(mapping_csv_path)
    count_before = len(mapping.rows)

    patient_folders = find_patient_folders(input_dir)
    print(f"Found {len(patient_folders)} patient folder(s) under {input_dir!r}.")

    # ---- Pass 1: resolve identity + Anonymized ID for every folder first ----
    # (no files written yet -- see the two-pass note above resolve_patient_
    # identity()). This is what lets --output-dir's auto-numbered name below
    # reflect the FINAL mapping-CSV row count for this run.
    log: List[str] = []
    records: List[PatientRecord] = []
    for patient_dir in patient_folders:
        print("=" * 80)
        rec = resolve_patient_identity(patient_dir, mapping, log)
        records.append(rec)

    count_after = len(mapping.rows)

    if args.output_dir is not None:
        output_dir = os.path.abspath(args.output_dir)
    else:
        start, end = ac.compute_ranged_output_dir_bounds(count_before, count_after)
        auto_name = f"{OUTPUT_DIR_STUDY_PREFIX}_{start}_{end}"
        output_dir = os.path.join(SCRIPT_DIR, auto_name)
        print(f"[output-dir] --output-dir not given -- auto-named {output_dir!r} "
              f"(mapping-CSV record range {start}-{end} for this run).")

    # Guard: never let output land inside input, or vice versa.
    if output_dir == input_dir or output_dir.startswith(input_dir + os.sep) or input_dir.startswith(output_dir + os.sep):
        print(
            f"ERROR: --output-dir ({output_dir}) must not be the same as or nested inside "
            f"--input-dir ({input_dir}), or vice versa. Refusing to risk mutating source data.",
            file=sys.stderr,
        )
        return 2

    location_error = ac.validate_mapping_csv_location(mapping_csv_path, input_dir, output_dir)
    if location_error:
        print(f"ERROR: {location_error}", file=sys.stderr)
        return 2

    os.makedirs(output_dir, exist_ok=True)

    # ---- Pass 2: now that --output-dir is known, actually write everything ----
    for rec in records:
        if rec.identity_key is None:
            continue
        print("=" * 80)
        write_patient_outputs(rec, output_dir)

    mapping.save()

    print("=" * 80)
    print("RUN SUMMARY")
    print("=" * 80)
    any_hard_failure = False
    for rec in records:
        status = "OK" if rec.anon_id and not rec.failed_pdfs and not rec.errors else "NEEDS REVIEW"
        if status == "NEEDS REVIEW":
            any_hard_failure = True
        print(
            f"  {rec.folder_label:20s} -> {status:12s} anon_id={rec.anon_id} "
            f"redacted_pdfs={len(rec.redacted_pdfs)} passthrough={len(rec.passthrough_pdfs)} "
            f"dcm={rec.dcm_count} failed_pdfs={len(rec.failed_pdfs)} errors={len(rec.errors)}"
        )
        for p, reason in rec.failed_pdfs:
            print(f"        FAILED: {p} ({reason})")
        for e in rec.errors:
            print(f"        ERROR: {e}")

    print()
    print(f"Anonymized output written to: {output_dir}")
    print(f"Mapping CSV: {mapping_csv_path}")
    print(f"[NOTICE] {ac.CONFIDENTIALITY_NOTICE}")

    return 1 if any_hard_failure else 0


if __name__ == "__main__":
    raise SystemExit(main())

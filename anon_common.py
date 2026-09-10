"""
anon_common.py -- shared helpers for the NIMS PII anonymization scripts
(anonymize_eGFR.py, and any future study-specific script such as a KFRE
counterpart).

This module provides:
  1. The confidential Anonymized-ID <-> real-identity mapping CSV: load,
     create, generate new collision-free IDs, and persist.
  2. Generic PDF redaction helpers built on PyMuPDF's TRUE redaction API
     (page.add_redact_annot + page.apply_redactions) -- this actually
     deletes the underlying text/glyphs, unlike drawing a box on top of
     text (which leaves the original text extractable underneath no
     matter what color the box is). Erased fields are filled white --
     ordinary blank space, not a visible black redaction box -- while
     the identity key (CR No / Lab No.) is instead replaced in-kind with
     the new Anonymized ID; see redact_exact_text_values() and
     redact_and_replace_value() below.
  3. A generic, content-based (never filename-based) "is this PDF a
     scanned image with no extractable PII text, or does it need
     redaction" classifier.

IMPORTANT -- CONFIDENTIALITY NOTE
----------------------------------
The mapping CSV produced/maintained by `load_or_create_mapping()` /
`MappingStore` is the ONLY artifact in this workflow that still links an
Anonymized ID back to a real patient name and real hospital identifier
(CR No / Lab No.). It is NOT part of the anonymized dataset and must
NEVER be copied into an anonymized output folder, emailed, committed to
a public repo, or otherwise distributed alongside the anonymized
files. Keep it access-restricted (e.g. filesystem permissions, encrypted
storage) at all times.
"""

from __future__ import annotations

import csv
import hashlib
import os
import random
import re
import string
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

try:
    import pymupdf as fitz  # PyMuPDF; "pymupdf" is the modern import name for the "fitz" API
except ImportError:  # pragma: no cover - fallback for older installs
    import fitz  # type: ignore

# --------------------------------------------------------------------------
# Mapping CSV columns (exact order, exact header text) per spec.
# --------------------------------------------------------------------------
MAPPING_CSV_FIELDS = ["S. No", "Original CR No", "Anonymized ID", "Name", "Source Fingerprint"]

# The first four columns are the mapping proper and every file must have
# them. "Source Fingerprint" is additive -- a CSV written before it existed
# loads fine and simply has no fingerprints yet -- so it is NOT required.
MAPPING_CSV_REQUIRED_FIELDS = MAPPING_CSV_FIELDS[:4]

# Content-based scanned-PDF detection thresholds (see classify_pdf()).
SCANNED_TEXT_CHAR_THRESHOLD = 20  # total extractable chars below this = "scanned"
SCANNED_IMAGE_COVERAGE_RATIO = 0.90  # an image must cover >=90% of the page area

CONFIDENTIALITY_NOTICE = (
    "Keep this mapping file access-restricted; it is the only file in this "
    "workflow that still links Anonymized IDs to real patient identities. "
    "It must NEVER be copied into the anonymized output folder."
)


# ==========================================================================
# 1. Anonymized-ID mapping CSV
# ==========================================================================


@dataclass
class MappingStore:
    """
    In-memory view of the confidential Anonymized-ID mapping CSV, with
    load/create/save and collision-free ID generation.

    Usage:
        store = MappingStore.load_or_create(path)
        anon_id, is_new = store.get_or_assign(original_key="331012601372256",
                                                name="Thamalaraju Kasi Mallesh")
        ... (repeat for every patient encountered) ...
        store.save()
    """

    path: str
    rows: List[Dict[str, str]] = field(default_factory=list)
    # Original CR No / Lab No key (exact string as stored in "Original CR No") -> Anonymized ID
    _key_to_id: Dict[str, str] = field(default_factory=dict)
    # Same key -> the row itself, so a fingerprint can be read and written
    # in place without rescanning the list.
    _key_to_row: Dict[str, Dict[str, str]] = field(default_factory=dict)
    # Every Anonymized ID already on disk OR minted earlier this run -- never reused for a
    # *different* identity, and never re-generated as a *new* ID.
    _reserved_ids: set = field(default_factory=set)
    _existed_on_disk: bool = False

    # ---- construction -----------------------------------------------------

    @classmethod
    def load_or_create(cls, path: str) -> "MappingStore":
        store = cls(path=path)
        if os.path.exists(path):
            store._existed_on_disk = True
            with open(path, "r", newline="", encoding="utf-8-sig") as f:
                reader = csv.DictReader(f)
                missing = [c for c in MAPPING_CSV_REQUIRED_FIELDS if c not in (reader.fieldnames or [])]
                if missing:
                    raise ValueError(
                        f"Mapping CSV {path!r} exists but is missing expected column(s) "
                        f"{missing}; found columns {reader.fieldnames!r}. Refusing to guess "
                        f"-- fix the header or point at a different file."
                    )
                for row in reader:
                    row = {k: (v or "") for k, v in row.items() if k is not None}
                    # An older CSV has no fingerprint column at all; fill it in
                    # rather than special-casing every later read.
                    row.setdefault("Source Fingerprint", "")
                    store.rows.append(row)
                    key = row["Original CR No"].strip()
                    aid = row["Anonymized ID"].strip()
                    if key:
                        store._key_to_id[key] = aid
                        store._key_to_row[key] = row
                    if aid:
                        store._reserved_ids.add(aid)
            print(
                f"[mapping-csv] Loaded existing mapping file {path!r} with "
                f"{len(store.rows)} existing record(s)."
            )
        else:
            # Create fresh with just the header row -- zero pre-existing IDs to check
            # against, per spec ("first time, no need to check whether the generated
            # ID already exists" beyond what's minted within this same run).
            parent = os.path.dirname(os.path.abspath(path))
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(path, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=MAPPING_CSV_FIELDS)
                writer.writeheader()
            print(f"[mapping-csv] No existing mapping file at {path!r} -- created fresh with header only.")
        return store

    # ---- ID generation ------------------------------------------------

    @staticmethod
    def _generate_candidate_id() -> str:
        """4 distinct uppercase letters (no repeats) + 4 distinct digits (no repeats)."""
        letters = "".join(random.sample(string.ascii_uppercase, 4))
        digits = "".join(random.sample(string.digits, 4))
        return letters + digits

    def _new_unique_id(self) -> str:
        for _ in range(10000):
            candidate = self._generate_candidate_id()
            if candidate not in self._reserved_ids:
                self._reserved_ids.add(candidate)
                return candidate
        raise RuntimeError("Could not generate a unique Anonymized ID after 10000 attempts.")

    # ---- lookups --------------------------------------------------------

    def get_or_assign(self, original_key: str, name: str) -> Tuple[str, bool]:
        """
        Look up `original_key` (e.g. a CR No, or "Lab No.:<value>") in the mapping.
        If already present, reuse and return (existing_id, False).
        Otherwise mint a new collision-free Anonymized ID, append a new row, and
        return (new_id, True). Does NOT write to disk -- call save() when done.
        """
        original_key = original_key.strip()
        if original_key in self._key_to_id:
            return self._key_to_id[original_key], False

        new_id = self._new_unique_id()
        s_no = len(self.rows) + 1
        row = {
            "S. No": str(s_no),
            "Original CR No": original_key,
            "Anonymized ID": new_id,
            "Name": name,
            "Source Fingerprint": "",
        }
        self.rows.append(row)
        self._key_to_id[original_key] = new_id
        self._key_to_row[original_key] = row
        return new_id, True

    # ---- source fingerprint ----------------------------------------------
    #
    # One value per patient standing for the exact set of files they were
    # last anonymized from (see fingerprint_files). It answers the question
    # the Anonymized ID cannot: a known CR No means "I have seen this
    # PATIENT", not "I have seen these DOCUMENTS", so a folder that was
    # already anonymized and is handed over a second time looks identical
    # to a genuine follow-up visit -- and gets silently redone, overwriting
    # output that was already checked.
    #
    # Kept in the mapping CSV rather than a sidecar file so there is exactly
    # one thing to carry between sessions. A separate file would have to
    # travel with the CSV to be any use, and the first symptom of forgetting
    # it is work being repeated.

    def get_fingerprint(self, original_key: str) -> str:
        row = self._key_to_row.get(original_key.strip())
        return (row or {}).get("Source Fingerprint", "").strip()

    def set_fingerprint(self, original_key: str, fingerprint: str) -> None:
        row = self._key_to_row.get(original_key.strip())
        if row is not None:
            row["Source Fingerprint"] = fingerprint

    def find_key_by_fingerprint(self, fingerprint: str) -> Optional[str]:
        """The patient already recorded against this exact set of files, if
        it is someone else. Finding one means the same documents have been
        anonymized twice under two different Anonymized IDs -- a duplicate
        subject in the dataset, not a repeat to skip."""
        if not fingerprint:
            return None
        for row in self.rows:
            if row.get("Source Fingerprint", "").strip() == fingerprint:
                return row.get("Original CR No", "").strip()
        return None

    def lookup_only(self, original_key: str) -> Optional[str]:
        return self._key_to_id.get(original_key.strip())

    # ---- persistence ------------------------------------------------------

    def save(self) -> None:
        with open(self.path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=MAPPING_CSV_FIELDS)
            writer.writeheader()
            for row in self.rows:
                writer.writerow(row)
        print(f"[mapping-csv] Saved {len(self.rows)} record(s) to {self.path!r}.")
        print(f"[mapping-csv] {CONFIDENTIALITY_NOTICE}")


def fingerprint_files(paths: List[str]) -> str:
    """
    One value standing for the exact set of files given, independent of the
    order they arrive in.

    Deliberately the WHOLE SET rather than a hash per file. Per-file hashes
    would also identify which individual files repeat, but they need a list
    per patient -- and a list does not belong in a CSV cell that a human
    opens to look a patient up. The set-level answer is the one the workflow
    acts on: identical set means this exact submission has already been
    anonymized and nothing needs writing. Anything else -- a file added, a
    file changed, a fresh export -- is treated as new material and processed,
    which is the safe direction to be wrong in.

    Truncated to 32 hex characters (128 bits): far beyond any collision risk
    at hospital-study scale, and short enough to sit in a spreadsheet column
    without swamping the four columns anyone actually reads.

    A file that cannot be read contributes its path instead of its content,
    so it reads as different material rather than silently matching.
    """
    digests = []
    for path in sorted(paths):
        try:
            digests.append(sha256_file(path))
        except OSError:
            digests.append("unreadable:" + os.path.basename(path))
    combined = hashlib.sha256()
    for digest in sorted(digests):
        combined.update(digest.encode("utf-8"))
        combined.update(b"|")
    return combined.hexdigest()[:32]


def sha256_file(path: str, chunk_bytes: int = 1 << 20) -> str:
    """SHA-256 over a file's bytes, read in chunks so a 40 MB ultrasound
    series never lands in memory whole."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(chunk_bytes), b""):
            h.update(block)
    return h.hexdigest()


def resolve_mapping_csv_path(cli_arg: Optional[str], default_path: Optional[str] = None) -> str:
    """
    Resolve the mapping CSV path: an explicit --mapping-csv CLI argument
    always wins. Otherwise, if the caller supplies a study-specific
    `default_path` (the normal case -- each study script passes its own
    fixed root-folder default, e.g. "eGFR_anony_Mapping.csv"), use that
    automatically with no prompt: the first run creates it fresh, every
    later run loads and appends to the same file. Interactive input() is
    kept only as a last-resort fallback for a caller with no default.
    """
    if cli_arg:
        return cli_arg
    if default_path:
        print(f"[mapping-csv] --mapping-csv not given -- using default path {default_path!r}.")
        return default_path
    path = input("Path to ID-mapping CSV (existing or new): ").strip()
    if not path:
        raise ValueError("No mapping CSV path provided.")
    return path


def compute_ranged_output_dir_bounds(count_before: int, count_after: int) -> Tuple[int, int]:
    """
    Compute the (start, end) mapping-CSV record-number (S. No) range this
    run's auto-named --output-dir should advertise, per the session-
    numbering spec: `count_before` is the mapping CSV's row count before
    this run started (0 on a first-ever run), `count_after` its row count
    once every patient in this run has been resolved (existing identities
    reused, new ones minted) -- i.e. AFTER pass 1 (identity resolution),
    before any file is written (see resolve_patient_identity() / two-pass
    note in anonymize_eGFR.py and anonymize_KFRE.py).

    Normal case (this run minted >=1 new record): start = count_before + 1,
    end = count_after. E.g. a first run of 20 new patients: (0, 20) -> (1, 20).
    A later run with 40 already recorded, adding 15 more: (40, 55) -> (41, 55).

    Degenerate case (this run minted ZERO new records -- every patient was
    an existing/reused identity, e.g. a re-run over already-processed
    input): count_after == count_before, and (count_before+1, count_after)
    would be an inverted, empty range (e.g. "41_40"). Falls back to
    (count_before, count_before) instead -- a valid, non-inverted range
    that reads as "this run touched records up to #N, added none new".
    """
    if count_after > count_before:
        return count_before + 1, count_after
    return count_before, count_before


def compute_ranged_output_dir_name(study_prefix: str, count_before: int, count_after: int) -> str:
    """
    Build the auto-numbered --output-dir folder name "<study_prefix>_<start>_
    <end>" -- see compute_ranged_output_dir_bounds() for the (start, end)
    semantics.
    """
    start, end = compute_ranged_output_dir_bounds(count_before, count_after)
    return f"{study_prefix}_{start}_{end}"


def validate_mapping_csv_location(
    mapping_csv_path: str, input_dir: str, output_dir: str
) -> Optional[str]:
    """
    Guard against the confidential mapping CSV resolving inside --input-dir OR
    --output-dir (equal-path or nested-path), the same class of protection the
    callers already apply between --input-dir and --output-dir themselves.

    This closes a real leak path: --output-dir is the folder meant to be
    safe to share as the anonymized dataset. If an operator's --mapping-csv
    path happens to resolve inside it (e.g. "keep the mapping file alongside
    this run's output folder for convenience", or a copy-paste path mistake),
    the mapping CSV -- which holds every real CR No/Lab No AND every real
    patient name -- would sit right next to the anonymized files it exists
    to keep separate from. --input-dir is guarded too since the mapping CSV
    must never be written into the read-only source tree either.

    Callers must abspath all three arguments before calling (this function
    does its own abspath as a defensive second layer, but the error message
    is clearest when callers pass already-normalized paths).

    Returns None if the location is OK, or a human-readable error message
    string describing the violation if not (callers print it and exit
    non-zero -- this function never raises/exits itself, keeping it a pure,
    easily-unit-testable check).
    """
    mp = os.path.normcase(os.path.abspath(mapping_csv_path))
    for label, d in (("--output-dir", output_dir), ("--input-dir", input_dir)):
        dn = os.path.normcase(os.path.abspath(d))
        if mp == dn or mp.startswith(dn + os.sep):
            return (
                f"--mapping-csv ({mapping_csv_path!r}) must not be the same as, or located "
                f"inside, {label} ({d!r}). The mapping CSV is confidential re-identification "
                f"material -- it holds every real CR No/Lab No AND every real patient name -- "
                f"and must never be written into a folder that is meant to be safe-to-share "
                f"anonymized output (or into the read-only source tree). Point --mapping-csv "
                f"at a separate, access-restricted location."
            )
    return None


def find_pdfs_recursive(root_dir: str) -> List[str]:
    """
    Find every PDF anywhere under root_dir, at any depth, case-insensitive
    extension match. Shared by anonymize_eGFR.py's find_pdfs() and
    anonymize_KFRE.py's find_input_pdfs() so both scripts (and the desktop
    app's single-package flow) support two input shapes with the SAME
    function: PDFs sitting flat in the selected folder (this repo's own
    sample data), and PDFs nested inside named subfolders such as the
    data-collection portal's documented "Ultrasound_Reports/" /
    "Lab_Reports/" convention -- classification is always content-based
    (see classify_pdf() / detect_report_template()), so it never matters
    which subfolder a report actually sits in. Walking into the DICOM
    kidney-image subfolders too is harmless (they contain zero PDFs) and
    keeps this one function correct for every caller rather than needing
    per-caller exclusion lists.
    """
    out: List[str] = []
    for dirpath, _dirnames, filenames in os.walk(root_dir):
        for name in filenames:
            if name.lower().endswith(".pdf"):
                out.append(os.path.join(dirpath, name))
    return sorted(out)


# ==========================================================================
# 2. Content-based scanned-PDF classifier (never filename-based)
# ==========================================================================


@dataclass
class PdfClassification:
    is_scanned_no_pii: bool
    total_chars: int
    page_count: int
    reason: str


def classify_pdf(pdf_path: str) -> PdfClassification:
    """
    Classify a PDF as either:
      - "scanned / no extractable PII" (total extractable text is at/near zero
        AND every page is essentially one full-page embedded raster image), or
      - "must-redact" (contains extractable text that may carry PII).

    This is purely content-based -- filenames are never consulted, per spec.
    """
    doc = fitz.open(pdf_path)
    try:
        total_chars = 0
        page_count = len(doc)
        all_pages_are_full_image = True
        for page in doc:
            total_chars += len(page.get_text().strip())
            if not _page_is_full_page_image(page):
                all_pages_are_full_image = False

        if total_chars < SCANNED_TEXT_CHAR_THRESHOLD and all_pages_are_full_image:
            return PdfClassification(
                is_scanned_no_pii=True,
                total_chars=total_chars,
                page_count=page_count,
                reason=(
                    f"total extractable chars={total_chars} (<{SCANNED_TEXT_CHAR_THRESHOLD}) "
                    f"and every page ({page_count}) is a full-page embedded raster image"
                ),
            )
        return PdfClassification(
            is_scanned_no_pii=False,
            total_chars=total_chars,
            page_count=page_count,
            reason=(
                f"total extractable chars={total_chars}, all_pages_full_image="
                f"{all_pages_are_full_image} -- treat as text-bearing / must redact"
            ),
        )
    finally:
        doc.close()


def _page_is_full_page_image(page) -> bool:
    """True iff the page has >=1 embedded image whose bbox covers >=90% of the page area."""
    page_area = abs(page.rect.width * page.rect.height)
    if page_area <= 0:
        return False
    images = page.get_images(full=True)
    if not images:
        return False
    for img in images:
        xref = img[0]
        try:
            rects = page.get_image_rects(xref)
        except Exception:
            continue
        for rect in rects:
            area = abs(rect.width * rect.height)
            if area / page_area >= SCANNED_IMAGE_COVERAGE_RATIO:
                return True
    return False


# ==========================================================================
# 3. Generic PDF true-redaction helpers
# ==========================================================================


def redact_exact_text_values(page, values: List[str], case_sensitive: bool = True) -> int:
    """
    Locate every occurrence of each string in `values` on `page` via
    page.search_for() (precise quads, never a guessed rectangle) and mark them
    for TRUE redaction (add_redact_annot with a white fill, so the field
    reads as ordinary blank space -- erased, not a visible black box). Does
    NOT call apply_redactions() -- caller applies once per page after all annotations
    (text + barcode-font spans + anything else) have been queued, so a single
    apply_redactions() pass handles everything on that page.

    `case_sensitive` (default True, matching every current call site's
    behavior byte-for-byte): when False, any value that finds ZERO hits via
    the exact-case page.search_for(value) is retried against the common
    case-transform variants of that value (see `_case_insensitive_variants`)
    -- still routed through search_for() so quads stay precise, never a
    guessed rectangle. This covers realistic report-template case drift
    (e.g. a value stored/typed in Title Case but rendered in ALL CAPS on the
    page) but is NOT a full arbitrary-casing search -- an oddly-cased
    occurrence outside those variants (e.g. "sUJata") would still be missed.
    That residual gap is a documented, honest limitation of this fallback,
    not a silent one.

    IMPORTANT MEASURED CAVEAT (pinned PyMuPDF 1.28.2 / MuPDF 1.28.2, verified
    empirically -- see scripts/tests referenced in the KFRE fix self-test):
    `page.search_for()` itself already performs Unicode case-INSENSITIVE
    matching by default in this PyMuPDF build (inherited from MuPDF's own
    search implementation; there is no PyMuPDF flag this function can pass to
    force strict case-SENSITIVE matching). Practically this means the
    fallback added above for `case_sensitive=False` will rarely even trigger
    -- the primary page.search_for(value) call typically already finds
    differently-cased occurrences on its own, on this PyMuPDF version. This
    function does NOT filter those case-insensitive hits back out when
    `case_sensitive=True` (doing so via a post-hoc text-extraction filter was
    considered and rejected: it would risk *dropping* legitimate redaction
    hits on whitespace/extraction mismatches, which is a far more dangerous
    failure mode for a PII tool than an occasional harmless case-insensitive
    over-match). So: `case_sensitive=True` is an honest "we did not add any
    extra case-folding on top of whatever PyMuPDF already does" contract, NOT
    a guarantee of strict-case-only matching -- do not rely on it to leave a
    differently-cased NON-PII lookalike string unredacted.

    Returns the number of redaction annotations added.
    """
    count = 0
    for value in values:
        if not value:
            continue
        rects = page.search_for(value)
        if not rects and not case_sensitive:
            rects = _case_insensitive_search_for(page, value)
        for rect in rects:
            # White fill (matching the page background), not black -- the
            # field is ERASED (reads as blank space, same as the reference
            # anonymized samples under sample/), not marked with a visible
            # black box. The underlying text is still genuinely deleted by
            # apply_redactions() either way; only the box's fill color
            # (i.e. what's drawn in the now-empty space) changes.
            page.add_redact_annot(rect, fill=(1, 1, 1))
            count += 1
    return count


def _case_insensitive_variants(value: str) -> List[str]:
    """Deduplicated common case-transforms of `value`, original first."""
    seen: List[str] = []
    for v in (value, value.upper(), value.lower(), value.title(), value.capitalize()):
        if v not in seen:
            seen.append(v)
    return seen


def _case_insensitive_search_for(page, value: str) -> List["fitz.Rect"]:
    """
    Case-insensitive fallback for redact_exact_text_values(case_sensitive=False).
    Tries each common case-transform of `value` via page.search_for() (so
    every returned rect is still a precise, PyMuPDF-computed quad -- never a
    guessed rectangle) and returns the de-duplicated union of hits.
    """
    hits: List["fitz.Rect"] = []
    seen_keys = set()
    for variant in _case_insensitive_variants(value):
        for rect in page.search_for(variant):
            key = (round(rect.x0, 1), round(rect.y0, 1), round(rect.x1, 1), round(rect.y1, 1))
            if key not in seen_keys:
                seen_keys.add(key)
                hits.append(rect)
    return hits


def redact_and_replace_value(
    page, value: str, replacement: str, case_sensitive: bool = True
) -> int:
    """
    Like redact_exact_text_values(), but instead of leaving the redacted area
    blank, stamps `replacement` (typically the patient's new Anonymized ID)
    into the same location -- e.g. "CR No : QXMD7192" instead of a blank
    "CR No :". Uses PyMuPDF's native add_redact_annot(text=...) support, so
    the replacement text is drawn by apply_redactions() itself, in the same
    pass that deletes the original glyphs -- there is no risk of the real
    value surviving underneath a merely-visual overlay.

    White fill + black text (same white fill as the blank-erase style of
    redact_exact_text_values(), just with new text drawn on top) so the
    field reads as an ordinary report value that has been substituted, not
    as a redaction stamp -- matching the same "replace, don't erase"
    treatment already used for the DICOM PatientName/PatientID tags (see
    scrub_dicom_dataset()).

    `case_sensitive` behaves identically to redact_exact_text_values().
    Returns the number of occurrences located and queued for replacement.
    """
    if not value:
        return 0
    rects = page.search_for(value)
    if not rects and not case_sensitive:
        rects = _case_insensitive_search_for(page, value)
    count = 0
    for rect in rects:
        # Shrink the font to fit the original value's line height so the
        # replacement text doesn't overflow a tight field box; capped at 11pt
        # to match apply_redactions_and_stamp()'s label sizing.
        fontsize = max(6.0, min(11.0, rect.height * 0.72))
        page.add_redact_annot(
            rect,
            text=replacement,
            fontname="helv",
            fontsize=fontsize,
            align=fitz.TEXT_ALIGN_LEFT,
            fill=(1, 1, 1),
            text_color=(0, 0, 0),
        )
        count += 1
    return count


def redact_spans_by_font(page, font_name_substrings: List[str]) -> int:
    """
    Locate every text span on `page` whose font name contains any of
    `font_name_substrings` (case-insensitive) and mark its bbox for TRUE
    redaction. This is needed for specialty fonts (e.g. 'IDAutomation2D',
    '3of9Barcode') where page.search_for() on the encoded value returns a
    rect narrower than the actual rendered glyph ink -- confirmed by
    pixel-level render diff during validation: search_for()'s rect left
    visible barcode ink un-redacted on both edges even though the underlying
    text was deleted. The font-span bbox from get_text('dict') was verified
    to fully cover the rendered ink.

    Returns the number of redaction annotations added.
    """
    count = 0
    needles = [s.lower() for s in font_name_substrings]
    d = page.get_text("dict")
    for block in d.get("blocks", []):
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                font = (span.get("font") or "").lower()
                if any(n in font for n in needles):
                    rect = fitz.Rect(span["bbox"])
                    # White fill -- erased/blank, not a black box; see the
                    # matching note in redact_exact_text_values().
                    page.add_redact_annot(rect, fill=(1, 1, 1))
                    count += 1
    return count


def apply_redactions_and_stamp(page, anon_id: str, corner: str = "top-right") -> None:
    """
    Apply all queued redaction annotations on `page` (this is what actually
    deletes the underlying glyphs/content, not just visually covers them),
    then stamp a small fixed-position "Anonymized" label carrying the new
    Anonymized ID. Stamping happens AFTER apply_redactions() so the stamp
    itself is never at risk of being redacted.
    """
    page.apply_redactions()

    label = f"Anonymized - Subject ID: {anon_id}"
    rect = page.rect
    font_size = 8
    margin = 4
    if corner == "top-right":
        # insert_textbox anchored at a fixed simple box in the top-right corner
        box = fitz.Rect(rect.width - 220, margin, rect.width - margin, margin + 14)
    else:
        box = fitz.Rect(margin, margin, margin + 220, margin + 14)
    page.insert_textbox(
        box,
        label,
        fontsize=font_size,
        fontname="helv",
        color=(1, 0, 0),
        align=fitz.TEXT_ALIGN_RIGHT if corner == "top-right" else fitz.TEXT_ALIGN_LEFT,
    )


def save_redacted_pdf(doc, out_path: str) -> None:
    """Save with garbage collection + deflate so redacted content doesn't linger
    in incremental-update leftovers."""
    parent = os.path.dirname(os.path.abspath(out_path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    doc.save(out_path, garbage=4, deflate=True)


# ==========================================================================
# 4. Misc small helpers shared across study scripts
# ==========================================================================


def safe_copy_bytes(src_path: str, dst_path: str) -> None:
    """Byte-for-byte pass-through copy (used for scanned/no-PII files like the CRF)."""
    parent = os.path.dirname(os.path.abspath(dst_path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(src_path, "rb") as fsrc, open(dst_path, "wb") as fdst:
        fdst.write(fsrc.read())


def queue_region_redaction(page, rect) -> None:
    """Queue a plain geometric white-fill true-redaction over `rect` (e.g. for the
    Template-B QR-code region, which is baked into a whole-page background raster
    and cannot be isolated as its own image object -- see extract_template_b_fields
    / TEMPLATE_B_QR_ANCHOR_TEXT docs below). White, not black, so the region reads
    as erased/blank rather than a visible black box -- see the matching note in
    redact_exact_text_values(). Caller still must call page.apply_redactions() once,
    after all annotations for that page are queued."""
    page.add_redact_annot(fitz.Rect(rect), fill=(1, 1, 1))


# ==========================================================================
# 5. Report-template detection + field extraction (shared across studies)
# ==========================================================================
#
# Both the KFRE study (100% Template A) and the eGFR study (CKD cohort =
# Template A, Normal cohort = Template B) use the SAME two clinical-report
# PDF templates, so the detection/extraction logic lives here once rather
# than being duplicated per study script.
#
# TEMPLATE A -- "NIMS in-house / Department of Biochemistry" report.
#   Field layout in the raw text stream is strictly sequential: a label
#   ("CR No", "Patient Name", "Ward", ...) is immediately followed (across a
#   newline, or on the same line) by ": <value>". A plain regex over the
#   label text is reliable here (validated against all 33 KFRE files + all
#   10 eGFR-CKD files with zero parse failures / zero mismatches).
#
# TEMPLATE B -- external lab (Dr Lal PathLabs / SUJATHA DIAGNOSTICS) report.
#   The raw text stream groups ALL header VALUES together first, followed by
#   ALL header LABELS later -- label-then-value sequential regex does NOT
#   work here. Labels and values must instead be matched by shared
#   y-coordinate (same visual row) via page.get_text("words"). Validated
#   against all 5 eGFR-Normal-cohort files (Name + Lab No. extracted
#   correctly on every file, matching the ground-truth inventory exactly).

TEMPLATE_A_ANCHOR = "Department of Biochemistry"
TEMPLATE_B_ANCHORS = ("Dr Lal PathLabs", "SUJATHA DIAGNOSTICS")
TEMPLATE_B_QR_ANCHOR_TEXT = "Authenticity assured"

# Boilerplate contact-info line repeated (as real, extractable text, unlike
# the identical-looking baked-in footer band -- see TEMPLATE_B_FOOTER_
# CONTACT_RECT) inside the page-3 "IMPORTANT INSTRUCTIONS" disclaimer box.
# Confirmed byte-identical and in the same position (page index 2) across
# all 5 eGFR-Normal-cohort files -- a fixed literal blank-redaction target,
# same "any kind of address" rationale as the other provider-contact erasures.
TEMPLATE_B_BOILERPLATE_CONTACT_LINE = (
    "Tel: +91-11-49885050, Fax: - +91-11-2788-2134, E-mail: lalpathlabs@lalpathlabs.com"
)

# Presence-only check used by detect_report_template() below -- it only needs
# to know whether the "CR No" label exists at all, never its value, so it is
# unaffected by the multi-line-value bug fixed in _extract_template_a_value().
_CR_NO_PRESENCE_RE = re.compile(r"CR No\s*:")

# --------------------------------------------------------------------------
# Template A field-value extraction.
#
# BUG HISTORY (fixed -- adversarial-audit CRITICAL finding): the original
# implementation captured each field's value with a regex of the shape
# `Label\s*:\s*([^\n]*)`, which stops capturing at the FIRST newline. When a
# PII value spans two physical lines in the PDF's raw text stream (e.g. a
# long Patient Name that wraps), only the first line was captured, so the
# second line of the real PII value was never added to the redaction target
# list and survived, fully extractable, in the saved "anonymized" PDF --
# while the run still reported 0 errors / "OK".
#
# Fix: capture everything from right after the label's ":" up to the START
# of the NEXT known Template-A label (not just up to the next newline), so a
# value spanning any number of wrapped physical lines is captured in full.
# Internal newlines inside a wrapped value are collapsed to single spaces
# before being returned -- empirically verified that PyMuPDF's
# page.search_for() matches a single-spaced search string as a sequence of
# consecutive words on the page and returns one quad PER PHYSICAL LINE of
# the match, so a single-spaced multi-line value string still locates (and
# queues for redaction) every line of the original on-page value.
# --------------------------------------------------------------------------

# Every label that can appear in Template A's raw text stream, used ONLY to
# know where one field's value ends and the next field begins -- order does
# not matter for matching (the alternation tries all of them and the
# earliest match in the text wins), but COMPLETENESS does: any label missing
# from this list would let a value's capture run on and accidentally swallow
# the next field's text too.
_TEMPLATE_A_ALL_LABELS = [
    r"CR No",
    r"Lab/Study No\.",
    r"Requisition Date",
    r"Patient Name",
    r"Age/Sex",
    r"Coll\./Study Date",
    r"Sample Type/No",
    r"Clinician",
    r"Reporting Date",
    r"Dept/Unit",
    r"Diagnosis",
    r"Ward/OPD",
    r"\bWard\b",
    r"Room/Bed",
    r"Validated By",
]

# BUG HISTORY (fixed -- adversarial-audit MODERATE finding, over-redaction):
# Room/Bed is always the LAST PII-field label before the results table /
# boilerplate section starts, and the results-table header row ("Investigation
# / Result / Unit / Ref. Range") plus the trailing disclaimer paragraph are
# NOT PII-field labels, so none of them terminated the boundary search above.
# In every real Template-A file, Room/Bed's value capture ran on through the
# entire table header and disclaimer text (confirmed on NP1/NP6 etc.), and
# apply_redactions() then genuinely deleted that swallowed non-PII report
# content from the saved PDF. Fix: add a second boundary class for these
# known non-PII section-start markers (no trailing ":" required, unlike the
# PII-label boundaries above) so a value's capture also stops the instant it
# reaches the start of the results table or the boilerplate footer, whichever
# a document's content-stream order presents.
_TEMPLATE_A_SECTION_BOUNDARIES = [
    r"Investigation",
    r"Comments\s*:",
    r"\*{3,}\s*END OF THE REPORT",
    r"Test results relate only to item received",
    r"This is computer generated report",
    # Needed so "Validated By"'s own value capture (added for provider-name
    # erasure) stops at the page-footer boilerplate instead of running to
    # the end of the document.
    r"Page\s+\d+\s+of\s+\d+",
]

_NEXT_LABEL_BOUNDARY_RE = re.compile(
    r"\n\s*(?:" + "|".join(_TEMPLATE_A_ALL_LABELS) + r")\s*:"
    r"|"
    r"\n\s*(?:" + "|".join(_TEMPLATE_A_SECTION_BOUNDARIES) + r")"
)


def _extract_template_a_value(full_text: str, label_pattern: str) -> Optional[str]:
    """
    Find `label_pattern` (a regex fragment matching one Template-A field's
    label text) followed by ":" and return everything up to -- but NOT
    including -- the start of the next known Template-A label, with internal
    whitespace/newlines collapsed to single spaces (so a value that wraps
    across physical lines is returned whole, not truncated to its first
    line). Returns None if the label isn't found, or its value is empty.
    """
    m = re.search(label_pattern + r"\s*:\s*", full_text)
    if not m:
        return None
    start = m.end()
    boundary = _NEXT_LABEL_BOUNDARY_RE.search(full_text, start)
    end = boundary.start() if boundary else len(full_text)
    value = re.sub(r"\s+", " ", full_text[start:end]).strip()
    return value or None


def detect_report_template(doc) -> str:
    """
    Content-based template detection (NEVER filename-based). Returns "A", "B",
    or "unknown". Looks at the first two pages' text (Template B repeats its
    full header on every page; Template A's anchor is always on page 0).
    """
    text = ""
    for page in doc[: min(2, len(doc))]:
        text += page.get_text() + "\n"

    if TEMPLATE_A_ANCHOR in text or _CR_NO_PRESENCE_RE.search(text):
        return "A"
    if any(anchor in text for anchor in TEMPLATE_B_ANCHORS) or (
        "Lab No." in text and re.search(r"\bName\b", text) and "CR No" not in text
    ):
        return "B"
    return "unknown"


# Placeholder-only values a field can hold that are NOT real PII (e.g. an
# unpopulated "Clinician" field rendered as "--") -- redacting these would be
# harmless but pointless noise, so they're treated as "field is empty".
_TEMPLATE_A_PLACEHOLDER_VALUES = {"", "-", "--", "---", "n/a", "na"}


@dataclass
class TemplateAFields:
    cr_no: Optional[str]
    patient_name: Optional[str]
    ward_label: Optional[str]  # "Ward" or "Ward/OPD" or None if absent
    ward_value: Optional[str]
    room_bed_value: Optional[str]  # optional -- absent on the "Ward/OPD" outpatient variant
    lab_study_no: Optional[str]  # per-sample accession number -- see module-level note on why this is now erased too
    validated_by: Optional[str]  # validating biochemist/resident's name + credentials
    clinician: Optional[str]  # referring clinician, if ever populated (observed as "--" in every real sample)

    def redaction_targets(self) -> List[str]:
        """Every PII value string that must be located+redacted on every page
        (used by callers that only need the completeness guard's full list --
        does not distinguish blank-vs-replace treatment, see
        blank_redaction_targets()/identity_target() for that)."""
        return [
            v
            for v in (
                self.cr_no,
                self.patient_name,
                self.ward_value,
                self.room_bed_value,
                self.lab_study_no,
                self.validated_by,
                self.clinician,
            )
            if v
        ]

    def blank_redaction_targets(self) -> List[str]:
        """PII values that are fully blanked (no replacement text) -- every
        Template-A PII field EXCEPT the identity key, which is instead
        REPLACED with the new Anonymized ID so the field still reads as a
        normal (now-pseudonymous) value -- see identity_target().

        Includes lab_study_no (a per-sample accession number that changes
        between a patient's own report PDFs and adds no follow-up-tracking
        value beyond CR No + the dates already preserved -- erased per
        explicit request rather than kept), validated_by (the validating
        provider's name -- erased per explicit request; previously kept as
        "provider info, not patient PII", now treated the same as any other
        name on the document), and clinician (only when it holds a real
        value, not an empty-field placeholder like "--")."""
        targets = [self.patient_name, self.ward_value, self.room_bed_value, self.lab_study_no, self.validated_by]
        if self.clinician and self.clinician.strip().lower() not in _TEMPLATE_A_PLACEHOLDER_VALUES:
            targets.append(self.clinician)
        return [v for v in targets if v]

    def identity_target(self) -> Optional[str]:
        """The value that should be REPLACED with the new Anonymized ID
        (rather than blanked): CR No, the stable per-patient identity key
        for Template A."""
        return self.cr_no


def extract_template_a_fields(doc) -> TemplateAFields:
    """
    Extract Template A's per-patient PII fields via label->colon->value
    extraction over the concatenated text of ALL pages (a multi-page report,
    e.g. a large lab panel, repeats the full header block on every page --
    confirmed on KFRE's "Sr.CR KFRE 9.pdf", a 2-page file). Each value is
    captured up to the start of the next known label (see
    _extract_template_a_value()) so a value that wraps onto a second
    physical line is captured in full, not truncated to its first line.
    """
    full_text = "\n".join(page.get_text() for page in doc)

    cr_no = _extract_template_a_value(full_text, r"CR No")
    patient_name = _extract_template_a_value(full_text, r"Patient Name")

    ward_opd_value = _extract_template_a_value(full_text, r"Ward/OPD")
    if ward_opd_value is not None:
        ward_label = "Ward/OPD"
        ward_value = ward_opd_value
    else:
        ward_value = _extract_template_a_value(full_text, r"\bWard\b")
        ward_label = "Ward" if ward_value is not None else None

    room_bed_value = _extract_template_a_value(full_text, r"Room/Bed")
    lab_study_no = _extract_template_a_value(full_text, r"Lab/Study No\.")
    validated_by = _extract_template_a_value(full_text, r"Validated By")
    clinician = _extract_template_a_value(full_text, r"Clinician")

    return TemplateAFields(
        cr_no=cr_no,
        patient_name=patient_name,
        ward_label=ward_label,
        ward_value=ward_value,
        room_bed_value=room_bed_value,
        lab_study_no=lab_study_no,
        validated_by=validated_by,
        clinician=clinician,
    )


@dataclass
class TemplateBFields:
    name: Optional[str]
    lab_no: Optional[str]
    ref_by: Optional[str]  # referring physician's name -- erased per explicit request

    def redaction_targets(self) -> List[str]:
        return [v for v in (self.name, self.lab_no, self.ref_by) if v]

    def blank_redaction_targets(self) -> List[str]:
        """PII values that are fully blanked (no replacement text) -- "Name"
        and "Ref By" (referring physician); "Lab No." is instead REPLACED
        with the new Anonymized ID, see identity_target()."""
        return [v for v in (self.name, self.ref_by) if v]

    def identity_target(self) -> Optional[str]:
        """The value that should be REPLACED with the new Anonymized ID
        (rather than blanked): Lab No., the best-available identity key for
        Template B (no CR No field exists on this template)."""
        return self.lab_no


def _cluster_words_into_rows(words, y_tolerance: float = 1.6):
    """Group page.get_text('words') tuples into visual rows by shared y0,
    independent of PyMuPDF's own (block, line) indices -- those were found to
    be unreliable for this template's multi-column layout. Two words are in
    the same row iff their y0 differs from the row's reference y0 by no more
    than `y_tolerance` points (validated against the real line pitch of
    ~11pt in this template, comfortably larger than typical same-row y0
    jitter of ~1pt between label / colon / value glyphs)."""
    rows: List[list] = []
    for w in sorted(words, key=lambda w: w[1]):
        y0 = w[1]
        if rows and abs(y0 - rows[-1][0][1]) <= y_tolerance:
            rows[-1].append(w)
        else:
            rows.append([w])
    return rows


def _row_text_in_x_range(row, x0_min: float, x0_max: float) -> str:
    toks = [w for w in row if x0_min <= w[0] < x0_max and w[4] != ":"]
    toks.sort(key=lambda w: w[0])
    return " ".join(w[4] for w in toks).strip()


# Column bands for Template B's left label/value pair (points from page left
# edge), per the validated layout: label column ~x24-67, colon ~x84-87,
# value column ~x91 up to just before the right-hand Age/Gender/... pair
# which starts ~x292.
#
# FIX (adversarial-audit MINOR finding): the upper value-column bound was
# previously hardcoded to 260.0pt -- narrower than the row's actual
# available width (~292pt, per the very comment above), the same class of
# hardcoded-width truncation risk as the Template A critical finding. A
# long Name/Lab No. value positioned between x260 and x292 would have been
# silently dropped from extraction (and therefore never redacted). Widened
# to just short of the Age/Gender column's own start so the full available
# label width is used without bleeding into that column's text.
_TEMPLATE_B_LABEL_X = (0.0, 70.0)
_TEMPLATE_B_VALUE_X = (88.0, 291.0)


def extract_template_b_fields(doc) -> TemplateBFields:
    """
    Extract Template B's "Name", "Lab No.", and "Ref By" values by matching
    label words to value words on the SAME visual row (shared y-coordinate)
    -- a plain sequential regex does NOT work for this template (see module
    docstring above): the raw text stream groups all header values together,
    followed by all header labels, so label and value are far apart in
    reading order.
    """
    if len(doc) == 0:
        return TemplateBFields(name=None, lab_no=None, ref_by=None)
    page = doc[0]
    words = page.get_text("words")
    rows = _cluster_words_into_rows(words)

    name_val: Optional[str] = None
    lab_no_val: Optional[str] = None
    ref_by_val: Optional[str] = None
    for row in rows:
        label = _row_text_in_x_range(row, *_TEMPLATE_B_LABEL_X)
        if label == "Name":
            name_val = _row_text_in_x_range(row, *_TEMPLATE_B_VALUE_X) or None
        elif label == "Lab No.":
            lab_no_val = _row_text_in_x_range(row, *_TEMPLATE_B_VALUE_X) or None
        elif label == "Ref By":
            ref_by_val = _row_text_in_x_range(row, *_TEMPLATE_B_VALUE_X) or None

    return TemplateBFields(name=name_val, lab_no=lab_no_val, ref_by=ref_by_val)


def find_template_b_qr_redaction_rect(page):
    """
    Locate the geometric redaction rectangle that blanks Template B's baked-in
    QR-code pixels (+ the barcode-font Lab No. repeat + the "Page N of 3"
    label) on the one page that carries the "Authenticity assured" footer
    phrase (confirmed: always page index 1 of 3, never page 0 or 2, across
    all 5 eGFR-Normal-cohort files). The QR is rendered into the whole-page
    background raster, not addressable as its own image object via
    page.get_images(), so a text-anchored geometric rectangle is used
    instead (validated end-to-end: blanks the QR/barcode/page-label; the
    pathologist signature above it is a separate region, see
    find_template_b_signature_redaction_rect() -- also now erased, just via
    its own dedicated rectangle rather than this one).

    Returns a fitz.Rect, or None if this page does not carry the anchor text
    (e.g. pages 0 and 2 -- callers should skip this redaction on those).
    """
    hits = page.search_for(TEMPLATE_B_QR_ANCHOR_TEXT)
    if not hits:
        return None
    anchor = hits[0]
    return fitz.Rect(0, anchor.y0 - 100, page.rect.width, anchor.y1 + 50)


# --------------------------------------------------------------------------
# Template B: additional erasure regions (provider address + signature),
# added per explicit follow-up request -- "any kind of address" and "any
# kind of signature" should be erased, not just the identity/name fields.
# --------------------------------------------------------------------------

# The top registration-office line ("Regd. Office: ... / Web: ... CIN: ...")
# and the bottom customer-care contact band ("Tel: ... Fax: ... E-mail: ...")
# are baked into the whole-page background raster (confirmed: neither string
# is present in page.get_text() at all), so -- like the QR code -- they
# cannot be located via search_for() and must be erased by fixed, page-
# relative geometry instead. Measured directly against the embedded
# background image's own pixel data (not eyeballed off a screenshot) and
# cross-checked visually against 2 different patients across all 3 pages:
# position is identical on every page/patient (same repeating template
# graphic, only the report content changes). Page is A4, 595x841pt.
TEMPLATE_B_TOP_ADDRESS_RECT = fitz.Rect(0, 36, 595, 66)
TEMPLATE_B_FOOTER_CONTACT_RECT = fitz.Rect(0, 799, 595, 841)


def find_template_b_processed_at_address_rect(page):
    """
    Locate the "Processed at" field's VALUE -- the processing lab's multi-
    line street address (e.g. "LPL-HYDERABAD / 4th Floor, Oyster Oasis
    Centre... / ... / Begumpet, Hyderabad -500016") -- which wraps across up
    to 5 physical lines in the right-hand header column with no per-line
    label, so the usual row-matched label->value extraction doesn't apply.
    Anchored dynamically off the "Processed at" label's own position (never
    hardcoded absolute coordinates) and extended down/right by a fixed
    margin sized to comfortably cover every observed instance (measured:
    real address blocks span ~51pt over 5 lines; margin gives ~2x headroom).

    Returns a fitz.Rect, or None if the "Processed at" label isn't found on
    this page.
    """
    hits = page.search_for("Processed at")
    if not hits:
        return None
    label = hits[0]
    return fitz.Rect(label.x1 + 20, label.y0 - 2, page.rect.width, label.y0 + 58)


def find_template_b_signature_redaction_rect(page):
    """
    Locate the validating pathologist's signature block (a scanned signature
    IMAGE followed by 4 lines of printed name/credential/role/company TEXT)
    on the page that carries it (confirmed: page index 1 of 3, same page as
    the QR code, above it). Generalized rather than hardcoded to any
    specific doctor's name so it keeps working if a future batch is
    validated by someone else:

      1. Find the "signature-shaped" image on the page: distinct from the
         whole-page background (near-page-sized) and from the NABL seal
         (near-square, ~1:1 aspect) by being a short, wide image (aspect
         ratio ~2-6:1, height under 70pt) -- matches the one observed
         instance (124x40pt, ratio 3.13) with headroom in both directions.
      2. Find "End of report" (the boilerplate line that always immediately
         follows the signature block) as the lower bound.
      3. Redact from just above the signature image down to just above
         "End of report" -- covering the image and every credential line
         below it, whatever the printed name/role text actually says.

    Falls back to a fixed lookback margin above "End of report" (no image
    anchor) if no signature-shaped image is found, so a future variant that
    prints the name without an accompanying signature image still gets its
    name text erased rather than silently skipped.

    Returns a fitz.Rect, or None if "End of report" isn't found on this page
    (callers should skip this redaction on pages that don't carry it).
    """
    end_hits = page.search_for("End of report")
    if not end_hits:
        return None
    lower_y = min(h.y0 for h in end_hits)

    sig_top_y = None
    for img in page.get_images(full=True):
        xref = img[0]
        try:
            rects = page.get_image_rects(xref)
        except Exception:
            continue
        for r in rects:
            if r.height <= 0 or r.height >= 70:
                continue
            ratio = r.width / r.height
            if 2.0 <= ratio <= 6.0:
                if sig_top_y is None or r.y0 < sig_top_y:
                    sig_top_y = r.y0

    if sig_top_y is not None:
        upper_y = sig_top_y - 5
    else:
        # Fallback: no signature-shaped image found -- still erase a fixed
        # margin above "End of report" so printed name/credential text
        # (with no accompanying image) doesn't silently survive.
        upper_y = lower_y - 90

    if upper_y >= lower_y:
        return None
    return fitz.Rect(0, upper_y, page.rect.width, lower_y)

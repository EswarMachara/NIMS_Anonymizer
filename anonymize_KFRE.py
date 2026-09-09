#!/usr/bin/env python3
"""
anonymize_KFRE.py -- PII anonymization script for the NIMS "KFRE" hospital
research study.

WHAT THIS DOES
---------------
The KFRE study folder holds 33 clinical-report PDFs (11 patients x 3 files:
"Sr.CR KFRE <n>.pdf", "UACR KFRE <n>.pdf", "UPCR KFRE <n>.pdf") sitting flat,
with no per-patient subfolders. The trailing number in each filename is an
ARBITRARY enrollment/sample-batch index -- it is NOT trusted as a grouping or
identity key anywhere in this script.

Instead, this script:
  1. Opens every PDF in --input-dir and, purely from PDF CONTENT, classifies
     it as a scanned/no-extractable-PII file (pass-through, unexercised by the
     current KFRE corpus but handled defensively), a "Template A" NIMS
     in-house lab report (the only template present in the current KFRE
     corpus -- all 33/33 files), or a "Template B" external-lab report
     (present in the sibling eGFR study, not in KFRE today, but handled
     rather than silently mishandled in case a future KFRE batch draws from
     the same external lab).
  2. Extracts each report's STABLE per-patient identity key from content --
     "CR No" for Template A, "Lab No." (materially weaker, per-visit) for
     Template B -- and GROUPS files sharing the same key into one patient
     record, regardless of what number is in their filenames.
  3. For each patient record, looks up (or mints) an Anonymized ID via the
     confidential mapping CSV (see --mapping-csv below): a patient whose
     identity key already appears in the mapping CSV (e.g. a returning
     patient on a later visit, filed under new arbitrary filenames) gets
     their EXISTING Anonymized ID reused; a new identity key mints a fresh,
     collision-free ID.
  4. TRUE-redacts (deletes the underlying glyphs, not just paints over them --
     via PyMuPDF's page.add_redact_annot + page.apply_redactions) every PII
     value on every page it appears on, then stamps a fixed-position
     "Anonymized - Subject ID: <AnonID>" label, and writes the result to
     --output-dir. --input-dir is NEVER written to or mutated.

     The identity key itself (CR No / Lab No.) is NOT blanked like the other
     PII fields -- it is REPLACED with the new Anonymized ID (e.g. "CR No :
     QXMD7192"), via the same true-redaction mechanism, so the field still
     reads as a normal value rather than a redaction mark. Every other PII
     field (Patient Name, Ward/Room-Bed, Template B's "Name") is fully
     erased -- white-filled blank space, not a visible black box.

CONFIDENTIALITY
----------------
The --mapping-csv file is the ONLY artifact in this workflow that still links
an Anonymized ID back to a real patient name and real hospital identifier
(CR No / Lab No.). It is NOT part of the anonymized dataset: this script
never copies it into --output-dir, and you must keep it access-restricted
(filesystem permissions, encrypted storage, etc.) at all times.

USAGE
-----
    python3 anonymize_KFRE.py [--input-dir DIR] [--output-dir DIR]
                               [--mapping-csv PATH]

If --mapping-csv is omitted, the script prompts for a path interactively. If
that path does not exist yet, a fresh mapping CSV is created with just the
header row.
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass, field
from typing import Dict, List, Optional

try:
    import pymupdf as fitz
except ImportError:  # pragma: no cover
    import fitz  # type: ignore

import anon_common as ac

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_INPUT_DIR = os.path.join(SCRIPT_DIR, "KFRE")
DEFAULT_MAPPING_CSV = os.path.join(SCRIPT_DIR, "KFRE_anony_Mapping.csv")
OUTPUT_DIR_STUDY_PREFIX = "KFRE"


# ==========================================================================
# Per-file classification result
# ==========================================================================


@dataclass
class FileRecord:
    src_path: str
    filename: str
    template: str  # "A" | "B" | "scanned_no_pii" | "unrecognized"
    identity_key: Optional[str] = None  # value used for CSV "Original CR No" column
    identity_key_kind: Optional[str] = None  # "CR No" | "Lab No." | None
    patient_name: Optional[str] = None
    redaction_targets: List[str] = field(default_factory=list)
    template_a_fields: Optional[ac.TemplateAFields] = None
    template_b_fields: Optional[ac.TemplateBFields] = None


# ==========================================================================
# Step 1: classify + extract identity key from every input PDF
# ==========================================================================


def classify_input_file(path: str) -> FileRecord:
    filename = os.path.basename(path)
    doc = fitz.open(path)
    try:
        cls = ac.classify_pdf(path)
        if cls.is_scanned_no_pii:
            return FileRecord(src_path=path, filename=filename, template="scanned_no_pii")

        # Not confidently scanned. If it also has near-zero text (the
        # ambiguous case: low_text True but the image-coverage corroborating
        # signal did NOT confirm), this is exactly the case the brief warns
        # about -- do NOT silently pass it through. Flag loudly instead.
        if cls.total_chars < ac.SCANNED_TEXT_CHAR_THRESHOLD:
            return FileRecord(src_path=path, filename=filename, template="unrecognized")

        template = ac.detect_report_template(doc)
        if template == "A":
            fields = ac.extract_template_a_fields(doc)
            rec = FileRecord(
                src_path=path,
                filename=filename,
                template="A",
                identity_key=fields.cr_no,
                identity_key_kind="CR No" if fields.cr_no else None,
                patient_name=fields.patient_name,
                redaction_targets=fields.redaction_targets(),
                template_a_fields=fields,
            )
            return rec
        elif template == "B":
            fields = ac.extract_template_b_fields(doc)
            identity_key = f"Lab No.:{fields.lab_no}" if fields.lab_no else None
            rec = FileRecord(
                src_path=path,
                filename=filename,
                template="B",
                identity_key=identity_key,
                identity_key_kind="Lab No." if fields.lab_no else None,
                patient_name=fields.name,
                redaction_targets=fields.redaction_targets(),
                template_b_fields=fields,
            )
            return rec
        else:
            return FileRecord(src_path=path, filename=filename, template="unrecognized")
    finally:
        doc.close()


# ==========================================================================
# Step 2: group classified files by identity key
# ==========================================================================


@dataclass
class PatientGroup:
    identity_key: str
    identity_key_kind: str
    patient_name: str
    files: List[FileRecord] = field(default_factory=list)


def group_by_identity(records: List[FileRecord]) -> List[PatientGroup]:
    groups: Dict[str, PatientGroup] = {}
    order: List[str] = []
    for rec in records:
        if rec.identity_key is None:
            continue
        if rec.identity_key not in groups:
            groups[rec.identity_key] = PatientGroup(
                identity_key=rec.identity_key,
                identity_key_kind=rec.identity_key_kind or "unknown",
                patient_name=rec.patient_name or "",
            )
            order.append(rec.identity_key)
        groups[rec.identity_key].files.append(rec)
        # Prefer a non-empty patient_name if the first file in the group
        # happened to have one missing (shouldn't normally happen, defensive).
        if not groups[rec.identity_key].patient_name and rec.patient_name:
            groups[rec.identity_key].patient_name = rec.patient_name
    return [groups[k] for k in order]


# ==========================================================================
# Main pipeline
# ==========================================================================


def find_input_pdfs(input_dir: str) -> List[str]:
    """
    Find every PDF anywhere under input_dir (any depth), regardless of
    extension casing (.pdf, .PDF, .Pdf, .pDf, ...). Delegates to
    ac.find_pdfs_recursive(), which lists directories and checks
    name.lower().endswith(".pdf") rather than glob("*.pdf")/glob("*.PDF")
    -- on a case-sensitive filesystem (this environment is Linux) a
    glob-based approach only covers two of the many possible casings, and
    any other casing would silently vanish from the scan with zero log
    line and zero warning. Recursing (rather than the input_dir top level
    only) means this still works unchanged for the current flat 33-file
    KFRE batch layout AND for a single-patient package whose files sit
    inside a named subfolder (e.g. the data-collection portal's
    "Lab_Reports/" convention) -- grouping is content-based (by extracted
    CR No/Lab No, see group_by_identity()), so it never matters which
    subfolder or how many different patients' files a scan turns up.
    """
    return ac.find_pdfs_recursive(input_dir)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Anonymize the KFRE study's clinical-report PDFs. Groups files by "
            "content-extracted CR No (never by filename), assigns/reuses a "
            "confidential Anonymized ID per patient, and TRUE-redacts (deletes, "
            "not just visually covers) every PII value into --output-dir. Never "
            "writes into --input-dir. The --mapping-csv file is the ONLY place "
            "Anonymized IDs are still linked to real identities -- keep it "
            "access-restricted and never copy it into --output-dir."
        )
    )
    parser.add_argument("--input-dir", default=DEFAULT_INPUT_DIR, help=f"KFRE source folder (read-only; default: {DEFAULT_INPUT_DIR})")
    parser.add_argument(
        "--output-dir",
        default=None,
        help=(
            "Path to write the anonymized dataset to. If omitted (the normal case), it is "
            "created automatically in this script's own directory, auto-named "
            "'KFRE_<start>_<end>' where <start>/<end> is the range of mapping-CSV record "
            "numbers (S. No) this run covers -- e.g. 'KFRE_1_20' the first time (20 patients, "
            "none yet in the mapping CSV), 'KFRE_41_55' on a later run if 40 patients were "
            "already recorded and this run adds 15 more. Pass this flag explicitly to override "
            "with a fixed path instead."
        ),
    )
    parser.add_argument(
        "--mapping-csv",
        default=None,
        help=(
            "Path to the confidential ID-mapping CSV (existing or new). If omitted (the normal "
            f"case), it defaults automatically to {DEFAULT_MAPPING_CSV!r} -- created fresh on "
            "the first run, loaded and appended to on every later run."
        ),
    )
    args = parser.parse_args(argv)

    input_dir = os.path.abspath(args.input_dir)

    if not os.path.isdir(input_dir):
        print(f"[FATAL] --input-dir {input_dir!r} is not a directory.", file=sys.stderr)
        return 2

    mapping_csv_path = os.path.abspath(ac.resolve_mapping_csv_path(args.mapping_csv, default_path=DEFAULT_MAPPING_CSV))
    store = ac.MappingStore.load_or_create(mapping_csv_path)
    count_before = len(store.rows)

    pdf_paths = find_input_pdfs(input_dir)
    print(f"[scan] Found {len(pdf_paths)} PDF file(s) in {input_dir!r}.")

    records = [classify_input_file(p) for p in pdf_paths]

    unrecognized = [r for r in records if r.template == "unrecognized"]
    scanned = [r for r in records if r.template == "scanned_no_pii"]
    a_or_b = [r for r in records if r.template in ("A", "B")]

    # Loudly refuse anything we could not classify -- never silently leak it.
    if unrecognized:
        print("\n[REFUSED] The following file(s) could NOT be confidently classified as a known", file=sys.stderr)
        print("report template, AND do not confidently pass the scanned/no-PII test. Refusing", file=sys.stderr)
        print("to process them (no output written for these files) rather than risk a silent leak:", file=sys.stderr)
        for rec in unrecognized:
            print(f"  - {rec.src_path}", file=sys.stderr)
        print("Inspect these file(s) manually and extend the script's template handling if needed.\n", file=sys.stderr)

    # Group Template A/B files by content-extracted identity key.
    missing_key = [r for r in a_or_b if r.identity_key is None]
    if missing_key:
        print("\n[REFUSED] The following file(s) matched a known template but no stable identity", file=sys.stderr)
        print("key (CR No / Lab No.) could be extracted from their content. Refusing to process", file=sys.stderr)
        print("them rather than guess or silently drop them:", file=sys.stderr)
        for rec in missing_key:
            print(f"  - {rec.src_path} (template {rec.template})", file=sys.stderr)

    groupable = [r for r in a_or_b if r.identity_key is not None]
    groups = group_by_identity(groupable)

    print(f"\n[group] {len(groupable)} template-matched file(s) grouped into {len(groups)} patient record(s) by content-extracted identity key.\n")

    # Assign/reuse each patient's Anonymized ID up front (before any file I/O
    # -- --output-dir's auto-numbered name below depends on the mapping CSV's
    # FINAL row count for this run, which is only known once every group has
    # been resolved), so the console log below also reads as one clean
    # per-patient block.
    for group in groups:
        anon_id, is_new = store.get_or_assign(original_key=group.identity_key, name=group.patient_name)
        status = "NEW" if is_new else "REUSED (returning patient / repeat visit)"
        print(f"--- Patient record: {group.identity_key_kind}={group.identity_key!r}  Name={group.patient_name!r} ---")
        print(f"    Anonymized ID: {anon_id}  [{status}]")

    count_after = len(store.rows)

    if args.output_dir is not None:
        output_dir = os.path.abspath(args.output_dir)
    else:
        start, end = ac.compute_ranged_output_dir_bounds(count_before, count_after)
        output_dir = os.path.join(SCRIPT_DIR, f"{OUTPUT_DIR_STUDY_PREFIX}_{start}_{end}")
        print(f"\n[output-dir] --output-dir not given -- auto-named {output_dir!r} "
              f"(mapping-CSV record range {start}-{end} for this run).")

    if os.path.normcase(input_dir) == os.path.normcase(output_dir):
        print("[FATAL] --output-dir must not be the same path as --input-dir.", file=sys.stderr)
        return 2
    # Guard against writing into the read-only source tree via a nested path too.
    if os.path.normcase(output_dir).startswith(os.path.normcase(input_dir) + os.sep):
        print("[FATAL] --output-dir must not be located inside --input-dir.", file=sys.stderr)
        return 2

    location_error = ac.validate_mapping_csv_location(mapping_csv_path, input_dir, output_dir)
    if location_error:
        print(f"[FATAL] {location_error}", file=sys.stderr)
        return 2

    os.makedirs(output_dir, exist_ok=True)

    # Pass through genuinely scanned / no-extractable-PII files unchanged
    # (deferred until here -- needs --output-dir, now known, to exist).
    for rec in scanned:
        # No identity key available for a scanned file (no extractable text at
        # all) -- keep the original filename verbatim under the output dir so
        # it is at least traceable back to which input file it came from; it
        # carries no PII to redact in the first place.
        dst = os.path.join(output_dir, rec.filename)
        ac.safe_copy_bytes(rec.src_path, dst)
        print(f"[pass-through] {rec.filename}: scanned / no extractable PII text -> copied unchanged to {dst!r}")

    print()
    redact_exit_code = _run_redaction_and_save(groups, store, output_dir)
    return redact_exit_code or (1 if (unrecognized or missing_key) else 0)


def _run_redaction_and_save(groups: List[PatientGroup], store: "ac.MappingStore", output_dir: str) -> int:
    """Actually redact + save every file in every group, and persist the
    mapping CSV once at the end. Kept as a separate pass (rather than folded
    into main()'s grouping loop) so each file's PyMuPDF Document handle stays
    open exactly as long as it needs to (redact -> save -> close) without
    reopening the source file twice."""
    exit_code = 0
    for group in groups:
        anon_id = store.lookup_only(group.identity_key)
        assert anon_id is not None, "internal error: group processed before ID assignment"
        for rec in group.files:
            doc = fitz.open(rec.src_path)
            try:
                # Template A's field layout is always "Label\n: value" (or
                # "Label: value" on one line) -- confirmed across all 33 KFRE
                # files. Searching for the BARE value string is unsafe when a
                # value happens to be a literal substring of its own field's
                # label text: e.g. patient 8's "Ward/OPD" field has label text
                # "Ward/OPD" and value "OPD" -- page.search_for("OPD") matches
                # BOTH the real value AND the "OPD" substring inside the label
                # itself, over-redacting into the field label (caught via
                # visual render diff during self-test; not a PII leak, but a
                # real over-redaction bug). Prefixing the search string with
                # ": " (colon+space, exactly how every value is introduced in
                # this template) disambiguates the value occurrence from any
                # coincidental substring match inside a label -- validated
                # against all 33 KFRE files with zero missed matches and the
                # Ward/OPD collision confirmed fixed. Template B's layout has
                # no such colon-adjacency convention, so its targets are
                # searched as-is.
                #
                # The identity value (CR No for Template A, Lab No. for
                # Template B) is handled separately from the other PII
                # targets: it is REPLACED with the new Anonymized ID (so the
                # field reads e.g. "CR No : QXMD7192") rather than blanked --
                # see redact_and_replace_value(). The ": " disambiguation
                # prefix is applied to both the search value AND the
                # replacement text for Template A, so the rendered colon
                # survives unchanged and only the digits after it change.
                fields = rec.template_a_fields if rec.template == "A" else rec.template_b_fields
                if fields is not None:
                    blank_targets = fields.blank_redaction_targets()
                    identity_value = fields.identity_target()
                else:
                    blank_targets = list(rec.redaction_targets)
                    identity_value = None

                if rec.template == "A":
                    blank_search_values = [": " + v for v in blank_targets]
                else:
                    blank_search_values = blank_targets

                per_page_hits = []
                for page in doc:
                    n = ac.redact_exact_text_values(page, blank_search_values)
                    if identity_value:
                        if rec.template == "A":
                            n += ac.redact_and_replace_value(page, ": " + identity_value, ": " + anon_id)
                        else:
                            n += ac.redact_and_replace_value(page, identity_value, anon_id)
                    if rec.template == "B":
                        # Specialty-font barcode/2D-barcode spans (same
                        # rationale as anonymize_eGFR.py's Template B path --
                        # these render Lab No. a second time as a scannable
                        # barcode glyph run, not as plain text).
                        n += ac.redact_spans_by_font(page, ["IDAutomation", "Barcode", "3of9"])
                        # Boilerplate lab contact line repeated as real text
                        # inside the page-3 disclaimer box.
                        n += ac.redact_exact_text_values(page, [ac.TEMPLATE_B_BOILERPLATE_CONTACT_LINE])
                        qr_rect = ac.find_template_b_qr_redaction_rect(page)
                        if qr_rect is not None:
                            ac.queue_region_redaction(page, qr_rect)
                            n += 1
                        # Provider addresses ("any kind of address") + the
                        # pathologist signature block ("any kind of
                        # signature") -- same geometry as the eGFR path,
                        # kept here defensively in case a future KFRE batch
                        # ever draws from this external-lab template too.
                        ac.queue_region_redaction(page, ac.TEMPLATE_B_TOP_ADDRESS_RECT)
                        ac.queue_region_redaction(page, ac.TEMPLATE_B_FOOTER_CONTACT_RECT)
                        n += 2
                        processed_at_rect = ac.find_template_b_processed_at_address_rect(page)
                        if processed_at_rect is not None:
                            ac.queue_region_redaction(page, processed_at_rect)
                            n += 1
                        signature_rect = ac.find_template_b_signature_redaction_rect(page)
                        if signature_rect is not None:
                            ac.queue_region_redaction(page, signature_rect)
                            n += 1
                    per_page_hits.append(n)
                    ac.apply_redactions_and_stamp(page, anon_id)

                out_name = f"{anon_id}_{rec.filename}"
                out_path = os.path.join(output_dir, out_name)
                ac.save_redacted_pdf(doc, out_path)

                targets_str = ", ".join(repr(t) for t in rec.redaction_targets)
                print(
                    f"    [redacted] {rec.filename} (template {rec.template}, "
                    f"{len(doc)} page(s), hits/page={per_page_hits}) -> {out_name}"
                )
                print(f"               targets redacted: {targets_str}")

                expected_min_hits = len(rec.redaction_targets) * len(doc)
                if sum(per_page_hits) < expected_min_hits:
                    print(
                        f"    [WARN] {rec.filename}: expected >= {expected_min_hits} total redaction "
                        f"hits ({len(rec.redaction_targets)} target(s) x {len(doc)} page(s)) but only "
                        f"got {sum(per_page_hits)}. A target value may not have appeared verbatim on "
                        f"every page (e.g. an unusual page layout) -- INSPECT THIS FILE MANUALLY.",
                        file=sys.stderr,
                    )
                    exit_code = 1
            finally:
                doc.close()
        print()
    store.save()
    print(f"[DONE] Wrote {sum(len(g.files) for g in groups)} redacted file(s) to {output_dir!r}.")
    print(f"[DONE] Mapping CSV: {store.path!r} -- {ac.CONFIDENTIALITY_NOTICE}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

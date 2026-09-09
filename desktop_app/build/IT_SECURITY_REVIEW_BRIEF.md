# TANUH Renal Anonymizer — IT / Security Review Brief

For whoever administers endpoint security (antivirus / EDR policy) on the
Windows workstations this app will run on. Purpose: get this internal tool
allow-listed centrally, once, so clinical/admin staff never see a security
prompt when they open it.

## What this application is

A local, **fully offline** desktop tool that anonymizes one patient's eGFR or
KFRE data package (DICOM ultrasound images + clinical-report PDFs) before it
leaves a hospital workstation — removing patient-identifying fields and
replacing them with a generated Anonymized ID, using the same redaction
engine already used by this project's batch command-line scripts. It makes
no network connections, does not phone home, and does not transmit any data
anywhere; the only files it writes are the anonymized copies and an
access-restricted local ID-mapping CSV under
`%LOCALAPPDATA%\TANUH-Renal-Anonymizer\` on the same machine.

- Publisher (embedded in the .exe's version info): **TANUH — The AI CoE in
  Healthcare (NIMS)**
- Product name: **TANUH Renal Anonymizer**
- Personalized for: **Nizam's Institute of Medical Sciences (NIMS), Hyderabad**

## Why it may trip antivirus / EDR on first run

This is a Python application packaged into a standalone Windows `.exe` via
**PyInstaller** (a standard, widely-used Python packaging tool — not a
custom or obfuscated build process). Two things commonly cause heuristic
antivirus engines to flag a **brand-new, unsigned** PyInstaller build on
first contact, independent of what the code actually does:

1. It is **unsigned** — no publisher certificate yet, so Windows/AV treat it
   as an unknown/unproven binary (this is the same warning any small
   internally-developed tool gets before it's either signed or allow-listed).
2. The PyInstaller bootloader's self-extracting-archive execution pattern
   is a known trigger for generic heuristic/ML detections across multiple
   AV vendors — a well-documented false-positive category, not a sign of
   actual malicious behavior.

This build already follows the standard mitigations for that: `upx=False`
(no executable compression, which sharply increases false-positive rates),
a `--onedir` layout rather than `--onefile` (less self-extraction-like
behavior at launch), and proper version-resource metadata (publisher/
product/description, above) instead of shipping blank/anonymous.

## What we're asking for

Please allow-list this specific build in your endpoint security console
(Defender for Endpoint / Intune, McAfee ePO, or whatever manages the target
workstations) so it runs without a warning or automatic quarantine on
first launch, using whichever of these your product supports:

- **By file hash** (safest, most specific — recommended):
  - File: `TANUH-Renal-Anonymizer.exe`
  - **SHA-256: `4D734F3343950C5BA827FF4A763D5ECB8EAA8789FCCB31919E87A3F71B9953CD`**
  - Size: 8,389,887 bytes
- **By folder path**, if your product only supports path-based exclusions:
  the full `TANUH-Renal-Anonymizer` distribution folder (the `.exe` plus
  its sibling `_internal\` folder — both must ship together, this is a
  `--onedir` build, not a single self-contained file) wherever it's
  installed on each target workstation.

## Longer-term fix (recommended in addition, not instead of the above)

Code-signing this build with a purchased Authenticode certificate under
NIMS/TANUH's organizational identity would remove the "unknown publisher"
condition entirely and make future updates to this tool (and other internal
NIMS tools) pass review without a repeat of this hand-off. That's a
separate procurement/organizational decision, not a blocker for getting
*this* build cleared today.

## Source / provenance

Built from the project's own source tree (`desktop_app/`, itself a thin
wrapper around this project's existing, already-in-use `anon_common.py` /
`anonymize_eGFR.py` / `anonymize_KFRE.py` redaction scripts) — available for
inspection on request.

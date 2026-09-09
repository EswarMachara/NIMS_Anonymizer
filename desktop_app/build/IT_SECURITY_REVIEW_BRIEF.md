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

- **By file hash** (safest, most specific — recommended). Two files
  matter, and the hash of each changes with every build, so please take
  them from the build being deployed rather than from this document:
  - The installer, `TANUH-Renal-Anonymizer-Setup-<version>.exe`
  - The application itself, `TANUH-Renal-Anonymizer.exe`, which the
    installer writes into the install folder

  Both hashes are printed in the build log of the GitHub Actions run that
  produced the release (steps "Verify build output" and "Build the Windows
  installer"), and can be recomputed from the downloaded files with
  `Get-FileHash <file> -Algorithm SHA256`.

- **By folder path**, if your product only supports path-based exclusions —
  the whole install folder, because this is a `--onedir` build: the `.exe`
  cannot run without its sibling `_internal\` tree.
  - Per-user install (the default, no admin rights required):
    `%LOCALAPPDATA%\Programs\TANUH Renal Anonymizer\`
  - All-users install (chosen by an administrator at install time):
    `%ProgramFiles%\TANUH Renal Anonymizer\`

### Observed behaviour on an unprotected build

On a test workstation running McAfee alongside FortiClient (with Windows
Defender consequently disabled), McAfee quarantined
`TANUH-Renal-Anonymizer.exe` **at launch** — after a clean install, with no
detection name surfaced to the user. The application simply vanished. This
is the exact outcome the allow-list request above is meant to prevent, and
it is why "just run it and see" is not a viable deployment plan for this
tool without your involvement.

## Longer-term fix (recommended in addition, not instead of the above)

Code-signing this build with a purchased Authenticode certificate under
NIMS/TANUH's organizational identity would remove the "unknown publisher"
condition entirely and make future updates to this tool (and other internal
NIMS tools) pass review without a repeat of this hand-off. That's a
separate procurement/organizational decision, not a blocker for getting
*this* build cleared today.

## How it is delivered

As a signed-by-nobody but conventional Windows installer built with **Inno
Setup 6** (`desktop_app/build/installer.iss`): one downloadable `.exe`,
which writes the application, a Start Menu shortcut and an Add/Remove
Programs entry. It requires no administrator rights by default and installs
per-user; an administrator may instead choose an all-users install.

It makes no registry changes beyond its own uninstall registration, installs
no services or drivers, sets no autostart entries, and adds nothing to
`PATH`. Uninstalling removes the program and leaves the operator's data
(ID-mapping CSVs and anonymized output under
`%LOCALAPPDATA%\TANUH-Renal-Anonymizer\`) in place deliberately.

## Source / provenance

Built from the project's own source tree (`desktop_app/`, itself a thin
wrapper around this project's existing, already-in-use `anon_common.py` /
`anonymize_eGFR.py` / `anonymize_KFRE.py` redaction scripts) — available for
inspection on request.

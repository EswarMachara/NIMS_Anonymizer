# TANUH Renal Anonymizer — Desktop App

A local, offline desktop app that runs on a hospital workstation and
anonymizes one patient's eGFR or KFRE package (DICOM ultrasound images +
clinical-report PDFs) before it ever leaves the hospital's network. This is
the "production anonymization logic" the data-collection portal's own
front-end (`Renal-Data-Collection/public/index.html`'s `#anonymization-screen`)
describes but doesn't yet implement — the portal's screen is a UI mockup
with fake demo JS; this app is the real thing, built to match that same
visual template.

Personalized for **Nizam's Institute of Medical Sciences (NIMS), Hyderabad**
(see `backend/engine.py`'s `HOSPITAL_CODE`/`HOSPITAL_NAME` — a future build
for a different partner hospital would only need to change those two
constants and the logo file, not the rest of the app).

## What it does

1. **Select package** — Choose Folder, Choose Files, or drag files onto the
   drop zone. A native OS picker is used for Folder/Files (not a browser
   `<input>`), so it always returns real, absolute filesystem paths.
2. Auto-detects **eGFR** (any `.dcm` file present) vs **KFRE** (PDF-only),
   with a manual override toggle if you need to force one.
3. Runs the **real** `anon_common.py` / `anonymize_eGFR.py` /
   `anonymize_KFRE.py` engines (imported directly, not reimplemented) —
   same redaction logic, same Anonymized-ID mapping CSV, same follow-up
   visit reuse behavior as the batch CLI scripts.
4. **Preview output** — original files vs. the anonymized copy, side by
   side, before you rely on the result.
5. **Approve** — marks the review as done. Deleting the original source
   files is a **separate, explicitly confirmed** action (a button that
   only appears after Approve, gated by a native confirm dialog) — nothing
   ever deletes source data as a side effect of anonymizing it.

## Where data lives

Both the confidential ID-mapping CSVs (`eGFR_anony_Mapping.csv` /
`KFRE_anony_Mapping.csv` — the only files that still link an Anonymized ID
back to a real patient) and the accumulated anonymized output live under:

```
%LOCALAPPDATA%\TANUH-Renal-Anonymizer\
├── eGFR_anony_Mapping.csv
├── KFRE_anony_Mapping.csv
├── Anonymized_eGFR\<AnonID>\...
└── Anonymized_KFRE\<AnonID>\...
```

(`~/.local/share/TANUH-Renal-Anonymizer/` on macOS/Linux, used only for
development/testing on this machine — see `backend/engine.get_app_data_dir()`.)
The app's footer shows these exact paths and opens them on click. Keep this
folder access-restricted; back it up like you would any other PHI-adjacent
record.

## Running from source (development)

```bash
cd desktop_app
python3 -m venv venv && source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt
python3 main.py
```

## Building the Windows .exe

See **`build/WINDOWS_BUILD.md`** — must be run on an actual Windows machine
(PyInstaller does not cross-compile). Short version:

```powershell
cd desktop_app
pip install -r requirements.txt
cd build
pyinstaller app.spec
```

Output: `desktop_app/build/dist/TANUH-Renal-Anonymizer/TANUH-Renal-Anonymizer.exe`

## Project layout

```
desktop_app/
├── main.py              # pywebview host + JS↔Python bridge (Api class)
├── backend/
│   └── engine.py        # single-package adapter around the sibling anon_common.py /
│                         # anonymize_eGFR.py / anonymize_KFRE.py engines (imported, not duplicated)
├── web/                 # UI, adapted from the portal's own anon-* template
│   ├── index.html
│   ├── style.css
│   ├── app.js
│   └── assets/          # tanuh.png, nims-logo.webp (read-only copies)
├── requirements.txt
└── build/
    ├── app.spec          # PyInstaller spec
    ├── app_icon.ico
    └── WINDOWS_BUILD.md
```

## Design notes worth knowing

- **Content-based classification, not folder-name-based.** Any of these
  layouts work as input: this project's own ad-hoc sample layout (flat
  PDFs + `Left kidney`/`Right kidney` folders), or the data-collection
  portal's documented `Patient_Folder/Left_Kidney_Images/.../Lab_Reports/`
  convention. `anon_common.find_pdfs_recursive()` walks the whole selected
  folder; file *type* is always determined by content (DICOM tags / PDF
  template detection), never by which subfolder it happened to sit in.
- **One patient per run.** The batch CLI scripts (`anonymize_eGFR.py
  --input-dir eGFR`) process a whole folder of many numbered patients at
  once with a ranged output-folder name (`eGFR_1_20`). This app processes
  one interactively-selected package at a time, so output just accumulates
  into one persistent `Anonymized_eGFR/<AnonID>/` folder per patient —
  no ranged naming needed here.
- **"Choose Files" staging.** A native multi-file dialog only lets you pick
  from one folder at a time, so `stage_selected_files()` reconstructs
  enough folder structure (kidney-side subfolders preserved by name) in a
  temp directory before handing off to the same identity-resolution code
  path a folder selection uses. The original files are only ever copied,
  never moved.
- **Drag-and-drop** uses pywebview's documented DOM event bridge, which
  injects a `pywebviewFullPath` onto dropped files (browsers otherwise hide
  real paths for security) — see `main.py`'s `_setup_drag_and_drop()` for
  the exact mechanism and its documented fallback behavior. Only individual
  *files* can be dropped (HTML5 drag-and-drop can't enumerate a dropped
  folder's contents) — Choose Folder remains the reliable way to select a
  whole package in one action.

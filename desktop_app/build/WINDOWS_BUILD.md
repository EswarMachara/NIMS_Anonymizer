# Building the Windows .exe

This must be done **on a Windows machine** — PyInstaller does not
cross-compile; a Linux/Mac build of PyInstaller cannot produce a Windows
`.exe`. Follow this once on your Windows laptop.

## 0. One-time prerequisites

1. **Python 3.11 or 3.12 (64-bit)** from [python.org](https://www.python.org/downloads/windows/).
   During install, check **"Add python.exe to PATH"**.
   (Avoid brand-new Python releases like 3.13/3.14 for this build — some of
   our dependencies' prebuilt Windows wheels lag behind new Python releases
   by a few months. 3.11/3.12 has full, solid wheel support for everything
   here today.)
2. **Microsoft Edge WebView2 Runtime** — this is what actually renders the
   app's UI on Windows (pywebview uses it instead of bundling its own
   browser engine).
   - **Windows 11**: already included, nothing to do.
   - **Windows 10**: install the "Evergreen Bootstrapper" from
     <https://developer.microsoft.com/microsoft-edge/webview2/> if it's not
     already present (most Windows 10 machines with Edge installed already
     have it). The app will show a blank/black window without this.

## 1. Copy the project onto the Windows laptop

Copy the whole `NIMS/` project folder over (USB drive, network share, or
`git clone` if it's in a repo) — **keep the folder layout intact**:

```
NIMS/
├── anon_common.py
├── anonymize_eGFR.py
├── anonymize_KFRE.py
└── desktop_app/
    ├── main.py
    ├── backend/
    ├── web/
    ├── requirements.txt
    └── build/
        ├── app.spec
        ├── app_icon.ico
        └── WINDOWS_BUILD.md   <- you are here
```

`desktop_app/` must stay a **direct subfolder** of the same folder holding
the three `anonymize_*.py` / `anon_common.py` files — the app imports them
from its parent directory.

## 2. Set up a virtual environment and install dependencies

Open **PowerShell** (or Command Prompt), `cd` into `desktop_app/`, then:

```powershell
cd path\to\NIMS\desktop_app
python -m venv venv
venv\Scripts\activate
pip install --upgrade pip
pip install -r requirements.txt
```

## 3. Run it from source first (before packaging)

Always test this before building the `.exe` — it's much faster to fix a
problem here than to rebuild the whole package repeatedly:

```powershell
python main.py
```

The app window should open. Test the real workflow: **Choose Folder** on
one of the sample `eGFR/NP1 (CKD)` or `KFRE`-style patient folders, confirm
it anonymizes correctly, and check the output folder it reports.

## 4. Build the .exe

```powershell
cd build
pyinstaller app.spec
```

First build takes a minute or two. Output goes to:

```
desktop_app\build\dist\TANUH-Renal-Anonymizer\TANUH-Renal-Anonymizer.exe
```

This is a **"--onedir" build** — a whole folder, not a single file. That's
deliberate: it starts faster than a "--onefile" build (which re-extracts
itself to a temp folder on every launch) and is less likely to trip
antivirus/SmartScreen heuristics that specifically target self-extracting
single-file PyInstaller binaries. Distribute the **whole
`TANUH-Renal-Anonymizer` folder** (zip it), not just the `.exe` inside it —
the `.exe` needs its sibling files in that same folder to run.

To rebuild after any code change:

```powershell
pyinstaller --clean app.spec
```

(`--clean` clears PyInstaller's cache — use it if a rebuild ever behaves
oddly after editing `app.spec` itself.)

## 5. First run on a clean machine — what to expect

- **SmartScreen warning** ("Windows protected your PC"): expected for an
  unsigned executable from a new publisher. Click "More info" → "Run
  anyway". This goes away once/if the exe is code-signed with a purchased
  certificate — not required to function, only to skip this warning.
- **Antivirus scan on first launch**: some AV products briefly scan a new
  unrecognized `.exe` the first time it runs (a few seconds delay). Normal.
- **Where the app keeps its data**: `%LOCALAPPDATA%\TANUH-Renal-Anonymizer\`
  — the confidential ID-mapping CSVs and the accumulated anonymized output
  folders live there, not inside the installed app folder (so they survive
  reinstalling/updating the app itself). The app's own footer shows the
  exact paths and lets you click to open them.

## 5b. Build the installer

Step 4 produces a folder. Step 5b turns it into the single file people
actually download.

```powershell
# One time, pinned to the version CI uses and this script is tested against
winget install --id JRSoftware.InnoSetup --version 6.7.3 --exact

# Every build, from desktop_app/build/, AFTER pyinstaller has run
& "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe" installer.iss
```

Output: `Output\TANUH-Renal-Anonymizer-Setup-1.0.0.exe` — about 38 MB,
down from 108 MB unpacked. Pass `/DAppVersion=1.2.3` to stamp a different
version (CI does this from the release tag).

`.github/workflows/build-windows-exe.yml` runs all of the above on a clean
runner, then **installs the result and self-tests the installed copy**
before uploading it. That last part is the check that matters: self-testing
`dist/` proves PyInstaller produced a working app, not that the installer
ships all of it.

## 6. Distributing to other hospital workstations

**Give people the installer.** It is one file, it puts the app in the Start
Menu so it can be opened by name, and it registers an entry in Add/Remove
Programs. Hand end users [`INSTALL.md`](../../INSTALL.md) rather than this
document.

The installer also fixes a real defect, not just an inconvenience. Copying
or downloading the unpacked folder as a **zip** gives every extracted file a
Mark-of-the-Web stream (`Zone.Identifier`, `ZoneId=3`), and .NET refuses to
load a managed assembly that carries one — which is how pywebview reaches
WebView2. The app died at launch with `Python.Runtime.Loader.Initialize`
until all 173 files were unblocked by hand. Inno writes its payload from its
own archive, so installed files carry no such stream; CI asserts this on
every build.

Copying the unpacked `TANUH-Renal-Anonymizer` folder directly (over a share
or on a USB stick, not through a browser download or a zip) still works and
needs no Python on the target. Only the WebView2 Runtime prerequisite
(step 0) applies either way, and the installer checks for it.

## Troubleshooting

- **"Failed to execute script main" / import errors mentioning
  anon_common**: the three sibling `.py` files weren't found during the
  build. Re-check the folder layout in step 1, and that you ran
  `pyinstaller app.spec` from inside `desktop_app/build/` (the spec's
  relative paths assume that working directory).
- **Blank/black window**: WebView2 Runtime isn't installed — see step 0.
- **Antivirus deletes/quarantines the .exe immediately after building**:
  a false positive (common with PyInstaller builds, more so if `upx=True`
  is turned on in `app.spec` — this spec already ships with `upx=False`
  for exactly this reason). Add an exclusion for the `dist/` folder in your
  AV settings during development, or submit the file to your AV vendor for
  reclassification before wide distribution.
- **Need a single-file .exe instead**: change `exclude_binaries=True` to
  `False` in the `EXE(...)` block of `app.spec`, remove the trailing
  `COLLECT(...)` block, and rebuild — this is the "--onefile" equivalent.
  Slower to launch and more antivirus-sensitive; only do this if you have a
  specific reason to prefer a single file over a folder.

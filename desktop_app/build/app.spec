# app.spec -- PyInstaller build spec for the TANUH Renal Anonymizer desktop app.
#
# Build (run from inside desktop_app/build/, on WINDOWS -- PyInstaller does
# not cross-compile, this must run on the OS you're building for):
#     pyinstaller app.spec
#
# Output: desktop_app/build/dist/TANUH-Renal-Anonymizer/TANUH-Renal-Anonymizer.exe
# (a "--onedir" build: a folder, not a single file -- see WINDOWS_BUILD.md
# for why this is the recommended default over --onefile).
#
# See WINDOWS_BUILD.md in this same folder for the full, step-by-step
# first-time build walkthrough.

import sys

block_cipher = None

# anon_common.py / anonymize_eGFR.py / anonymize_KFRE.py live one level
# above desktop_app/ (project root) and are imported via a plain
# `import anon_common` (see backend/engine.py) rather than a package-
# relative import, so PyInstaller's static analysis needs an explicit
# nudge in both directions: `pathex` so it can FIND them to analyze/bundle
# in the first place, and `hiddenimports` so it actually includes them
# even though no import statement anywhere names them as a literal string
# constant it can trace automatically.
a = Analysis(
    ['../main.py'],
    pathex=['..', '../..'],
    binaries=[],
    datas=[
        ('../web', 'web'),
    ],
    hiddenimports=[
        'anon_common',
        'anonymize_eGFR',
        'anonymize_KFRE',
        'backend.engine',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    cipher=block_cipher,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='TANUH-Renal-Anonymizer',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,          # UPX-compressed PyInstaller binaries are a common
                         # antivirus false-positive trigger; leave off for
                         # a hospital-IT-facing build.
    console=False,       # windowed app, no terminal popup
    icon='app_icon.ico',
    version='version_info.txt',  # CompanyName/ProductName/etc. -- see that
                                  # file's header comment for why this matters
                                  # for AV/EDR heuristics on an unsigned exe.
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='TANUH-Renal-Anonymizer',
)

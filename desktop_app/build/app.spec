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

from PyInstaller.utils.hooks import collect_submodules, copy_metadata

block_cipher = None

# pydicom loads its pixel-data decoder plugins by importing them DYNAMICALLY
# by name at decode time, so PyInstaller's static analysis never sees them and
# would ship a build that silently anonymizes the report PDFs while writing
# ZERO ultrasound images (every .dcm failing with "Unable to decompress ...
# plugins ... are all missing dependencies"). They must therefore be forced in
# by hand -- see the decoder block in ../requirements.txt.
#
# pylibjpeg additionally finds its own codec (the `libjpeg` module) through
# setuptools entry points, which PyInstaller does not preserve unless the
# package metadata is bundled too -- hence copy_metadata() for both. `pillow`
# (imported as PIL) is a deliberate second decoder: it bundles very reliably
# and covers JPEG Baseline on its own, so it still works even if pylibjpeg's
# entry-point discovery fails inside the frozen app.
DICOM_DECODER_HIDDENIMPORTS = [
    'pylibjpeg',
    'libjpeg',
    'PIL',
    'PIL.Image',
    # numpy is imported by pydicom LAZILY, from inside pixel-data conversion
    # -- nothing in this app or the three engine scripts ever writes
    # `import numpy`, so PyInstaller's static analysis cannot see it and the
    # packaged app failed every image with "NumPy is required when
    # converting pixel data to an ndarray" while source runs worked fine.
    'numpy',
    # pydicom selects its decoder plugin module by name at decode time, same
    # dynamic-import problem one level up.
    'pydicom.pixels',
    'pydicom.pixels.decoders',
    'pydicom.pixels.decoders.pylibjpeg',
    'pydicom.pixels.decoders.pillow',
] + collect_submodules('numpy._core')
# numpy._core's internals import their own submodules dynamically, so the
# bare 'numpy' hiddenimport above still left numpy._core._exceptions out of
# the bundle (confirmed: 280 numpy modules collected, 26 under numpy._core,
# that one absent). numpy's C-extension import then failed, and every later
# `import numpy` in the process reported the misleading "cannot load module
# more than once per process" instead -- which pydicom relabelled again as
# "NumPy is required...". Enumerating the package rather than trusting the
# import graph stops that class of gap; scoped to numpy._core specifically
# instead of all of numpy, to avoid dragging in f2py/distutils/testing.
DICOM_DECODER_METADATA = (
    copy_metadata('pylibjpeg')
    + copy_metadata('pylibjpeg-libjpeg')
    + copy_metadata('pillow')
)

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
    ] + DICOM_DECODER_METADATA,
    hiddenimports=[
        'anon_common',
        'anonymize_eGFR',
        'anonymize_KFRE',
        'backend.engine',
    ] + DICOM_DECODER_HIDDENIMPORTS,
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

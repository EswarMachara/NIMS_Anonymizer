#!/usr/bin/env python3
"""
main.py -- TANUH Renal Anonymizer desktop app entry point.

Hosts the web/ UI (adapted from the Renal-Data-Collection portal's own
"Anonymization Tool" template) in a native window via pywebview, and
exposes a small JS-Python bridge (the Api class below) that the page's
app.js calls into for every action that needs real file-system / redaction
work -- native folder/file pickers, running the actual anonymization
engine (backend/engine.py), and revealing output in the OS file explorer.

Run directly for development:
    python3 main.py
Packaged for Windows via PyInstaller -- see build/app.spec and
build/WINDOWS_BUILD.md.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import traceback

import webview
from webview.dom import DOMEventHandler

if getattr(sys, "frozen", False):
    # Running as a PyInstaller-built .exe -- sys._MEIPASS is PyInstaller's
    # own extraction directory at runtime (both onefile and onedir modes),
    # the standard, version-stable way to locate bundled data files (see
    # build/app.spec's `datas` entry for web/). Relying on __file__ instead
    # is not reliably consistent across PyInstaller build modes.
    APP_DIR = sys._MEIPASS  # type: ignore[attr-defined]
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(APP_DIR, "web")

# numpy is imported here, before `backend.engine` pulls in pydicom (and
# through it pylibjpeg, which imports numpy itself). Two reasons:
#
#  1. Diagnostics. If numpy's import fails, the FIRST failure is the real
#     one; by the time anything else has touched it, a rolled-back partial
#     import leaves the C extension registered and every later attempt
#     reports the useless "cannot load module more than once per process"
#     instead of the actual cause.
#  2. It makes the successful module the one everything downstream shares,
#     rather than each consumer racing to initialise it.
try:
    import numpy  # noqa: F401
    _NUMPY_IMPORT_TRACEBACK = None
except Exception:  # noqa: BLE001
    _NUMPY_IMPORT_TRACEBACK = traceback.format_exc()

if not getattr(sys, "frozen", False):
    # Source runs need this directory importable so `from backend import
    # engine` resolves. When FROZEN it must be skipped: __file__ then sits
    # inside sys._MEIPASS (the onedir `_internal` folder), so this would put
    # the bundle's own binary directory on sys.path and give numpy a second,
    # competing import route -- see the matching note in backend/engine.py.
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from backend import engine  # noqa: E402


class Api:
    """Every method here is callable from JS as
    window.pywebview.api.<method_name>(...) and must return a
    JSON-serializable value (pywebview handles the marshalling). Any
    exception raised here is caught and turned into a
    {"success": False, "errors": [...]} payload rather than propagating
    as an opaque JS-side rejection with no useful message."""

    def __init__(self):
        # Server-side allowlist of paths the UI is currently permitted to
        # delete via delete_original_files() -- populated only from the
        # ACTUAL "original" paths in the most recent successful
        # process_package() result, never from whatever the JS side sends.
        # This is a defense-in-depth backstop (devtools are disabled --
        # webview.start() below is called without debug=True -- so the JS
        # bridge isn't normally reachable except through this page's own
        # code) so a bug anywhere upstream can never turn into deleting an
        # unrelated file: delete_original_files() rejects any path not in
        # this set instead of trusting its argument blindly.
        self._deletable_paths = set()

    def get_app_info(self):
        return engine.get_app_info()

    def pick_folder(self):
        """Native OS folder picker -- the primary, most reliable "Choose
        Folder" path (works identically for the portal's Patient_Folder/
        convention and this project's own ad-hoc sample-data layout,
        since classification is content-based, not folder-name-based)."""
        window = webview.windows[0]
        result = window.create_file_dialog(webview.FOLDER_DIALOG)
        if not result:
            return {"picked": False}
        return {"picked": True, "paths": [result[0]], "is_folder": True}

    def pick_files(self):
        """Native OS multi-file picker -- the "Choose Files" fallback for
        a package whose files aren't all under one folder the user can
        point at directly."""
        window = webview.windows[0]
        result = window.create_file_dialog(webview.OPEN_DIALOG, allow_multiple=True)
        if not result:
            return {"picked": False}
        return {"picked": True, "paths": list(result), "is_folder": False}

    def pick_mapping_csv(self):
        """
        Choose an EXISTING ID-mapping CSV to continue from.

        This is the file that makes a returning patient resolve to the
        Anonymized ID they were already given, and it doubles as the list
        of issued IDs a newly minted one is checked against. Picking the
        wrong one, or forgetting it, means a follow-up visit silently
        becomes a second patient. Hence a dedicated picker rather than an
        implied default the operator never sees.

        A companion file sits next to it -- <same name>_processed_files.json,
        the record of which files have already been anonymized (see
        anon_common.ledger_path_for_mapping_csv). Only the CSV is picked
        here, because the companion is found from the CSV's own path; but
        the two must be COPIED together when a mapping is moved between
        machines, or repeat submissions stop being recognised. The session
        panel says so when the companion is missing.
        """
        window = webview.windows[0]
        result = window.create_file_dialog(
            webview.OPEN_DIALOG,
            allow_multiple=False,
            file_types=("CSV mapping file (*.csv)", "All files (*.*)"),
        )
        if not result:
            return {"picked": False}
        return {"picked": True, "path": result[0]}

    def pick_output_folder(self):
        """Choose where anonymized output is written (a per-study subfolder
        is still created inside it, so two studies can share one
        destination without mixing)."""
        window = webview.windows[0]
        result = window.create_file_dialog(webview.FOLDER_DIALOG)
        if not result:
            return {"picked": False}
        return {"picked": True, "path": result[0]}

    def describe_session(self, mapping_csv=None, output_dir=None, study="egfr"):
        """Resolve what THIS run would actually use, and report what the
        mapping file already contains, so the operator can see whether they
        are continuing an existing ID space or starting a new one before
        anything is written."""
        try:
            return {"success": True, **engine.describe_session(mapping_csv, output_dir, study)}
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "errors": [str(exc)]}

    def process_package(self, paths, is_folder, study_override=None, mapping_csv=None, output_dir=None):
        # Reset the allowlist on every new run -- only the package that was
        # JUST processed (below) is ever eligible for deletion, never a
        # stale set left over from an earlier package in this session.
        self._deletable_paths = set()
        try:
            result = engine.process_package(
                paths,
                is_folder,
                study_override or None,
                mapping_csv or None,
                output_dir or None,
            )
            self._deletable_paths = {
                pair["original"] for pair in result.get("preview_pairs", []) if pair.get("original")
            }
            return result
        except Exception as exc:  # noqa: BLE001 -- surface any failure to the UI, never crash silently
            return {
                "success": False,
                "errors": [f"Unexpected error: {exc}"],
                "traceback": traceback.format_exc(),
            }

    # ---- Cross-check review -------------------------------------------
    # Each preview row is fetched on demand as it scrolls into view rather
    # than all at once: one patient's ten ultrasound frames decode to tens
    # of megabytes, and inlining that into the page would stall the webview.

    def get_crosscheck_manifest(self, preview_pairs):
        try:
            return {"success": True, **engine.get_crosscheck_manifest(preview_pairs or [])}
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "errors": [f"Could not build the cross-check list: {exc}"]}

    def render_dicom_preview(self, path):
        return engine.render_dicom_preview(path)

    def render_pdf_preview(self, path, page_index=0):
        return engine.render_pdf_preview(path, int(page_index or 0))

    def get_dicom_metadata_pair(self, original_path, anonymized_path):
        return engine.get_dicom_metadata_pair(original_path, anonymized_path)

    def reveal_in_explorer(self, path):
        """Open the OS file browser at `path` (or its parent folder, if
        `path` is a file) -- lets the operator jump straight to the
        anonymized output or the confidential mapping CSV's folder."""
        target = path if os.path.isdir(path) else os.path.dirname(path)
        try:
            if sys.platform.startswith("win"):
                os.startfile(target)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", target])
            else:
                subprocess.Popen(["xdg-open", target])
            return {"success": True}
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "errors": [str(exc)]}

    def delete_original_files(self, paths):
        """
        Permanently deletes the given ORIGINAL source file(s) from disk.
        Deliberately a separate, explicit call from process_package() --
        anonymization never deletes source data as a side effect, only
        this method does, and only when the operator has explicitly
        confirmed it (see app.js: a native confirm() dialog gates every
        call to this). Never called automatically. Only ever deletes the
        exact paths passed in -- never a directory, never a glob.

        Defense in depth: every path must also be in self._deletable_paths
        (set by process_package() to exactly that run's own original file
        paths) -- a path this app didn't itself just process is refused
        outright, never touched, regardless of what the caller passes.
        """
        deleted, errors = [], []
        for p in paths:
            if p not in self._deletable_paths:
                errors.append(f"{p}: refused -- not part of the most recently processed package")
                continue
            try:
                if os.path.isfile(p):
                    os.remove(p)
                    deleted.append(p)
                    self._deletable_paths.discard(p)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{p}: {exc}")
        return {"success": not errors, "deleted": deleted, "errors": errors}


def _setup_drag_and_drop(window: "webview.Window"):
    """
    Native drag-and-drop wiring for the #package-tile drop zone. Browsers
    deliberately withhold a dropped file's real filesystem path for
    security, but pywebview's own DOM event bridge (window.dom.get_element
    + Element.on('drop', ...)) injects a 'pywebviewFullPath' key onto each
    dropped file entry specifically to solve this -- confirmed present in
    the installed pywebview version's source (webview/util.py). This is
    the officially-supported mechanism, not a hack, but drag-and-drop
    backend behavior can still vary across platforms/webview engines, so
    every path is defensively re-validated with os.path.exists() before
    use, and any file missing pywebviewFullPath is skipped with a clear
    on-page message rather than silently failing -- the Choose Folder /
    Choose Files buttons remain the guaranteed-reliable path regardless.

    A whole-FOLDER drag (as opposed to individual files) is not handled
    here -- HTML5 drag-and-drop only exposes a flat file list, not nested
    directory contents, so folder drags are intentionally left to the
    native "Choose Folder" dialog; the UI copy sets this expectation.
    """
    window.events.loaded.wait()
    tile = window.dom.get_element("#package-tile")
    if tile is None:
        return

    def on_dragover(_event):
        window.evaluate_js("window.__anonSetDragOver && window.__anonSetDragOver(true)")

    def on_dragleave(_event):
        window.evaluate_js("window.__anonSetDragOver && window.__anonSetDragOver(false)")

    def on_drop(event):
        window.evaluate_js("window.__anonSetDragOver && window.__anonSetDragOver(false)")
        files = ((event or {}).get("dataTransfer") or {}).get("files") or []
        paths = [f["pywebviewFullPath"] for f in files if f.get("pywebviewFullPath")]
        paths = [p for p in paths if os.path.exists(p)]
        if not paths:
            window.evaluate_js(
                "window.__anonOnDropResolved && window.__anonOnDropResolved(null, "
                "'Could not read a real file path from that drop -- please use Choose Folder or Choose Files instead.')"
            )
            return
        window.evaluate_js(f"window.__anonOnDropResolved && window.__anonOnDropResolved({json.dumps(paths)}, null)")

    tile.on("dragover", DOMEventHandler(on_dragover, prevent_default=True))
    tile.on("dragleave", DOMEventHandler(on_dragleave, prevent_default=True))
    tile.on("drop", DOMEventHandler(on_drop, prevent_default=True, stop_propagation=True))


def self_test() -> int:
    """
    Verify that this build can actually do its job, and exit non-zero if it
    cannot. Run by CI against the BUILT .exe (`--self-test`).

    This exists because every packaging bug in this app so far has been the
    same shape: a dependency that pydicom imports dynamically, which
    PyInstaller's static analysis cannot see, so it is missing from the
    bundle while running from source works perfectly. Checking the build
    ENVIRONMENT catches none of that -- the environment always has
    everything. Only the frozen app can answer whether the frozen app
    works.

    Two failures already shipped this way and were found by a human running
    the exe: no JPEG decoder plugin (every ultrasound image failed), then
    numpy missing (same, one layer down). Both are asserted here.

    Deliberately does not touch patient data: it proves the decode PATH is
    wired up, not that any particular file redacts correctly.
    """
    problems = []

    if _NUMPY_IMPORT_TRACEBACK:
        # The first, real numpy failure, captured at module import time before
        # anything else could turn it into "cannot load module more than once".
        problems.append("numpy failed to import at startup (first failure, see traceback)")
        print("numpy FIRST import traceback:")
        print(_NUMPY_IMPORT_TRACEBACK)

    try:
        import numpy
        print(f"numpy: OK ({numpy.__version__})")
        print(f"       numpy.__file__ = {getattr(numpy, '__file__', '?')}")
    except Exception as exc:  # noqa: BLE001
        # Full traceback, not just the message: the failure that cost two
        # builds here ("cannot load module more than once per process") says
        # nothing about WHICH import route collided, and pydicom relabels it
        # as "NumPy is required..." further up. The frame list is the only
        # thing that actually locates it.
        problems.append(f"numpy is not importable in this build: {exc}")
        print("numpy import traceback:")
        traceback.print_exc()
        print(f"  sys.frozen   = {getattr(sys, 'frozen', False)}")
        print(f"  sys._MEIPASS = {getattr(sys, '_MEIPASS', '<unset>')}")
        print("  sys.path:")
        for entry in sys.path:
            print(f"    - {entry}")

    try:
        from pydicom.pixels import get_decoder
        from pydicom.uid import JPEGBaseline8Bit

        plugins = get_decoder(JPEGBaseline8Bit).available_plugins
        print(f"JPEG Baseline decoder plugins: {plugins}")
        if not plugins:
            problems.append(
                "no JPEG Baseline decoder plugin is available -- every ultrasound "
                "image would silently fail to anonymize"
            )
    except Exception as exc:  # noqa: BLE001
        problems.append(f"pydicom pixel decoding is unavailable in this build: {exc}")

    try:
        import fitz  # PyMuPDF, used for PDF redaction and report previews
        print("PyMuPDF: OK")
    except Exception as exc:  # noqa: BLE001
        problems.append(f"PyMuPDF (fitz) is not importable in this build: {exc}")

    for module in ("anon_common", "anonymize_eGFR", "anonymize_KFRE"):
        try:
            __import__(module)
            print(f"{module}: OK")
        except Exception as exc:  # noqa: BLE001
            problems.append(f"{module} is not importable in this build: {exc}")

    # Repeat detection, exercised end to end on a throwaway file rather than
    # merely imported: a broken ledger here would not crash anything, it
    # would quietly re-anonymize work already done, which is precisely the
    # class of failure nobody notices until the output folder is wrong.
    try:
        import anon_common as _ac

        with tempfile.TemporaryDirectory() as tmp:
            probe = os.path.join(tmp, "probe.bin")
            with open(probe, "wb") as f:
                f.write(b"repeat-detection self-test")
            digest = _ac.sha256_file(probe)

            ledger = _ac.ProcessedFileLedger.load_or_create(
                _ac.ledger_path_for_mapping_csv(os.path.join(tmp, "probe_Mapping.csv"))
            )
            fresh = _ac.classify_repeat([("probe.bin", digest, 26)], "TEST1234", ledger, True)
            ledger.record(digest, anon_id="TEST1234", study="egfr", name="probe.bin", size=26)
            ledger.save()

            reloaded = _ac.ProcessedFileLedger.load_or_create(ledger.path)
            same = _ac.classify_repeat([("probe.bin", digest, 26)], "TEST1234", reloaded, True)
            other = _ac.classify_repeat([("probe.bin", digest, 26)], "OTHER999", reloaded, True)

        if fresh.verdict != "new":
            problems.append(f"an unseen file was not treated as new (got {fresh.verdict!r})")
        if not same.skip:
            problems.append(f"a repeat submission was not recognised (got {same.verdict!r})")
        if not other.conflicts:
            problems.append("a file re-filed under a different Anonymized ID was not flagged")
        print(f"repeat detection: OK (new -> {fresh.verdict}, repeat -> {same.verdict}, "
              f"{len(other.conflicts)} conflict(s) caught)")
    except Exception as exc:  # noqa: BLE001
        problems.append(f"repeat detection is broken in this build: {exc}")
        traceback.print_exc()

    if problems:
        print("\nSELF-TEST FAILED:")
        for p in problems:
            print(f"  - {p}")
        return 1

    print("\nSELF-TEST PASSED")
    return 0


def main():
    if "--self-test" in sys.argv[1:]:
        raise SystemExit(self_test())

    try:
        engine.cleanup_stale_staging_dirs()
    except Exception:  # noqa: BLE001 -- best-effort only, never block startup
        pass
    api = Api()
    window = webview.create_window(
        "TANUH Renal Anonymizer",
        url=os.path.join(WEB_DIR, "index.html"),
        js_api=api,
        width=1360,
        height=880,
        min_size=(1040, 720),
        # Open maximized. The workbench is meant to be taken in as one
        # frame, and on a laptop screen the difference between an 880px
        # window and the full display is exactly the difference between the
        # action row being in view and being below the fold. width/height
        # above remain the size the window restores to when un-maximized.
        maximized=True,
        background_color="#eaf9f6",
    )
    webview.start(_setup_drag_and_drop, window)


if __name__ == "__main__":
    main()

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

    def process_package(self, paths, is_folder, study_override=None):
        # Reset the allowlist on every new run -- only the package that was
        # JUST processed (below) is ever eligible for deletion, never a
        # stale set left over from an earlier package in this session.
        self._deletable_paths = set()
        try:
            result = engine.process_package(paths, is_folder, study_override or None)
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


def main():
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
        background_color="#eaf9f6",
    )
    webview.start(_setup_drag_and_drop, window)


if __name__ == "__main__":
    main()

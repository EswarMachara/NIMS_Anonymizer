"""
Dev-only: render the workbench in the SAME WebView2 engine the app ships
with, drive it, then assert on the DOM it produced.

Catches the class of bug a Python-side test cannot: a status class that
never got CSS, a note row that renders empty, a session panel that does not
repaint. Runs the real web/app.js -- nothing about the rendering is
reimplemented here.

The bridge is a real pywebview js_api, not a JavaScript stub. An earlier
version assigned window.pywebview.api by hand and pywebview overwrote it
during its own startup, so calls silently became "is not a function"
partway through a run -- the harness reported a frozen UI that was entirely
its own doing. Going through the real bridge also means the logic under
test (describe_session, detect_study, suggest_destination) is the REAL
backend; only what a test genuinely cannot do is substituted: native file
dialogs, and the anonymization run itself.

    ui_preview.py [payload.json] [driver.js] [WxH]
"""

import json
import os
import re
import sys
import time

import webview

BUILD_DIR = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.path.dirname(BUILD_DIR)
WEB_DIR = os.path.join(APP_DIR, "web")
PREVIEW = os.path.join(WEB_DIR, "_ui_preview.html")

sys.path.insert(0, APP_DIR)
from backend import engine  # noqa: E402

args = sys.argv[1:]
payload_path = next((a for a in args if a.lower().endswith(".json")), None)
driver_path = next((a for a in args if a.lower().endswith(".js")), None)
size = next((a for a in args if re.fullmatch(r"\d+x\d+", a)), "1500x950")
WIN_W, WIN_H = (int(v) for v in size.split("x"))

payload = json.load(open(payload_path, encoding="utf-8")) if payload_path else {
    "success": True, "batch": True, "study": "kfre", "patients": [],
    "summary": {"total": 0, "succeeded": 0, "failed": 0, "new_ids": 0, "reused_ids": 0,
                "skipped_duplicates": 0, "partial_repeats": 0, "conflicts": 0},
    "errors": [],
}
driver = open(driver_path, encoding="utf-8").read() if driver_path else \
    'renderBatchResult(PAYLOAD, "KFRE");'

# Where a stubbed "Browse folder" pretends the operator navigated to.
PICKED_OUTPUT_DIR = os.path.join(os.environ.get("TEMP", "."), "nims_preview_chosen_output")

# A sample selection the drivers can use without hardcoding a machine path.
SELECTION = next((a for a in args if os.path.isdir(a)), None) or os.path.join(
    os.environ.get("TEMP", "."), "nims_flow", "sample_NIMS", "KFRE")
os.makedirs(SELECTION, exist_ok=True)


class PreviewApi:
    """Real backend for everything that can be exercised for real."""

    def get_app_info(self):
        return engine.get_app_info()

    def describe_session(self, mapping_csv=None, output_dir=None, study="egfr"):
        return engine.describe_session(mapping_csv or None, output_dir or None, study)

    def detect_study(self, paths, is_folder):
        return {"success": True,
                "study": engine.detect_study_for_selection(list(paths or []), bool(is_folder))}

    def suggest_destination(self, paths, is_folder):
        return {"success": True,
                "path": engine.suggest_output_dir(list(paths or []), bool(is_folder))}

    # -- substituted: a test cannot open a native dialog, and must not
    #    actually anonymize anything --
    def pick_output_folder(self):
        os.makedirs(PICKED_OUTPUT_DIR, exist_ok=True)
        return {"picked": True, "path": PICKED_OUTPUT_DIR}

    def pick_mapping_csv(self):
        return {"picked": True, "path": os.path.join(PICKED_OUTPUT_DIR, "carried_over.csv")}

    def pick_folder(self):
        return {"picked": False}

    def pick_files(self):
        return {"picked": False}

    def process_package(self, paths, is_folder, study_override=None,
                        mapping_csv=None, output_dir=None):
        return payload

    def get_crosscheck_manifest(self, preview_pairs):
        return {"success": True, "images": [], "reports": [], "metadata": []}

    def reveal_in_explorer(self, path):
        return {"success": True}


index = open(os.path.join(WEB_DIR, "index.html"), encoding="utf-8").read()
body = re.search(r"<body>(.*)</body>", index, re.S).group(1)
body = body.replace('<script src="app.js"></script>', "")

# The charset matters: the preview page takes index.html's BODY only, so
# without this it inherits no encoding and mangles every non-ASCII
# character the UI renders -- the ellipsis in a shortened path included.
open(PREVIEW, "w", encoding="utf-8").write(f"""<meta charset="utf-8">
<link rel="stylesheet" href="style.css">
{body}
<script>
window.PAYLOAD = {json.dumps(payload)};
window.SELECTION = {json.dumps(SELECTION)};
const SELECTION = window.SELECTION;
window.__err = null;
window.__rejections = [];
window.__console = [];
window.addEventListener('error', e => {{ window.__err = window.__err || String(e.message); }});
window.addEventListener('unhandledrejection', e => {{
  window.__rejections.push(String((e.reason && (e.reason.stack || e.reason.message)) || e.reason));
}});
(function () {{
  const real = console.error;
  console.error = function (...a) {{ window.__console.push(a.map(String).join(' ')); real.apply(console, a); }};
}})();
</script>
<script src="app.js"></script>
<script>
// A flag, not a Promise: pywebview's evaluate_js does not await promises,
// so polling for a plain boolean is the only reliable way to know the
// driver has finished. Reading the DOM before it has is how a harness
// reports a passing UI that never actually rendered.
window.__done = false;
window.addEventListener('pywebviewready', () => {{
  (async () => {{
    try {{
      await refreshSession();
      {driver}
    }} catch (e) {{ window.__err = String(e && e.stack || e); }}
    window.__done = true;
  }})();
}});
</script>
""")

results = {}


def inspect(window):
    deadline = time.time() + 40
    while not window.evaluate_js("window.__done === true") and time.time() < deadline:
        time.sleep(0.1)
    if not window.evaluate_js("window.__done === true"):
        results["timed_out"] = True
    results["csv_path"] = window.evaluate_js("document.getElementById('csv-path').innerText")
    results["output_path"] = window.evaluate_js("document.getElementById('out-path').innerText")
    results["study_note"] = window.evaluate_js(
        "document.getElementById('study-detected-note')?.innerText || ''")
    results["banner"] = window.evaluate_js("document.getElementById('result-banner').innerText")
    results["summary"] = window.evaluate_js("document.getElementById('batch-summary').innerText")
    results["csv_hint"] = window.evaluate_js("document.getElementById('csv-hint').innerText")
    results["rows"] = window.evaluate_js("""
      Array.from(document.querySelectorAll('#batch-tbody tr')).map(tr => ({
        cls: tr.className,
        status: tr.querySelector('.batch-status')?.textContent || '',
        statusCls: tr.querySelector('.batch-status')?.className || '',
        text: tr.innerText.replace(/\\s+/g, ' ').trim().slice(0, 700),
      }))
    """)
    results["overflow"] = window.evaluate_js(
        "document.documentElement.scrollWidth > document.documentElement.clientWidth")
    results["trace"] = window.evaluate_js("window.__trace || []")
    results["frame"] = window.evaluate_js("window.__frame || null")
    results["after"] = window.evaluate_js("window.__after || null")
    results["selectedFrame"] = window.evaluate_js("window.__selectedFrame || null")
    results["flow"] = window.evaluate_js("window.__flow || []")
    results["wording"] = window.evaluate_js("window.__wording || null")
    results["spacing"] = window.evaluate_js("window.__spacing || null")
    results["driver_error"] = window.evaluate_js("window.__err")
    results["rejections"] = window.evaluate_js("window.__rejections || []")
    results["console"] = window.evaluate_js("window.__console || []")
    window.destroy()


win = webview.create_window("UI preview", url=PREVIEW, js_api=PreviewApi(),
                            width=WIN_W, height=WIN_H)
webview.start(inspect, win)

if os.path.exists(PREVIEW):
    os.remove(PREVIEW)

# Written to a file rather than printed: this tool reports paths, which on
# Windows contain characters the console encoding mangles (a shortened path
# carries an ellipsis, and stdout here is cp1252). Fighting that produced
# several rounds of unreadable output that looked like UI bugs.
RESULT_PATH = os.path.join(os.environ.get("TEMP", "."), "ui_preview_result.json")
with open(RESULT_PATH, "w", encoding="utf-8") as f:
    json.dump(results, f, indent=2, ensure_ascii=False)
print(RESULT_PATH)

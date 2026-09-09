"""
Dev-only: render the workbench in the SAME WebView2 engine the app ships
with, drive it with a real engine payload, then assert on the DOM it
produced.

Catches the class of bug a Python-side test cannot: a status class that
never got CSS, a note row that renders empty, a session panel that does not
repaint. Runs the real web/app.js -- nothing about the rendering is
reimplemented here.

    ui_preview.py [payload.json] [driver.js]

payload.json is what the stubbed process_package returns, and is exposed to
the driver as `PAYLOAD`. Without a driver, the default one renders it as a
batch result. Both arguments are optional.
"""

import json
import os
import re
import sys
import time

import webview

BUILD_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(os.path.dirname(BUILD_DIR), "web")
PREVIEW = os.path.join(WEB_DIR, "_ui_preview.html")

args = sys.argv[1:]
payload_path = next((a for a in args if a.lower().endswith(".json")), None)
driver_path = next((a for a in args if a.lower().endswith(".js")), None)

payload = json.load(open(payload_path, encoding="utf-8")) if payload_path else {
    "success": True, "batch": True, "study": "kfre", "patients": [],
    "summary": {"total": 0, "succeeded": 0, "failed": 0, "new_ids": 0, "reused_ids": 0,
                "skipped_duplicates": 0, "partial_repeats": 0, "conflicts": 0},
    "errors": [],
}
driver = open(driver_path, encoding="utf-8").read() if driver_path else \
    'renderBatchResult(PAYLOAD, "KFRE");'

index = open(os.path.join(WEB_DIR, "index.html"), encoding="utf-8").read()
body = re.search(r"<body>(.*)</body>", index, re.S).group(1)
body = body.replace('<script src="app.js"></script>', "")

app_info = {
    "hospital_code": "NIMS",
    "hospital_name": "Nizam's Institute of Medical Sciences (NIMS), Hyderabad",
    "app_data_dir": r"C:\Users\clinic\AppData\Local\TANUH-Renal-Anonymizer",
    "egfr_mapping_csv": "", "kfre_mapping_csv": "",
    "egfr_output_dir": "", "kfre_output_dir": "",
}

# describe_session is answered the way the real backend would: per study, so
# a harness cannot accidentally hide the very bug it is checking for.
session_js = r"""
  describe_session: async (mappingCsv, outputDir, study) => {
    const base = outputDir || "C:\\AppData\\TANUH-Renal-Anonymizer";
    const name = study === "kfre" ? "KFRE" : "eGFR";
    window.__sessionCalls = (window.__sessionCalls || []).concat(study);
    return {
      study, mapping_csv: mappingCsv || (base + "\\" + name + "_anony_Mapping.csv"),
      mapping_csv_exists: false, mapping_csv_is_default: !mappingCsv,
      mapping_csv_beside_output: !mappingCsv && !!outputDir,
      existing_patients: 0, existing_ids: 0, continuing: false,
      output_dir: base + "\\Anonymized_" + name,
      output_is_default: !outputDir, processed_files: 0,
      ledger_path: "", error: null,
    };
  },
"""

open(PREVIEW, "w", encoding="utf-8").write(f"""<link rel="stylesheet" href="style.css">
{body}
<script>
window.PAYLOAD = {json.dumps(payload)};
window.pywebview = {{ api: {{
  get_app_info: async () => ({json.dumps(app_info)}),
  {session_js}
  detect_study: async (paths, isFolder) => ({{
    success: true,
    study: (paths || []).some(p => /\\.dcm$/i.test(p)) || /egfr/i.test((paths || [])[0] || "")
      ? "egfr" : "kfre",
  }}),
  process_package: async () => window.PAYLOAD,
}} }};
</script>
<script src="app.js"></script>
<script>
window.__err = null;
window.addEventListener('error', e => {{ window.__err = window.__err || String(e.message); }});
// A flag, not a Promise: pywebview's evaluate_js does not await promises,
// so polling for a plain boolean is the only reliable way to know the
// driver has finished. Reading the DOM before it has is how a harness
// reports a passing UI that never actually rendered.
window.__done = false;
(async () => {{
  try {{
    await refreshSession();
    {driver}
  }} catch (e) {{ window.__err = String(e && e.stack || e); }}
  window.__done = true;
}})();
</script>
""")

results = {}


def inspect(window):
    deadline = time.time() + 30
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
    results["describe_session_studies"] = window.evaluate_js("window.__sessionCalls || []")
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
    results["driver_error"] = window.evaluate_js("window.__err")
    window.destroy()


win = webview.create_window("UI preview", url=PREVIEW, width=1500, height=950)
webview.start(inspect, win)

if os.path.exists(PREVIEW):
    os.remove(PREVIEW)

print(json.dumps(results, indent=2))

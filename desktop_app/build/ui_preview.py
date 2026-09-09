"""
Dev-only: render the workbench in the SAME WebView2 engine the app ships
with, driven by a real engine payload, then assert on the DOM it produced.

Catches the class of bug a Python-side test cannot: a status class that
never got CSS, a note row that renders empty, an escaping slip. Runs the
real web/app.js -- nothing about the rendering is reimplemented here.

    desktop_app/venv/Scripts/python.exe desktop_app/build/ui_preview.py <payload.json>
"""

import json
import os
import re
import sys
import threading

import webview

BUILD_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(os.path.dirname(BUILD_DIR), "web")
PREVIEW = os.path.join(WEB_DIR, "_ui_preview.html")

payload = json.load(open(sys.argv[1], encoding="utf-8"))

index = open(os.path.join(WEB_DIR, "index.html"), encoding="utf-8").read()
body = re.search(r"<body>(.*)</body>", index, re.S).group(1)
body = body.replace('<script src="app.js"></script>', "")

session = {
    "study": "kfre", "mapping_csv": r"D:\Renal Study\KFRE\KFRE_anony_Mapping.csv",
    "mapping_csv_exists": True, "mapping_csv_is_default": True,
    "mapping_csv_beside_output": True, "existing_patients": 11, "existing_ids": 11,
    "continuing": True, "output_dir": r"D:\Renal Study\KFRE\Anonymized_KFRE",
    "output_is_default": False, "processed_files": 33, "error": None,
    "ledger_path": r"D:\Renal Study\KFRE\KFRE_anony_Mapping_processed_files.json",
}
app_info = {
    "hospital_code": "NIMS",
    "hospital_name": "Nizam's Institute of Medical Sciences (NIMS), Hyderabad",
    "egfr_output_dir": session["output_dir"], "kfre_output_dir": session["output_dir"],
    "egfr_mapping_csv": session["mapping_csv"], "kfre_mapping_csv": session["mapping_csv"],
    "app_data_dir": r"C:\Users\clinic\AppData\Local\TANUH-Renal-Anonymizer",
}

open(PREVIEW, "w", encoding="utf-8").write(f"""<link rel="stylesheet" href="style.css">
{body}
<script>
window.pywebview = {{ api: {{
  get_app_info: async () => ({json.dumps(app_info)}),
  describe_session: async () => ({json.dumps(session)}),
}} }};
</script>
<script src="app.js"></script>
<script>
window.__ready = (async () => {{
  await refreshSession();
  renderBatchResult({json.dumps(payload)}, "KFRE");
  return true;
}})();
</script>
""")

results = {}


def inspect(window):
    window.evaluate_js("window.__ready")
    results["banner"] = window.evaluate_js("document.getElementById('result-banner').innerText")
    results["summary"] = window.evaluate_js("document.getElementById('batch-summary').innerText")
    results["csv_hint"] = window.evaluate_js("document.getElementById('csv-hint').innerText")
    results["rows"] = window.evaluate_js("""
      Array.from(document.querySelectorAll('#batch-tbody tr')).map(tr => ({
        cls: tr.className,
        status: tr.querySelector('.batch-status')?.textContent || '',
        statusCls: tr.querySelector('.batch-status')?.className || '',
        statusColor: tr.querySelector('.batch-status')
          ? getComputedStyle(tr.querySelector('.batch-status')).backgroundColor : '',
        text: tr.innerText.replace(/\\s+/g, ' ').trim().slice(0, 700),
      }))
    """)
    results["overflow"] = window.evaluate_js(
        "document.documentElement.scrollWidth > document.documentElement.clientWidth")
    window.destroy()


win = webview.create_window("UI preview", url=PREVIEW, width=1500, height=950)
threading.Timer(0.1, lambda: None).start()
webview.start(inspect, win)

if os.path.exists(PREVIEW):
    os.remove(PREVIEW)

print(json.dumps(results, indent=2))

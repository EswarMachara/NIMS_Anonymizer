"""
Repeat-submission detection: the same files handed over a second time must be
recognised as ALREADY ANONYMIZED, not reported as a follow-up visit.

Exercises the real engines against whatever sample corpora are present
(eGFR/ and KFRE/ at the project root -- both gitignored, since they hold real
patient data), and skips a study whose folder is absent.

    desktop_app/venv/Scripts/python.exe desktop_app/backend/test_repeat_detection.py
"""

import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from backend import engine  # noqa: E402
import anon_common as ac  # noqa: E402

NIMS = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
failures = []


def check(label, condition, detail=""):
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}" + (f" -- {detail}" if detail else ""))
    if not condition:
        failures.append(label)


def fresh_dest(name):
    work = os.path.join(tempfile.gettempdir(), "nims_repeat_test", name)
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work, exist_ok=True)
    return work


def run(study, src, dest, mapping_csv=None):
    fn = engine.process_kfre_batch if study == "kfre" else engine.process_egfr_batch
    return fn(src, mapping_csv, dest)


def verdicts(result):
    return sorted((p.get("repeat") or {}).get("verdict", "?") for p in result["patients"])


def snapshot(dest):
    """(size, mtime) per file under the destination, excluding the two files
    that are SUPPOSED to be rewritten on every run."""
    out = {}
    for dirpath, _dirs, files in os.walk(dest):
        for name in files:
            if name.endswith((".json", ".csv")):
                continue
            full = os.path.join(dirpath, name)
            st = os.stat(full)
            out[os.path.relpath(full, dest)] = (st.st_size, int(st.st_mtime))
    return out


def mapping_rows(dest, study):
    path = engine.get_mapping_csv_path(study, None, dest)
    return len(ac.MappingStore.load_or_create(path).rows)


# ---------------------------------------------------------------------------


def scenario_same_files_twice(study, src):
    print(f"\n=== {study.upper()}: the same folder submitted twice ===")
    dest = fresh_dest(f"{study}_twice")

    t0 = time.time()
    first = run(study, src, dest)
    t_first = time.time() - t0
    s1 = first["summary"]
    print(f"  run 1: {s1['succeeded']}/{s1['total']} patient(s) in {t_first:.1f}s -> {verdicts(first)}")
    check("run 1 succeeds", first["success"], str(first.get("errors")))
    check("run 1 sees no repeats", s1["skipped_duplicates"] == 0 and s1["partial_repeats"] == 0)
    check("run 1 mints a new ID per patient", s1["new_ids"] == s1["total"], f"new={s1['new_ids']}")

    after_first = snapshot(dest)

    t0 = time.time()
    second = run(study, src, dest)
    t_second = time.time() - t0
    s2 = second["summary"]
    print(f"  run 2: {s2['succeeded']}/{s2['total']} patient(s) in {t_second:.1f}s -> {verdicts(second)}")
    check("run 2 succeeds", second["success"], str(second.get("errors")))
    check("run 2 flags EVERY patient as a duplicate",
          s2["skipped_duplicates"] == s2["total"], f"{s2['skipped_duplicates']}/{s2['total']}")
    check("run 2 reports zero new IDs", s2["new_ids"] == 0)
    check("run 2 raises no ID conflicts", s2["conflicts"] == 0)
    check("run 2 is faster, because the work was skipped", t_second < t_first,
          f"{t_first:.1f}s -> {t_second:.1f}s")
    check("existing output was NOT rewritten", snapshot(dest) == after_first,
          "sizes and mtimes unchanged")
    check("a skipped patient can still be cross-checked",
          all(p["preview_pairs"] for p in second["patients"]))
    msg = (second["patients"][0].get("repeat") or {}).get("message", "")
    check("a skipped patient carries an explanation", "Already anonymized" in msg, msg)
    check("the mapping CSV gained no duplicate rows",
          mapping_rows(dest, study) == s1["total"], f"{mapping_rows(dest, study)} rows")
    return dest


def scenario_deleted_output(study, src, dest):
    print(f"\n=== {study.upper()}: same files, but the anonymized output was deleted ===")
    out_root = engine.get_output_root(study, dest)
    victim = os.path.join(out_root, sorted(os.listdir(out_root))[0])
    if os.path.isdir(victim):
        shutil.rmtree(victim, ignore_errors=True)
    else:
        os.remove(victim)
    print(f"  removed: {os.path.basename(victim)}")

    third = run(study, src, dest)
    v = verdicts(third)
    print(f"  run 3 -> {v}")
    check("run 3 succeeds", third["success"], str(third.get("errors")))
    check("the patient whose output vanished is written again", "recreate" in v, str(v))
    check("every other patient is still a duplicate", v.count("duplicate") == len(v) - 1, str(v))
    check("the deleted output is back", os.path.exists(victim))


def scenario_partial_followup(study, src):
    """A returning patient whose folder holds last visit's files PLUS a new
    one. This is the shape a real follow-up actually arrives in -- the
    operator hands over the whole patient folder again -- so it must be
    processed, not skipped, and the repeat must be reported as partial."""
    print(f"\n=== {study.upper()}: known patient, one genuinely new report ===")
    import pymupdf as fitz

    staged = fresh_dest(f"{study}_partial_src")
    corpus = os.path.join(staged, "corpus")
    shutil.copytree(src, corpus)
    dest = fresh_dest(f"{study}_partial_dest")

    # Seed from the untouched corpus: everything is new material.
    seed = run(study, corpus, dest)
    check("the seeding run succeeds", seed["success"], str(seed.get("errors")))
    seeded = len(seed["patients"])

    # Now add a report that did not exist last time. Re-encoded through
    # PyMuPDF so the bytes genuinely differ while the content -- and so the
    # identity key extracted from it -- stays the same patient's.
    target = None
    for dirpath, _dirs, files in os.walk(corpus):
        for name in sorted(files):
            if name.lower().endswith(".pdf"):
                target = os.path.join(dirpath, name)
                break
        if target:
            break
    if not target:
        print("  (no PDF found to re-encode -- skipping)")
        return
    twin = os.path.join(os.path.dirname(target), "FOLLOWUP_" + os.path.basename(target))
    doc = fitz.open(target)
    doc.save(twin, garbage=4, deflate=True)
    doc.close()
    check("the added report really is different bytes",
          ac.sha256_file(twin) != ac.sha256_file(target))
    print(f"  added: {os.path.relpath(twin, corpus)}")

    result = run(study, corpus, dest)
    v = verdicts(result)
    print(f"  -> {v}")
    check("run succeeds", result["success"], str(result.get("errors")))
    check("the patient with new material is reported as a partial repeat",
          "partial" in v, str(v))
    check("the other patients are still skipped as duplicates",
          v.count("duplicate") == seeded - 1, f"{v.count('duplicate')} of {seeded - 1}")

    partial = [p for p in result["patients"] if (p.get("repeat") or {}).get("verdict") == "partial"]
    if partial:
        r = partial[0]["repeat"]
        print(f"     {r['message']}")
        check("the message counts old and new files separately",
              r["already"] > 0 and r["fresh"] > 0, f"already={r['already']} fresh={r['fresh']}")
        check("the new report was actually written",
              any("FOLLOWUP_" in os.path.basename(p["original"]) for p in partial[0]["preview_pairs"]))
    shutil.rmtree(staged, ignore_errors=True)


def scenario_wrong_mapping_csv(study, src):
    print(f"\n=== {study.upper()}: a DIFFERENT mapping CSV (operator picked the wrong file) ===")
    dest = fresh_dest(f"{study}_conflict")
    first = run(study, src, dest)
    check("first pass over a fresh destination succeeds", first["success"], str(first.get("errors")))

    # Same files, same destination -- but a mapping CSV with no history, so
    # every patient mints a brand new ID for files already filed under another.
    # The ledger is carried across, which is what makes the clash visible.
    other_csv = os.path.join(dest, "other_mapping.csv")
    shutil.copyfile(
        ac.ledger_path_for_mapping_csv(engine.get_mapping_csv_path(study, None, dest)),
        ac.ledger_path_for_mapping_csv(other_csv),
    )

    second = run(study, src, dest, mapping_csv=other_csv)
    s = second["summary"]
    print(f"  -> {s['conflicts']} conflicting file(s), verdicts {verdicts(second)}")
    check("re-filing under a new ID is a conflict, not a duplicate",
          s["conflicts"] > 0 and s["skipped_duplicates"] == 0)
    warns = [w for p in second["patients"] for w in (p.get("warnings") or [])]
    check("the conflict is explained to the operator",
          any("different Anonymized ID" in w for w in warns))
    if warns:
        print(f"     {warns[0][:160]}...")
    check("nothing is silently skipped -- the files are still processed",
          all(p["preview_pairs"] for p in second["patients"]))


def scenario_ledger_placement(study, dest):
    print(f"\n=== {study.upper()}: where the ledger landed ===")
    csv_path = engine.get_mapping_csv_path(study, None, dest)
    ledger = ac.ledger_path_for_mapping_csv(csv_path)
    out_root = engine.get_output_root(study, dest)
    print(f"  mapping: {csv_path}")
    print(f"  ledger : {ledger}")
    check("the ledger exists", os.path.exists(ledger))
    check("it sits beside the mapping CSV", os.path.dirname(ledger) == os.path.dirname(csv_path))
    check("it is NOT inside the shareable output folder",
          not os.path.normcase(ledger).startswith(os.path.normcase(out_root) + os.sep))
    leaked = [f for _d, _s, fs in os.walk(out_root) for f in fs if f.endswith(".json")]
    check("no copy of it leaked into the output folder", not leaked, str(leaked))

    d = engine.describe_session(None, dest, study)
    print(f"  describe_session: {d['existing_patients']} patient(s), {d['processed_files']} file(s) on record")
    check("describe_session reports the ledger", d["processed_files"] > 0)


for study_name, source in (("kfre", os.path.join(NIMS, "KFRE")), ("egfr", os.path.join(NIMS, "eGFR"))):
    if not os.path.isdir(source):
        print(f"\n!! skipping {study_name}: {source} is not present")
        continue
    destination = scenario_same_files_twice(study_name, source)
    scenario_deleted_output(study_name, source, destination)
    scenario_partial_followup(study_name, source)
    scenario_ledger_placement(study_name, destination)
    scenario_wrong_mapping_csv(study_name, source)

print("\n" + "=" * 62)
if failures:
    print(f"{len(failures)} CHECK(S) FAILED:")
    for name in failures:
        print("  -", name)
    sys.exit(1)
print("ALL REPEAT-DETECTION CHECKS PASSED")

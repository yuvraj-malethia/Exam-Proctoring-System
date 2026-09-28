"""
video_batch.py — replay every clip in a folder and compare with its label.

WHY THIS EXISTS
───────────────────────────────────────────────────────────────────────
The point of recording clips (record_clip.py) is to stop re-testing by
hand. After ANY change — a threshold, a new detector, a bug fix — run:

    python video_batch.py

and every clip is replayed through the real engine (one clean process
per clip, via `module_headpose_multi_web.py --video`), each result is
compared with what the clip was recorded to show, and the table says
what changed since the previous batch. That is the "check it against
real footage" step this project has never had.

LABELS (clips/labels.json, written by record_clip.py)
    {"phone_in_lap.mp4": {"label": "suspicious", "note": "..."}, ...}
  innocent    the clip should NOT be flagged for review
  suspicious  the clip SHOULD be flagged for review
"Flagged for review" means exactly what captures evidence in a live
session: a student's verdict reached ALERT or FLAG.

WHAT A RESULT MEANS — AND DOES NOT
A clip matching its label shows the system behaved as intended on that
footage. It is not proof the system is right in general, and a clip
differing from its label is a thing to look at, not a verdict on the
student in it. Same under-claiming rule as the session report.

OUTPUT
    video_results/batch_<time>/<clip>.json   full result per clip
    video_results/batch_<time>/summary.json  table data + settings used
    video_results/batch_<time>/summary.md    readable table
"""

import argparse
import glob
import json
import os
import subprocess
import sys
import time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
ENGINE = os.path.join(HERE, "module_headpose_multi_web.py")
VIDEO_EXTENSIONS = (".mp4", ".avi", ".mov", ".mkv", ".m4v", ".wmv")
RESULTS_ROOT = "video_results"
_RANK = {"CALIBRATING": 0, "OK": 1, "WATCH": 2, "ALERT": 3, "FLAG": 4}


def load_labels(folder):
    path = os.path.join(folder, "labels.json")
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    # Accept {"clip.mp4": "innocent"} as well as {"clip.mp4": {"label": ...}}.
    return {k: (v if isinstance(v, dict) else {"label": v}) for k, v in raw.items()}


def run_clip(clip_path, out_json, with_report):
    cmd = [sys.executable, ENGINE, "--video", clip_path, "--no-window",
           "--no-dashboard", "--fast", "--results", out_json]
    if with_report:
        cmd.append("--report")
    started = time.perf_counter()
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", cwd=os.getcwd())
    took = time.perf_counter() - started
    if proc.returncode != 0 or not os.path.exists(out_json):
        tail = "\n".join((proc.stdout + proc.stderr).strip().splitlines()[-15:])
        return None, took, tail
    with open(out_json, encoding="utf-8") as f:
        return json.load(f), took, None


def summarise(clip, result, label_info):
    label = (label_info or {}).get("label")
    row = {"clip": clip, "label": label, "note": (label_info or {}).get("note")}
    if result is None:
        row.update(outcome="ENGINE ERROR")
        return row
    students = result.get("students", {})
    verdicts = [s["highest_verdict"] for s in students.values()]
    directions = {}
    patterns = {}
    for s in students.values():
        for d, n in s.get("events_by_direction", {}).items():
            directions[d] = directions.get(d, 0) + n
        for p in s.get("patterns", []):
            patterns[p["label"]] = max(patterns.get(p["label"], 0.0), p["best_confidence"])
    flagged = bool(result.get("flagged_for_review"))
    if label == "innocent":
        outcome = "matches label" if not flagged else "DIFFERS FROM LABEL"
    elif label == "suspicious":
        outcome = "matches label" if flagged else "DIFFERS FROM LABEL"
    else:
        outcome = "no label"
    row.update(
        flagged=flagged,
        highest_verdict=max(verdicts, key=_RANK.get) if verdicts else "no face",
        peak_score=max((s["peak_score"] for s in students.values()), default=0.0),
        events=sum(s["events"] for s in students.values()),
        directions=directions,
        patterns=[k for k, _ in sorted(patterns.items(), key=lambda kv: -kv[1])],
        students_seen=len(students),
        face_seen_pct=result["video"]["face_seen_pct"],
        footage_sec=result["video"]["footage_sec"],
        outcome=outcome,
    )
    return row


def previous_batch(exclude_dir):
    batches = sorted(d for d in glob.glob(os.path.join(RESULTS_ROOT, "batch_*"))
                     if os.path.abspath(d) != os.path.abspath(exclude_dir)
                     and os.path.exists(os.path.join(d, "summary.json")))
    if not batches:
        return None
    with open(os.path.join(batches[-1], "summary.json"), encoding="utf-8") as f:
        data = json.load(f)
    data["_dir"] = batches[-1]
    return data


def changes_since(prev, rows, settings):
    if prev is None:
        return ["(first batch — nothing to compare with yet)"]
    lines = []
    if prev.get("settings") != settings:
        changed = sorted(k for k in set(prev.get("settings", {})) | set(settings)
                         if prev.get("settings", {}).get(k) != settings.get(k))
        lines.append("Settings changed since last batch: " + ", ".join(
            f"{k} {prev['settings'].get(k)} -> {settings.get(k)}" for k in changed))
    before = {r["clip"]: r for r in prev.get("rows", [])}
    for r in rows:
        b = before.get(r["clip"])
        if b is None:
            lines.append(f"{r['clip']}: new clip")
            continue
        if "flagged" not in r or "flagged" not in b:
            continue
        diffs = []
        if r["flagged"] != b["flagged"]:
            diffs.append(f"flagged {b['flagged']} -> {r['flagged']}")
        if r["highest_verdict"] != b["highest_verdict"]:
            diffs.append(f"verdict {b['highest_verdict']} -> {r['highest_verdict']}")
        if abs(r["peak_score"] - b["peak_score"]) >= 0.5:
            diffs.append(f"peak {b['peak_score']:.1f}% -> {r['peak_score']:.1f}%")
        if r["events"] != b["events"]:
            diffs.append(f"events {b['events']} -> {r['events']}")
        if diffs:
            lines.append(f"{r['clip']}: " + ", ".join(diffs))
    return lines or ["No differences from the previous batch."]


def table(rows):
    head = f"{'clip':30s} {'label':10s} {'flagged':7s} {'verdict':8s} {'peak':>6s} {'events':>6s}  outcome"
    out = [head, "-" * len(head)]
    for r in rows:
        if "flagged" not in r:
            out.append(f"{r['clip'][:30]:30s} {str(r['label'] or '-'):10s} {'-':7s} {'-':8s} {'-':>6s} {'-':>6s}  {r['outcome']}")
            continue
        out.append(f"{r['clip'][:30]:30s} {str(r['label'] or '-'):10s} {('yes' if r['flagged'] else 'no'):7s} "
                   f"{r['highest_verdict']:8s} {r['peak_score']:5.1f}% {r['events']:6d}  {r['outcome']}")
    return out


def markdown(rows, changes, settings, folder):
    md = [f"# Clip replay batch — {datetime.now():%Y-%m-%d %H:%M}", "",
          f"Clips folder: `{folder}`", "",
          "| Clip | Label | Flagged for review | Highest verdict | Peak score | Events | Directions | Patterns | Outcome |",
          "|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        if "flagged" not in r:
            md.append(f"| {r['clip']} | {r['label'] or '-'} | - | - | - | - | - | - | {r['outcome']} |")
            continue
        dirs = ", ".join(f"{k} {v}" for k, v in r["directions"].items()) or "-"
        md.append(f"| {r['clip']} | {r['label'] or '-'} | {'yes' if r['flagged'] else 'no'} | "
                  f"{r['highest_verdict']} | {r['peak_score']:.1f}% | {r['events']} | {dirs} | "
                  f"{', '.join(r['patterns']) or '-'} | {r['outcome']} |")
    md += ["", "## Changes since previous batch", ""] + [f"- {c}" for c in changes]
    md += ["", "## Settings used", "", "```", json.dumps(settings, indent=2), "```", "",
           "A clip matching its label shows the system behaved as intended on that "
           "footage only. It is not proof of accuracy in general, and a flag is a "
           "prompt for human review, never a finding of misconduct."]
    return "\n".join(md) + "\n"


def main():
    ap = argparse.ArgumentParser(description="Replay every clip in a folder through the engine and compare with labels.")
    ap.add_argument("folder", nargs="?", default="clips", help="folder of clips (default: clips)")
    ap.add_argument("--report", action="store_true", help="also build a PDF report per clip (slower)")
    args = ap.parse_args()

    clips = sorted(f for f in os.listdir(args.folder)
                   if f.lower().endswith(VIDEO_EXTENSIONS)) if os.path.isdir(args.folder) else []
    if not clips:
        print(f"No video clips found in '{args.folder}'. Record some with record_clip.py first.")
        return
    labels = load_labels(args.folder)
    batch_dir = os.path.join(RESULTS_ROOT, f"batch_{datetime.now():%Y_%m_%d_%H%M%S}")
    os.makedirs(batch_dir, exist_ok=True)
    print(f"Replaying {len(clips)} clip(s) from '{args.folder}' -> {batch_dir}\n")

    rows, settings = [], {}
    for i, clip in enumerate(clips, 1):
        out_json = os.path.join(batch_dir, os.path.splitext(clip)[0] + ".json")
        print(f"[{i}/{len(clips)}] {clip} ...", end=" ", flush=True)
        result, took, error = run_clip(os.path.join(args.folder, clip), out_json, args.report)
        if error:
            print(f"FAILED after {took:.0f}s\n{error}\n")
        else:
            print(f"done in {took:.0f}s")
            settings = result.get("settings", settings)
        rows.append(summarise(clip, result, labels.get(clip)))

    prev = previous_batch(batch_dir)
    changes = changes_since(prev, rows, settings)
    with open(os.path.join(batch_dir, "summary.json"), "w", encoding="utf-8") as f:
        json.dump({"created": datetime.now().isoformat(timespec="seconds"),
                   "folder": os.path.abspath(args.folder), "settings": settings,
                   "rows": rows}, f, indent=2)
    with open(os.path.join(batch_dir, "summary.md"), "w", encoding="utf-8") as f:
        f.write(markdown(rows, changes, settings, args.folder))

    print("\n" + "\n".join(table(rows)))
    labelled = [r for r in rows if r.get("label") in ("innocent", "suspicious") and "flagged" in r]
    matched = sum(r["outcome"] == "matches label" for r in labelled)
    print(f"\n{matched} of {len(labelled)} labelled clip(s) matched their label.")
    print("\nChanges since previous batch" + (f" ({os.path.basename(prev['_dir'])})" if prev else "") + ":")
    for c in changes:
        print(f"  - {c}")
    print(f"\nFull results: {batch_dir}")


if __name__ == "__main__":
    main()

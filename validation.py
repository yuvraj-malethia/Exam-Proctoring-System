"""
validation.py — data behind the dashboard's Validation page.

WHY THIS EXISTS
───────────────────────────────────────────────────────────────────────
"How do you know it works?" is the first thing an evaluation panel will
ask, and until Sep 2026 the only answer was synthetic tests. The pieces
for a real answer already existed as command-line tools: record_clip.py
(labelled clips of known behaviour), video_batch.py (replay every clip
through the real engine and compare with its label) and whatif.py
(re-score the same clips under different settings). This module puts
them on one page:
  - which suggested clips are recorded, and the exact command for the rest
  - the latest batch, as caught / missed / false alarm / correctly left alone
  - every clip's result, and what changed since the previous batch
  - a "Run the check" button (video_batch.py in the background, with
    progress)
  - the what-if trade-off for one setting (whatif.py, in its own process)

ROUTES ONLY LIVE IN dashboard_server.py; the logic is here, matching how
dashboard_auth.py and session_replay.py are split.

SAFETY
  - whatif.py changes Config values while it runs, so it is ALWAYS run as a
    separate process, never inside the dashboard or a live engine.
  - A batch replays every clip at full speed and takes minutes of CPU, so
    the server refuses to start one while a live engine is attached.
  - Nothing here decides anything or changes a default. Results are shown
    with their sample size, and the page says what they cannot show.
"""

import glob
import json
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime

import video_batch
import whatif
from record_clip import SUGGESTED_CLIPS

HERE = os.path.dirname(os.path.abspath(__file__))
_BATCH_RE = re.compile(r"^batch_\d{4}_\d{2}_\d{2}_\d{6}$")
_PROGRESS_RE = re.compile(r"\[(\d+)/(\d+)\] (.+?) \.\.\.")
_VALUE_RE = re.compile(r"^(-?\d+(\.\d+)?|true|false)$", re.I)


# ── Reading clips and results ────────────────────────────────────────
def _clips(clips_dir):
    labels = video_batch.load_labels(clips_dir) if os.path.isdir(clips_dir) else {}
    files = sorted(f for f in os.listdir(clips_dir)
                   if f.lower().endswith(video_batch.VIDEO_EXTENSIONS)) if os.path.isdir(clips_dir) else []
    return files, labels


def _counts(rows):
    c = {"caught": 0, "missed": 0, "false_alarm": 0, "cleared": 0, "unlabelled": 0, "errors": 0}
    for r in rows:
        if "flagged" not in r:
            c["errors"] += 1
        else:
            c[whatif.outcome(r.get("label"), r["flagged"])] += 1
    return c


def _load_summary(batch_dir):
    with open(os.path.join(batch_dir, "summary.json"), encoding="utf-8") as f:
        return json.load(f)


def _batches(results_dir):
    dirs = sorted((d for d in glob.glob(os.path.join(results_dir, "batch_*"))
                   if os.path.exists(os.path.join(d, "summary.json"))), reverse=True)
    out = []
    for d in dirs:
        try:
            s = _load_summary(d)
        except (OSError, ValueError):
            continue
        out.append({"id": os.path.basename(d), "dir": d, "created": s.get("created"),
                    "settings": s.get("settings", {}), "rows": s.get("rows", []),
                    "counts": _counts(s.get("rows", []))})
    return out


def _clip_detail(batch_dir, row):
    """Per-clip extras from the clip's own results file."""
    path = os.path.join(batch_dir, os.path.splitext(row["clip"])[0] + ".json")
    extra = {"seconds_per_verdict": {}, "cross_student": [], "has_inputs": False,
             "students": {}}
    if not os.path.exists(path):
        return extra
    try:
        with open(path, encoding="utf-8") as f:
            res = json.load(f)
    except (OSError, ValueError):
        return extra
    spv = {}
    for label, s in (res.get("students") or {}).items():
        for v, sec in (s.get("seconds_per_verdict") or {}).items():
            spv[v] = round(spv.get(v, 0.0) + sec, 1)
        extra["students"][label] = {k: s.get(k) for k in (
            "highest_verdict", "peak_score", "events", "longest_event_sec", "total_away_sec",
            "events_by_direction")}
    extra["seconds_per_verdict"] = spv
    extra["cross_student"] = res.get("cross_student") or []
    extra["has_inputs"] = os.path.exists(os.path.join(
        batch_dir, os.path.splitext(row["clip"])[0] + ".inputs.json.gz"))
    return extra


def overview(clips_dir, results_dir):
    files, labels = _clips(clips_dir)
    names = {os.path.splitext(f)[0]: f for f in files}
    plan = [{"name": n, "label": lab, "what": what, "recorded": n in names,
             "command": f"python record_clip.py {n} --label {lab}"}
            for n, lab, what in SUGGESTED_CLIPS]
    clips = [{"file": f, "label": (labels.get(f) or {}).get("label"),
              "note": (labels.get(f) or {}).get("note"),
              "seconds": (labels.get(f) or {}).get("seconds"),
              "recorded_at": (labels.get(f) or {}).get("recorded_at")} for f in files]

    batches = _batches(results_dir)
    latest = None
    if batches:
        b = batches[0]
        rows = [dict(r, **_clip_detail(b["dir"], r)) for r in b["rows"]]
        prev = batches[1] if len(batches) > 1 else None
        changes = (video_batch.changes_since({"settings": prev["settings"], "rows": prev["rows"]},
                                             b["rows"], b["settings"])
                   if prev else ["(first batch: nothing to compare with yet)"])
        latest = {"id": b["id"], "created": b["created"], "settings": b["settings"],
                  "counts": b["counts"], "rows": rows, "changes": changes,
                  "previous": prev["id"] if prev else None,
                  "clips_now_not_in_batch": sorted(set(files) - {r["clip"] for r in b["rows"]})}
    tunable = [dict(t, current=getattr(whatif.Config, t["name"], None)) for t in whatif.TUNABLE]
    return {
        "clips_dir": clips_dir,
        "results_dir": results_dir,
        "plan": plan,
        "clips": clips,
        "batches": [{k: b[k] for k in ("id", "created", "settings", "counts")} for b in batches[:20]],
        "latest": latest,
        "job": JOB.status(),
        "tunable": tunable,
    }


# ── Running a batch in the background ────────────────────────────────
class BatchJob:
    """One video_batch.py run at a time, with progress read from its output."""

    def __init__(self):
        self._lock = threading.Lock()
        self._s = {"state": "idle"}

    def status(self):
        with self._lock:
            s = dict(self._s)
        if s.get("state") == "running" and s.get("started"):
            s["elapsed_sec"] = round(time.time() - s["started"], 1)
        return s

    def start(self, clips_dir):
        files, _ = _clips(clips_dir)
        if not files:
            return False, "No clips to check yet. Record some with record_clip.py first."
        with self._lock:
            if self._s.get("state") == "running":
                return False, "A check is already running."
            self._s = {"state": "running", "i": 0, "n": len(files), "clip": None,
                       "started": time.time(), "log": ""}
        threading.Thread(target=self._run, args=(clips_dir,), daemon=True,
                         name="validation-batch").start()
        return True, self.status()

    def _run(self, clips_dir):
        cmd = [sys.executable, os.path.join(HERE, "video_batch.py"), clips_dir]
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    cwd=os.getcwd(), env=dict(os.environ, PYTHONIOENCODING="utf-8"))
            buf = ""
            while True:
                chunk = os.read(proc.stdout.fileno(), 4096)   # whatever is ready
                if not chunk:
                    break
                buf += chunk.decode("utf-8", "replace")
                found = _PROGRESS_RE.findall(buf)
                with self._lock:
                    if found:
                        i, n, clip = found[-1]
                        self._s.update(i=int(i), n=int(n), clip=clip)
                    self._s["log"] = buf[-4000:]
            code = proc.wait()
            m = re.search(r"Full results: (\S+)", buf)
            with self._lock:
                self._s.update(state="done" if code == 0 else "failed",
                               finished=time.time(),
                               batch=os.path.basename(m.group(1).rstrip("\\/")) if m else None,
                               error=None if code == 0 else buf[-1500:])
        except Exception as e:   # the page must show a failure, never hang on "running"
            with self._lock:
                self._s.update(state="failed", finished=time.time(), error=str(e))


JOB = BatchJob()


def start_batch(clips_dir):
    return JOB.start(clips_dir)


# ── What-if, in its own process ──────────────────────────────────────
def run_whatif(results_dir, batch_id, setting, values, timeout=180):
    if not _BATCH_RE.match(batch_id or ""):
        return False, "Unknown batch."
    batch_dir = os.path.join(results_dir, batch_id)
    if not os.path.exists(os.path.join(batch_dir, "summary.json")):
        return False, "That batch no longer exists."
    names = {t["name"] for t in whatif.TUNABLE}
    if setting not in names:
        return False, "That setting cannot be tried from the dashboard."
    vals = [str(v).strip() for v in (values or []) if str(v).strip()]
    if not vals or len(vals) > 8 or not all(_VALUE_RE.match(v) for v in vals):
        return False, "Give between one and eight numbers (or true / false)."
    cmd = [sys.executable, os.path.join(HERE, "whatif.py"), batch_dir,
           "--sweep", f"{setting}={','.join(vals)}", "--json"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                              cwd=os.getcwd(), encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        return False, "The what-if took too long and was stopped."
    lines = [l for l in proc.stdout.strip().splitlines() if l.startswith("{")]
    if not lines:
        return False, "The what-if failed: " + (proc.stderr.strip().splitlines() or ["no output"])[-1]
    data = json.loads(lines[-1])
    if "error" in data:
        return False, data["error"]
    return True, data

"""
whatif.py — re-score recorded clips with different settings, in seconds.

WHY THIS EXISTS
───────────────────────────────────────────────────────────────────────
The fairness audit this project has had on its list since the session-
memory bug (CLAUDE.md, "planned next" 3) asks: are K_SIGMA, the EMA
alpha and the alert thresholds tuned for a short demo rather than a real
three-hour exam? Answering that needs every candidate setting checked
against the same labelled footage. Replaying a clip through the full
engine takes about as long as the clip itself, mostly MediaPipe, and
most of that work does not change when a threshold does.

So video replays (video_run.py) now save exactly what went into the
scoring on every frame: raw yaw, raw pitch, profile flag, face ratio, the
engine's own t and dt, and any seat reset or record merge. This file
feeds those numbers into fresh StudentPipelines with a setting changed
and applies verdict.py's rules, exactly as the engine does. That takes a
fraction of a second per clip instead of a minute.

HONESTY CHECK, EVERY RUN
Before any "what if", every clip is re-scored with the settings it was
recorded under and compared with what the engine itself reported
(verdict reached, seconds at each verdict, peak and final score, event
count). If they do not match exactly, the results say so. A what-if is
only as trustworthy as that check.

WHAT IT CANNOT DO
  - Change anything upstream of the scoring: face detection, identity
    tracking, which face went to which seat. Those are fixed by the
    original replay (none of them read a scoring setting).
  - Say which setting is RIGHT. It shows the trade-off (clips caught
    against innocent clips flagged) on the clips you have. With a
    handful of staged clips from one person, that is direction, not
    accuracy — and never a reason on its own to change a default.

RUNS IN ITS OWN PROCESS. It changes Config values while it works, so it
must never share a process with a live engine: the dashboard starts it as
a subprocess (validation.py), and so should anything else.

USAGE
    python whatif.py                               latest batch: check + current results
    python whatif.py --set K_SIGMA=2.6             one alternative
    python whatif.py --sweep K_SIGMA=2.0,2.3,2.6,3.0
    python whatif.py video_results/batch_...  --sweep LEVEL_HIGH=55,65,75 --json
"""

import argparse
import glob
import gzip
import json
import os
import sys
from contextlib import contextmanager

from config import Config
from occupant_archive import OccupantArchive
from student_pipeline import MAX_STUDENTS, StudentPipeline
from verdict import compute_verdict

RESULTS_ROOT = "video_results"
_RANK = {"CALIBRATING": 0, "OK": 1, "WATCH": 2, "ALERT": 3, "FLAG": 4}

# Settings offered on the dashboard's Validation page, with plain words.
# Anything else in Config can still be tried from the command line.
TUNABLE = [
    {"name": "K_SIGMA", "label": "How far a head must turn to count as away",
     "help": "Multiples of the student's own normal wobble. Lower catches smaller turns, "
             "and flags more ordinary movement.",
     "suggest": [1.8, 2.0, 2.3, 2.6, 3.0]},
    {"name": "MIN_EVENT_DURATION", "label": "Shortest look away that counts (seconds)",
     "help": "Glances shorter than this are ignored entirely.",
     "suggest": [0.6, 0.9, 1.2, 1.6, 2.0]},
    {"name": "SUSPICION_EMA_ALPHA", "label": "How quickly the score reacts",
     "help": "Higher follows each moment closely; lower smooths over bursts.",
     "suggest": [0.035, 0.05, 0.08, 0.12]},
    {"name": "LEVEL_HIGH", "label": "Score counted as HIGH RISK (%)",
     "help": "One of the four verdict checks is 'score has ever reached this'.",
     "suggest": [55, 60, 65, 70, 75]},
    {"name": "CONFIRMED_THRESHOLD", "label": "Score counted as 'high' for sustained time (%)",
     "help": "Time spent above this feeds the 'time spent with a high score' check.",
     "suggest": [60, 70, 75, 80, 85]},
    {"name": "TEMPORAL_WINDOW_SEC", "label": "Window for 'repeated looks' (seconds)",
     "help": "Three or more looks away inside this window is one verdict check.",
     "suggest": [10, 15, 20, 30]},
    {"name": "SESSION_MEMORY_HALFLIFE_SEC", "label": "How long past behaviour keeps counting (half-life, s)",
     "help": "Older looks away count for less; after one half-life, half as much.",
     "suggest": [300, 600, 900, 1800]},
    {"name": "PITCH_CALIBRATION_FREEZE_ENABLED", "label": "Freeze each student's up/down baseline after calibration",
     "help": "Off today, until real clips show whether a frozen baseline flags ordinary writing. "
             "An 'innocent_writing' clip answers exactly this.",
     "suggest": [False, True]},
]


# ── Settings ─────────────────────────────────────────────────────────
def parse_value(name, text):
    """A value typed as text, converted to the type the setting has now."""
    if not hasattr(Config, name):
        raise ValueError(f"Config has no setting called {name}")
    cur = getattr(Config, name)
    if isinstance(cur, bool):
        low = str(text).strip().lower()
        if low in ("1", "true", "on", "yes"):
            return True
        if low in ("0", "false", "off", "no"):
            return False
        raise ValueError(f"{name} is on/off; use true or false")
    try:
        f = float(text)
    except (TypeError, ValueError):
        raise ValueError(f"{name} needs a number, not '{text}'")
    if isinstance(cur, int) and f.is_integer():
        return int(f)
    return f


@contextmanager
def overridden(overrides):
    """Temporarily set Config values; always restored, even on error."""
    saved = {}
    try:
        for k, v in (overrides or {}).items():
            if not hasattr(Config, k):
                raise ValueError(f"Config has no setting called {k}")
            saved[k] = getattr(Config, k)
            setattr(Config, k, v)
        yield
    finally:
        for k, v in saved.items():
            setattr(Config, k, v)


def current_settings(names):
    return {n: getattr(Config, n) for n in names if hasattr(Config, n)}


# ── Re-scoring one clip ──────────────────────────────────────────────
def load_inputs(path):
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return json.load(f)


def rescore(inputs, overrides=None):
    """Replay one clip's recorded scoring inputs; returns the same
    per-student fields VideoRunStats reports, plus flagged_for_review."""
    with overridden(overrides):
        flagged_verdicts = tuple(Config.EVIDENCE_CAPTURE_VERDICTS)
        students = {i: StudentPipeline(i) for i in range(MAX_STUDENTS)}
        # TemporalPatternEngine stamps its start with clock.now() when it
        # is created. Set it to what the engine's pipelines were stamped
        # with, rather than touching the process-wide clock.
        for sid, st in students.items():
            start = inputs["session_starts"].get(str(sid))
            if start is not None:
                st.temporal._session_start = start
        archive = OccupantArchive(None)
        records = {}                 # engine record_id -> re-scored record
        seen = {}                    # slot -> {"verdict_sec": {...}}
        for t, dt, ops in inputs["frames"]:
            active = []
            for op in ops:
                kind, sid = op[0], op[1]
                st = students[sid]
                if kind == "r":
                    outgoing = list(st.temporal.events)
                    summary = st.reset(t)
                    st.temporal._session_start = t
                    records[op[2]] = archive.close_record(
                        slot=sid, label=st.label, summary=summary, events=outgoing,
                        started_at=t, ended_at=t, reason="whatif replay")
                elif kind == "p":
                    # Inputs recorded before eye tracking existed have
                    # six fields, not eight. Read what is there.
                    st.process(op[2], op[3], op[4], dt, t, pitch_raw=op[5],
                               eye_raw=(op[6] if len(op) > 6 else None),
                               eye_openness=(op[7] if len(op) > 7 else None),
                               eye_down=(op[8] if len(op) > 8 else None))
                    active.append(sid)
                elif kind == "m":
                    prior = records.get(op[2])
                    if prior is not None:
                        st.merge_archived_record(prior, t)
            for sid in active:
                v, _ = compute_verdict(students[sid], t)
                vs = seen.setdefault(sid, {})
                vs[v] = vs.get(v, 0.0) + dt
        out = {}
        for sid, vs in sorted(seen.items()):
            st = students[sid]
            reached = [v for v in vs if v in _RANK]
            highest = max(reached, key=_RANK.get) if reached else "CALIBRATING"
            out[st.label] = {
                "highest_verdict": highest,
                "flagged_for_review": highest in flagged_verdicts,
                "seconds_per_verdict": {k: round(v, 1) for k, v in vs.items()},
                "peak_score": round(st.fs.max_probability, 1),
                "final_score": round(st.fs.probability, 1),
                "events": len(st.temporal.events),
            }
        return {"flagged_for_review": any(s["flagged_for_review"] for s in out.values()),
                "students": out}


def matches_engine(inputs, rescored):
    """Does a re-score with unchanged settings reproduce the engine?"""
    eng = inputs["engine_result"]
    if eng["flagged_for_review"] != rescored["flagged_for_review"]:
        return False
    if set(eng["students"]) != set(rescored["students"]):
        return False
    for label, e in eng["students"].items():
        r = rescored["students"][label]
        for f in ("highest_verdict", "seconds_per_verdict", "peak_score", "final_score", "events"):
            if e[f] != r[f]:
                return False
    return True


# ── A batch of clips ─────────────────────────────────────────────────
def latest_batch(root=RESULTS_ROOT):
    batches = sorted(d for d in glob.glob(os.path.join(root, "batch_*"))
                     if os.path.exists(os.path.join(d, "summary.json")))
    return batches[-1] if batches else None


def batch_clips(batch_dir):
    """[(clip, label, inputs)] for every clip in the batch with inputs."""
    with open(os.path.join(batch_dir, "summary.json"), encoding="utf-8") as f:
        summary = json.load(f)
    out = []
    for row in summary.get("rows", []):
        path = os.path.join(batch_dir, os.path.splitext(row["clip"])[0] + ".inputs.json.gz")
        if os.path.exists(path):
            out.append((row["clip"], row.get("label"), load_inputs(path)))
    return out


def outcome(label, flagged):
    if label == "suspicious":
        return "caught" if flagged else "missed"
    if label == "innocent":
        return "false_alarm" if flagged else "cleared"
    return "unlabelled"


def evaluate(clips, overrides=None, precomputed=None):
    rows, counts = [], {"caught": 0, "missed": 0, "false_alarm": 0, "cleared": 0, "unlabelled": 0}
    for k, (clip, label, inputs) in enumerate(clips):
        r = precomputed[k] if precomputed is not None else rescore(inputs, overrides)
        oc = outcome(label, r["flagged_for_review"])
        counts[oc] += 1
        best = max(r["students"].values(), key=lambda s: _RANK.get(s["highest_verdict"], 0),
                   default={"highest_verdict": "no face", "peak_score": 0.0, "events": 0})
        rows.append({"clip": clip, "label": label, "flagged": r["flagged_for_review"],
                     "outcome": oc, "highest_verdict": best["highest_verdict"],
                     "peak_score": best["peak_score"],
                     "events": sum(s["events"] for s in r["students"].values())})
    return {"rows": rows, "counts": counts}


def run(batch_dir, sweep=None, one=None):
    clips = batch_clips(batch_dir)
    base = [rescore(inputs, None) for _, _, inputs in clips]
    check = [{"clip": clip, "reproduced": matches_engine(inputs, r)}
             for (clip, _, inputs), r in zip(clips, base)]
    current = evaluate(clips, precomputed=base)
    result = {
        "batch": os.path.basename(os.path.normpath(batch_dir)),
        "clips_with_inputs": len(clips),
        "fidelity": check,
        "fidelity_ok": bool(check) and all(c["reproduced"] for c in check),
        "current": current,
    }
    if one:
        result["alternative"] = {"overrides": one, **evaluate(clips, one)}
    if sweep:
        name, values = sweep
        today = getattr(Config, name)
        result["sweep"] = {"setting": name, "current_value": today,
                           "points": [{"value": v, **(current if v == today else evaluate(clips, {name: v}))}
                                      for v in values]}
    return result


def _print(result):
    print(f"Batch {result['batch']}: {result['clips_with_inputs']} clip(s) with scoring inputs.")
    bad = [c["clip"] for c in result["fidelity"] if not c["reproduced"]]
    print("Re-score with recorded settings reproduces the engine on every clip."
          if result["fidelity_ok"] else
          "WARNING: re-score does not reproduce the engine for: " + ", ".join(bad or ["(no clips)"]))

    def line(tag, ev):
        c = ev["counts"]
        return (f"{tag:>18}  caught {c['caught']}/{c['caught'] + c['missed']} suspicious · "
                f"false alarms {c['false_alarm']}/{c['false_alarm'] + c['cleared']} innocent")
    print(line("current settings", result["current"]))
    if "alternative" in result:
        print(line(", ".join(f"{k}={v}" for k, v in result["alternative"]["overrides"].items()),
                   result["alternative"]))
    if "sweep" in result:
        s = result["sweep"]
        print(f"\nSweep of {s['setting']} (today: {s['current_value']})")
        for p in s["points"]:
            print(line(f"{s['setting']}={p['value']}", p))
    print("\nA trade-off on these clips only: direction, not accuracy.")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Re-score recorded clips with different settings.")
    ap.add_argument("batch", nargs="?", help="video_results/batch_... (default: the latest)")
    ap.add_argument("--set", action="append", default=[], metavar="NAME=VALUE",
                    help="one alternative setting (repeatable)")
    ap.add_argument("--sweep", metavar="NAME=V1,V2,...", help="try several values of one setting")
    ap.add_argument("--json", action="store_true", help="print the result as JSON")
    args = ap.parse_args(argv)

    batch = args.batch or latest_batch()
    if not batch or not os.path.exists(os.path.join(batch, "summary.json")):
        msg = "No clip batch found. Record clips (record_clip.py), then run video_batch.py."
        print(json.dumps({"error": msg}) if args.json else msg)
        return 1
    try:
        one = {}
        for item in args.set:
            k, v = item.split("=", 1)
            one[k.strip()] = parse_value(k.strip(), v)
        sweep = None
        if args.sweep:
            k, vs = args.sweep.split("=", 1)
            k = k.strip()
            sweep = (k, [parse_value(k, v) for v in vs.split(",") if v.strip()])
    except ValueError as e:
        print(json.dumps({"error": str(e)}) if args.json else f"[STOP] {e}")
        return 2
    result = run(batch, sweep=sweep, one=one or None)
    if args.json:
        print(json.dumps(result))
    else:
        _print(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())

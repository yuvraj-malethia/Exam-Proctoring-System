"""
evaluation.py — ground truth and scoring rules for research experiments

THE GAP THIS CLOSES: until now the only question the project could answer
about a recording was "did the clip as a whole get flagged?". That is one
yes/no per two-minute clip, which is far too coarse for a paper: a clip
that was flagged because of ONE lucky event and a clip in which every
single glance was caught look identical, and an innocent clip that
produced six spurious look-away events but stayed under the verdict bar
looks perfect. A reviewer asks different questions:

  - Of the moments the student actually cheated, how many were caught?
    (event recall)
  - Of the moments the system said "looked away", how many were real?
    (event precision)
  - How often does it raise a false alarm on an honest student?
    (false alarms per hour of honest footage)
  - How long after the behaviour started did it notice? (latency)

Answering them needs two things this file provides:

1. A GROUND-TRUTH FORMAT. A person marks, for each clip, the stretches of
   time in which cheating behaviour actually happened. One JSON file per
   clip in <clips folder>/annotations/<clip>.json (see annotate_clip.py,
   which writes them). An innocent clip with no annotation file is taken
   to contain no cheating at all, because that is what its label says.
   A cheating clip with no annotation file can only be scored as a whole.

2. FIXED, WRITTEN-DOWN MATCHING RULES, applied identically to every
   system being compared (the full engine, every ablation and every
   baseline). Choosing these after seeing results is how papers fool
   themselves, so they are constants here and are printed with every
   result:
     - TOLERANCE_SEC: an engine event counts as catching a marked
       interval if the two overlap once the interval is widened by this
       much on each side. Human marks are imprecise by a second or so,
       and a look-away event is only closed when the student looks back.
     - WARMUP_SEC: the first seconds of every clip are ignored for every
       system, because the engine spends them learning the student's
       normal posture and cannot raise anything there by design.

Nothing in here touches the engine. It reads events and per-frame states
that experiment.py produces and turns them into numbers.
"""

import csv
import datetime
import json
import math
import os
import shutil

# ── Matching rules (fixed in advance; printed with every result) ─────────
TOLERANCE_SEC = 1.5
WARMUP_SEC = 8.0

# What a marked interval can be. Short keys go in the JSON; the text is
# what the annotation tool and the reports show.
BEHAVIOURS = {
    "head_turn": "Head turned to a neighbour, notes or anything off the paper",
    "eyes_only": "Head kept still, eyes moved to one side",
    "look_down": "Looking down below the desk line (lap, floor, sleeve)",
    "phone": "Using a phone or other device",
    "long_look": "One sustained look of five seconds or more",
    "copy_neighbour": "Group session: looking at a particular neighbour's paper",
    "signal": "Group session: signalling or passing something to a neighbour",
    "other": "Other cheating behaviour (say what in the note)",
}
BEHAVIOUR_KEYS = list(BEHAVIOURS)


# ── Ground truth ────────────────────────────────────────────────────────

def annotation_dir(clips_folder):
    return os.path.join(clips_folder, "annotations")


def annotation_path(clips_folder, clip):
    stem = os.path.splitext(os.path.basename(clip))[0]
    return os.path.join(annotation_dir(clips_folder), stem + ".json")


def parse_time(text):
    """'83.4', '1:23.4' or '0:01:23.4' -> seconds."""
    s = str(text).strip()
    if not s:
        raise ValueError("empty time")
    parts = s.split(":")
    if len(parts) > 3:
        raise ValueError(f"cannot read time '{text}'")
    total = 0.0
    for p in parts:
        total = total * 60 + float(p)
    return total


def fmt_time(sec):
    m, s = divmod(max(sec, 0.0), 60)
    return f"{int(m)}:{s:04.1f}"


def new_annotation(clip, annotator=""):
    return {
        "clip": os.path.basename(clip),
        "annotator": annotator,
        "participant": "",
        "annotated_at": None,
        # complete = "every cheating moment in this clip is marked". Only a
        # complete annotation can be used to count false alarms, because
        # an unmarked stretch of an incomplete one might still be cheating.
        "complete": False,
        "intervals": [],
        # Stretches nobody should be scored on (camera knocked, someone
        # walked in front). Excluded for every system alike.
        "exclude": [],
        "source": "file",
    }


def validate(ann):
    """Return a cleaned copy, or raise ValueError naming every problem."""
    problems = []
    out = dict(ann)
    ivs = []
    for i, iv in enumerate(ann.get("intervals", [])):
        try:
            s, e = float(iv["start"]), float(iv["end"])
        except (KeyError, TypeError, ValueError):
            problems.append(f"interval {i + 1}: needs numeric start and end")
            continue
        if e <= s:
            problems.append(f"interval {i + 1}: ends ({e}) before it starts ({s})")
        kind = iv.get("type", "other")
        if kind not in BEHAVIOURS:
            problems.append(f"interval {i + 1}: unknown type '{kind}' "
                            f"(use one of: {', '.join(BEHAVIOUR_KEYS)})")
        ivs.append({"start": round(s, 2), "end": round(e, 2), "type": kind,
                    "student": iv.get("student") or "S1",
                    "partner": iv.get("partner") or "",
                    "note": iv.get("note", "")})
    exc = []
    for i, ex in enumerate(ann.get("exclude", [])):
        try:
            exc.append({"start": float(ex["start"]), "end": float(ex["end"]),
                        "reason": ex.get("reason", "")})
        except (KeyError, TypeError, ValueError):
            problems.append(f"exclude {i + 1}: needs numeric start and end")
    if problems:
        raise ValueError("; ".join(problems))
    out["intervals"] = sorted(ivs, key=lambda v: (v["student"], v["start"]))
    out["exclude"] = sorted(exc, key=lambda v: v["start"])
    out["complete"] = bool(ann.get("complete", False))
    return out


def load_labels(clips_folder):
    p = os.path.join(clips_folder, "labels.json")
    if not os.path.exists(p):
        return {}
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def load_annotation(clips_folder, clip, labels=None):
    """The ground truth for one clip, or None if there is none.

    An innocent-labelled clip without a file is returned as a complete
    annotation with no intervals, marked source='label', so reports can
    say where every piece of ground truth came from."""
    p = annotation_path(clips_folder, clip)
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            ann = validate(json.load(f))
        ann["source"] = "file"
        return ann
    labels = labels if labels is not None else load_labels(clips_folder)
    meta = labels.get(os.path.basename(clip)) or {}
    if meta.get("label") == "innocent":
        ann = new_annotation(clip)
        ann.update(complete=True, source="label")
        return ann
    return None


def save_annotation(clips_folder, ann):
    """Write the annotation, keeping the previous version as .bak."""
    ann = validate(ann)
    ann["annotated_at"] = datetime.datetime.now().isoformat(timespec="seconds")
    ann.pop("source", None)
    p = annotation_path(clips_folder, ann["clip"])
    os.makedirs(os.path.dirname(p), exist_ok=True)
    if os.path.exists(p):
        shutil.copy2(p, p + ".bak")
    with open(p, "w", encoding="utf-8") as f:
        json.dump(ann, f, indent=2)
    return p


def intervals_from_csv(path):
    """Rows of start,end,type[,student,note] -> intervals. Times may be
    written as seconds or m:ss. Lets an operator mark times on paper or a
    spreadsheet during a session and import them afterwards."""
    out = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        for n, row in enumerate(csv.DictReader(f), start=2):
            if not any((v or "").strip() for v in row.values()):
                continue
            try:
                out.append({"start": parse_time(row["start"]), "end": parse_time(row["end"]),
                            "type": (row.get("type") or "other").strip(),
                            "student": (row.get("student") or "S1").strip(),
                            "note": (row.get("note") or "").strip()})
            except (KeyError, ValueError) as e:
                raise ValueError(f"{path} line {n}: {e}")
    return out


# ── Interval arithmetic ─────────────────────────────────────────────────

def _merge(spans):
    spans = sorted((a, b) for a, b in spans if b > a)
    out = []
    for a, b in spans:
        if out and a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return out


def _measure(spans, lo, hi):
    return sum(max(0.0, min(b, hi) - max(a, lo)) for a, b in _merge(spans))


def _overlaps(a0, a1, b0, b1, tol):
    return a0 <= b1 + tol and b0 <= a1 + tol


def _inside(t, spans):
    return any(a <= t <= b for a, b in spans)


# ── Scoring one clip ────────────────────────────────────────────────────

def score_clip(run, ann, tol=TOLERANCE_SEC, warmup=WARMUP_SEC):
    """Score one system's output on one clip against its ground truth.

    run = {
      "duration": seconds of footage,
      "students": {label: {"events": [{"start","end","direction"}],
                           "frames": [(t, dt, away, alarm)],
                           "flagged": bool}},
    }
    All times are seconds from the start of the clip.
    """
    dur = run["duration"]
    excl = [(e["start"], e["end"]) for e in (ann or {}).get("exclude", [])]
    res = {
        "flagged": any(s["flagged"] for s in run["students"].values()),
        "annotated": ann is not None,
        "complete": bool(ann and ann.get("complete")),
        "gt": 0, "gt_detected": 0, "latencies": [],
        "events": 0, "events_true": 0, "events_false": 0,
        "clean_sec": 0.0,
        "complete_gt": 0,
        "frame_tp": 0.0, "frame_fp": 0.0, "frame_fn": 0.0, "frame_tn": 0.0,
        "alarm_clean_sec": 0.0,
        "by_type": {},
        "missed": [],
    }
    if ann is None:
        return res
    gts = [iv for iv in ann["intervals"] if iv["end"] > warmup]
    for label, st in run["students"].items():
        mine = [iv for iv in gts if iv["student"] == label]
        spans = [(iv["start"], iv["end"]) for iv in mine]
        wide = [(a - tol, b + tol) for a, b in spans]
        evs = [e for e in st["events"]
               if e["end"] > warmup and not any(_overlaps(e["start"], e["end"], a, b, 0) for a, b in excl)]

        # Recall: each marked interval, caught or not.
        for iv in mine:
            hits = [e for e in evs if _overlaps(e["start"], e["end"], iv["start"], iv["end"], tol)]
            bt = res["by_type"].setdefault(iv["type"], [0, 0])
            bt[0] += 1
            res["gt"] += 1
            if hits:
                res["gt_detected"] += 1
                bt[1] += 1
                res["latencies"].append(max(0.0, min(h["start"] for h in hits) - iv["start"]))
            else:
                res["missed"].append({"student": label, "start": iv["start"],
                                      "end": iv["end"], "type": iv["type"]})

        # Precision and false alarms: only meaningful when every cheating
        # moment is marked, otherwise an unmarked real one would count
        # against the system.
        if ann.get("complete"):
            res["complete_gt"] += len(mine)
            for e in evs:
                res["events"] += 1
                if any(_overlaps(e["start"], e["end"], a, b, tol) for a, b in spans):
                    res["events_true"] += 1
                else:
                    res["events_false"] += 1
            # An alarm only counts as false BEFORE this student first
            # cheats. The verdict is cumulative by design: a student who
            # has cheated stays flagged, and that is not a false alarm.
            first_cheat = min((a for a, _ in spans), default=float("inf")) - tol
            # Honest time is time the student was actually in view and
            # not near any marked cheating: footage the camera could not
            # read is not exposure for a false alarm.
            for t, dt, away, alarm in st["frames"]:
                if t < warmup or _inside(t, excl):
                    continue
                truth = _inside(t, spans)
                if not _inside(t, wide):
                    res["clean_sec"] += dt
                if away and truth:
                    res["frame_tp"] += dt
                elif away:
                    res["frame_fp"] += dt
                elif truth:
                    res["frame_fn"] += dt
                else:
                    res["frame_tn"] += dt
                if alarm and t < first_cheat:
                    res["alarm_clean_sec"] += dt
    return res


# ── Statistics ──────────────────────────────────────────────────────────

def wilson(k, n, z=1.96):
    """Proportion with a Wilson 95% interval; (None, None, None) if n=0."""
    if n == 0:
        return None, None, None
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return p, max(0.0, c - h), min(1.0, c + h)


def poisson_rate(k, hours):
    """Events per hour with an exact (Garwood) 95% interval."""
    if hours <= 0:
        return None, None, None
    try:
        from scipy.stats import chi2
        lo = 0.0 if k == 0 else chi2.ppf(0.025, 2 * k) / 2
        hi = chi2.ppf(0.975, 2 * k + 2) / 2
    except ImportError:          # normal approximation as a fallback
        lo = max(0.0, k - 1.96 * math.sqrt(k))
        hi = k + 1.96 * math.sqrt(max(k, 1))
    return k / hours, lo / hours, hi / hours


def aggregate(clip_results, labels):
    """Pool per-clip results for one system into the headline numbers."""
    tot = {k: 0 for k in ("gt", "gt_detected", "events", "events_true", "events_false", "complete_gt")}
    clean = alarm = 0.0
    ftp = ffp = ffn = 0.0
    lat = []
    by_type = {}
    caught = n_cheat = false_flags = n_innocent = 0
    for clip, r in clip_results.items():
        lab = (labels.get(clip) or {}).get("label")
        if lab == "suspicious":
            n_cheat += 1
            caught += r["flagged"]
        elif lab == "innocent":
            n_innocent += 1
            false_flags += r["flagged"]
        for k in tot:
            tot[k] += r[k]
        clean += r["clean_sec"]
        alarm += r["alarm_clean_sec"]
        ftp += r["frame_tp"]; ffp += r["frame_fp"]; ffn += r["frame_fn"]
        lat += r["latencies"]
        for t, (n, d) in r["by_type"].items():
            bt = by_type.setdefault(t, [0, 0]); bt[0] += n; bt[1] += d
    hours = clean / 3600.0
    rec = wilson(tot["gt_detected"], tot["gt"])
    # Precision means nothing until some complete ground truth contains
    # cheating: on honest clips alone every event is false by definition.
    prec = (wilson(tot["events_true"], tot["events"]) if tot["complete_gt"]
            else (None, None, None))
    fa = poisson_rate(tot["events_false"], hours)
    fprec = ftp / (ftp + ffp) if ftp + ffp else None
    frec = ftp / (ftp + ffn) if ftp + ffn else None
    f1 = (2 * fprec * frec / (fprec + frec)) if fprec and frec else None
    lat.sort()
    return {
        "clips": len(clip_results),
        "cheating_clips": n_cheat, "cheating_caught": caught,
        "innocent_clips": n_innocent, "innocent_flagged": false_flags,
        "gt_intervals": tot["gt"], "gt_detected": tot["gt_detected"],
        "event_recall": rec,
        "events": tot["events"], "events_true": tot["events_true"],
        "events_false": tot["events_false"],
        "event_precision": prec,
        "clean_hours": round(hours, 4),
        "false_alarms_per_hour": fa,
        "alarm_min_per_clean_hour": (alarm / 60.0) / hours if hours else None,
        "frame_precision": fprec, "frame_recall": frec, "frame_f1": f1,
        "median_latency_sec": lat[len(lat) // 2] if lat else None,
        "recall_by_type": {t: wilson(d, n) + (n,) for t, (n, d) in by_type.items()},
    }

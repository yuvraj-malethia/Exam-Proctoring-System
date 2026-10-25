"""
experiment.py — baselines and ablations on recorded clips, for the paper

THE GAP THIS CLOSES: the Validation page answers "does the engine get
these clips right?". A paper has to answer a harder question: "is it
better than the obvious simple approach, and which of its parts are
actually responsible?" Without that, every design choice in this
project (personal calibration, the eye channel, looking-down, frozen
calibration, fading session memory) is a claim with no measurement
behind it, and a reviewer will say so.

So this runs several SYSTEMS over exactly the same recorded inputs and
scores every one of them with exactly the same rules (evaluation.py):

  full         the engine as it ships today
  no_eyes      the same, with the eye channel switched off
  no_pitch     the same, with looking-down switched off
  no_personal  the same, but every student gets the SAME baseline and
               spread (the population average) instead of their own
  no_freeze    calibration keeps updating forever (the pre-Sep-2026 bug)
  no_decay     session memory never fades (the pre-Aug-2026 bug)
  fixed_15     the common simple rule: head turned more than 15 degrees
               from straight at the camera, for at least as long as the
               engine's own minimum, is a look away; three looks inside
               the engine's own window raises the alarm

Every ablation is one of the engine's own kill switches, or (for
no_personal) the same engine with its calibration pinned, so nothing is
reimplemented. The fixed-angle baseline uses the same smoothing filter
and the same minimum duration, so the ONLY difference is the decision
rule. With --curve it also sweeps thresholds for the full engine and the
fixed rule, which gives the trade-off curve (catches against false
alarms) that a single operating point cannot show.

HOW IT STAYS HONEST
  - It re-scores from the inputs video_batch.py recorded, through fresh
    StudentPipelines, the same way whatif.py does. Before anything else,
    the "full" replay of every clip is compared field by field with the
    engine's own result for that clip. If any clip differs, the whole
    experiment is marked UNTRUSTED in its output.
  - The matching rules (tolerance, warm-up) are fixed constants in
    evaluation.py and are printed with every result.
  - Numbers that need ground truth this experiment does not have are
    shown as "-" and the report says which clips need annotating.
  - It changes Config values while it runs, like whatif.py, so it must
    never share a process with a live engine. It is a command-line tool
    only.

USAGE
    python experiment.py                       latest batch, all systems
    python experiment.py --curve               also sweep thresholds
    python experiment.py --batch video_results/batch_2026_10_08_171124
    python experiment.py --systems full,no_eyes,fixed_15

Output goes to research/results/<date_time>/: table.md (read this first),
results.json (everything), per_clip.csv, and curve.png with --curve.
"""

import argparse
import csv
import datetime
import json
import os
import statistics
import sys
import time

from config import Config
from kalman_filter import KalmanFilter1D
from occupant_archive import OccupantArchive
from student_pipeline import MAX_STUDENTS, StudentPipeline
from verdict import compute_verdict
import evaluation as ev
import whatif

OUT_ROOT = os.path.join("research", "results")
_RANK = whatif._RANK

SYSTEMS = {
    "full": ("Full system", {}),
    "no_eyes": ("Without eye tracking", {"EYE_TRACKING_ENABLED": False}),
    "no_pitch": ("Without looking-down", {"PITCH_ENABLED": False}),
    "no_personal": ("Same baseline for everyone", {}),
    "no_freeze": ("Calibration never frozen", {"CALIBRATION_FREEZE_ENABLED": False}),
    "no_decay": ("Session memory never fades", {"SESSION_MEMORY_DECAY_ENABLED": False}),
    "fixed_15": ("Fixed 15 degree head rule", {}),
}
ORDER = list(SYSTEMS)

CURVE_SCALE = [0.7, 0.8, 0.9, 1.0, 1.1, 1.25, 1.4, 1.6]
CURVE_ANGLES = [6, 8, 10, 12, 15, 20, 25, 30]


# ── The engine, replayed ────────────────────────────────────────────────

def _pin(calib, base, std):
    """Make one CalibrationEngine behave as if every student had the
    population's baseline and spread. Wraps the instance only: the
    learning still runs, so is_calibrated flips at exactly the same
    frame, but the values it would have learned are replaced."""
    orig_feed, orig_adapt = calib.feed, calib.adapt

    def feed(v):
        r = orig_feed(v)
        if calib.is_calibrated:
            calib.baseline, calib.std = base, std
            if calib.initial_baseline is not None:
                calib.initial_baseline = base
        return r

    def adapt(v, z):
        orig_adapt(v, z)
        calib.baseline = base

    calib.feed, calib.adapt = feed, adapt


def _pin_student(st, pop):
    for attr, key in (("calib", "yaw"), ("pitch_calib", "pitch"), ("eye_calib", "eye")):
        if key in pop:
            _pin(getattr(st, attr), *pop[key])


def replay_engine(inputs, overrides=None, population=None):
    """Re-score one clip. Returns (run, summary, calib_stats).

    run      what evaluation.score_clip() reads: per-student events and
             per-frame (t, dt, away, alarm), in seconds from clip start
    summary  the same fields whatif.rescore() returns, so the replay can
             be checked against the engine's own result
    stats    each student's learned baselines and spreads (for building
             the population used by no_personal)
    """
    t0 = min(inputs["session_starts"].values())
    flagged_verdicts = tuple(Config.EVIDENCE_CAPTURE_VERDICTS)
    with whatif.overridden(overrides):
        students = {i: StudentPipeline(i) for i in range(MAX_STUDENTS)}
        for sid, st in students.items():
            start = inputs["session_starts"].get(str(sid))
            if start is not None:
                st.temporal._session_start = start
            if population:
                _pin_student(st, population)
        archive = OccupantArchive(None)
        records, seen, frames, old_events = {}, {}, {}, {}
        last_t = t0
        for t, dt, ops in inputs["frames"]:
            last_t = t
            active = []
            for op in ops:
                kind, sid = op[0], op[1]
                st = students[sid]
                if kind == "r":
                    outgoing = list(st.temporal.events)
                    old_events.setdefault(sid, []).extend(outgoing)
                    summary = st.reset(t)
                    st.temporal._session_start = t
                    if population:
                        _pin_student(st, population)
                    records[op[2]] = archive.close_record(
                        slot=sid, label=st.label, summary=summary, events=outgoing,
                        started_at=t, ended_at=t, reason="experiment replay")
                elif kind == "p":
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
                st = students[sid]
                v, _ = compute_verdict(st, t)
                vs = seen.setdefault(sid, {})
                vs[v] = vs.get(v, 0.0) + dt
                frames.setdefault(sid, []).append(
                    (round(t - t0, 3), dt, st.temporal.is_currently_away(), v in flagged_verdicts))

        run = {"duration": round(last_t - t0, 2), "students": {}}
        summary = {"students": {}}
        stats = {}
        for sid, vs in sorted(seen.items()):
            st = students[sid]
            evs = old_events.get(sid, []) + list(st.temporal.events)
            uniq = {}
            for e in evs:
                uniq[(round(e.timestamp, 3), round(e.duration, 3))] = e
            out = [{"start": round(e.timestamp - e.duration - t0, 2),
                    "end": round(e.timestamp - t0, 2), "direction": e.direction}
                   for e in sorted(uniq.values(), key=lambda e: e.timestamp)]
            # A look still in progress when the clip ends is a look, as
            # long as it already lasted the engine's own minimum.
            if st.temporal._away_start is not None:
                d = last_t - st.temporal._away_start
                if d >= Config.MIN_EVENT_DURATION:
                    out.append({"start": round(st.temporal._away_start - t0, 2),
                                "end": round(last_t - t0, 2),
                                "direction": st.temporal._away_dir, "open": True})
            fr = frames.get(sid, [])
            run["students"][st.label] = {
                "events": out, "frames": fr,
                "flagged": any(a for (tt, _, _, a) in fr if tt >= ev.WARMUP_SEC),
            }
            reached = [v for v in vs if v in _RANK]
            highest = max(reached, key=_RANK.get) if reached else "CALIBRATING"
            summary["students"][st.label] = {
                "highest_verdict": highest,
                "flagged_for_review": highest in flagged_verdicts,
                "seconds_per_verdict": {k: round(v, 1) for k, v in vs.items()},
                "peak_score": round(st.fs.max_probability, 1),
                "final_score": round(st.fs.probability, 1),
                "events": len(st.temporal.events),
            }
            s = {}
            if st.calib.is_calibrated:
                b = st.calib.initial_baseline if st.calib.initial_baseline is not None else st.calib.baseline
                s["yaw"] = (b, st.calib.std)
            if st.pitch_calib.is_calibrated:
                s["pitch"] = (st.pitch_calib.baseline, st.pitch_calib.std)
            if st.eye_calib.is_calibrated:
                s["eye"] = (st.eye_calib.baseline, st.eye_calib.std)
            stats[st.label] = s
        summary["flagged_for_review"] = any(s["flagged_for_review"] for s in summary["students"].values())
    return run, summary, stats


# ── The simple baseline ─────────────────────────────────────────────────

def replay_fixed(inputs, angle):
    """Head more than `angle` degrees from straight at the camera (left,
    right, or down) = away. Same Kalman settings as the engine's head
    path, same minimum event length, same 'three looks in the window'
    count the engine's first verdict check uses. No calibration: the
    point of this baseline is that it does not know the student."""
    t0 = min(inputs["session_starts"].values())
    win = Config.TEMPORAL_WINDOW_SEC
    st = {}
    last_t = t0

    def fresh():
        return {"ky": KalmanFilter1D(process_noise=0.5, measurement_noise=6.0),
                "kp": KalmanFilter1D(process_noise=0.5, measurement_noise=6.0),
                "pitch": None, "away_since": None, "events": [], "frames": []}

    for t, dt, ops in inputs["frames"]:
        last_t = t
        for op in ops:
            kind, sid = op[0], op[1]
            if kind == "r":
                if sid in st:
                    keep = st[sid]["events"], st[sid]["frames"]
                    st[sid] = fresh()
                    st[sid]["events"], st[sid]["frames"] = keep
                continue
            if kind != "p":
                continue
            s = st.setdefault(sid, fresh())
            yaw, _ = s["ky"].update(op[2], dt)
            if op[5] is not None and not op[3]:
                s["pitch"], _ = s["kp"].update(op[5], dt)
            away = abs(yaw) > angle or (
                Config.PITCH_ENABLED and s["pitch"] is not None
                and s["pitch"] * Config.PITCH_DOWN_SIGN > angle)
            if away and s["away_since"] is None:
                s["away_since"] = t
            elif not away and s["away_since"] is not None:
                if t - s["away_since"] >= Config.MIN_EVENT_DURATION:
                    s["events"].append((s["away_since"], t))
                s["away_since"] = None
            recent = sum(1 for (_, e) in s["events"] if t - win < e <= t)
            s["frames"].append((round(t - t0, 3), dt, away, recent >= 3))

    run = {"duration": round(last_t - t0, 2), "students": {}}
    for sid, s in sorted(st.items()):
        evs = list(s["events"])
        if s["away_since"] is not None and last_t - s["away_since"] >= Config.MIN_EVENT_DURATION:
            evs.append((s["away_since"], last_t))
        run["students"][f"S{sid + 1}"] = {
            "events": [{"start": round(a - t0, 2), "end": round(b - t0, 2), "direction": "head"}
                       for a, b in evs],
            "frames": s["frames"],
            "flagged": any(a for (tt, _, _, a) in s["frames"] if tt >= ev.WARMUP_SEC),
        }
    return run


# ── Running a set of systems over a batch ───────────────────────────────

def _unit(clip, ann, labels):
    """Who a clip belongs to, for leave-one-out. A participant id if the
    annotation or label gives one, otherwise the clip itself."""
    for src in ((ann or {}).get("participant"), (labels.get(clip) or {}).get("participant")):
        if src:
            return str(src)
    return clip


def _population(stats_by_clip, units, exclude_unit):
    """Mean baseline and median spread per channel, from every clip that
    does NOT belong to `exclude_unit` — so a student is never pinned to
    a population that includes themselves."""
    pools = {}
    for clip, per_student in stats_by_clip.items():
        if units[clip] == exclude_unit:
            continue
        for s in per_student.values():
            for ch, (b, sd) in s.items():
                pools.setdefault(ch, []).append((b, sd))
    return {ch: (statistics.fmean(b for b, _ in v), statistics.median(sd for _, sd in v))
            for ch, v in pools.items() if v}


def run_systems(clips, anns, labels, systems, scale=None, angle=None):
    """{system: {clip: score_clip result}}, plus fidelity and stats."""
    results, fidelity, stats = {}, {}, {}
    units = {c: _unit(c, anns.get(c), labels) for c, _, _ in clips}

    def kover(f):
        return {"K_SIGMA": Config.K_SIGMA * f, "PITCH_K_SIGMA": Config.PITCH_K_SIGMA * f,
                "EYE_K_SIGMA": Config.EYE_K_SIGMA * f}

    need_full = "full" in systems or "no_personal" in systems
    if need_full:
        results["full"] = {}
        for clip, _, inputs in clips:
            run, summary, st = replay_engine(inputs, kover(scale) if scale else None)
            stats[clip] = st
            if not scale:
                fidelity[clip] = whatif.matches_engine(inputs, summary)
            results["full"][clip] = ev.score_clip(run, anns.get(clip))
    for name in systems:
        if name in ("full",):
            continue
        results[name] = {}
        for clip, _, inputs in clips:
            if name == "fixed_15":
                run = replay_fixed(inputs, angle if angle else 15.0)
            elif name == "no_personal":
                pop = _population(stats, units, units[clip])
                run, _, _ = replay_engine(inputs, None, population=pop)
            else:
                run, _, _ = replay_engine(inputs, SYSTEMS[name][1])
            results[name][clip] = ev.score_clip(run, anns.get(clip))
    if "full" not in systems:
        results.pop("full", None)
    return results, fidelity, units


# ── Reporting ───────────────────────────────────────────────────────────

def _p(t, pct=True):
    if not t or t[0] is None:
        return "-"
    p, lo, hi = t[:3]
    if pct:
        return f"{p * 100:.0f}% ({lo * 100:.0f}-{hi * 100:.0f})"
    return f"{p:.1f} ({lo:.1f}-{hi:.1f})"


def _n(v, fmt="{:.1f}"):
    return "-" if v is None else fmt.format(v)


def write_report(out_dir, batch_dir, clips, anns, labels, agg, per_clip, fidelity, units, curve, took):
    trusted = all(fidelity.values()) if fidelity else False
    gt_cheat = [c for c, _, _ in clips if (labels.get(c) or {}).get("label") == "suspicious"]
    annotated = [c for c in gt_cheat if anns.get(c)]
    L = []
    L.append(f"# Experiment - {datetime.datetime.now():%d %b %Y, %H:%M}\n")
    L.append(f"Batch: `{batch_dir}` - {len(clips)} clips - {len(set(units.values()))} "
             f"participant unit(s) - took {took:.0f} s\n")
    if not trusted:
        bad = [c for c, ok in fidelity.items() if not ok]
        L.append(f"**UNTRUSTED: the replay did not reproduce the engine on {', '.join(bad) or 'any clip'}.** "
                 "Do not use these numbers.\n")
    else:
        L.append("Fidelity check passed: the full-system replay reproduced the engine's own result "
                 "exactly on every clip.\n")
    L.append(f"Matching rules, fixed in advance: tolerance {ev.TOLERANCE_SEC} s either side of a marked "
             f"interval; first {ev.WARMUP_SEC} s of every clip ignored for every system.\n")
    L.append(f"Ground truth: {len(annotated)} of {len(gt_cheat)} cheating clips annotated; innocent clips "
             "count as containing no cheating, as labelled.\n")
    if len(annotated) < len(gt_cheat):
        L.append("Not annotated yet (event recall cannot use them): "
                 + ", ".join(sorted(set(gt_cheat) - set(annotated)))
                 + ". Run `python annotate_clip.py clips/<clip>` on each.\n")

    L.append("\n## Main table\n")
    L.append("| System | Cheating clips flagged | Innocent clips flagged | Event recall (95% CI) "
             "| Event precision | False looks / honest hour (95% CI) | Alarm min / honest hour |")
    L.append("|---|---|---|---|---|---|---|")
    for name in [s for s in ORDER if s in agg]:
        a = agg[name]
        L.append(f"| {SYSTEMS[name][0]} | {a['cheating_caught']} of {a['cheating_clips']} "
                 f"| {a['innocent_flagged']} of {a['innocent_clips']} | {_p(a['event_recall'])} "
                 f"| {_p(a['event_precision'])} | {_p(a['false_alarms_per_hour'], pct=False)} "
                 f"| {_n(a['alarm_min_per_clean_hour'])} |")
    L.append("")
    L.append("- *False looks / honest hour*: look-away events the system produced while the student was "
             "not cheating, per hour of honest footage in view. An event is not an accusation; it is the "
             "raw signal the verdict is built from.")
    L.append("- *Alarm min / honest hour*: minutes spent at ALERT or FLAG (the fixed rule: three looks "
             "inside the window) during honest footage before the student's first cheating moment. After a "
             "student has cheated, staying flagged is the intended behaviour, not a false alarm. This is "
             "what an invigilator would actually see.")
    L.append("- *Same baseline for everyone* pins every student to the average of the OTHER participants. "
             "With only one participant the 'population' is the same person's other clips, so this "
             "understates the value of personal calibration until more people are recorded.")
    honest_h = next(iter(agg.values()))["clean_hours"] if agg else 0
    L.append(f"- Honest footage behind the per-hour figures: {honest_h * 60:.1f} minutes. Intervals "
             "this wide are the honest consequence of a small sample.")

    L.append("\n## Per clip (flagged / looks produced)\n")
    names = [s for s in ORDER if s in per_clip]
    L.append("| Clip | Label | " + " | ".join(SYSTEMS[n][0] for n in names) + " |")
    L.append("|---|---|" + "---|" * len(names))
    for clip, _, _ in clips:
        lab = (labels.get(clip) or {}).get("label", "?")
        cells = []
        for n in names:
            r = per_clip[n][clip]
            looks = r["events"] if r["complete"] else "?"
            cells.append(f"{'FLAG' if r['flagged'] else 'ok'} / {looks}")
        L.append(f"| {clip} | {lab} | " + " | ".join(cells) + " |")
    L.append("\n'?' = looks not counted, because the clip has no complete ground truth.")

    if curve:
        L.append("\n## Trade-off curve\n")
        L.append("Thresholds swept for the full engine (all three z-thresholds scaled together) and for the "
                 "fixed rule (angle). See curve.png.\n")
        L.append("| System | Setting | Cheating clips flagged | Innocent clips flagged | Event recall "
                 "| False looks / honest hour |")
        L.append("|---|---|---|---|---|---|")
        for row in curve:
            L.append(f"| {row['system']} | {row['setting']} | {row['caught']} | {row['false_flags']} "
                     f"| {_p(row['recall'])} | {_p(row['fa'], pct=False)} |")
    L.append("\n*Staged clips show direction, not general accuracy. Report sample sizes with every number.*")
    with open(os.path.join(out_dir, "table.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")


def plot_curve(curve, out_dir, has_recall):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(figsize=(6.4, 4.2))
    for sysname, colour in (("Full system", "#b8432f"), ("Fixed head-angle rule", "#2f5d8a")):
        pts = [r for r in curve if r["system"] == sysname]
        xs = [r["fa"][0] for r in pts]
        if has_recall:
            ys = [r["recall"][0] * 100 if r["recall"][0] is not None else None for r in pts]
            ylab = "event recall (%)"
        else:
            ys = [r["caught_frac"] * 100 for r in pts]
            ylab = "cheating clips flagged (%)"
        ax.plot(xs, ys, "o-", color=colour, label=sysname)
        for r, x, y in zip(pts, xs, ys):
            if y is not None:
                ax.annotate(r["setting"], (x, y), fontsize=7, xytext=(3, 3), textcoords="offset points")
    ax.set_xlabel("false looks per honest hour")
    ax.set_ylabel(ylab)
    ax.set_title("Catches against false alarms as the threshold moves", fontsize=10)
    ax.legend(frameon=False, fontsize=8)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    fig.tight_layout()
    fig.savefig(os.path.join(out_dir, "curve.png"), dpi=200)
    plt.close(fig)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Baselines and ablations on recorded clips.")
    ap.add_argument("--batch", help="batch folder (default: the latest in video_results/)")
    ap.add_argument("--clips", help="clips folder holding labels.json and annotations/ "
                                    "(default: the folder the batch was run on)")
    ap.add_argument("--systems", default=",".join(ORDER), help="comma-separated, from: " + ", ".join(ORDER))
    ap.add_argument("--curve", action="store_true", help="also sweep thresholds (slower)")
    ap.add_argument("--out", default=OUT_ROOT)
    a = ap.parse_args(argv)

    systems = [s.strip() for s in a.systems.split(",") if s.strip()]
    bad = [s for s in systems if s not in SYSTEMS]
    if bad:
        raise SystemExit(f"unknown system(s): {', '.join(bad)}")
    batch = a.batch or whatif.latest_batch()
    if not batch:
        raise SystemExit("no batch found; run video_batch.py first")
    with open(os.path.join(batch, "summary.json"), encoding="utf-8") as f:
        folder = a.clips or json.load(f).get("folder") or "clips"
    clips = whatif.batch_clips(batch)
    if not clips:
        raise SystemExit(f"{batch} has no recorded scoring inputs")
    labels = ev.load_labels(folder)
    anns = {c: ev.load_annotation(folder, c, labels) for c, _, _ in clips}

    t_start = time.time()
    print(f"{len(clips)} clips from {batch}; ground truth from {folder}")
    res, fidelity, units = run_systems(clips, anns, labels, systems)
    for clip, ok in fidelity.items():
        if not ok:
            print(f"[WARN] replay does not reproduce the engine on {clip}")
    agg = {name: ev.aggregate(r, labels) for name, r in res.items()}

    curve = []
    if a.curve:
        print("sweeping thresholds ...")
        for f in CURVE_SCALE:
            r, _, _ = run_systems(clips, anns, labels, ["full"], scale=f)
            g = ev.aggregate(r["full"], labels)
            curve.append({"system": "Full system", "setting": f"x{f:g}", **_curve_row(g)})
        for ang in CURVE_ANGLES:
            r, _, _ = run_systems(clips, anns, labels, ["fixed_15"], angle=float(ang))
            g = ev.aggregate(r["fixed_15"], labels)
            curve.append({"system": "Fixed head-angle rule", "setting": f"{ang} deg", **_curve_row(g)})

    out_dir = os.path.join(a.out, datetime.datetime.now().strftime("%Y_%m_%d_%H%M%S"))
    os.makedirs(out_dir, exist_ok=True)
    took = time.time() - t_start
    write_report(out_dir, batch, clips, anns, labels, agg, res, fidelity, units, curve, took)
    with open(os.path.join(out_dir, "per_clip.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["system", "clip", "label", "flagged", "gt_intervals", "gt_detected",
                    "events", "events_false", "honest_sec", "alarm_honest_sec"])
        for name, per in res.items():
            for clip, r in per.items():
                w.writerow([name, clip, (labels.get(clip) or {}).get("label"), r["flagged"], r["gt"],
                            r["gt_detected"], r["events"], r["events_false"],
                            round(r["clean_sec"], 1), round(r["alarm_clean_sec"], 1)])
    with open(os.path.join(out_dir, "results.json"), "w", encoding="utf-8") as f:
        json.dump({"batch": batch, "clips_folder": folder,
                   "rules": {"tolerance_sec": ev.TOLERANCE_SEC, "warmup_sec": ev.WARMUP_SEC},
                   "fidelity": fidelity, "trusted": all(fidelity.values()) if fidelity else False,
                   "units": units,
                   "ground_truth": {c: (None if a_ is None else {"source": a_["source"],
                                                                 "complete": a_["complete"],
                                                                 "intervals": len(a_["intervals"])})
                                    for c, a_ in anns.items()},
                   "aggregate": agg, "per_clip": res, "curve": curve}, f, indent=1, default=str)
    if curve:
        has_recall = any(r["recall"][0] is not None for r in curve)
        plot_curve(curve, out_dir, has_recall)
    print(f"done in {took:.0f} s -> {out_dir}")
    with open(os.path.join(out_dir, "table.md"), encoding="utf-8") as f:
        sys.stdout.buffer.write(f.read().encode("utf-8"))
    return 0


def _curve_row(g):
    return {"caught": f"{g['cheating_caught']} of {g['cheating_clips']}",
            "caught_frac": g["cheating_caught"] / g["cheating_clips"] if g["cheating_clips"] else 0.0,
            "false_flags": f"{g['innocent_flagged']} of {g['innocent_clips']}",
            "recall": g["event_recall"], "fa": g["false_alarms_per_hour"]}


if __name__ == "__main__":
    sys.exit(main())

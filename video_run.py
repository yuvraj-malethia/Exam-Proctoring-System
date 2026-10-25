"""
video_run.py — replay a recorded video through the real engine.

WHY THIS EXISTS
───────────────────────────────────────────────────────────────────────
Until now every change to a threshold could only be checked by sitting
in front of the webcam again and trying to repeat the same behaviour —
slow, never exactly repeatable, and impossible for multi-person
scenarios without people in the room. Real exam halls also already have
CCTV footage that could be reviewed after the fact.

With this, a few clips are recorded once (see record_clip.py) and then
replayed through module_headpose_multi_web.main() — the SAME loop, the
SAME pipeline, the SAME identity tracking — after every change (see
video_batch.py). Nothing about the analysis is reimplemented here; this
file only supplies frames and time, and summarises what came out.

Two pieces:
  VideoSource    stands in for cv2.VideoCapture(0). Takes each frame's
                 timestamp from the file itself, so the engine's clock
                 (clock.py) follows the footage rather than the computer,
                 and the same clip always gives the same answer however
                 fast it is analysed. Optionally paces playback to the
                 clip's own speed so the dashboard / 3D view can be
                 watched replaying it.
  VideoRunStats  collects a per-clip summary while the loop runs (verdicts
                 reached, time in each, events, named patterns) and writes
                 it as JSON for video_batch.py to compare against labels.
                 Read-only: it never feeds anything back into the engine.

Sep 2026 — SCORING INPUTS. VideoRunStats also records exactly what went
into every StudentPipeline.process() call (raw yaw, profile flag, face
ratio, raw pitch, t, dt), plus every seat reset and record merge, and
writes them next to the results as <clip>.inputs.json.gz. whatif.py
replays those numbers through fresh pipelines with different settings:
the slow part (MediaPipe) runs once per clip, and a threshold can then be
tried against every clip in seconds instead of minutes. Values are stored
as full-precision floats, so a re-score with unchanged settings
reproduces the engine's own results exactly (whatif.py checks this every
time). Still read-only: recording never changes what the engine does.
"""

import gzip
import json
import os
import time
from collections import Counter
from datetime import datetime

import cv2

from config import Config

# Verdict severity order, for "highest verdict reached".
_VERDICT_RANK = {"CALIBRATING": 0, "OK": 1, "WATCH": 2, "ALERT": 3, "FLAG": 4}


class VideoSource:
    """Drop-in for cv2.VideoCapture(0): isOpened() / read() / release()."""

    def __init__(self, path: str, paced: bool):
        self.path = path
        self.cap = cv2.VideoCapture(path)
        fps = self.cap.get(cv2.CAP_PROP_FPS) if self.cap.isOpened() else 0.0
        self.fps = fps if fps and 1.0 <= fps <= 240.0 else 30.0
        self.frame_count = int(self.cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0) if self.cap.isOpened() else 0
        self.paced = paced
        # Video second 0 is anchored to the moment the replay starts, so
        # every timestamp the engine produces still looks like an ordinary
        # epoch time to evidence files, logs and the dashboard.
        self.origin = time.time()
        self.video_sec = 0.0
        self.frames_read = 0
        self._pace_zero = None

    def isOpened(self) -> bool:
        return self.cap.isOpened()

    def read(self):
        ok, frame = self.cap.read()
        if not ok:
            return ok, frame
        # Prefer the file's own timestamp: phone and Windows Camera
        # recordings often have a variable frame rate, so frame_number/fps
        # drifts. Fall back to counting frames whenever a backend reports
        # nothing usable, goes backwards, or jumps implausibly.
        pos = self.cap.get(cv2.CAP_PROP_POS_MSEC) / 1000.0
        if self.frames_read == 0:
            if not (0.0 <= pos < 5.0):
                pos = 0.0
        elif not (self.video_sec < pos <= self.video_sec + 5.0):
            pos = self.video_sec + 1.0 / self.fps
        self.video_sec = pos
        self.frames_read += 1
        if self.paced:
            if self._pace_zero is None:
                self._pace_zero = time.perf_counter() - pos
            delay = self._pace_zero + pos - time.perf_counter()
            if delay > 0:
                time.sleep(delay)
        return ok, frame

    def now(self) -> float:
        """Engine time for the frame most recently read (see clock.py)."""
        return self.origin + self.video_sec

    def release(self):
        self.cap.release()


class VideoRunStats:
    """Per-clip summary, gathered from state the loop already computed."""

    def __init__(self, source: VideoSource):
        self.source = source
        self.frames = 0
        self.frames_with_face = 0
        self.presence = {}        # slot -> visibility summary (presence.py)
        self._last_t = None       # engine time of the frame being recorded
        self.per_slot = {}        # slot -> visible time, verdict time, looking-down frames
        self.pattern_best = {}    # slot -> {pattern label: best confidence}
        self.cross_best = {}      # "label (A & B)" -> best confidence
        self._perf_start = time.perf_counter()
        # Scoring inputs for whatif.py: one entry per frame that did any
        # scoring work, [t, dt, ops], where ops run in the engine's order:
        #   ["r", slot, record_id]                  seat reset (new occupant)
        #   ["p", slot, yaw, is_profile, ratio, pitch]   process() call
        #   ["m", slot, record_id]                  archived record merged back
        self.input_frames = []
        self.session_starts = None   # each pipeline's session start, as created
        self._frame = None

    # ── Scoring inputs (read-only hooks called from the main loop) ──────
    def begin_frame(self, t, dt, students):
        if self.session_starts is None:
            self.session_starts = {str(sid): st.temporal._session_start
                                   for sid, st in students.items()}
        self._last_t = t
        self._frame = [t, dt, []]

    def _op(self, op):
        if self._frame is None:
            return
        if not self._frame[2]:
            self.input_frames.append(self._frame)
        self._frame[2].append(op)

    def note_reset(self, sid, record_id):
        self._op(["r", sid, record_id])

    def note_input(self, sid, yaw_raw, is_profile, face_ar, pitch_raw,
                   eye_raw=None, eye_openness=None, eye_down=None):
        # Eye values are appended, never inserted: an inputs file written
        # before eye tracking existed still unpacks correctly, and
        # whatif.py reads them positionally with a length check.
        self._op(["p", sid, yaw_raw, bool(is_profile), face_ar, pitch_raw,
                  eye_raw, eye_openness, eye_down])

    def note_merge(self, sid, record_id):
        self._op(["m", sid, record_id])

    def write_inputs(self, path, results):
        """Gzipped JSON of every scoring input, plus the settings and the
        engine's own per-student outcome so whatif.py can prove a re-score
        with unchanged settings gives the same answer."""
        data = {
            "version": 1,
            "clip": results["clip"],
            "fps": results["video"]["fps"],
            "session_starts": self.session_starts or {},
            "settings": results["settings"],
            "engine_result": {
                "flagged_for_review": results["flagged_for_review"],
                "students": {k: {f: v[f] for f in ("highest_verdict", "seconds_per_verdict",
                                                   "peak_score", "final_score", "events")}
                             for k, v in results["students"].items()},
            },
            "frames": self.input_frames,
        }
        with gzip.open(path, "wt", encoding="utf-8") as f:
            json.dump(data, f, separators=(",", ":"))
        return path

    def observe(self, dt, active_ids, students, verdicts, patterns, correlations,
                presence=None):
        self.frames += 1
        if active_ids:
            self.frames_with_face += 1
        for sid in active_ids:
            s = self.per_slot.setdefault(sid, {"visible_frames": 0, "visible_sec": 0.0,
                                               "verdict_sec": {}, "looking_down_frames": 0})
            s["visible_frames"] += 1
            s["visible_sec"] += dt
            v = verdicts.get(sid)
            if v:
                s["verdict_sec"][v] = s["verdict_sec"].get(v, 0.0) + dt
            if students[sid].looking_down:
                s["looking_down_frames"] += 1
        for sid, found in (patterns or {}).items():
            best = self.pattern_best.setdefault(sid, {})
            for p in found or []:
                label = p.get("label") or p.get("key")
                best[label] = max(best.get(label, 0.0), float(p.get("confidence") or 0.0))
        # Visibility, kept per clip so a replay can report the thing
        # that used to be silent: how long this seat could not be read
        # at all (see presence.py).
        if presence is not None:
            self.presence = presence.summary(self._last_t if self._last_t is not None else 0.0)
        for c in correlations or []:
            key = f"{c.get('label')} ({' & '.join(str(x) for x in c.get('pair', []))})"
            self.cross_best[key] = max(self.cross_best.get(key, 0.0), float(c.get("confidence") or 0.0))

    def results(self, students, session_id, report=None, faults=None) -> dict:
        src = self.source
        processing = time.perf_counter() - self._perf_start
        flagged_verdicts = tuple(Config.EVIDENCE_CAPTURE_VERDICTS)
        slots = {}
        for sid, seen in sorted(self.per_slot.items()):
            st = students[sid]
            events = list(st.temporal.events)
            reached = [v for v in seen["verdict_sec"] if v in _VERDICT_RANK]
            highest = max(reached, key=_VERDICT_RANK.get) if reached else "CALIBRATING"
            slots[st.label] = {
                "name": getattr(st, "display_name", st.label),
                "occupants_in_seat": st.occupant_serial,
                "calibrated": st.calib.is_calibrated,
                "visible_sec": round(seen["visible_sec"], 1),
                "highest_verdict": highest,
                "flagged_for_review": highest in flagged_verdicts,
                "seconds_per_verdict": {k: round(v, 1) for k, v in seen["verdict_sec"].items()},
                "peak_score": round(st.fs.max_probability, 1),
                "final_score": round(st.fs.probability, 1),
                "confirmed_sec": round(st.fs.confirmed_secs, 1),
                "events": len(events),
                "events_by_direction": dict(Counter(e.direction for e in events)),
                "longest_event_sec": round(max((e.duration for e in events), default=0.0), 1),
                "total_away_sec": round(st.temporal.total_away_s, 1),
                "looking_down_frames": seen["looking_down_frames"],
                "presence": self.presence.get(sid),
                "patterns": [{"label": k, "best_confidence": round(v, 3)}
                             for k, v in sorted(self.pattern_best.get(sid, {}).items(),
                                                key=lambda kv: -kv[1])],
                "calibration": {
                    "initial_baseline": (round(st.calib.initial_baseline, 2)
                                         if st.calib.initial_baseline is not None else None),
                    "final_baseline": round(st.calib.baseline, 2),
                    "std": round(st.calib.std, 2),
                },
            }
        return {
            "clip": os.path.basename(src.path),
            "clip_path": os.path.abspath(src.path),
            "analysed_at": datetime.now().isoformat(timespec="seconds"),
            "session_id": session_id,
            "video": {
                "fps": round(src.fps, 2),
                "frames_analysed": self.frames,
                "footage_sec": round(src.video_sec, 1),
                "face_seen_pct": round(100.0 * self.frames_with_face / max(self.frames, 1), 1),
                # Frames the engine swallowed an error on. A replay that
                # failed must never read as a replay that saw an empty
                # room — see frame_faults.py.
                "faults": (faults.payload() if faults is not None else None),
                "processing_sec": round(processing, 1),
                "speed_vs_realtime": round(src.video_sec / processing, 2) if processing > 0 else None,
            },
            # The settings that produced these numbers, so two runs can be
            # compared knowing whether a threshold changed in between.
            "settings": {
                "k_sigma": Config.K_SIGMA,
                "calibration_frames": Config.CALIBRATION_FRAMES,
                "calibration_freeze": getattr(Config, "CALIBRATION_FREEZE_ENABLED", None),
                "pitch_enabled": Config.PITCH_ENABLED,
                "pitch_calibration_freeze": getattr(Config, "PITCH_CALIBRATION_FREEZE_ENABLED", None),
                "session_memory_decay": Config.SESSION_MEMORY_DECAY_ENABLED,
            },
            # "Flagged" means exactly what triggers evidence capture live:
            # a verdict of ALERT or FLAG. It means "worth a human's review",
            # never "cheating happened".
            "flag_definition": "highest verdict reached is one of " + ", ".join(flagged_verdicts),
            "flagged_for_review": any(s["flagged_for_review"] for s in slots.values()),
            "students": slots,
            "cross_student": [{"finding": k, "best_confidence": round(v, 3)}
                              for k, v in sorted(self.cross_best.items(), key=lambda kv: -kv[1])],
            "outputs": {
                "evidence_dir": os.path.join("evidence", session_id),
                "report": report,
            },
        }

    def write(self, path, students, session_id, report=None, faults=None) -> str:
        path = path or os.path.join("video_results", f"{session_id}.json")
        folder = os.path.dirname(path)
        if folder:
            os.makedirs(folder, exist_ok=True)
        results = self.results(students, session_id, report, faults=faults)
        inputs_path = os.path.splitext(path)[0] + ".inputs.json.gz"
        try:
            self.write_inputs(inputs_path, results)
            results["outputs"]["scoring_inputs"] = os.path.basename(inputs_path)
        except Exception as e:   # results matter more than the what-if extra
            print(f"[WARN] Scoring inputs not written: {e}")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(results, f, indent=2)
        return path

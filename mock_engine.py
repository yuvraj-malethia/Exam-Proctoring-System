"""
mock_engine.py — Fully isolated DEMO/MOCK data generator.

┌──────────────────────────────────────────────────────────────────────┐
│  ISOLATION GUARANTEE                                                  │
│  This module NEVER imports StudentPipeline, CalibrationEngine,       │
│  KinematicAnalyzer, TemporalPatternEngine, SuspicionFusion, or any    │
│  other class from module_headpose_multi_web.py. It manufactures      │
│  fake numbers that follow the SAME JSON shape serialize_full_state() │
│  produces, purely so the dashboard's visual design can be built and  │
│  tested without a camera or real students in the room.               │
│                                                                        │
│  There is no import, function call, or shared object in either       │
│  direction between this file and the real intelligence engine.       │
│  Mock data can never reach a real StudentPipeline, and a real        │
│  StudentPipeline can never be influenced by this file.               │
└──────────────────────────────────────────────────────────────────────┘

Every value here is simulated/fabricated for UI development. Nothing in
this file is evidence of anything — it exists only so a developer can
see the dashboard "light up" with 4 students at NORMAL / MILD /
MODERATE / HIGH before ever pointing a webcam at a real classroom.
"""

import time
import math
import random

import cv2
import numpy as np

# Mirrors Config values in module_headpose_multi_web.py so the demo UI
# shows realistic numbers (e.g. correct signal weights). This is a plain
# literal copy, NOT an import — keeping mock_engine.py free of any
# dependency on mediapipe/the engine, so `python dashboard_server.py`
# alone can be used for pure UI testing on a lighter machine.
MOCK_CONFIG = {
    "calibration_frames": 150,
    "min_std_deg": 3.5,
    "max_std_deg": 5.0,
    "k_sigma": 2.3,
    "temporal_window_sec": 20,
    "min_event_duration": 1.2,
    "weights": {
        "position": 0.15, "velocity": 0.10, "frequency": 0.20,
        "duration": 0.25, "session": 0.30,
    },
    "ema_alpha": 0.08,
    "levels": {"low": 18, "mild": 35, "moderate": 50, "high": 65},
    "confirmed_threshold": 75.0,
    "max_students": 4,
    # Mirrors the real Config so the demo risk chart scales its axis the
    # same way a live session would. Without it the frontend silently
    # fell back to a hardcoded default, which is exactly the kind of
    # quiet drift between demo and live that makes a demo misleading.
    "risk_timeline": {"window_sec": 300.0, "points": 90},
}

_LEVEL_BY_SCORE = [
    (65, "HIGH RISK", "high"),
    (50, "MODERATE", "moderate"),
    (35, "MILD", "mild"),
    (18, "LOW", "low"),
    (0, "NORMAL", "normal"),
]


def _level_for(score):
    for cutoff, label, key in _LEVEL_BY_SCORE:
        if score >= cutoff:
            return label, key
    return "NORMAL", "normal"


_STUDENT_COLORS_HEX = ["#ffb400", "#00c8ff", "#b400ff", "#00ff78"]

# Sep 2026: the demo now fills every panel the real engine fills —
# names, verdicts with their checks, named patterns, the event log, seat
# positions and one cross-student finding — so the dashboard can be
# designed and shown without a camera. All of it is fabricated, like
# everything else in this file, and the dashboard labels it SPECIMEN.
_DEMO_NAMES = ["Aarav", "Priya", "Meera", "Kabir"]
# Face-centre x in the (mirrored) 720-px demo frame, left to right in the
# same order the placeholder frame draws the boxes.
_DEMO_CX = [110.0, 280.0, 450.0, 620.0]
# Literal copies of verdict.py's four checks and Config values, NOT
# imports (see MOCK_CONFIG for why this file never imports the engine).
_VERDICT_LABELS = ["OK", "WATCH", "ALERT", "FLAG", "FLAG"]
_DEMO_PATTERNS = [
    ("restless", "Restless but undirected", "exonerating",
     "{n} short looks spread across different directions — consistent with "
     "ordinary restlessness rather than referencing anything."),
    ("directional_lock", "Consistent-direction pattern", "suspicious",
     "{n} recent looks mostly went the same way — a consistent bearing "
     "suggests a fixed reference point rather than random movement."),
    ("burst", "Answer-checking pattern", "suspicious",
     "{n} brief looks away in quick succession — the short, repeated rhythm "
     "typical of checking an answer rather than reading."),
    ("downward", "Below-desk-line pattern", "suspicious",
     "{d} downward looks beyond this student's own normal reading angle — "
     "consistent with material held below the desk, such as a phone."),
]
# One demo "between students" finding: for part of every cycle Meera (S3)
# and Kabir (S4) glance towards each other, and the finding is shown.
_CORR_CYCLE_SEC = 70.0
_CORR_GLANCE_FROM = 36.0
_CORR_SHOW_FROM = 46.0

_PROFILES = [
    {"label": "S1", "target": 8,  "amp": 4, "event_every": 40, "event_dur": 1.3, "hint": "target: NORMAL"},
    {"label": "S2", "target": 40, "amp": 7, "event_every": 14, "event_dur": 2.0, "hint": "target: MILD"},
    {"label": "S3", "target": 58, "amp": 9, "event_every": 9,  "event_dur": 2.6, "hint": "target: MODERATE"},
    {"label": "S4", "target": 80, "amp": 8, "event_every": 5,  "event_dur": 3.4, "hint": "target: HIGH RISK"},
]


class _MockStudent:
    def __init__(self, sid, profile):
        self.sid = sid
        self.label = profile["label"]
        self.color = _STUDENT_COLORS_HEX[sid]
        self.profile = profile
        self.start = time.time()
        self.events = []       # list of dicts: time/duration/direction/z_score
        self.graph = []        # list of {"t":..., "yaw":...}
        self.score_hist = []   # suspicion score samples for the chart
        self._last_score_t = 0.0
        self.next_event_at = time.time() + random.uniform(3, profile["event_every"])
        self.away_start = None
        self.away_dur = 0.0
        self.away_dir = 1
        self.total_away = 0.0
        # Pitch (added Sep 2026 for the 3D exam-room view). The real
        # payload has carried a "pitch" block since Phase 3 but this demo
        # never did, so a demo avatar could only ever shake its head,
        # never nod. A PRIVATE random stream decides which glances are
        # downward, so the original yaw demo sequence is left untouched.
        # The non-zero baseline is deliberate: it proves the dashboard
        # draws pitch relative to the student's own baseline, not zero.
        self._pitch_rng = random.Random(1000 + sid)
        self.away_down = False
        self.pitch_baseline = -3.0 + sid * 1.5
        self.name = _DEMO_NAMES[sid]
        self.cx = _DEMO_CX[sid]
        self.event_ends = []   # when each look away ended (verdict check 1)
        self.force_dir = None  # set by MockEngine for the demo mutual glance

    def _pitch_payload(self, elapsed, calibrated, currently_away):
        """Demo "pitch" block, same shape as serialize_full_state() sends.
        Mirrors the real rule in StudentPipeline.process(): looking down
        must clear BOTH PITCH_K_SIGMA * std and the PITCH_MIN_DOWN_DEG
        floor. The constants are literal copies of Config, not imports —
        see MOCK_CONFIG for why this file never imports the engine."""
        down_sign, k_sigma, min_down_deg, std = -1.0, 2.6, 12.0, 4.0
        down_threshold = max(k_sigma * std, min_down_deg)
        looking_down = bool(calibrated and currently_away and self.away_down)
        if looking_down:
            pitch = self.pitch_baseline + down_sign * (down_threshold + 3.0)
        else:
            pitch = self.pitch_baseline + math.sin(elapsed * 0.45 + self.sid * 1.7) * 1.6
        return {
            "enabled": True,
            "calibrated": calibrated,
            "pitch": round(pitch, 2),
            # Like the real CalibrationEngine, baseline and z stay 0.0
            # until calibration completes.
            "baseline": round(self.pitch_baseline, 2) if calibrated else 0.0,
            "z_score": round((pitch - self.pitch_baseline) / std, 3) if calibrated else 0.0,
            "looking_down": looking_down,
            "running_mean": round(self.pitch_baseline, 2),
            # Mirrors CalibrationEngine.initial_baseline (None until calibrated).
            "initial_baseline": round(self.pitch_baseline, 2) if calibrated else None,
            "std": std,
            "down_sign": down_sign,
            "down_threshold_deg": down_threshold,
        }

    def tick(self, t):
        elapsed = t - self.start
        prof = self.profile

        # Calibration ramps to "complete" over ~4 simulated seconds.
        cal_progress = min(elapsed / 4.0, 1.0)
        calibrated = cal_progress >= 1.0
        samples = int(cal_progress * MOCK_CONFIG["calibration_frames"])

        baseline = 0.0
        std = 4.0
        threshold = MOCK_CONFIG["k_sigma"] * std

        # ── Simple away-event state machine (demo only) ────────────
        currently_away = False
        if self.away_start is not None:
            if t < self.away_start + self.away_dur:
                currently_away = True
            else:
                self.events.append({
                    "time": time.strftime("%H:%M:%S", time.localtime(t)),
                    "duration": round(self.away_dur, 2),
                    "direction": ("Down" if self.away_down else
                                  ("Right" if self.away_dir > 0 else "Left")),
                    "z_score": round(self.away_dir * (threshold / std + 0.4), 2),
                })
                self.total_away += self.away_dur
                self.event_ends.append(t)
                self.away_start = None
        elif calibrated and t >= self.next_event_at:
            self.away_start = t
            self.away_dir = random.choice([-1, 1])
            if self.force_dir:
                self.away_dir = self.force_dir
            self.away_dur = prof["event_dur"] * random.uniform(0.7, 1.3)
            self.next_event_at = t + self.away_dur + random.uniform(
                prof["event_every"] * 0.6, prof["event_every"] * 1.4
            )
            # About one glance in three is downward (phone-in-lap
            # posture). Drawn from the private pitch stream — see __init__.
            self.away_down = self._pitch_rng.random() < 0.33
            currently_away = True

        wobble = math.sin(elapsed * 0.7 + self.sid) * (prof["amp"] * 0.25)
        if currently_away and not self.away_down:
            yaw = baseline + self.away_dir * (threshold + prof["amp"] * 0.6)
        else:
            yaw = baseline + wobble

        z = (yaw - baseline) / std
        self.graph.append({"t": round(t, 2), "yaw": round(yaw, 2)})
        if len(self.graph) > 600:
            self.graph = self.graph[-600:]

        # Suspicion score gently animates toward this student's target
        # with cosmetic noise — a demo value, not a real fusion calculation.
        noise = math.sin(elapsed * 0.33 + self.sid * 2) * (prof["amp"] * 0.4)
        score = max(0.0, min(100.0, prof["target"] + noise)) if calibrated else 0.0
        level, level_key = _level_for(score)

        # Sample the score every ~2s, matching the real engine's
        # REPORT_SCORE_SAMPLE_SEC, so the demo chart scrolls at the
        # same rate a real session would.
        if t - self._last_score_t >= 2.0:
            self._last_score_t = t
            self.score_hist.append(score)
            if len(self.score_hist) > 90:
                self.score_hist = self.score_hist[-90:]

        session_len = max(elapsed, 1.0)
        away_ratio_raw = self.total_away / session_len
        recent_events = self.events[-4:]
        recent_n = sum(1 for e in self.event_ends
                       if t - e <= MOCK_CONFIG["temporal_window_sec"])
        confirmed = round(max(elapsed - 20, 0) * 0.1, 1) if score > 75 else 0.0
        peak = round(max(score, prof["target"]), 1)

        # Verdict: verdict.py's four checks, applied to these fake numbers.
        if calibrated:
            checks = [
                {"key": "repeated", "label": "Repeated looks away",
                 "met": recent_n >= 3, "value": recent_n, "limit": 3,
                 "window_sec": MOCK_CONFIG["temporal_window_sec"]},
                {"key": "away_ratio", "label": "Share of the exam spent looking away",
                 "met": away_ratio_raw > 0.15, "value": round(away_ratio_raw, 4), "limit": 0.15},
                {"key": "confirmed", "label": "Time spent with a high score",
                 "met": confirmed > 5, "value": confirmed, "limit": 5},
                {"key": "peak", "label": "Score has reached HIGH RISK",
                 "met": peak >= MOCK_CONFIG["levels"]["high"], "value": peak,
                 "limit": MOCK_CONFIG["levels"]["high"]},
            ]
            ev = sum(1 for c in checks if c["met"])
            verdict = {"label": _VERDICT_LABELS[min(ev, 4)], "evidence": ev, "checks": checks}
        else:
            verdict = {"label": "CALIBRATING", "evidence": 0, "checks": []}

        # Named patterns, same shape as behavior_patterns.py produces.
        patterns = []
        if calibrated and self.events:
            n = len(self.events)
            downs = sum(1 for e in self.events if e["direction"] == "Down")
            key, label, severity, text = _DEMO_PATTERNS[self.sid]
            if n >= (3 if severity == "exonerating" else 2) and (key != "downward" or downs >= 2):
                patterns.append({
                    "key": key, "label": label, "severity": severity,
                    "confidence": round(min(n / 6.0, 1.0) * 0.9, 3),
                    "description": text.format(n=min(n, 8), d=downs),
                    "evidence": {"events": n, "down_events": downs},
                })
            longest = max(e["duration"] for e in self.events)
            if longest >= 3.5:
                patterns.append({
                    "key": "sustained", "label": "Prolonged reference pattern",
                    "severity": "suspicious",
                    "confidence": round(min(longest / 7.5, 1.0), 3),
                    "description": (f"A single continuous look away lasting {longest:.1f} "
                                    f"seconds — long enough to read from something "
                                    f"rather than simply glance."),
                    "evidence": {"longest_event_sec": longest},
                })
            patterns.sort(key=lambda p: -p["confidence"])
        pos_pct = min(abs(z) / (MOCK_CONFIG["k_sigma"] * 2.5), 1.0) * 100 if calibrated else 0
        vel_pct = min(abs(wobble) * 6 / 120.0, 1.0) * 100
        freq_pct = min(max(len(recent_events) - 1, 0) / 4.0, 1.0) * 100
        dur_pct = min(math.sqrt(max(away_ratio_raw, 0) / 0.40), 1.0) * 100
        sess_pct = min(score / 100.0, 1.0) * 100

        return {
            "sid": self.sid,
            "label": self.label,
            "display_name": self.name,
            "color": self.color,
            "visible": True,
            "verdict": verdict,
            "patterns": patterns,
            "headline_pattern": patterns[0] if patterns else None,
            "latest_evidence": None,
            "calibration": {
                "complete": calibrated,
                "samples": samples,
                "required": MOCK_CONFIG["calibration_frames"],
                "progress": cal_progress,
                "running_mean": round(baseline, 2),
                "baseline": round(baseline, 2),
                # Mirrors CalibrationEngine.initial_baseline (None until calibrated).
                "initial_baseline": round(baseline, 2) if calibrated else None,
                "std": std,
                "k_sigma": MOCK_CONFIG["k_sigma"],
                "threshold": round(threshold, 2),
                "lower_threshold": round(baseline - threshold, 2),
                "upper_threshold": round(baseline + threshold, 2),
            },
            "head_pose": {
                "raw_yaw": round(yaw + random.uniform(-0.3, 0.3), 2),
                "filtered_yaw": round(yaw, 2),
                "z_score": round(z, 3) if calibrated else None,
                "direction": (
                    ("Right" if z > 0 else "Left") if abs(z) > MOCK_CONFIG["k_sigma"] else "Forward"
                ),
                "state": "AWAY" if currently_away else ("FORWARD" if calibrated else "CALIBRATING"),
                "is_profile": False,
                "face_ratio": 0.82,
            },
            "kinematics": {
                "velocity": round(abs(wobble) * 6, 1),
                "acceleration": round(abs(wobble) * 2, 1),
                "max_velocity": round(prof["amp"] * 9, 1),
                "velocity_norm": round(vel_pct / 100.0, 3),
                "accel_norm": round(min(abs(wobble) * 2 / 400.0, 1.0), 3),
            },
            "temporal": ({
                "current_state": "AWAY" if currently_away else "FORWARD",
                "currently_away": currently_away,
                "current_away_duration": (t - self.away_start) if currently_away else 0.0,
                "events_window": recent_n,
                "window_sec": MOCK_CONFIG["temporal_window_sec"],
                "total_events": len(self.events),
                "total_away_time": round(self.total_away, 1),
                "away_ratio_raw": round(away_ratio_raw, 3),
                "away_ratio_score": round(dur_pct / 100.0, 3),
                "frequency_score": round(freq_pct / 100.0, 3),
                "duration_score": round(min(prof["event_dur"] / 6.0, 1.0), 3),
                "session_score": round(sess_pct / 100.0, 3),
                # Mirrors the real serialize_temporal() fields added
                # alongside session-memory decay, so the demo dashboard
                # renders exactly the same as live mode. Fabricated
                # values, same as everything else in this file.
                "session_lifetime_raw": round(len(self.events) * 0.6, 3),
                "session_age_profile": {
                    "last_5min": round(len(recent_events) * 0.6, 3),
                    "5_to_15min": 0.0,
                    "older": round(max(len(self.events) - len(recent_events), 0) * 0.6, 3),
                },
                "session_decay_enabled": True,
                "session_halflife_sec": 600.0,
                "last_event": self.events[-1] if self.events else None,
                "longest_event": (
                    max(self.events, key=lambda e: e["duration"]) if self.events else None
                ),
                "recent_events": list(reversed(self.events[-20:])),
            } if calibrated else None),
            "signals": ({
                "position": round(pos_pct, 1),
                "velocity": round(vel_pct, 1),
                "frequency": round(freq_pct, 1),
                "duration": round(dur_pct, 1),
                "session": round(sess_pct, 1),
            } if calibrated else None),
            "suspicion": ({
                "score": round(score, 1),
                "peak": peak,
                "confirmed_secs": confirmed,
                "level": level,
                "level_key": level_key,
                "components": {
                    "position": round(pos_pct, 1),
                    "velocity": round(vel_pct, 1),
                    "frequency": round(freq_pct, 1),
                    "duration": round(dur_pct, 1),
                    "session": round(sess_pct, 1),
                },
            } if calibrated else None),
            # Score history for the live risk timeline, in the same
            # shape serialize_student() produces. Without this the demo
            # dashboard would show an empty chart while every other
            # panel was populated.
            "score_history": [
                {"ago": round((len(self.score_hist) - 1 - k) * 2.0, 1),
                 "score": round(v, 1)}
                for k, v in enumerate(self.score_hist)
            ],
            "graph": list(self.graph[-300:]),
            # Same shape as the real payload's "pitch" block (see
            # _pitch_payload) — without it a demo avatar could never nod.
            "pitch": self._pitch_payload(elapsed, calibrated, currently_away),
        }


class MockEngine:
    """Self-contained demo generator — see module docstring for the
    isolation guarantee. Call .tick() to get (state_dict, jpeg_bytes)."""

    def __init__(self):
        self.students = [_MockStudent(i, _PROFILES[i]) for i in range(4)]
        self.session_start = time.time()
        self._frame_cache = None
        self._frame_cache_t = 0.0
        # Same shape as event_logger.EventLogger entries.
        self.events = []
        self._prev_verdict = {}
        self._prev_cal = {}
        self._log("SYSTEM", "Exam started", self.session_start)

    def _log(self, who, text, t, score=0.0):
        self.events.append({"student": who, "time": time.strftime("%H:%M:%S", time.localtime(t)),
                            "event": text, "score": round(score, 1), "duration": 0.0})
        self.events = self.events[-200:]

    def _correlations(self, t):
        """The demo's one "between students" finding (fabricated): shown
        for the last part of each cycle, after Meera and Kabir have been
        made to glance towards each other."""
        c = (t - self.session_start) % _CORR_CYCLE_SEC
        a, b = self.students[2], self.students[3]
        glancing = c >= _CORR_GLANCE_FROM
        # Meera sits left of Kabir (smaller cx), so towards each other is
        # Meera looking Right and Kabir looking Left, as in correlation.py.
        a.force_dir, b.force_dir = (1, -1) if glancing else (None, None)
        if c < _CORR_SHOW_FROM:
            return []
        return [{
            "key": "mutual_glance", "label": "Possible mutual glancing",
            "pair": [a.name, b.name], "slots": [2, 3],
            "confidence": 0.62, "severity": "suspicious",
            "description": (f"{a.name} and {b.name} looked towards each other 3 times "
                            f"within 1.2s of one another on average. {a.name} sits to the "
                            f"left of {b.name}, so these were {a.name} looking right and "
                            f"{b.name} looking left — directed at each other rather than "
                            f"away at random."),
            "evidence": {"coincidences": 3, "mean_time_gap_sec": 1.2},
            "caveat": ("Correlation is not proof — students may be reacting to the "
                       "same distraction. Requires human review."),
        }]

    def tick(self):
        t = time.time()
        correlations = self._correlations(t)
        payloads = {}
        calibrated = 0
        attention = 0
        highest = None
        for s in self.students:
            p = s.tick(t)
            payloads[p["label"]] = p
            if p["calibration"]["complete"] and not self._prev_cal.get(s.sid):
                self._prev_cal[s.sid] = True
                self._log(s.label, "Calibration complete", t)
            v = p["verdict"]["label"]
            if v != self._prev_verdict.get(s.sid):
                self._prev_verdict[s.sid] = v
                self._log(s.label, "Verdict: " + v, t,
                          p["suspicion"]["score"] if p["suspicion"] else 0.0)
            if p["calibration"]["complete"]:
                calibrated += 1
                lvl_key = p["suspicion"]["level_key"]
                if lvl_key not in ("normal", "low"):
                    attention += 1
                score = p["suspicion"]["score"]
                if highest is None or score > highest[0]:
                    highest = (score, p["label"], p["suspicion"]["level"])

        state = {
            "timestamp": t,
            "system": {
                "mock": True,
                "engine_connected": False,
                "fps": 30.0,
                "visible_students": 4,
                "calibrated_students": calibrated,
                "attention_students": attention,
                "highest_risk": (
                    {"label": highest[1], "level": highest[2], "score": highest[0]}
                    if highest else None
                ),
                "session_time": t - self.session_start,
                "max_students": 4,
                "replay": None,
            },
            "config": MOCK_CONFIG,
            "students": payloads,
            "events": self.events[-20:],
            # Same shape as IdentityTracker.slot_status(): everyone seated,
            # left to right in seat order.
            "identity": {
                s.sid: {"state": "tracked", "seconds_since_seen": 0.0,
                        "occupant_serial": 1, "on_break": False,
                        "cx": s.cx, "break_seconds": None,
                        "occupied_since": self.session_start}
                for s in self.students
            },
            "roster_cap": None,          # demo: nothing real to cap
            "roster": {"enabled": False},
            "identity_reviews": [],
            "correlations": correlations,
        }
        return state, self._demo_frame(t)

    def _demo_frame(self, t):
        # Regenerated at most ~5x/sec. This is a labeled placeholder
        # image, never a real camera frame.
        if self._frame_cache is not None and t - self._frame_cache_t < 0.2:
            return self._frame_cache
        h, w = 480, 720
        frame = np.full((h, w, 3), (18, 16, 14), dtype=np.uint8)
        cv2.putText(frame, "DEMO DATA - NO CAMERA CONNECTED", (24, 36),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 210, 255), 2, cv2.LINE_AA)
        box_w = (w - 5 * 20) // 4
        for i, s in enumerate(self.students):
            x = 20 + i * (box_w + 20)
            y = 80
            hexc = s.color.lstrip("#")
            bgr = (int(hexc[4:6], 16), int(hexc[2:4], 16), int(hexc[0:2], 16))
            cv2.rectangle(frame, (x, y), (x + box_w, y + 260), bgr, 2)
            cv2.putText(frame, s.label + " " + s.name, (x + 8, y + 26),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.7, bgr, 2, cv2.LINE_AA)
            cv2.putText(frame, s.profile.get("hint", ""), (x + 8, y + 50),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.42, (150, 150, 150), 1, cv2.LINE_AA)
        ok, jpg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
        self._frame_cache = jpg.tobytes() if ok else None
        self._frame_cache_t = t
        return self._frame_cache

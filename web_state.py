"""
web_state.py — WEB DASHBOARD INTEGRATION (read-only serialization)

Split out of module_headpose_multi_web.py during the multi-file refactor.
Code below is moved verbatim from the original module's "WEB DASHBOARD
INTEGRATION" section: SharedSnapshot, the serialize_*() functions, and
start_dashboard_server(). Everything here only *reads* attributes the
intelligence engine already computed — see the section docstring below
for the full data-flow rule this section enforces.
"""

import threading
from datetime import datetime
from typing import Optional

from config import Config
from calibration import CalibrationEngine
from kinematics import KinematicAnalyzer
from temporal import TemporalPatternEngine
from suspicion import SuspicionState, SuspicionFusion
from student_pipeline import StudentPipeline, MAX_STUDENTS
from verdict import verdict_payload

# ═══════════════════════════════════════════════════════════════════
# WEB DASHBOARD INTEGRATION — READ ONLY
# ═══════════════════════════════════════════════════════════════════
# Everything below this line is NEW. Nothing above this line was changed
# except for the three import lines and the docstring note at the top
# of the file.
#
# RULE ENFORCED BY THIS SECTION: data flows ONE WAY.
#
#     StudentPipeline / Config  ──(read only)──>  serialize_*()  ──>  SharedSnapshot  ──>  Flask
#
# Nothing here ever calls back into a CalibrationEngine, KinematicAnalyzer,
# TemporalPatternEngine, SuspicionFusion or StudentPipeline method that
# mutates state. Every function below only *reads* attributes that the
# intelligence engine already computed on its own.
#
# The one genuinely new thing computed here is small aggregation for the
# Overview page (how many students are visible / calibrated / need
# attention, and who currently has the highest score). That is arithmetic
# over already-computed per-student scores (count/max), not a new scoring
# algorithm — it invents no thresholds and changes no student's score.

_LEVEL_KEYS = {
    "NORMAL": "normal",
    "LOW": "low",
    "MILD": "mild",
    "MODERATE": "moderate",
    "HIGH RISK": "high",
}


def _bgr_to_hex(bgr) -> str:
    """Cosmetic only: converts an existing OpenCV BGR color tuple (used for
    the on-camera student box/label) into a CSS hex string. Does not
    change, read, or influence any intelligence value."""
    b, g, r = bgr
    return f"#{r:02x}{g:02x}{b:02x}"


def _fmt_clock(ts: float) -> str:
    """Cosmetic only: formats an existing event's epoch timestamp
    (DeviationEvent.timestamp, already recorded by TemporalPatternEngine)
    as a wall-clock HH:MM:SS string for the event timeline. Does not
    change the stored timestamp."""
    return datetime.fromtimestamp(ts).strftime("%H:%M:%S")


class SharedSnapshot:
    """
    Thread-safe mailbox between the CV thread and the Flask server thread.

    The CV loop calls .update(state, jpeg_bytes) once per processed frame.
    The web server calls .get() from its own thread(s) whenever it needs
    the latest values (SSE stream, MJPEG stream). A simple lock is enough
    here — there is one writer (the CV loop) and a handful of readers
    (per-connected-browser request threads), and each read/write is just
    swapping two references, not doing any computation.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._state = None       # latest serialize_full_state() dict
        self._jpeg = None        # latest encoded camera frame (bytes) or None

    def update(self, state: dict, jpeg_bytes: Optional[bytes]):
        with self._lock:
            self._state = state
            if jpeg_bytes is not None:
                self._jpeg = jpeg_bytes

    def get(self):
        """Returns (state, jpeg_bytes). Both may be None if the CV loop
        has not produced a frame yet (e.g. browser opened before the
        webcam/engine finished starting up)."""
        with self._lock:
            return self._state, self._jpeg


def serialize_config() -> dict:
    """Static config snapshot. Read once (values never change at runtime),
    but included in every payload so the frontend never has to hardcode a
    weight or threshold — it always reads it from here, straight out of
    the real Config class."""
    return {
        "calibration_frames": Config.CALIBRATION_FRAMES,
        "min_std_deg": Config.MIN_STD_DEG,
        "max_std_deg": Config.MAX_STD_DEG,
        "k_sigma": Config.K_SIGMA,
        "temporal_window_sec": Config.TEMPORAL_WINDOW_SEC,
        "min_event_duration": Config.MIN_EVENT_DURATION,
        "weights": {
            "position": Config.W_POSITION,
            "velocity": Config.W_VELOCITY,
            "frequency": Config.W_FREQUENCY,
            "duration": Config.W_DURATION,
            "session": Config.W_SESSION,
        },
        "ema_alpha": Config.SUSPICION_EMA_ALPHA,
        "levels": {
            "low": Config.LEVEL_LOW,
            "mild": Config.LEVEL_MILD,
            "moderate": Config.LEVEL_MODERATE,
            "high": Config.LEVEL_HIGH,
        },
        "confirmed_threshold": Config.CONFIRMED_THRESHOLD,
        "max_students": MAX_STUDENTS,
        # Added Aug 2026 alongside session-memory decay and identity
        # tracking, so the frontend/report can state the actual values
        # in use rather than hardcoding them.
        # Window/point settings for the live risk timeline, so the chart
        # scales its own axis from the engine's actual configuration
        # rather than hardcoding a duplicate of it that could silently
        # drift out of step.
        "risk_timeline": {
            "window_sec": Config.RISK_TIMELINE_WINDOW_SEC,
            "points": Config.RISK_TIMELINE_POINTS,
        },
        "session_memory": {
            "decay_enabled": Config.SESSION_MEMORY_DECAY_ENABLED,
            "halflife_sec": Config.SESSION_MEMORY_HALFLIFE_SEC,
            "divisor": Config.SESSION_MEMORY_DIVISOR,
        },
        "identity": {
            "match_radius_frac": Config.IDENTITY_MATCH_RADIUS_FRAC,
            "grace_period_sec": Config.IDENTITY_GRACE_PERIOD_SEC,
            "reset_on_reassign": Config.IDENTITY_RESET_ON_REASSIGN,
        },
    }


def serialize_calibration(calib: CalibrationEngine) -> dict:
    """Reads CalibrationEngine attributes only. No new statistics —
    'threshold/lower/upper' use the exact formula already drawn in
    _draw_sec_calibration() (threshold = K_SIGMA * std)."""
    threshold = Config.K_SIGMA * calib.std
    return {
        "complete": calib.is_calibrated,
        "samples": min(calib.n, Config.CALIBRATION_FRAMES),
        "required": Config.CALIBRATION_FRAMES,
        "progress": calib.progress,
        "running_mean": calib.mean,
        "baseline": calib.baseline,
        # Baseline as it stood when calibration completed (None before).
        # Read-only; the 3D view draws "straight ahead" from this because
        # .baseline keeps drifting — see CalibrationEngine.initial_baseline.
        "initial_baseline": calib.initial_baseline,
        "std": calib.std,
        "k_sigma": Config.K_SIGMA,
        "threshold": threshold,
        "lower_threshold": calib.baseline - threshold,
        "upper_threshold": calib.baseline + threshold,
    }


def serialize_kinematics(kin: KinematicAnalyzer) -> dict:
    """Reads KinematicAnalyzer attributes only."""
    return {
        "velocity": kin.velocity,
        "acceleration": kin.acceleration,
        "max_velocity": kin.max_velocity,
        "velocity_norm": kin.velocity_norm,
        "accel_norm": kin.accel_norm,
    }


def serialize_temporal(temporal: TemporalPatternEngine, t: float) -> dict:
    """
    Reads TemporalPatternEngine attributes/methods only — recent_count(),
    total_count(), away_ratio(), frequency_score(), duration_score(),
    session_score() and the existing .events list already contain every
    field the calibration monitor / events timeline needs. No additional
    event logging was required — DeviationEvent already stores timestamp,
    duration, direction and z_score for each completed event.
    """
    currently_away = temporal.is_currently_away()
    current_away_duration = (t - temporal._away_start) if currently_away else 0.0
    session_len = max(t - temporal._session_start, 1.0)
    raw_away_ratio = temporal.total_away_s / session_len

    recent_events = temporal.events[-20:]  # bounded — never send full history
    events_payload = [
        {
            "time": _fmt_clock(e.timestamp),
            "duration": round(e.duration, 2),
            "direction": e.direction,
            "z_score": round(e.z_score, 2),
        }
        for e in reversed(recent_events)
    ]
    last_event = events_payload[0] if events_payload else None
    longest = max(temporal.events, key=lambda e: e.duration) if temporal.events else None
    longest_event = (
        {
            "time": _fmt_clock(longest.timestamp),
            "duration": round(longest.duration, 2),
            "direction": longest.direction,
        }
        if longest
        else None
    )

    return {
        "current_state": "AWAY" if currently_away else "FORWARD",
        "currently_away": currently_away,
        "current_away_duration": current_away_duration,
        "events_window": temporal.recent_count(t),
        "window_sec": Config.TEMPORAL_WINDOW_SEC,
        "total_events": temporal.total_count(),
        "total_away_time": temporal.total_away_s,
        "away_ratio_raw": raw_away_ratio,
        "away_ratio_score": temporal.away_ratio(t),
        "frequency_score": temporal.frequency_score(t),
        "duration_score": temporal.duration_score(t),
        "session_score": temporal.session_score(t),
        # ── Session-memory decay (added Aug 2026) ──────────────────
        # session_score above is now age-weighted: recent behaviour
        # counts fully, older behaviour fades (see Config
        # .SESSION_MEMORY_HALFLIFE_SEC). The two fields below expose
        # what decay is hiding, so the full picture stays visible even
        # though only the decayed value drives the verdict:
        #   session_lifetime_raw — undecayed total across the whole
        #     exam, i.e. what this score used to return.
        #   session_age_profile  — how much of it came from the last
        #     5 min / 5-15 min / older.
        # Both are read-only reporting fields, never fed back into
        # any scoring path.
        "session_lifetime_raw": temporal.total_event_contribution(),
        "session_age_profile": temporal.session_memory_age_profile(t),
        "session_decay_enabled": Config.SESSION_MEMORY_DECAY_ENABLED,
        "session_halflife_sec": Config.SESSION_MEMORY_HALFLIFE_SEC,
        "last_event": last_event,
        "longest_event": longest_event,
        "recent_events": events_payload,
    }


def serialize_suspicion(fs: SuspicionState) -> dict:
    """Reads the existing SuspicionState dataclass only."""
    level, _ = SuspicionFusion.get_level(fs.probability)
    return {
        "score": fs.probability,
        "peak": fs.max_probability,
        "confirmed_secs": fs.confirmed_secs,
        "level": level,
        "level_key": _LEVEL_KEYS.get(level, "normal"),
        "components": {
            "position": fs.pos_component,
            "velocity": fs.vel_component,
            "frequency": fs.freq_component,
            "duration": fs.dur_component,
            "session": fs.session_component,
        },
    }


def serialize_events(event_logger, n: int = 20) -> list:
    """
    Read-only: returns the last n logged events (verdict transitions +
    lifecycle markers, see event_logger.EventLogger) for the dashboard's
    event timeline. event_logger may be None (e.g. mock mode) — returns
    an empty list in that case. Never read back into any algorithm.
    """
    if event_logger is None:
        return []
    return event_logger.recent(n)


def serialize_student(student: "StudentPipeline", visible: bool, t: float,
                       yaw_log, score_log=None) -> dict:
    """
    Combines the read-only serializers above into one student record.
    yaw_log is a small (timestamp, yaw) rolling buffer maintained ONLY for
    the web graph (see main loop) — it mirrors the same UI-only pattern
    already used by DeveloperUIState.push_graph_sample() for the OpenCV
    dev panel, just kept for all 4 students instead of only the selected
    one, and never read back into any algorithm.
    """
    calib = student.calib

    if calib.is_calibrated:
        is_away_flag, direction = calib.is_away(student.yaw)
        state_str = "AWAY" if is_away_flag else "FORWARD"
    else:
        direction, state_str = "N/A", "CALIBRATING"

    graph = [{"t": round(gt, 2), "yaw": round(gy, 2)} for gt, gy in yaw_log]

    # Suspicion-score history for the live risk timeline. Downsampled
    # to a fixed number of points: the buffer holds up to an hour and a
    # half of samples, and shipping all of them eight times a second to
    # every connected browser would dwarf the rest of the payload for
    # detail no chart that size could ever show. Times are sent as
    # SECONDS AGO rather than absolute, so the browser needs no clock
    # agreement with the engine to place a point correctly.
    score_history = []
    if score_log:
        pts = list(score_log)
        cutoff = t - Config.RISK_TIMELINE_WINDOW_SEC
        pts = [p for p in pts if p[0] >= cutoff]
        # Ceiling division, not floor: with floor, any count between
        # the target and twice the target gave step=1 and shipped the
        # lot — 150 points went out against a stated cap of 90.
        step = max(1, -(-len(pts) // Config.RISK_TIMELINE_POINTS))
        score_history = [
            {"ago": round(t - pt[0], 1), "score": round(pt[1], 1)}
            for pt in pts[::step]
        ]

    return {
        "sid": student.sid,
        "label": student.label,
        "color": _bgr_to_hex(student.color),
        "visible": visible,
        "calibration": serialize_calibration(calib),
        "head_pose": {
            "raw_yaw": student.raw_yaw,
            "filtered_yaw": student.yaw,
            "z_score": student.z if calib.is_calibrated else None,
            "direction": direction,
            "state": state_str,
            "is_profile": student.is_profile,
            "face_ratio": student.face_ar,
        },
        "kinematics": serialize_kinematics(student.kinematic),
        "temporal": serialize_temporal(student.temporal, t) if calib.is_calibrated else None,
        "signals": serialize_suspicion(student.fs)["components"] if calib.is_calibrated else None,
        "suspicion": serialize_suspicion(student.fs) if calib.is_calibrated else None,
        "graph": graph,
        "score_history": score_history,
    }


def serialize_full_state(students: dict, active_ids: list, t: float,
                          session_start: float, fps: float, yaw_logs: dict,
                          event_logger=None, evidence=None,
                          identity=None, archive=None, gallery=None,
                          reviews=None, patterns=None, correlations=None,
                          roster=None, score_logs=None, replay=None) -> dict:
    """
    Top-level snapshot sent to the browser. Builds the per-student records
    above, then adds a small Overview-page aggregation (counts + who has
    the highest current score right now). That aggregation is arithmetic
    over already-computed scores — see the module docstring above.

    event_logger / evidence are optional (default None, so this stays
    backward compatible with any existing caller): when provided, their
    read-only data rides this same payload — event_logger.EventLogger's
    recent events under "events", and evidence_capture.EvidenceCapture's
    latest saved record per student under each student's
    "latest_evidence". Neither is read back into any algorithm.
    """
    active_set = set(active_ids)
    student_payloads = {}
    calibrated_count = 0
    attention_count = 0
    highest = None  # (probability, label, level)

    evidence_latest = evidence.latest() if evidence is not None else {}

    for sid, student in students.items():
        visible = sid in active_set
        payload = serialize_student(student, visible, t, yaw_logs[sid],
                                     score_log=(score_logs or {}).get(sid))
        payload["latest_evidence"] = evidence_latest.get(student.label)
        # The verdict (OK / WATCH / ALERT / FLAG) and the four checks
        # behind it — the SAME verdict that triggers evidence capture and
        # the event log (verdict.py). Added Sep 2026: the dashboard
        # previously showed only the score, so the invigilator could not
        # see the verdict that actually decides when evidence is saved,
        # nor which checks a student had tripped. Read-only.
        payload["verdict"] = verdict_payload(student, t)
        # Named behavioural patterns for this student (see
        # behavior_patterns.py). Explains WHY the score is what it is,
        # in language a teacher can act on — and can exonerate as well
        # as accuse. Read-only; never affects the score.
        _pats = (patterns or {}).get(sid) or []
        payload["patterns"] = _pats
        payload["headline_pattern"] = (_pats[0] if _pats else None)
        # Human-facing name. Falls back to the seat label when nobody is
        # registered, so a partly-registered session still reads sensibly.
        payload["display_name"] = getattr(student, "display_name", student.label)
        # Pitch / looking-down state, newly available now that the
        # discarded pitch angle is being read.
        payload["pitch"] = {
            "enabled": Config.PITCH_ENABLED,
            "calibrated": student.pitch_calib.is_calibrated,
            "pitch": round(student.pitch, 2),
            "baseline": round(student.pitch_calib.baseline, 2),
            "z_score": round(student.pitch_z, 3),
            "looking_down": student.looking_down,
            # Added Sep 2026 for the 3D exam-room view (dashboard/
            # digital_twin.js). Read-only, same pattern as
            # serialize_calibration(): the browser needs the pitch
            # threshold and the sign convention to draw them, and must
            # never re-derive either on its own.
            #   running_mean       — pre-calibration centre, so the avatar
            #                        is level while still calibrating
            #   std / down_sign    — straight from the engine
            #   down_threshold_deg — the effective degree bar the
            #     looking_down rule in StudentPipeline.process() applies:
            #     it needs BOTH z > PITCH_K_SIGMA and degrees >
            #     PITCH_MIN_DOWN_DEG, which is the same as
            #     degrees > max(PITCH_K_SIGMA * std, PITCH_MIN_DOWN_DEG).
            "running_mean": round(student.pitch_calib.mean, 2),
            "initial_baseline": (round(student.pitch_calib.initial_baseline, 2)
                                 if student.pitch_calib.initial_baseline is not None
                                 else None),
            "std": round(student.pitch_calib.std, 3),
            "down_sign": Config.PITCH_DOWN_SIGN,
            "down_threshold_deg": round(max(
                Config.PITCH_K_SIGMA * student.pitch_calib.std,
                Config.PITCH_MIN_DOWN_DEG), 2),
        }
        student_payloads[student.label] = payload

        if student.calib.is_calibrated:
            calibrated_count += 1
            if visible:
                level_key = payload["suspicion"]["level_key"]
                if level_key not in ("normal", "low"):
                    attention_count += 1
                prob = payload["suspicion"]["score"]
                if highest is None or prob > highest[0]:
                    highest = (prob, student.label, payload["suspicion"]["level"])

    return {
        "timestamp": t,
        "system": {
            "mock": False,
            "engine_connected": True,
            "fps": round(fps, 1),
            "visible_students": len(active_ids),
            "calibrated_students": calibrated_count,
            "attention_students": attention_count,
            "highest_risk": (
                {"label": highest[1], "level": highest[2], "score": highest[0]}
                if highest else None
            ),
            "session_time": t - session_start,
            "max_students": MAX_STUDENTS,
            # Set only while a recorded clip is being replayed (see
            # video_run.py): {"clip", "footage_sec", "footage_total_sec"}.
            # The dashboard shows REPLAY instead of LIVE when present, so
            # a recording can never be mistaken for a live exam.
            "replay": replay,
        },
        "config": serialize_config(),
        "students": student_payloads,
        "events": serialize_events(event_logger),
        # Per-slot identity state (see identity_tracker.py). Lets the
        # dashboard distinguish "seat is empty" from "seat is being
        # held for a student who stepped away", instead of both
        # looking identical. Optional — None in mock mode.
        "identity": (identity.slot_status(t) if identity is not None else None),
        # Roster cap + phantom-rejection counters, so the dashboard can
        # show the control and how much it is filtering out.
        # Was sent as "roster" until Sep 2026 — the same key as the
        # registration roster further down, which silently overwrote it,
        # so the "Students in room" buttons never showed the real cap.
        "roster_cap": ({
            "expected_students": identity.expected_students,
            "max_students": identity.max_students,
            "small_faces_dropped": identity.total_small_faces_dropped,
            "over_cap_dropped": identity.total_over_cap_dropped,
            "duplicates_dropped": identity.total_duplicates_dropped,
        } if identity is not None else None),
        # Previous occupants of each seat (see occupant_archive.py).
        # Lets the dashboard surface "this seat also had an earlier
        # occupant who peaked at N%" instead of only ever showing the
        # person currently sitting there.
        "occupant_history": (archive.summary_by_slot() if archive is not None else {}),
        "occupant_records": (archive.all_records() if archive is not None else []),
        # Face re-identification status (see face_signature.py) —
        # which slots are enrolled, and how matching is performing.
        "face_id": (gallery.stats() if gallery is not None else None),
        # Open identity questions the invigilator needs to answer
        # (see identity_review.py). Empty list when there are none,
        # which is the normal state.
        "identity_reviews": (reviews.open_questions() if reviews is not None else []),
        # Relationships BETWEEN students (see correlation.py) — the
        # layer no per-student score can produce. Normally empty.
        "correlations": (correlations or []),
        # Who the invigilator says is in the room (see roster.py).
        "roster": (roster.to_payload() if roster is not None
                    else {"enabled": False}),
        "identity_review_stats": (reviews.stats() if reviews is not None else None),
    }


def start_dashboard_server(snapshot: SharedSnapshot, identity=None,
                            on_break=None, reviews=None,
                            on_identity_review=None,
                            on_generate_report=None, roster=None,
                            on_roster_change=None):
    """
    Launches the Flask/SSE dashboard server in a background daemon thread
    so it can never block the camera/CV loop above. If Flask (or the
    dashboard_server module) is not installed/available, the CV engine and
    its OpenCV windows keep working exactly as before — this is wrapped so
    a dashboard failure can never take down the exam-monitoring engine.
    """
    try:
        import dashboard_server
    except Exception as e:
        print(f"[WARN] Dashboard server unavailable ({e}). "
              f"Continuing with OpenCV-only UI.")
        return

    # Where to serve (Sep 2026): Config.DASHBOARD_HOST / _PORT, never
    # beyond this computer while sign-in is switched off.
    import dashboard_auth
    host = dashboard_auth.safe_host(Config.DASHBOARD_HOST, Config.DASHBOARD_LOGIN_ENABLED)

    def _run():
        try:
            app = dashboard_server.create_app(snapshot, identity=identity,
                                               on_break=on_break,
                                               reviews=reviews,
                                               on_identity_review=on_identity_review,
                                               on_generate_report=on_generate_report,
                                               roster=roster,
                                               on_roster_change=on_roster_change)
            app.run(host=host, port=Config.DASHBOARD_PORT, threaded=True,
                     use_reloader=False, debug=False)
        except Exception as e:
            print(f"[WARN] Dashboard server stopped: {e}")

    thread = threading.Thread(target=_run, daemon=True, name="dashboard-server")
    thread.start()
    print("[INFO] Dashboard server starting...")
    dashboard_auth.print_urls(host, Config.DASHBOARD_PORT)



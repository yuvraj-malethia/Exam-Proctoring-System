"""
student_pipeline.py — STUDENT_COLORS, MAX_STUDENTS, StudentPipeline

Split out of module_headpose_multi_web.py during the multi-file refactor.
Code below is moved verbatim from the original module.
"""

from config import Config
from kalman_filter import KalmanFilter1D
from calibration import CalibrationEngine
from kinematics import KinematicAnalyzer
from temporal import TemporalPatternEngine
from suspicion import SuspicionFusion
import eye_gaze

# ═══════════════════════════════════════════════════════════════════
# STUDENT PIPELINE — Wraps all per-student state in one object
# ═══════════════════════════════════════════════════════════════════
# DESIGN: Every student gets their own independent instance of every
# engine. Nothing is shared between students. This is intentional —
# calibration baseline, temporal history, and suspicion score must
# be completely independent per person.

# Colour assigned to each student for their bounding box + labels
STUDENT_COLORS = [
    (255, 180, 0),  # S0 — amber
    (0, 200, 255),  # S1 — cyan
    (180, 0, 255),  # S2 — purple
    (0, 255, 120),  # S3 — green
]

MAX_STUDENTS = 4


class StudentPipeline:
    """
    One complete analysis pipeline for one student.
    Owns: KalmanFilter1D, CalibrationEngine, KinematicAnalyzer,
          TemporalPatternEngine, SuspicionFusion.

    Call process() once per frame with the raw yaw from MediaPipe.
    Returns a ProcessResult with everything the UI needs.
    """

    def __init__(self, student_id: int):
        self.sid = student_id
        self.label = f"S{student_id + 1}"
        # Human-facing name. Defaults to the seat label and is replaced
        # when the invigilator registers a real student (see roster.py).
        # Kept SEPARATE from .label on purpose: label is a stable key
        # used throughout the payload, the evidence folders and the
        # event log, and renaming it would break all of them. Only
        # display surfaces — cards, reports, narrative — use this.
        self.display_name = self.label
        self.color = STUDENT_COLORS[student_id]
        # ── Independent engine instances ──────────────────────────
        self.kalman = KalmanFilter1D(process_noise=0.5, measurement_noise=6.0)
        self.calib = CalibrationEngine()
        self.kinematic = KinematicAnalyzer()
        self.temporal = TemporalPatternEngine()
        self.fusion = SuspicionFusion()
        # ── State ─────────────────────────────────────────────────
        self.fs = self.fusion.state  # live SuspicionState ref
        self.yaw = 0.0  # last smoothed yaw (Kalman output)
        self.z = 0.0  # last z-score
        self.is_profile = False
        self.face_ar = 1.0
        # raw_yaw: stored ONLY for developer UI observability.
        # Set at the top of process() before Kalman filtering.
        # Does NOT feed back into any algorithm.
        self.raw_yaw = 0.0

        # ── Pitch pipeline (parallel to yaw) ───────────────────────
        # Its own filter and its own calibrated baseline, because every
        # student holds their head at a different natural reading angle.
        # Closes the "phone in the lap" blind spot — see Config.
        self.pitch_kalman = KalmanFilter1D(process_noise=0.5, measurement_noise=6.0)
        self.pitch_calib = CalibrationEngine(
            freeze_after_calibration=Config.PITCH_CALIBRATION_FREEZE_ENABLED)
        self.raw_pitch = 0.0
        self.pitch = 0.0
        self.pitch_z = 0.0
        self.looking_down = False

        # ── Eye pipeline (parallel to both) ────────────────────────
        # Where the eyes point inside their sockets, which is a
        # different thing from where the head points. Same shape as the
        # pitch pipeline above — own filter, own calibrated baseline,
        # because every student's eyes rest differently against their
        # own eye corners. Closes the "still head, moving eyes" gap.
        self.eye_kalman = KalmanFilter1D(process_noise=0.3, measurement_noise=4.0)
        self.eye_calib = CalibrationEngine(
            freeze_after_calibration=Config.CALIBRATION_FREEZE_ENABLED)
        self.raw_eye = 0.0
        self.eye = 0.0
        self.eye_z = 0.0
        # Vertical eye channel, its own filter and baseline again. It
        # exists for the one posture neither head signal can see: a level
        # head with the eyes dropped to something on the desk. Pitch sees
        # nothing (the head did not move) and the sideways eye channel
        # sees nothing (the eyes did not go sideways).
        self.eye_v_kalman = KalmanFilter1D(process_noise=0.3, measurement_noise=4.0)
        self.eye_v_calib = CalibrationEngine(
            freeze_after_calibration=Config.CALIBRATION_FREEZE_ENABLED)
        self.raw_eye_v = 0.0
        self.eye_v = 0.0
        self.eye_v_z = 0.0
        self.eye_openness = 0.0
        self.eyes_aside = False       # eyes away while the head is not
        self.eye_direction = None     # "Eyes Left" / "Eyes Right"
        self.eye_seen = False         # iris landmarks available at all
        self.room_away = None         # last room-context damping input

        # How many distinct people have occupied this slot this session.
        # Starts at 1; incremented by reset() when the seat changes hands.
        self.occupant_serial = 1
        # Archived records reunited into this live pipeline after an
        # invigilator confirmed the occupant's identity. Report-only.
        self.merged_records = []
        # When the CURRENT occupant took this seat — used to describe
        # the "unidentified" window on a merge note.
        self.occupant_started_at = None

    def reset(self, t: float) -> dict:
        """
        Wipe this slot's analysis state because a DIFFERENT person has
        taken the seat (see identity_tracker.IdentityTracker).

        Why this exists: without it, a newcomer inherits the previous
        occupant's calibration baseline, suspicion score, event history,
        and evidence trail — an innocent student wearing someone else's
        record. Resetting is the whole point of detecting the handover.

        Returns a summary of what the OUTGOING occupant had accumulated,
        so the caller can write it to the event log / black-box recorder
        before it is discarded. Nothing is silently lost: the handover
        and the departing student's final figures are both auditable.

        Uses the same construction as __init__ — fresh instances of every
        engine, so there is no chance of a stale field surviving.
        """
        summary = {
            "slot": self.sid,
            "label": self.label,
            "outgoing_occupant_serial": self.occupant_serial,
            "final_probability": round(self.fs.probability, 1),
            "peak_probability": round(self.fs.max_probability, 1),
            "confirmed_secs": round(self.fs.confirmed_secs, 1),
            "total_events": self.temporal.total_count(),
            "total_away_s": round(self.temporal.total_away_s, 1),
            "was_calibrated": self.calib.is_calibrated,
        }

        self.kalman = KalmanFilter1D(process_noise=0.5, measurement_noise=6.0)
        self.calib = CalibrationEngine()
        self.pitch_kalman = KalmanFilter1D(process_noise=0.5, measurement_noise=6.0)
        self.pitch_calib = CalibrationEngine(
            freeze_after_calibration=Config.PITCH_CALIBRATION_FREEZE_ENABLED)
        self.raw_pitch = 0.0
        self.pitch = 0.0
        self.pitch_z = 0.0
        self.looking_down = False

        # ── Eye pipeline (parallel to both) ────────────────────────
        # Where the eyes point inside their sockets, which is a
        # different thing from where the head points. Same shape as the
        # pitch pipeline above — own filter, own calibrated baseline,
        # because every student's eyes rest differently against their
        # own eye corners. Closes the "still head, moving eyes" gap.
        self.eye_kalman = KalmanFilter1D(process_noise=0.3, measurement_noise=4.0)
        self.eye_calib = CalibrationEngine(
            freeze_after_calibration=Config.CALIBRATION_FREEZE_ENABLED)
        self.raw_eye = 0.0
        self.eye = 0.0
        self.eye_z = 0.0
        # Vertical eye channel, its own filter and baseline again. It
        # exists for the one posture neither head signal can see: a level
        # head with the eyes dropped to something on the desk. Pitch sees
        # nothing (the head did not move) and the sideways eye channel
        # sees nothing (the eyes did not go sideways).
        self.eye_v_kalman = KalmanFilter1D(process_noise=0.3, measurement_noise=4.0)
        self.eye_v_calib = CalibrationEngine(
            freeze_after_calibration=Config.CALIBRATION_FREEZE_ENABLED)
        self.raw_eye_v = 0.0
        self.eye_v = 0.0
        self.eye_v_z = 0.0
        self.eye_openness = 0.0
        self.eyes_aside = False       # eyes away while the head is not
        self.eye_direction = None     # "Eyes Left" / "Eyes Right"
        self.eye_seen = False         # iris landmarks available at all
        self.room_away = None         # last room-context damping input
        self.kinematic = KinematicAnalyzer()
        self.temporal = TemporalPatternEngine()
        self.fusion = SuspicionFusion()
        self.fs = self.fusion.state
        self.yaw = 0.0
        self.z = 0.0
        self.is_profile = False
        self.face_ar = 1.0
        self.raw_yaw = 0.0
        self.occupant_serial += 1
        # A new occupant inherits nothing — including merge history.
        self.merged_records = []
        self.occupant_started_at = t

        return summary

    def merge_archived_record(self, record: dict, t: float,
                               unconfirmed_since: float = None) -> dict:
        """
        Reunite this live pipeline with the archived record of the same
        student from before they left the room.

        Called only after an invigilator has CONFIRMED that the person
        now in this seat is the student described by `record` (see
        identity_review.py). The system never does this on its own from
        an uncertain face match.

        WHAT THIS DOES (and deliberately does not do):

        Merged in — the parts that describe accumulated behaviour:
          * the archived deviation events, re-inserted with their
            ORIGINAL timestamps. Session memory decays by event age, so
            behaviour from before the student left correctly counts less
            than what they have done since returning. That falls out of
            the existing decay logic — nothing special is needed here.
          * peak suspicion (the higher of the two — a peak reached
            earlier in the exam is still a peak they reached)
          * confirmed seconds and total away time (added together)

        NOT merged — calibration:
          The student was recalibrated from scratch while unidentified,
          so the pipeline already holds a fresh, valid baseline for how
          they are sitting right now. Their old baseline is deliberately
          left behind rather than restored or averaged: a baseline
          describes a posture at a moment in time, and after a break
          they may well be sitting differently. Keeping the current one
          is both simpler and more accurate.

        The window during which they were unidentified is recorded on
        the merge note, so the report can show plainly that this part of
        their record was gathered before their identity was confirmed
        rather than presenting a seamless story that hides it.
        """
        from temporal import DeviationEvent

        restored = 0
        for e in (record.get("events") or []):
            ts = e.get("timestamp")
            if ts is None:
                continue
            self.temporal.events.append(DeviationEvent(
                timestamp=ts,
                duration=float(e.get("duration") or 0.0),
                direction=e.get("direction") or "Unknown",
                z_score=float(e.get("z_score") or 0.0),
            ))
            restored += 1

        # Keep the event list ordered by time — session_score() and the
        # report both read it chronologically, and re-inserted history
        # is older than everything already there.
        self.temporal.events.sort(key=lambda ev: ev.timestamp)

        prior_peak = float(record.get("peak_probability") or 0.0)
        self.fs.max_probability = max(self.fs.max_probability, prior_peak)
        self.fs.confirmed_secs += float(record.get("confirmed_secs") or 0.0)
        self.temporal.total_away_s += float(record.get("total_away_s") or 0.0)

        note = {
            "record_id": record.get("record_id"),
            "merged_at": t,
            "events_restored": restored,
            "prior_peak": prior_peak,
            "prior_confirmed_secs": record.get("confirmed_secs"),
            "prior_started_at_str": record.get("started_at_str"),
            "prior_ended_at_str": record.get("ended_at_str"),
            "unconfirmed_from": unconfirmed_since,
            "unconfirmed_to": t,
            "unconfirmed_duration_sec": (
                round(t - unconfirmed_since, 1) if unconfirmed_since else None
            ),
            "calibration_note": (
                "recalibrated while unidentified; original baseline not "
                "restored (see merge_archived_record)"
            ),
        }
        self.merged_records.append(note)
        return note

    def process(
        self, yaw_raw: float, is_profile: bool, face_ar: float, dt: float, t: float,
        pitch_raw: float = None, eye_raw: float = None, eye_openness: float = None,
        eye_down: float = None, room_away: float = None
    ) -> None:
        """
        Run one frame of analysis for this student.
        Updates self.fs, self.yaw, self.z in place.
        No return value — read self.fs directly afterwards.

        pitch_raw is optional (default None) so every existing caller
        and the equivalence tests keep working untouched. When supplied,
        and Config.PITCH_ENABLED is on, a downward head deviation
        produces an ordinary deviation event tagged "Down" — which the
        existing frequency / duration / session signals then pick up
        with NO change to the fusion weights. That is how the
        phone-in-the-lap blind spot closes without disturbing the tuned
        yaw algorithm.
        """
        self.is_profile = is_profile
        self.face_ar = face_ar
        # Store raw yaw for developer UI (before Kalman smoothing).
        # This line only stores a value — it changes no algorithm behaviour.
        self.raw_yaw = yaw_raw

        # 1. Kalman smooth
        self.yaw, _ = self.kalman.update(yaw_raw, dt)

        # 1b. Pitch pipeline — entirely parallel; never feeds back into
        # the yaw path above. Skipped on profile frames, where pitch from
        # foreshortened landmarks is unreliable (extract_head_pose
        # returns None for those rather than guessing at it).
        pitch_down = False
        if Config.PITCH_ENABLED and pitch_raw is not None:
            self.raw_pitch = pitch_raw
            self.pitch, _ = self.pitch_kalman.update(pitch_raw, dt)
            self.pitch_calib.feed(self.pitch)
            if self.pitch_calib.is_calibrated:
                self.pitch_z = self.pitch_calib.z_score(self.pitch)
                deg_from_base = self.pitch - self.pitch_calib.baseline
                # Must clear BOTH the statistical bar and an absolute
                # degree floor: a student who sits very still has a tiny
                # std, and a z-score alone would flag ordinary movement.
                pitch_down = (
                    (self.pitch_z * Config.PITCH_DOWN_SIGN) > Config.PITCH_K_SIGMA
                    and (deg_from_base * Config.PITCH_DOWN_SIGN)
                    > Config.PITCH_MIN_DOWN_DEG
                )
                # Same adaptive-baseline protection as yaw: the baseline
                # only drifts while the student is clearly NOT looking
                # down, so holding a downward pose cannot slowly redefine
                # "normal" and hide itself.
                self.pitch_calib.adapt(self.pitch, abs(self.pitch_z))
        self.looking_down = pitch_down

        # 1c. Eye pipeline — parallel again, and the same shape. Whether
        # it is allowed to COUNT is decided below, once we know whether
        # the head is already away: see the comment there.
        self.eye_seen = eye_raw is not None
        eye_deviating = False
        if Config.EYE_TRACKING_ENABLED and eye_raw is not None and not is_profile:
            self.raw_eye = eye_raw
            self.eye_openness = eye_openness if eye_openness is not None else 0.0
            self.eye, _ = self.eye_kalman.update(eye_raw, dt)
            self.eye_calib.feed(self.eye)
            h_from_base = v_from_base = 0.0
            if self.eye_calib.is_calibrated:
                self.eye_z = self.eye_calib.z_score(self.eye)
                h_from_base = self.eye - self.eye_calib.baseline
                # Both bars, for the same reason pitch has both: a
                # student whose eyes barely move has a tiny std, and a
                # z-score on its own would flag ordinary reading.
                eye_deviating = (
                    abs(self.eye_z) > Config.EYE_K_SIGMA
                    and abs(h_from_base) > Config.EYE_MIN_OFFSET
                )
                self.eye_calib.adapt(self.eye, abs(self.eye_z))

            # Vertical, run the same way and judged on its own floor,
            # which is smaller: an eye travels less far up and down than
            # side to side before the lid gets in the way.
            head_level = not pitch_down and (
                abs(self.pitch_z) <= Config.PITCH_K_SIGMA
                if self.pitch_calib.is_calibrated else True)
            if (Config.EYE_VERTICAL_ENABLED and eye_down is not None
                    and (head_level or not Config.EYE_REQUIRE_HEAD_LEVEL)):
                self.raw_eye_v = eye_down
                self.eye_v, _ = self.eye_v_kalman.update(eye_down, dt)
                self.eye_v_calib.feed(self.eye_v)
                if self.eye_v_calib.is_calibrated:
                    self.eye_v_z = self.eye_v_calib.z_score(self.eye_v)
                    v_from_base = self.eye_v - self.eye_v_calib.baseline
                    if (abs(self.eye_v_z) > Config.EYE_K_SIGMA
                            and abs(v_from_base) > Config.EYE_MIN_OFFSET_V):
                        eye_deviating = True
                    self.eye_v_calib.adapt(self.eye_v, abs(self.eye_v_z))

            if eye_deviating:
                self.eye_direction = eye_gaze.direction_label(
                    h_from_base, v_from_base if eye_down is not None else None)

        # 2. Calibration phase
        self.calib.feed(self.yaw)

        # 3. Monitoring (only after calibration)
        if self.calib.is_calibrated:
            self.kinematic.update(self.yaw, t)

            self.z = self.calib.z_score(self.yaw)
            away, direction = self.calib.is_away(self.yaw)
            self.calib.adapt(self.yaw, abs(self.z))

            # Looking down counts as being away, tagged so reports and
            # the pattern engine can tell it from a side glance. Yaw wins
            # when both fire, since a sideways look is more specific.
            if pitch_down and not away:
                away = True
                direction = "Down"

            # Eyes last, and only while the head is NOT already away.
            # Two reasons, both deliberate: a turned head is already
            # covered by yaw above, and a turned head also drags the
            # iris against the eye corners through plain perspective, so
            # a reading taken then is contaminated as well as redundant.
            # What is left is precisely the case the head alone cannot
            # see — still head, eyes to the side.
            self.eyes_aside = False
            if eye_deviating and not (away and Config.EYE_REQUIRE_HEAD_FORWARD):
                self.eyes_aside = True
                if not away:
                    away = True
                    direction = self.eye_direction or "Eyes"

            if away:
                self.temporal.mark_away(t, direction, abs(self.z))
            else:
                self.temporal.mark_forward(t)

            # Position score now uses whichever axis deviates more, so a
            # purely downward look is no longer scored as zero deviation.
            # With PITCH_ENABLED off this reduces exactly to the original
            # yaw-only expression.
            _pos_z = abs(self.z)
            if pitch_down:
                _pos_z = max(_pos_z, abs(self.pitch_z))
            if self.eyes_aside:
                _pos_z = max(_pos_z, abs(self.eye_z), abs(self.eye_v_z))
            pos_score = min(_pos_z / (Config.K_SIGMA * 2.5), 1.0)
            # Room context (room_context.py). A glance the whole room
            # shares is weaker evidence than the same glance made alone,
            # so the instantaneous position term is discounted by how
            # much of the rest of the room was also away. ONLY this term:
            # the event itself is still recorded and still counts toward
            # frequency, away-ratio and session memory. room_away is None
            # whenever the feature is off or there is no room to compare
            # against, and then nothing changes.
            self.room_away = room_away
            if Config.ROOM_CONTEXT_ENABLED and room_away is not None:
                pos_score *= max(0.0, 1.0 - Config.ROOM_DAMP * room_away)
            vel_score = self.kinematic.velocity_norm
            freq_score = self.temporal.frequency_score(t)
            dur_score = self.temporal.away_ratio(t)
            sess_score = self.temporal.session_score(t)

            self.fs = self.fusion.update(
                pos_score, vel_score, freq_score, dur_score, sess_score, dt
            )



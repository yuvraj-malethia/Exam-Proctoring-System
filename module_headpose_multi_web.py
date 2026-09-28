"""
╔══════════════════════════════════════════════════════════════════════════╗
║     HEAD POSE INTELLIGENCE ENGINE  — Capstone CPG 127                    ║
║     AI-Based Classroom Exam Proctoring System                            ║
║                                                                          ║
║     Module: module_headpose_multi_web.py  |  Version 5.0                 ║
║                                                                          ║
║     Architecture:                                                        ║
║       KalmanFilter1D         → Smooth noisy yaw signal                   ║
║       CalibrationEngine      → Welford online statistics                 ║
║       KinematicAnalyzer      → Rolling-window velocity & acceleration    ║
║       TemporalPatternEngine  → Event frequency, away-ratio, duration     ║
║       SuspicionFusion        → Weighted multi-signal EMA scoring         ║
╚══════════════════════════════════════════════════════════════════════════╝

Why these design choices:
- Kalman filter: Raw MediaPipe yaw is noisy frame-to-frame. A 1D Kalman
  gives us smooth position AND velocity as a free byproduct.
- Welford algorithm: Numerically stable, single-pass running mean/variance.
  No need to store all samples. Correct for online calibration.
- Adaptive threshold: threshold = k * std_dev, NOT a hardcoded number.
  A student who naturally tilts 5° has a different "normal" than one who
  sits perfectly straight.
- Adaptive baseline drift: EMA update ONLY when z-score is low (student
  is clearly looking forward). Prevents cheating from "poisoning" the
  baseline.
- Rolling-window velocity: average over 5 frames, not frame-diff.
  Frame-diff velocity is O(noise), not O(signal).
- Temporal analysis: Single events are NOT evidence. Patterns are.
  Frequency + ratio + duration in a 20s window is what matters.
- EMA smoothing on suspicion: Prevents flickering alerts that mean nothing.

────────────────────────────────────────────────────────────────────────────
WEB DASHBOARD INTEGRATION (added on top of module_headpose_multi_debug.py)
────────────────────────────────────────────────────────────────────────────
This file is a MINIMALLY modified copy of module_headpose_multi_debug.py.
Every intelligence class/function above is byte-for-byte unchanged. The only
additions are in a single "WEB DASHBOARD INTEGRATION" section near the
bottom of the file (read-only serialization + a shared snapshot + a few
extra lines inside main()). See README_DASHBOARD.md for the full
integration audit. The original module_headpose_multi_debug.py is left
untouched as a rollback copy.

────────────────────────────────────────────────────────────────────────────
MULTI-FILE REFACTOR
────────────────────────────────────────────────────────────────────────────
This file used to contain the entire engine (Config, every intelligence
class, all OpenCV drawing/UI code, the web-serialization layer, and main())
in one ~2600-line module. It has been split into focused modules that this
file imports and wires together; this file now holds only the docstring,
imports, and the multi-student main() capture loop. No algorithm,
threshold, or variable was changed as part of the split — code was moved,
not rewritten:

    config.py             Config
    kalman_filter.py       KalmanFilter1D
    calibration.py          CalibrationEngine
    kinematics.py           KinematicAnalyzer
    temporal.py             DeviationEvent, TemporalPatternEngine
    suspicion.py             SuspicionState, SuspicionFusion
    head_pose.py            landmark IDs, extract_yaw()
    cv_dashboard.py          _draw_bar(), draw_dashboard() (legacy, unused)
    student_pipeline.py      STUDENT_COLORS, MAX_STUDENTS, StudentPipeline
    developer_ui_state.py    DeveloperUIState
    dev_panel.py             dev-panel Sections A-G + render_developer_view()
    camera_overlays.py       draw_face_overlay(), draw_summary_panel(), etc.
    web_state.py             SharedSnapshot, serialize_*(), start_dashboard_server()
    module_headpose_multi_web.py   this file — imports + main() only
"""

import cv2
import mediapipe as mp
import os
import sys
import subprocess
import time
from collections import deque

import clock
from video_run import VideoSource, VideoRunStats

from config import Config
from student_pipeline import StudentPipeline, MAX_STUDENTS
from developer_ui_state import DeveloperUIState
from head_pose import extract_yaw, extract_head_pose, _LANDMARK_IDS, _BBOX_IDS
from camera_overlays import (
    get_face_bbox,
    draw_face_overlay,
    draw_selected_highlight,
    draw_summary_panel,
)
from dev_panel import render_developer_view
from web_state import SharedSnapshot, serialize_full_state, start_dashboard_server
from verdict import compute_verdict
from event_logger import EventLogger
from evidence_capture import EvidenceCapture
from frame_profiler import FrameProfiler
from identity_tracker import IdentityTracker
from occupant_archive import OccupantArchive
from face_signature import SignatureGallery
from identity_review import IdentityReviewQueue
from behavior_patterns import BehaviorPatternEngine
from correlation import CrossStudentCorrelator
from session_report import SessionReportBuilder
from roster import StudentRoster


# ═══════════════════════════════════════════════════════════════════
# MAIN LOOP — Multi-student version
# ═══════════════════════════════════════════════════════════════════


def main(video_path=None, show_window=True, dashboard=True, paced=None,
         results_path=None, write_report=None):
    """
    Live webcam by default: called with no arguments this behaves exactly
    as it always has.

    video_path    replay a recorded clip instead of the webcam (see
                  video_run.py and clock.py). Every frame goes through the
                  same loop below; only where frames and "now" come from
                  differs.
    show_window   False = no OpenCV window.
    dashboard     False = no web dashboard. video_batch.py turns both off.
    paced         video: play at the clip's own speed. Default: yes if a
                  window or dashboard is showing it, otherwise as fast as
                  the machine allows.
    results_path  video: where to write the results JSON.
    write_report  build the PDF at the end. Default: Config.REPORT_ENABLED,
                  except for a video run with no window and no dashboard.
    """
    # ── MediaPipe — now up to 4 faces ─────────────────────────────
    mp_face = mp.solutions.face_mesh
    face_mesh = mp_face.FaceMesh(
        max_num_faces=MAX_STUDENTS,
        refine_landmarks=False,
        min_detection_confidence=0.55,
        min_tracking_confidence=0.55,
    )

    video = None
    if video_path:
        if paced is None:
            paced = show_window or dashboard
        video = VideoSource(video_path, paced=paced)
        cap = video
        if not cap.isOpened():
            print(f"[ERROR] Could not open video file: {video_path}")
            return
        # From here on the engine's "now" is the timestamp of the frame
        # being analysed. Must happen BEFORE any StudentPipeline exists,
        # because each one stamps its session start on creation.
        clock.use_source(video.now)
        print(f"[INFO] VIDEO REPLAY : {video_path} ({video.fps:.1f} fps, "
              f"{video.frame_count} frames, "
              f"{'real time' if paced else 'as fast as possible'})")
    else:
        cap = cv2.VideoCapture(0)
        if not cap.isOpened():
            print("[ERROR] Could not open webcam.")
            return
    if write_report is None:
        write_report = Config.REPORT_ENABLED and (video is None or show_window or dashboard)

    print("[INFO] HEAD POSE INTELLIGENCE — MULTI-STUDENT MODE")
    print(f"[INFO] Max students : {MAX_STUDENTS}")
    print(f"[INFO] Calibration  : {Config.CALIBRATION_FRAMES} frames per student")
    print(f"[INFO] Threshold    : ±{Config.K_SIGMA}s")
    print("[INFO] Controls     : 1-4 = Developer detail | 0 = Overview | D = Toggle | ESC = Exit")
    print("[INFO] Press ESC to exit.\n")

    # ── One pipeline per student slot ─────────────────────────────
    # Pipelines are permanent — they persist even when a face disappears.
    # Face assignment is positional (leftmost face = S0), so pipelines
    # carry their history correctly as long as students don't swap seats.
    students = {i: StudentPipeline(i) for i in range(MAX_STUDENTS)}

    # ── WEB DASHBOARD INTEGRATION (new, read-only) ─────────────────
    # yaw_logs: UI-only rolling buffer per student slot for the browser's
    # live yaw graph (~20s at 30fps). Same pattern as DeveloperUIState's
    # existing yaw_history deque, just kept for all 4 students instead
    # of only the one selected in the OpenCV dev panel. Never read back
    # into any algorithm — see WEB DASHBOARD INTEGRATION section above.
    yaw_logs = {i: deque(maxlen=600) for i in range(MAX_STUDENTS)}
    session_start = clock.now()
    SNAPSHOT = SharedSnapshot()
    last_jpeg_t = 0.0  # throttles MJPEG encoding to ~15fps, see below

    # ── Developer UI state — pure view state (see DeveloperUIState). ──
    # Holds ONLY: current mode, selected student, UI-only graph history.
    # It never reads back into or alters the intelligence pipeline.
    # Overview mode is always the default startup mode (backward safety).
    ui_state = DeveloperUIState()

    # ── IDENTITY TRACKING (see identity_tracker.py) ─────────────────
    # Keeps each student bound to their own slot across frames instead
    # of re-deriving identity from left-to-right order every frame.
    identity = IdentityTracker(MAX_STUDENTS)

    # Session timestamp — shared by the evidence folder, the occupancy
    # archive, the profiler log and the watchdog log, so every
    # artefact from one run is identifiable together. Defined here
    # because the occupancy archive below already needs it.
    session_ts = time.strftime("%Y_%m_%d_%H%M%S")
    session_id = f"session_{session_ts}"
    if video is not None:
        # Replays get their own recognisable folder names, so one can
        # never be mistaken for a live exam session.
        _stem = "".join(c if c.isalnum() or c in "-_" else "_"
                        for c in os.path.splitext(os.path.basename(video_path))[0])
        session_id = f"video_{_stem}_{session_ts}"

    # ── OCCUPANT ARCHIVE (see occupant_archive.py) ──────────────────
    # A seat changing hands resets that slot so the newcomer does not
    # inherit a record. Without an archive that reset would DELETE the
    # previous occupant's history — meaning the report could only ever
    # show the last person in each seat, and walking out of the room
    # would be an effective way to erase a suspicion score. Records are
    # now filed here instead, and written to disk immediately.
    archive = OccupantArchive(os.path.join("evidence", session_id))

    # ── STUDENT ROSTER (see roster.py) ──────────────────────────────
    # Lets the invigilator say who is actually in the room, so reports
    # name people rather than seat positions, and so the system stops
    # having to GUESS an identity it was never told. Entirely optional:
    # with nothing registered, everything falls back to seat labels and
    # behaves exactly as before.
    roster = StudentRoster(MAX_STUDENTS,
                            session_dir=os.path.join("evidence", session_id))

    # ── FACE RE-IDENTIFICATION (see face_signature.py) ──────────────
    # Lets a student returning from a long absence (a bathroom break)
    # be recognised and given their OWN record back, rather than being
    # treated as a stranger. Attached to the tracker, which consults it
    # before treating any unplaceable face as a new person.
    gallery = SignatureGallery(MAX_STUDENTS)
    identity.gallery = gallery

    # ── IDENTITY REVIEW QUEUE (see identity_review.py) ──────────────
    # Face matching has three outcomes, not two. A confident match is
    # acted on automatically and a face resembling nobody is treated as
    # a new person automatically — but the uncertain middle case is a
    # question for the invigilator, who actually knows who left the
    # room, rather than something the system should settle by itself.
    # While a question is open the occupant is simply treated as a new
    # student; confirming later merges their old record back in.
    reviews = IdentityReviewQueue()

    # ── BEHAVIOURAL INTELLIGENCE (new, read-only) ───────────────────
    # patterns: turns a raw score into a NAMED, explainable pattern a
    #   teacher can act on ("answer-checking pattern") — and can also
    #   exonerate ("restless but undirected").
    # correlator: looks for relationships BETWEEN students. Cheating is
    #   usually social, and no per-student score can see two people
    #   glancing at each other. Neither engine changes any score.
    patterns_engine = BehaviorPatternEngine()
    correlator = CrossStudentCorrelator()
    student_patterns = {}   # slot -> list of detected patterns
    correlations = []       # cross-student findings
    last_intel_t = 0.0      # throttle: these analyse minutes of history,
                            # so recomputing every frame is pure waste
    # Suspicion score history for the report's per-student graph.
    # Sampled every couple of seconds rather than every frame — a
    # printed chart cannot show 30 points per second, and a 3-hour
    # session at full rate would be a third of a million points.
    score_logs = {i: deque(maxlen=Config.REPORT_SCORE_HISTORY_MAX)
                   for i in range(MAX_STUDENTS)}
    last_score_sample_t = 0.0
    current_verdicts = {}   # slot -> latest verdict, for the report
                            # so recomputing every frame is pure waste
    # Slots whose deferred identity check has already run for the
    # CURRENT occupant. Cleared for a slot when it changes hands.
    deferred_checked = set()
    # Last time a score-triggered snapshot was saved per slot, so a
    # single sustained episode yields a few useful frames rather than
    # hundreds of near-identical ones.
    last_score_evidence_t = {}

    def _on_identity_review(review_id, confirmed):
        """
        Called by the dashboard when an invigilator answers an identity
        question.

        YES → the archived record of who this student was before they
              left is merged back into their live pipeline, so the
              session ends with ONE continuous record instead of two
              orphaned halves. Their fresh calibration is kept as-is
              (see StudentPipeline.merge_archived_record for why).
        NO  → nothing changes. They stay the new student they already
              were, which is also what happens if nobody answers.

        Returns (ok, payload_or_error) for the HTTP layer.
        """
        now = clock.now()
        q, err = reviews.resolve(review_id, confirmed, now)
        if err:
            return False, err

        sid = q["slot"]
        label = students[sid].label
        if not confirmed:
            msg = (f"Identity question #{review_id} answered NO by invigilator — "
                   f"{label} remains a new student")
            print(f"[IDENTITY] {msg}")
            event_logger.add_event(label, msg)
            profiler.log_event(now, frame_no, label, msg)
            return True, {"merged": False, "review": q}

        rec_id = (q.get("archived") or {}).get("record_id")
        record = archive.get_record(rec_id) if rec_id is not None else None
        if record is None:
            msg = (f"Identity question #{review_id} confirmed but archived "
                   f"record {rec_id} could not be found — nothing merged")
            print(f"[WARN] {msg}")
            event_logger.add_event(label, msg)
            return False, msg

        note = students[sid].merge_archived_record(
            record, now, unconfirmed_since=students[sid].occupant_started_at
        )
        archive.mark_merged(rec_id, sid, now, score=q.get("score"))
        msg = (
            f"Identity question #{review_id} CONFIRMED by invigilator — "
            f"{label}'s earlier record reunited "
            f"({note['events_restored']} events restored, "
            f"prior peak {note['prior_peak']}%). Unidentified window: "
            f"{note['unconfirmed_duration_sec']}s."
        )
        print(f"[IDENTITY] {msg}")
        event_logger.add_event(label, msg)
        profiler.log_event(now, frame_no, label, msg)
        return True, {"merged": True, "review": q, "merge": note}

    def _on_roster_change(action, payload):
        """
        Called by the dashboard when the invigilator registers, removes,
        or locks in students.

        Registering also captures that student's averaged face signature
        if one is available. A signature taken at registration is the
        best sample the session will ever get - the student is sitting
        still, facing the camera, unhurried - so it is worth keeping on
        the roster where it survives a seat reset.

        Locking the roster additionally caps the tracker to the number
        of people actually registered, which is what finally makes the
        phantom-detection problem tractable: with three students in the
        room, a fourth detected "face" is definitionally not a student.

        Returns (ok, payload_or_error).
        """
        try:
            if action == "register":
                slot = payload.get("slot")
                good, err = roster.register(
                    slot, payload.get("name"), payload.get("roll_no"))
                if not good:
                    return False, err
                sig = gallery.mean_signature(slot)
                if sig:
                    roster.attach_signature(slot, sig)
                students[slot].display_name = roster.display_name(
                    slot, students[slot].label)
                msg = (f"{students[slot].label} registered as "
                        f"{roster.display_name(slot, students[slot].label)}"
                        + (f" (roll {payload.get('roll_no')})"
                           if payload.get("roll_no") else ""))
                print(f"[ROSTER] {msg}")
                event_logger.add_event(students[slot].label, msg)
                profiler.log_event(clock.now(), frame_no, students[slot].label, msg)
                return True, roster.to_payload()

            if action == "unregister":
                slot = payload.get("slot")
                good, err = roster.unregister(slot)
                if not good:
                    return False, err
                students[slot].display_name = students[slot].label
                msg = f"{students[slot].label} registration removed"
                print(f"[ROSTER] {msg}")
                event_logger.add_event(students[slot].label, msg)
                return True, roster.to_payload()

            if action == "lock":
                n_reg = roster.lock()
                if n_reg > 0:
                    identity.set_expected_students(n_reg)
                msg = (f"Roster locked for this exam: {n_reg} student(s) "
                        f"registered; tracking capped to {n_reg}")
                print(f"[ROSTER] {msg}")
                event_logger.add_event("SYSTEM", msg)
                profiler.log_event(clock.now(), frame_no, "SYSTEM", msg)
                return True, roster.to_payload()

            if action == "unlock":
                roster.unlock()
                event_logger.add_event("SYSTEM", "Roster unlocked")
                return True, roster.to_payload()

            return False, f"Unknown roster action: {action}"
        except Exception as e:
            print(f"[WARN] Roster change failed: {e}")
            return False, str(e)

    def _on_generate_report():
        """
        Called by the dashboard's "Generate report" button. Builds the
        PDF from the session so far, without ending the session.

        Runs on the Flask request thread rather than the capture loop on
        purpose: building a PDF takes far longer than one video frame,
        and monitoring must not stutter because someone pressed a button.
        Reads pipeline state only.

        Returns (ok, path_or_error).
        """
        try:
            stamp = time.strftime("%H%M%S")
            path = os.path.join("reports", f"report_{session_ts}_at_{stamp}.pdf")
            SessionReportBuilder(
                students=students,
                verdicts=current_verdicts,
                patterns_by_slot=student_patterns,
                correlations=correlations,
                event_logger=event_logger,
                archive=archive,
                evidence_dir=os.path.join("evidence", session_id),
                score_logs=score_logs,
                session_id=session_id,
                session_start=session_start,
                roster=roster,
            ).build(path, t=clock.now())
            msg = f"Interim session report generated: {path}"
            print(f"[REPORT] {msg}")
            event_logger.add_event("SYSTEM", msg)
            return True, os.path.basename(path)
        except Exception as e:
            print(f"[WARN] Report generation failed: {e}")
            return False, str(e)

    def _on_break_toggled(slot, on_break):
        """Called by the dashboard's break endpoint. Records the
        invigilator's action in the event log and the black-box
        recorder so a manual override is never invisible in the
        audit trail."""
        label = students[slot].label
        msg = (f"{label} marked ON AUTHORISED BREAK by invigilator — "
               f"slot held, monitoring paused") if on_break else (
               f"{label} returned from authorised break — monitoring resumed")
        print(f"[BREAK] {msg}")
        event_logger.add_event(label, msg)
        profiler.log_event(clock.now(), frame_no, label, msg)

    # Started here rather than earlier so the identity tracker and the
    # break callback already exist and can be handed to the server.
    if dashboard:
        start_dashboard_server(SNAPSHOT, identity=identity,
                                on_break=_on_break_toggled,
                                reviews=reviews,
                                on_identity_review=_on_identity_review,
                                on_generate_report=_on_generate_report,
                                roster=roster,
                                on_roster_change=_on_roster_change)

    # ── ANALYTICS INTEGRATION (new, read-only) ──────────────────────
    # session_ts is timestamped to the second so two runs on the same
    # day never collide, and is shared across the evidence/ folder name,
    # the profiler's log filename, and the watchdog's log filename, so
    # all three files from one run are trivially identifiable together.
    # event_logger: in-memory rolling log of verdict transitions +
    #   lifecycle markers (calibration complete, exam started/ended).
    # evidence: saves an annotated frame + JSON whenever a student's
    #   verdict crosses into ALERT/FLAG — same trigger, background thread.
    # prev_verdict / prev_calibrated: only used to detect *transitions*,
    #   so nothing gets logged/saved every frame a state merely persists.
    event_logger = EventLogger()
    evidence = EvidenceCapture(os.path.join("evidence", session_id))
    prev_verdict = {i: None for i in range(MAX_STUDENTS)}
    prev_calibrated = {i: False for i in range(MAX_STUDENTS)}
    event_logger.add_event("SYSTEM", "Exam started")

    # ── DIAGNOSTIC PROFILER (new, read-only, measures only) ──────────
    # Times every stage of the loop below and logs it to diagnostics/ —
    # see frame_profiler.py. Does not change any behavior, threshold,
    # or timing anywhere else — it only measures and records what's
    # already happening.
    profiler = FrameProfiler(session_id=session_ts)
    profiler.log_event(clock.now(), 0, "SYSTEM", "Exam started")
    frame_no = 0

    # ── EXTERNAL WATCHDOG (new, Phase 2) ─────────────────────────────
    # A completely separate OS process (see watchdog.py), not a thread
    # inside this one — so it keeps recording even if THIS process hard-
    # crashes or freezes so badly that even its own background threads
    # stop responding (already observed in real data from this project).
    # Also gives a "Task Manager" view of the WHOLE machine, since this
    # process's own memory use was shown to be a small fraction of total
    # system memory pressure — the watchdog is what can show where the
    # rest is actually going. Best-effort: if it fails to start, the
    # rest of the session continues completely normally without it.
    watchdog_proc = None
    if video is None:   # a replay is not a live session: nothing to guard
        try:
            watchdog_path = os.path.join(
                os.path.dirname(os.path.abspath(__file__)), "watchdog.py"
            )
            watchdog_proc = subprocess.Popen(
                [sys.executable, watchdog_path,
                 "--pid", str(os.getpid()),
                 "--session-id", session_ts,
                 "--log-dir", "diagnostics"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            print(f"[WATCHDOG] Started external watchdog (PID {watchdog_proc.pid})")
        except Exception as e:
            print(f"[WARN] Could not start external watchdog: {e}")

    prev_t = clock.now()
    run_stats = VideoRunStats(video) if video is not None else None

    while True:
        frame_no += 1

        _t_capture0 = time.perf_counter()
        ret, frame = cap.read()
        capture_ms = (time.perf_counter() - _t_capture0) * 1000.0
        if not ret:
            if video is not None:
                print(f"[INFO] End of video after {video.frames_read} frames "
                      f"({video.video_sec:.1f}s of footage)")
                profiler.log_event(clock.now(), frame_no, "SYSTEM",
                                    "End of video — replay ending")
            else:
                print(f"[WARN] Camera read failed at frame {frame_no} — ending session")
                profiler.log_event(clock.now(), frame_no, "SYSTEM",
                                    "Camera read failed — session ending")
            break

        # ── PER-FRAME SAFETY NET (new) ───────────────────────────
        # Everything below (preprocessing through this frame's profiler
        # log) is now wrapped in one outer try/except. The dashboard
        # serialization and analytics blocks already had their own
        # try/except (kept, unchanged) for their SPECIFIC known risks;
        # this outer one is a catch-all for anything unanticipated
        # (e.g. malformed landmark data). On any such error: log it as
        # data (frame number, exception type/message, short traceback)
        # instead of crashing the whole session, then move on to the
        # next frame. A diagnostic run should keep recording through a
        # bad frame, not go silent because of one.
        try:
            _t_pre0 = time.perf_counter()
            frame = cv2.flip(frame, 1)
            h, w, _ = frame.shape
            t = clock.now()
            dt = max(t - prev_t, 1e-3)
            prev_t = t
            # Video replay only: record this frame's scoring inputs for
            # whatif.py (see video_run.py). Read-only.
            if run_stats is not None:
                run_stats.begin_frame(t, dt, students)

            # ── Crop frame so panel fits — leave right 260px for panel ─
            panel_w = 260
            cam_w = w - panel_w  # camera feed occupies left portion

            # ── MediaPipe ─────────────────────────────────────────────
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            preprocess_ms = (time.perf_counter() - _t_pre0) * 1000.0

            _t_fm0 = time.perf_counter()
            results = face_mesh.process(rgb)

            # ── Identity assignment (see identity_tracker.py) ─────────
            # REPLACES the previous strategy of re-sorting every face
            # left→right every frame with no memory of who was who. That
            # old approach meant a student leaving their seat caused
            # everyone to their right to shift up a slot and silently
            # inherit the missing student's calibration, suspicion score
            # and evidence trail — a false-accusation bug.
            #
            # Now: faces are matched to the slot whose last known
            # position they are nearest to; a vacated slot is held for a
            # grace period so a neighbour cannot absorb it; and a slot
            # genuinely handed to a new person is reset and logged
            # instead of quietly inheriting a record.
            detected_faces = list(results.multi_face_landmarks or [])
            # Yaw/profile for each detected face, computed once here so
            # face re-identification can refuse to act on a turned head.
            # extract_yaw() is called again per-student below on the
            # normal path; this pre-pass exists only so identity can be
            # resolved BEFORE we decide which pipeline each face feeds.
            _face_pose = {}
            for _fi, _lm in enumerate(detected_faces[:MAX_STUDENTS]):
                try:
                    _y, _p, _ = extract_yaw(_lm, w, h)
                    _face_pose[_fi] = (_y, _p)
                except Exception:
                    _face_pose[_fi] = (None, False)

            assignment, reassigned_slots, held_slots, restored_slots = identity.assign(
                detected_faces, w, h, t, _BBOX_IDS,
                yaw_lookup=lambda fi: _face_pose.get(fi, (None, False))[0],
                profile_lookup=lambda fi: _face_pose.get(fi, (None, False))[1],
            )
            facemesh_ms = (time.perf_counter() - _t_fm0) * 1000.0

            # A student recognised on return keeps their own record —
            # no reset, nothing lost. Logged so the restore is visible
            # in the audit trail rather than happening invisibly.
            for info in restored_slots:
                msg = (
                    f"{students[info['slot']].label} recognised on return "
                    f"(face match {info['score']}) — record restored, not reset"
                )
                print(f"[IDENTITY] {msg}")
                event_logger.add_event(students[info["slot"]].label, msg)
                profiler.log_event(t, frame_no, students[info["slot"]].label, msg)

            # A slot handed to a genuinely different person is wiped so
            # the newcomer starts from a clean calibration and zero
            # suspicion. The outgoing occupant's final figures are
            # written to the event log and the recorder first, so the
            # handover is auditable rather than silent.
            for info in reassigned_slots:
                sid = info["slot"]
                record = None
                if info.get("first_occupant"):
                    # Brand-new seat: nothing to archive and nothing to
                    # reset. Only reached when the face resembled
                    # someone who already left, so the sole purpose here
                    # is to raise the question below.
                    students[sid].occupant_started_at = t
                elif Config.IDENTITY_RESET_ON_REASSIGN:
                    # Copy the outgoing occupant's events BEFORE reset()
                    # wipes the engine — the archive is what makes reset
                    # safe. Nothing is deleted, only filed.
                    outgoing_events = list(students[sid].temporal.events)
                    outgoing_started = info.get("previous_occupied_since") or session_start
                    summary = students[sid].reset(t)
                    record = archive.close_record(
                        slot=sid,
                        label=students[sid].label,
                        summary=summary,
                        events=outgoing_events,
                        started_at=outgoing_started,
                        ended_at=t,
                        reason=(
                            f"seat vacant {info['vacant_for_sec']}s, "
                            f"taken by a different person"
                        ),
                        # Keep the departing student's averaged face
                        # signature so the deferred check below can
                        # compare average-to-average later — far more
                        # reliable than one noisy frame vs an average.
                        face_signature=gallery.mean_signature(sid),
                    )
                    if run_stats is not None:
                        run_stats.note_reset(sid, record.get("record_id") if record else None)
                    # The newcomer must not be matched against the old
                    # occupant's face, or they would be "recognised" as
                    # them and handed the record straight back.
                    # Retire rather than delete: the departing student's
                    # signature moves into the person registry so they can
                    # be recognised wherever they sit next, not only in
                    # this same chair (see gallery.retire_slot).
                    gallery.retire_slot(sid, record.get("record_id") if record else None,
                                         t, label=students[sid].label)
                    yaw_logs[sid].clear()
                    deferred_checked.discard(sid)  # new occupant, check again
                else:
                    summary = {"slot": sid, "note": "reset disabled by config"}
                    record = None

                if not info.get("first_occupant"):
                    msg = (
                        f"Slot {students[sid].label} reassigned to a new person "
                        f"after {info['vacant_for_sec']}s vacant — pipeline reset, "
                        f"previous occupant ARCHIVED "
                        f"(peak={summary.get('peak_probability')}%, "
                        f"events={summary.get('total_events')})"
                    )
                    print(f"[IDENTITY] {msg}")
                    event_logger.add_event(students[sid].label, msg)
                    profiler.log_event(t, frame_no, students[sid].label, msg)

                # ── Uncertain identity → ask the invigilator ──────────
                # The face looked familiar but not certainly so. The
                # occupant is already proceeding as a new student (the
                # safe default, and what stands if nobody answers). All
                # that happens here is that the question gets asked.
                unc = info.get("uncertain_identity")
                if unc:
                    cand_slot = unc["candidate_slot"]
                    # Prefer the record just archived from THIS seat —
                    # a returning student almost always sits back down
                    # in the same place. Otherwise fall back to the last
                    # closed record for whichever seat the face resembles.
                    cand_record = record
                    if cand_record is None or cand_slot != sid:
                        cand_record = archive.latest_for_slot(cand_slot) or record
                    if cand_record is not None:
                        q = reviews.raise_question(
                            slot=sid,
                            label=students[sid].label,
                            candidate_slot=cand_slot,
                            candidate_label=students[cand_slot].label,
                            score=unc["score"],
                            reason=unc["reason"],
                            archived_record=cand_record,
                            t=t,
                        )
                        if q:
                            qmsg = (
                                f"IDENTITY QUESTION #{q['id']}: is the person now in "
                                f"{students[sid].label} the same student who left "
                                f"{q['candidate_label']} "
                                f"(face similarity {unc['score']})? "
                                f"Treating as a NEW student until an invigilator confirms."
                            )
                            print(f"[IDENTITY] {qmsg}")
                            event_logger.add_event(students[sid].label, qmsg)
                            profiler.log_event(t, frame_no, students[sid].label, qmsg)

            # Close out questions nobody answered. Expiry means exactly
            # the same as "no" — the occupant simply carries on as the
            # new student they already were.
            for q in reviews.expire_due(t):
                emsg = (
                    f"Identity question #{q['id']} for {q['label']} expired "
                    f"unanswered — occupant remains a new student"
                )
                event_logger.add_event(q["label"], emsg)
                profiler.log_event(t, frame_no, q["label"], emsg)

            active_ids = sorted(assignment.keys())  # slots with a face this frame

            # ── Process each detected face through its pipeline ────────
            student_processing_ms = 0.0
            drawing_ms = 0.0
            for i in active_ids:
                lm = assignment[i]
                student = students[i]

                # A student marked as on an authorised break is not
                # processed at all: feeding frames of an empty chair (or
                # of them walking past) would corrupt their calibration
                # baseline and could accrue suspicion while they are
                # legitimately away. Their record is simply frozen.
                if Config.BREAK_PAUSES_SCORING and identity.is_on_break(i):
                    continue

                _t_sp0 = time.perf_counter()
                # Extract yaw + PITCH + profile flag. Pitch was always
                # computed by solvePnP and thrown away; reading it closes
                # the phone-in-the-lap blind spot (see head_pose.py).
                yaw_raw, pitch_raw, is_profile, face_ar = extract_head_pose(lm, w, h)
                if yaw_raw is None:
                    yaw_raw = 0.0

                # Run the full pipeline for this student
                student.process(yaw_raw, is_profile, face_ar, dt, t,
                                 pitch_raw=pitch_raw)
                if run_stats is not None:
                    run_stats.note_input(i, yaw_raw, is_profile, face_ar, pitch_raw)

                # WEB DASHBOARD INTEGRATION (new): log yaw for the browser's
                # live graph. Read-only — appends student.yaw, changes nothing.
                yaw_logs[i].append((t, student.yaw))

                # Feed the face-signature gallery (see face_signature.py).
                # Enrolment is passive: it only accepts near-frontal frames
                # and simply never completes for a student who never faces
                # the camera, in which case identity falls back to position
                # tracking exactly as before. Read-only w.r.t. the pipeline.
                gallery.observe(i, lm, student.yaw, is_profile, student.label)

                # ── Deferred identity check ──────────────────────────
                # The first frame after someone sits down gives only a
                # single, noisy signature to judge by, which made the
                # system ask the invigilator far more often than it
                # needed to. Once this occupant has enrolled properly
                # (an AVERAGE over many frames, ~1 second), compare that
                # average against the averaged signature of whoever
                # previously left this seat. Average-to-average is much
                # more reliable, so a genuine returner is usually
                # recognised outright and the invigilator is never
                # bothered at all. Runs once per occupancy.
                if (i not in deferred_checked and gallery.is_enrolled(i)
                        and Config.FACE_SIG_ENABLED):
                    deferred_checked.add(i)
                    mine = gallery.mean_signature(i)
                    # Compare against EVERY student who has left a seat
                    # this session, not just whoever previously sat in
                    # this one. Comparing only against the same chair
                    # meant a student who moved seats was matched against
                    # a different person entirely.
                    outcome, entry, sc, why = gallery.identify_departed(mine)
                    prior = (archive.get_record(entry["record_id"])
                             if entry and entry.get("record_id") is not None else None)
                    if prior:
                        if outcome == "match":
                            # Confident enough to act without troubling
                            # anyone. Any question already raised about
                            # this seat is answered by this evidence.
                            for c in reviews.cancel_for_slot(
                                    i, t, "resolved automatically by deferred face check"):
                                event_logger.add_event(
                                    students[i].label,
                                    f"Identity question #{c['id']} withdrawn — "
                                    f"confident face match on fuller evidence")
                            note = student.merge_archived_record(
                                prior, t,
                                unconfirmed_since=student.occupant_started_at)
                            if run_stats is not None:
                                run_stats.note_merge(i, prior["record_id"])
                            archive.mark_merged(prior["record_id"], i, t, score=sc)
                            dmsg = (
                                f"{students[i].label} recognised on return "
                                f"({why}) — earlier record reunited automatically "
                                f"({note['events_restored']} events restored)"
                            )
                            print(f"[IDENTITY] {dmsg}")
                            event_logger.add_event(students[i].label, dmsg)
                            profiler.log_event(t, frame_no, students[i].label, dmsg)
                        else:
                            # Either genuinely unsure, or the face does
                            # not look like them at all. BOTH are asked
                            # about, because real testing showed the
                            # silent "no match" path was where wrong
                            # identities slipped through unnoticed —
                            # exactly the case an invigilator most needs
                            # to be told about. A low score is shown
                            # honestly as "probably not, please confirm".
                            q = reviews.raise_question(
                                slot=i, label=students[i].label,
                                candidate_slot=prior.get("slot", i),
                                candidate_label=prior.get("label") or "an earlier student",
                                score=sc, reason=why,
                                archived_record=prior, t=t,
                            )
                            if q:
                                qmsg = (
                                    f"IDENTITY QUESTION #{q['id']}: is {students[i].label} "
                                    f"the same student who left {q['candidate_label']} "
                                    f"(similarity {round(sc, 4)}, {outcome})? Treating as a "
                                    f"NEW student until an invigilator confirms."
                                )
                                print(f"[IDENTITY] {qmsg}")
                                event_logger.add_event(students[i].label, qmsg)
                                profiler.log_event(t, frame_no, students[i].label, qmsg)
                student_processing_ms += (time.perf_counter() - _t_sp0) * 1000.0

                _t_dr0 = time.perf_counter()
                # Draw tracking dots on the camera feed
                dot_color = (0, 80, 255) if is_profile else student.color
                for idx in _LANDMARK_IDS:
                    pt = lm.landmark[idx]
                    cx, cy = int(pt.x * w), int(pt.y * h)
                    cv2.circle(frame, (cx, cy), 2, dot_color, -1, cv2.LINE_AA)

                # Draw compact per-face overlay
                bbox = get_face_bbox(lm, w, h)
                draw_face_overlay(frame, student, bbox, t)

                # ── Developer Detail Mode: strongly highlight the selected
                # student's box on the main camera feed. Pure view overlay —
                # reads student/bbox only, writes nothing to pipeline state.
                if (
                    ui_state.mode == DeveloperUIState.MODE_DEVELOPER
                    and i == ui_state.selected_sid
                ):
                    draw_selected_highlight(frame, student, bbox, t)
                drawing_ms += (time.perf_counter() - _t_dr0) * 1000.0

            # ── WEB DASHBOARD INTEGRATION (new, read-only) ─────────────
            # Serializes existing pipeline state only — see serialize_full_state()
            # above. Wrapped in try/except so a dashboard bug can NEVER crash
            # the exam-monitoring CV loop.
            _t_ser0 = time.perf_counter()
            try:
                fps = 1.0 / dt
                web_state = serialize_full_state(
                    students, active_ids, t, session_start, fps, yaw_logs,
                    event_logger=event_logger, evidence=evidence,
                    identity=identity, archive=archive, gallery=gallery,
                    reviews=reviews,
                    patterns=student_patterns, correlations=correlations,
                    roster=roster, score_logs=score_logs,
                    replay=({"clip": os.path.basename(video.path),
                             "footage_sec": round(video.video_sec, 1),
                             "footage_total_sec": round(video.frame_count / video.fps, 1)}
                            if video is not None else None),
                )
                SNAPSHOT.update(web_state, None)
            except Exception as e:
                print(f"[WARN] Dashboard state serialization skipped: {e}")
            finally:
                serialization_ms = (time.perf_counter() - _t_ser0) * 1000.0

            # ── Draw summary panel on the right ───────────────────────
            _t_sum0 = time.perf_counter()
            draw_summary_panel(frame, students, active_ids, w, h, t)
            drawing_ms += (time.perf_counter() - _t_sum0) * 1000.0

            # ── ANALYTICS INTEGRATION (new, read-only) ──────────────────
            # Detects verdict transitions using compute_verdict() — the SAME
            # 4-part check already drawn in draw_summary_panel() above, so
            # every logged/captured event matches exactly what the
            # invigilator can see on screen right now. Only fires on a
            # *change* of verdict (or calibration completing), never every
            # frame a state merely persists. Wrapped in try/except so an
            # analytics bug can never crash the CV loop, same principle as
            # the dashboard serialization block below.
            _t_an0 = time.perf_counter()
            current_verdict = {}
            try:
                for i in range(MAX_STUDENTS):
                    student = students[i]

                    if student.calib.is_calibrated and not prev_calibrated[i]:
                        event_logger.add_event(student.label, "Calibration complete")
                        profiler.log_event(t, frame_no, student.label, "Calibration complete")
                        prev_calibrated[i] = True

                    verdict, ev_count = compute_verdict(student, t)
                    current_verdict[i] = verdict
                    current_verdicts[i] = verdict  # kept for the report
                    if verdict != prev_verdict[i]:
                        _ev_score = student.fs.probability if student.calib.is_calibrated else 0.0
                        _ev_dur = student.fs.confirmed_secs if student.calib.is_calibrated else 0.0
                        event_logger.add_event(
                            student.label, f"Verdict: {verdict}",
                            score=_ev_score, duration=_ev_dur,
                        )
                        profiler.log_event(t, frame_no, student.label,
                                            f"Verdict: {verdict}", score=_ev_score, duration=_ev_dur)
                        if verdict in Config.EVIDENCE_CAPTURE_VERDICTS:
                            # frame already has bounding boxes + summary panel
                            # drawn on it at this point — same "fully-annotated
                            # frame" the MJPEG stream reuses further below.
                            _reason = f"Verdict escalated to {verdict} (evidence {ev_count}/4)"
                            evidence.save_event(
                                frame,
                                student.label,
                                suspicion=student.fs.probability,
                                yaw=student.yaw,
                                duration=student.fs.confirmed_secs,
                                reason=_reason,
                            )
                            _emsg = f"EVIDENCE SAVED for {student.label}: {_reason}"
                            print(f"[EVIDENCE] {_emsg}")
                            event_logger.add_event(student.label, _emsg)
                            profiler.log_event(t, frame_no, student.label, _emsg)

                    # Remember this verdict so the block above fires only on
                    # a CHANGE. This line had slipped inside the
                    # score-triggered block below when that block was added
                    # (21 Aug 2026), so every student's verdict was re-logged
                    # on every frame — 27,683 identical "Verdict: ..." events
                    # per seat in one 22-minute real log, pushing real
                    # milestones out of the 500-entry event log within
                    # seconds — and while a student stayed at ALERT, evidence
                    # was saved on every frame (138 images in 8 seconds in
                    # session_2026_08_21_154352). Restored to where the
                    # original version (archive/2) had it.
                    prev_verdict[i] = verdict

                    # ── Score-triggered evidence ─────────────────────
                    # Verdict escalation alone proved far too rare in real
                    # testing: someone could visibly look away repeatedly
                    # and never trip it, because the verdict needs several
                    # slow-moving conditions at once. Capture also on the
                    # raw suspicion score crossing a threshold, which is
                    # what actually moves while a student is looking away,
                    # with a cooldown so one sustained episode produces a
                    # few useful snapshots rather than hundreds.
                    if (student.calib.is_calibrated
                            and student.fs.probability >= Config.EVIDENCE_SCORE_THRESHOLD
                            and (t - last_score_evidence_t.get(i, 0.0))
                            >= Config.EVIDENCE_SCORE_COOLDOWN_SEC):
                        last_score_evidence_t[i] = t
                        _sreason = (f"Suspicion score reached "
                                     f"{student.fs.probability:.0f}%")
                        evidence.save_event(
                            frame, student.label,
                            suspicion=student.fs.probability,
                            yaw=student.yaw,
                            duration=student.fs.confirmed_secs,
                            reason=_sreason,
                        )
                        _smsg = f"EVIDENCE SAVED for {student.label}: {_sreason}"
                        print(f"[EVIDENCE] {_smsg}")
                        event_logger.add_event(student.label, _smsg)
                        profiler.log_event(t, frame_no, student.label, _smsg)
            except Exception as e:
                print(f"[WARN] Analytics logging skipped: {e}")
            analytics_ms = (time.perf_counter() - _t_an0) * 1000.0

            # ── BEHAVIOURAL INTELLIGENCE PASS (new, read-only) ─────
            # Recognises named patterns per student, and relationships
            # BETWEEN students. Both read completed event history
            # spanning minutes, so recomputing them every frame would
            # be pure waste — throttled to twice a second, far quicker
            # than the behaviour they describe can change. Wrapped so
            # an analysis bug can never disturb the monitoring loop.
            _t_intel0 = time.perf_counter()
            if (t - last_intel_t) >= Config.INTELLIGENCE_INTERVAL_SEC:
                last_intel_t = t
                try:
                    for _sid in range(MAX_STUDENTS):
                        student_patterns[_sid] = patterns_engine.analyze(
                            students[_sid], t)
                    if Config.CORRELATION_ENABLED:
                        # Real seat positions decide who sits left of whom;
                        # slot numbers only follow seating for students who
                        # appeared in the same frame (see correlation.py).
                        correlations = correlator.analyze(
                            students, active_ids, t,
                            seat_x={_sid: identity.slots[_sid].cx for _sid in active_ids})
                except Exception as e:
                    print(f"[WARN] Behavioural intelligence pass skipped: {e}")
            intelligence_ms = (time.perf_counter() - _t_intel0) * 1000.0

            # Sample the suspicion score for the report's history graph.
            # Throttled hard — a printed chart cannot show 30 points a
            # second, and sampling every frame would burn memory for
            # detail no reader could ever see.
            if (t - last_score_sample_t) >= Config.REPORT_SCORE_SAMPLE_SEC:
                last_score_sample_t = t
                for _sid in range(MAX_STUDENTS):
                    if students[_sid].calib.is_calibrated:
                        score_logs[_sid].append(
                            (t, students[_sid].fs.probability))

            # Video replay summary (see video_run.py). Read-only.
            if run_stats is not None:
                run_stats.observe(dt, active_ids, students, current_verdict,
                                  student_patterns, correlations)

            # ── Header bar ────────────────────────────────────────────
            _t_hdr0 = time.perf_counter()
            ov = frame.copy()
            cv2.rectangle(ov, (0, 0), (w, 46), (10, 12, 18), -1)
            cv2.addWeighted(ov, 0.85, frame, 0.15, 0, frame)
            cv2.putText(
                frame,
                "HEAD POSE INTELLIGENCE  |  CPG-127  |  MULTI-STUDENT",
                (10, 30),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.58,
                (200, 200, 200),
                1,
                cv2.LINE_AA,
            )
            # Same counter as before; a "+N held" suffix appears only
            # while a slot is being reserved for a student who has
            # momentarily disappeared, so the operator can see that the
            # seat is deliberately vacant rather than lost.
            _face_txt = f"Faces: {len(active_ids)}/{MAX_STUDENTS}"
            if held_slots:
                _face_txt += f"  (+{len(held_slots)} held)"
            cv2.putText(
                frame,
                _face_txt,
                (w - 370, 30),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.50,
                (100, 200, 100) if active_ids else (80, 80, 80),
                1,
                cv2.LINE_AA,
            )

            # ── Mode / keyboard-control hint (developer observability) ──
            # Purely informational text — does not affect any pipeline state.
            if ui_state.mode == DeveloperUIState.MODE_DEVELOPER:
                mode_txt = f"DEV MODE: {students[ui_state.selected_sid].label}"
                mode_col = (0, 210, 255)
            else:
                mode_txt = "OVERVIEW MODE"
                mode_col = (120, 200, 120)
            cv2.putText(
                frame,
                f"[{mode_txt}]   Keys: 1-4 detail | 0/D overview | ESC exit",
                (10, 42),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.38,
                mode_col,
                1,
                cv2.LINE_AA,
            )

            drawing_ms += (time.perf_counter() - _t_hdr0) * 1000.0

            # ── Developer Detail Mode — separate large diagnostic window ──
            # render_developer_view() and push_graph_sample() ONLY READ
            # pipeline state (StudentPipeline / its engines). They never
            # write to it — see DeveloperUIState docstring.
            _t_dev0 = time.perf_counter()
            if ui_state.mode == DeveloperUIState.MODE_DEVELOPER:
                # Only feed the UI-only graph buffer with a LIVE sample —
                # i.e. only when the selected student was actually processed
                # this frame. Prevents a stale/frozen yaw from being plotted
                # as if it were live data (see "RAW VS SMOOTHED / stale vs
                # live" requirement).
                if ui_state.selected_sid in active_ids:
                    ui_state.push_graph_sample(students[ui_state.selected_sid])
                render_developer_view(students, active_ids, ui_state, t)
            dev_panel_ms = (time.perf_counter() - _t_dev0) * 1000.0

            # ── WEB DASHBOARD INTEGRATION (new, read-only) ─────────────
            # Reuses the SAME fully-drawn frame the OpenCV window is about to
            # show (bounding boxes, labels, panel, header already burned in).
            # Throttled to ~15fps to limit JPEG-encode overhead; the CV loop
            # itself keeps running at full camera framerate regardless.
            jpeg_ms = 0.0
            if t - last_jpeg_t >= (1.0 / 15.0):
                _t_jpg0 = time.perf_counter()
                ok, jpg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
                if ok:
                    SNAPSHOT.update(SNAPSHOT.get()[0], jpg.tobytes())
                    last_jpeg_t = t
                jpeg_ms = (time.perf_counter() - _t_jpg0) * 1000.0

            _t_disp0 = time.perf_counter()
            if show_window:
                cv2.imshow("Head Pose Intelligence - CPG 127  [MULTI]", frame)
            display_ms = (time.perf_counter() - _t_disp0) * 1000.0

            # ── DIAGNOSTIC PROFILER (new, read-only) ────────────────────
            # Logs this frame's full stage breakdown + per-student algorithm
            # state, read-only, before keyboard handling. See frame_profiler.py.
            try:
                _student_snapshot = {}
                for i in range(MAX_STUDENTS):
                    s = students[i]
                    _student_snapshot[s.label] = {
                        "visible": i in active_ids,
                        "calibrated": s.calib.is_calibrated,
                        "std": round(s.calib.std, 3),
                        "baseline": round(s.calib.baseline, 3),
                        "velocity": round(s.kinematic.velocity, 3),
                        "acceleration": round(s.kinematic.acceleration, 3),
                        "yaw": round(s.yaw, 3),
                        "z": round(s.z, 3) if s.calib.is_calibrated else None,
                        "probability": round(s.fs.probability, 2),
                        "verdict": current_verdict.get(i),
                        # Added Sep 2026. Up/down could not be debugged
                        # from real logs at all (pitch was never
                        # recorded), and raw yaw / is_profile show
                        # whether the profile fallback in head_pose.py
                        # ever fires. Logging only; read by nothing.
                        "raw_yaw": round(s.raw_yaw, 3),
                        "is_profile": s.is_profile,
                        "initial_baseline": (round(s.calib.initial_baseline, 3)
                                             if s.calib.initial_baseline is not None else None),
                        "pitch": round(s.pitch, 3),
                        "raw_pitch": round(s.raw_pitch, 3),
                        "pitch_baseline": round(s.pitch_calib.baseline, 3),
                        "pitch_calibrated": s.pitch_calib.is_calibrated,
                        "looking_down": s.looking_down,
                    }
                profiler.log_frame(
                    frame_no=frame_no,
                    t_wall=t,
                    dt=dt,
                    fps_instant=1.0 / dt,
                    stage_ms={
                        "capture": capture_ms,
                        "preprocess": preprocess_ms,
                        "facemesh": facemesh_ms,
                        "student_processing": student_processing_ms,
                        "drawing": drawing_ms,
                        "analytics": analytics_ms,
                    "intelligence": intelligence_ms,
                        "dev_panel": dev_panel_ms,
                        "serialization": serialization_ms,
                        "jpeg": jpeg_ms,
                        "display": display_ms,
                    },
                    active_students=len(active_ids),
                    student_snapshot=_student_snapshot,
                    evidence_queue_depth=evidence.queue_depth(),
                    # Identity state recorded every frame so a wrong
                    # identification can be reconstructed afterwards from
                    # the log rather than from memory: which slot each
                    # person held, how long since each was seen, how many
                    # face matches/questions have occurred.
                    identity_snapshot={
                        "slots": identity.slot_status(t),
                        "face_id": gallery.stats(),
                        "reviews": reviews.stats(),
                        "reassignments": identity.total_reassignments,
                        "face_restores": identity.total_face_restores,
                        "phantoms_rejected": identity.total_phantoms_rejected,
                        "duplicates_dropped": identity.total_duplicates_dropped,
                        "small_faces_dropped": identity.total_small_faces_dropped,
                        "over_cap_dropped": identity.total_over_cap_dropped,
                        "expected_students": identity.expected_students,
                        "pending": len(identity._pending),
                    },
                )
            except Exception as e:
                print(f"[WARN] Profiler logging skipped: {e}")

        except Exception as e:
            import traceback
            _tb = traceback.format_exc(limit=6)
            print(f"[WARN] Unhandled error in frame {frame_no}, skipping "
                  f"this frame (session keeps running): {e}")
            profiler.log_event(
                clock.now(), frame_no, "SYSTEM",
                f"ERROR frame {frame_no}: {type(e).__name__}: {e} | {_tb}",
            )


        # ── Keyboard handling ──────────────────────────────────────
        # Single waitKey() call per frame, exactly as in the original
        # single-mode version — this only reads/writes ui_state (view
        # state) and never touches the intelligence pipeline, so it
        # cannot interfere with frame processing above.
        key = (cv2.waitKey(1) & 0xFF) if show_window else 255
        if key == 27:  # ESC — exit, exactly as before
            break
        elif key in (ord("1"), ord("2"), ord("3"), ord("4")):
            sid = key - ord("1")
            if 0 <= sid < MAX_STUDENTS:
                ui_state.set_developer(sid)
        elif key == ord("0"):
            ui_state.set_overview()
        elif key in (ord("d"), ord("D")):
            # D toggles overview <-> developer. Per spec, 1-4/0 always
            # win in case of ambiguity — handled naturally above since
            # they are separate elif branches checked independently.
            if ui_state.mode == DeveloperUIState.MODE_OVERVIEW:
                ui_state.set_developer(ui_state.selected_sid)
            else:
                ui_state.set_overview()

    cap.release()
    cv2.destroyAllWindows()
    event_logger.add_event("SYSTEM", "Exam ended")
    profiler.log_event(clock.now(), frame_no, "SYSTEM", "Exam ended")

    # ── SESSION REPORT (new) ──────────────────────────────────────
    # The artefact that outlives the session. Everything observed has
    # until now lived only on a dashboard that closes, logs only a
    # developer can read, and unlabelled JPEGs — none of which anyone
    # could hand to a person who has to make a decision about a student.
    #
    # Generated here at the end rather than during the loop, because
    # building a PDF takes far longer than one video frame and must
    # never compete with capture. Failure is contained: no report is a
    # disappointment, a crash on the way out would lose the session
    # summary that has just been printed.
    report_written = None
    if write_report:
        try:
            _t_end = clock.now()
            _report_path = os.path.join(
                "reports", f"report_{session_ts}.pdf" if video is None
                else f"report_{session_id}.pdf")
            SessionReportBuilder(
                students=students,
                verdicts=current_verdicts,
                patterns_by_slot=student_patterns,
                correlations=correlations,
                event_logger=event_logger,
                archive=archive,
                evidence_dir=os.path.join("evidence", session_id),
                score_logs=score_logs,
                session_id=session_id,
                session_start=session_start,
                roster=roster,
            ).build(_report_path, t=_t_end)
            report_written = _report_path
            print(f"[REPORT] Session report written to: {_report_path}")
            event_logger.add_event("SYSTEM", f"Session report generated: {_report_path}")
        except Exception as e:
            print(f"[WARN] Session report could not be generated: {e}")

    # On a normal, clean exit, tell the watchdog directly instead of
    # making it wait out its own grace period. On a crash (this code
    # never runs), the watchdog's own death-detection is what catches
    # it — that path is independent of this one on purpose.
    if watchdog_proc is not None:
        try:
            watchdog_proc.terminate()
        except Exception:
            pass

    # ── Session summary ───────────────────────────────────────────
    print("\n[INFO] Session ended. Summary:")
    print("-" * 52)
    for sid, student in students.items():
        if student.temporal.total_count() > 0 or student.fs.max_probability > 0:
            print(
                f"  {student.label}: "
                f"events={student.temporal.total_count()}  "
                f"peak={student.fs.max_probability:.1f}%  "
                f"confirmed={student.fs.confirmed_secs:.1f}s  "
                f"baseline={student.calib.baseline:.1f}deg"
            )
    print("-" * 52)

    if run_stats is not None:
        try:
            _results = run_stats.write(results_path, students=students,
                                       session_id=session_id, report=report_written)
            print(f"[VIDEO] Results written to: {_results}")
        except Exception as e:
            print(f"[WARN] Video results could not be written: {e}")
        clock.use_wall_clock()


if __name__ == "__main__":
    import argparse
    _ap = argparse.ArgumentParser(
        description="CPG-127 head pose engine. With no options: live webcam, exactly as before.")
    _ap.add_argument("--video", help="replay a recorded video file instead of the webcam")
    _ap.add_argument("--no-window", action="store_true", help="do not open the OpenCV window")
    _ap.add_argument("--no-dashboard", action="store_true", help="do not start the web dashboard")
    _ap.add_argument("--fast", action="store_true",
                     help="video: analyse as fast as possible instead of at the clip's own speed")
    _ap.add_argument("--results", help="video: path for the results JSON")
    _ap.add_argument("--report", action="store_true",
                     help="video: also build the PDF when running with no window and no dashboard")
    _args = _ap.parse_args()
    if not _args.video and (_args.fast or _args.results or _args.report):
        _ap.error("--fast, --results and --report only apply together with --video")
    main(video_path=_args.video,
         show_window=not _args.no_window,
         dashboard=not _args.no_dashboard,
         paced=(False if _args.fast else None),
         results_path=_args.results,
         write_report=(True if _args.report else None))

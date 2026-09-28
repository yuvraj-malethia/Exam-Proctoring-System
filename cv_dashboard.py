"""
cv_dashboard.py — _draw_bar(), draw_dashboard()

Split out of module_headpose_multi_web.py during the multi-file refactor.
Code below is moved verbatim from the original module. This is the
single-student OpenCV overlay ("DASHBOARD UI — Professional OpenCV
overlay" section in the original file). Note: draw_dashboard() is not
called anywhere in the current multi-student main loop (draw_summary_panel
/ draw_face_overlay in camera_overlays.py replaced it for the 4-student
UI) — it is preserved here unchanged, exactly as it existed — unused —
in the original monolithic file.
"""

import cv2
import math

from config import Config
from calibration import CalibrationEngine
from kinematics import KinematicAnalyzer
from temporal import TemporalPatternEngine
from suspicion import SuspicionState, SuspicionFusion

# ═══════════════════════════════════════════════════════════════════
# DASHBOARD UI — Professional OpenCV overlay
# ═══════════════════════════════════════════════════════════════════


def _draw_bar(
    frame,
    x: int,
    y: int,
    value: float,
    color: tuple,
    label: str,
    bar_w: int = 220,
    bar_h: int = 13,
):
    pct = max(0.0, min(value / 100.0, 1.0))
    cv2.putText(
        frame,
        label,
        (x, y - 3),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.38,
        (140, 140, 140),
        1,
        cv2.LINE_AA,
    )
    cv2.rectangle(frame, (x, y), (x + bar_w, y + bar_h), (35, 35, 35), -1)
    if pct > 0:
        cv2.rectangle(frame, (x, y), (x + int(bar_w * pct), y + bar_h), color, -1)
    cv2.rectangle(frame, (x, y), (x + bar_w, y + bar_h), (70, 70, 70), 1)
    cv2.putText(
        frame,
        f"{value:.0f}%",
        (x + bar_w + 6, y + bar_h - 1),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.38,
        color,
        1,
        cv2.LINE_AA,
    )


def draw_dashboard(
    frame,
    w: int,
    h: int,
    calib: CalibrationEngine,
    kinematic: KinematicAnalyzer,
    temporal: TemporalPatternEngine,
    fusion_state: SuspicionState,
    yaw_smooth: float,
    t: float,
    is_profile: bool = False,
    face_ar: float = 1.0,
):

    prob = fusion_state.probability
    level, lc = SuspicionFusion.get_level(prob)

    # ── Header bar ────────────────────────────────────────────────
    ov = frame.copy()
    cv2.rectangle(ov, (0, 0), (w, 52), (10, 12, 18), -1)
    cv2.addWeighted(ov, 0.85, frame, 0.15, 0, frame)
    cv2.putText(
        frame,
        "HEAD POSE INTELLIGENCE  |  CPG-127",
        (12, 34),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.62,
        (200, 200, 200),
        1,
        cv2.LINE_AA,
    )
    # Right-align level text — measure its pixel width to avoid overlap
    (lw, _), _ = cv2.getTextSize(level, cv2.FONT_HERSHEY_SIMPLEX, 0.72, 2)
    cv2.putText(
        frame,
        level,
        (w - lw - 12, 34),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.72,
        lc,
        2,
        cv2.LINE_AA,
    )

    # ── Right panel background ────────────────────────────────────
    px = w - 300
    ov2 = frame.copy()
    cv2.rectangle(ov2, (px - 8, 55), (w, h), (8, 10, 16), -1)
    cv2.addWeighted(ov2, 0.78, frame, 0.22, 0, frame)
    cv2.line(frame, (px - 8, 55), (px - 8, h), (40, 40, 50), 1)

    def txt(text, y, color=(180, 180, 180), scale=0.46, bold=1):
        cv2.putText(
            frame,
            text,
            (px, y),
            cv2.FONT_HERSHEY_SIMPLEX,
            scale,
            color,
            bold,
            cv2.LINE_AA,
        )

    y = 82
    txt("SUSPICION SCORE", y, (220, 220, 220), 0.52, 2)
    y += 26
    _draw_bar(frame, px, y, prob, lc, "Overall Probability", 255, 16)
    y += 36

    txt("SIGNAL BREAKDOWN", y, (160, 160, 160), 0.40)
    y += 18
    _draw_bar(
        frame,
        px,
        y,
        fusion_state.pos_component,
        (100, 200, 255),
        "Position Anomaly  (20%)",
    )
    y += 26
    _draw_bar(
        frame,
        px,
        y,
        fusion_state.vel_component,
        (255, 165, 60),
        "Angular Velocity  (10%)",
    )
    y += 26
    _draw_bar(
        frame,
        px,
        y,
        fusion_state.freq_component,
        (180, 80, 255),
        "Event Frequency   (25%)",
    )
    y += 26
    _draw_bar(
        frame,
        px,
        y,
        fusion_state.dur_component,
        (255, 80, 80),
        "Away-Time Ratio   (25%)",
    )
    y += 26
    _draw_bar(
        frame,
        px,
        y,
        fusion_state.session_component,
        (255, 220, 0),
        "Session Memory    (20%)",
    )
    y += 32

    txt("KINEMATICS", y, (160, 160, 160), 0.40)
    y += 18
    txt(f"Yaw (smooth):  {yaw_smooth:+6.1f} deg", y)
    y += 20
    if is_profile:
        cv2.rectangle(frame, (px, y - 13), (px + 255, y + 5), (0, 50, 160), -1)
        cv2.putText(
            frame,
            f"PROFILE DETECTED  FAR={face_ar:.2f}",
            (px + 4, y),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.42,
            (0, 200, 255),
            1,
            cv2.LINE_AA,
        )
    else:
        txt(f"Face Ratio:    {face_ar:.2f}  (profile<0.52)", y, (120, 120, 120), 0.40)
    y += 20
    txt(f"Baseline:      {calib.baseline:+6.1f} deg", y)
    y += 20
    z = calib.z_score(yaw_smooth)
    zcol = (
        (0, 255, 60)
        if abs(z) < 1.0
        else (0, 100, 255) if abs(z) < Config.K_SIGMA else (0, 0, 255)
    )
    txt(f"Z-Score:       {z:+6.2f}  std={calib.std:.2f}", y, zcol)
    y += 20
    txt(f"Velocity:     {kinematic.velocity:6.1f} deg/s", y)
    y += 20
    txt(f"Max Velocity: {kinematic.max_velocity:6.1f} deg/s", y)
    y += 30

    txt("SESSION STATS", y, (160, 160, 160), 0.40)
    y += 18
    rc = temporal.recent_count(t)
    txt(
        f"Events (20s window): {rc}",
        y,
        (0, 0, 255) if rc >= 5 else (0, 200, 200) if rc >= 2 else (180, 180, 180),
    )
    y += 20
    txt(f"Total events:        {temporal.total_count()}", y)
    y += 20
    txt(f"Away ratio:          {temporal.away_ratio(t) * 100:.1f}%", y)
    y += 20
    txt(f"Peak suspicion:      {fusion_state.max_probability:.1f}%", y, lc)
    y += 20
    conf = fusion_state.confirmed_secs
    txt(
        f"Confirmed time:      {conf:.1f}s",
        y,
        (0, 0, 255) if conf > 3 else (180, 180, 180),
    )
    y += 26

    # ── TEACHER VERDICT ───────────────────────────────────────────
    rc = temporal.recent_count(t)
    ar = temporal.away_ratio(t)  # already non-linear score 0-1
    tc = temporal.total_count()

    ev_freq = 1 if rc >= 3 else 0
    ev_dur = 1 if ar > 0.55 else 0  # ~15% raw away time → 0.58 score
    ev_conf = 1 if conf > 5.0 else 0
    ev_peak = 1 if fusion_state.max_probability >= Config.LEVEL_HIGH else 0
    evidence = ev_freq + ev_dur + ev_conf + ev_peak

    if evidence >= 3:
        v_text, v_color, v_bg = "!! FLAGGED FOR REVIEW !!", (0, 0, 255), (60, 0, 0)
    elif evidence == 2:
        v_text, v_color, v_bg = "ALERT INVIGILATOR", (0, 100, 255), (40, 20, 0)
    elif evidence == 1:
        v_text, v_color, v_bg = "MONITOR CLOSELY", (0, 200, 200), (0, 30, 30)
    else:
        v_text, v_color, v_bg = "NO CONCERN", (80, 200, 80), (0, 20, 0)

    cv2.rectangle(frame, (px - 4, y - 2), (w - 4, y + 52), v_bg, -1)
    cv2.rectangle(frame, (px - 4, y - 2), (w - 4, y + 52), v_color, 1)
    txt("TEACHER VERDICT", y + 12, (160, 160, 160), 0.38)
    txt(v_text, y + 34, v_color, 0.48, 2)
    y += 60

    def ev_dot(label, passed, yy):
        dot_c = (0, 220, 80) if passed else (60, 60, 60)
        cv2.circle(frame, (px + 7, yy - 4), 5, dot_c, -1, cv2.LINE_AA)
        txt(label, yy, dot_c if passed else (90, 90, 90), 0.36)

    raw_ar = temporal.total_away_s / max(t - temporal._session_start, 1)
    ev_dot(f"Repeated looks  ({rc} in 20s, need 3)", ev_freq, y)
    y += 17
    ev_dot(f"Away time       ({raw_ar*100:.1f}%, need 15%)", ev_dur, y)
    y += 17
    ev_dot(f"High suspicion  ({conf:.1f}s, need 5s)", ev_conf, y)
    y += 17
    ev_dot(f"Peak HIGH       ({fusion_state.max_probability:.0f}%)", ev_peak, y)

    # ── Currently away indicator ───────────────────────────────────
    if temporal.is_currently_away():
        away_dur = t - temporal._away_start if temporal._away_start else 0
        pulse = int(127 + 127 * math.sin(t * 4))
        cv2.putText(
            frame,
            f"LOOKING AWAY  {away_dur:.1f}s",
            (px, y),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.52,
            (0, pulse // 2, pulse),
            2,
            cv2.LINE_AA,
        )

    # ── Alert border (pulsing at HIGH) ────────────────────────────
    if prob >= Config.LEVEL_HIGH:
        pulse_t = int(4 + 5 * abs(math.sin(t * 3.5)))
        cv2.rectangle(frame, (0, 0), (w - 1, h - 1), (0, 0, 255), pulse_t)
    elif prob >= Config.LEVEL_MODERATE:
        cv2.rectangle(frame, (0, 0), (w - 1, h - 1), (0, 100, 255), 3)

    # ── Calibration overlay ───────────────────────────────────────
    if not calib.is_calibrated:
        ov3 = frame.copy()
        cv2.rectangle(ov3, (0, h // 2 - 70), (w, h // 2 + 70), (5, 5, 10), -1)
        cv2.addWeighted(ov3, 0.82, frame, 0.18, 0, frame)

        pulse_c = int(180 + 75 * abs(math.sin(t * 2)))
        cv2.putText(
            frame,
            "AI CALIBRATING — LOOK FORWARD & STAY STILL",
            (w // 2 - 330, h // 2 - 18),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.75,
            (0, pulse_c, pulse_c),
            2,
            cv2.LINE_AA,
        )

        prog = calib.progress
        bw, bx, by = 480, w // 2 - 240, h // 2 + 12
        cv2.rectangle(frame, (bx, by), (bx + bw, by + 28), (30, 30, 30), -1)
        cv2.rectangle(
            frame, (bx, by), (bx + int(bw * prog), by + 28), (0, 200, 140), -1
        )
        cv2.rectangle(frame, (bx, by), (bx + bw, by + 28), (80, 80, 80), 1)
        cv2.putText(
            frame,
            f"{int(prog * 100)}%  ({calib.n}/{Config.CALIBRATION_FRAMES} frames)",
            (bx + bw // 2 - 60, by + 20),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.5,
            (255, 255, 255),
            1,
            cv2.LINE_AA,
        )



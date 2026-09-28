"""
camera_overlays.py — selected-student highlight + multi-student UI helpers

Split out of module_headpose_multi_web.py during the multi-file refactor.
Code below is moved verbatim from the original module: draw_selected_highlight(),
get_face_x_center(), get_face_bbox(), draw_face_overlay(), draw_summary_panel().
These are the overlays drawn directly on the main camera window (as opposed
to the separate developer-panel window handled by dev_panel.py).
"""

import cv2
import math
from typing import Tuple

from config import Config
from suspicion import SuspicionFusion
from student_pipeline import StudentPipeline, MAX_STUDENTS
from head_pose import _BBOX_IDS

# ═══════════════════════════════════════════════════════════════════
# SELECTED STUDENT HIGHLIGHT — camera window overlay
# ═══════════════════════════════════════════════════════════════════

def draw_selected_highlight(frame, student: "StudentPipeline",
                            bbox: Tuple[int, int, int, int],
                            t: float):
    """
    In developer mode, draws a prominent highlight on the selected student's
    bounding box in the main camera window.
    Other students continue to be rendered via draw_face_overlay normally.
    Does NOT change any pipeline state.
    """
    x1, y1, x2, y2 = bbox
    # Thick white pulsing outline
    pulse = int(2 + 2 * abs(math.sin(t * 3)))
    cv2.rectangle(frame, (x1 - 3, y1 - 3), (x2 + 3, y2 + 3),
                  (255, 255, 255), pulse)
    # "SELECTED" label
    cv2.rectangle(frame, (x1, y1 - 36), (x2, y1 - 22), (20, 20, 20), -1)
    cv2.putText(frame,
                f"{student.label}  —  DEVELOPER MODE SELECTED",
                (x1 + 4, y1 - 25),
                cv2.FONT_HERSHEY_SIMPLEX, 0.38,
                (255, 255, 255), 1, cv2.LINE_AA)


# ═══════════════════════════════════════════════════════════════════
# MULTI-STUDENT UI HELPERS


def get_face_x_center(lm, w: int) -> float:
    """Returns horizontal centre of face bounding box. Used for left→right sort."""
    xs = [lm.landmark[i].x * w for i in _BBOX_IDS]
    return (min(xs) + max(xs)) / 2.0


def get_face_bbox(lm, w: int, h: int) -> Tuple[int, int, int, int]:
    """Returns (x1, y1, x2, y2) pixel bounding box from face landmarks."""
    xs = [lm.landmark[i].x * w for i in _BBOX_IDS]
    ys = [lm.landmark[i].y * h for i in _BBOX_IDS]
    pad = 18
    return (
        max(int(min(xs)) - pad, 0),
        max(int(min(ys)) - pad, 0),
        min(int(max(xs)) + pad, w - 1),
        min(int(max(ys)) + pad, h - 1),
    )


def draw_face_overlay(
    frame, student: StudentPipeline, bbox: Tuple[int, int, int, int], t: float
):
    """
    Compact per-face overlay drawn directly on the camera frame.
    Shows: bounding box, student label, score bar, level, calibration progress.
    Keeps it readable even with 4 faces on screen.
    """
    x1, y1, x2, y2 = bbox
    color = student.color
    prob = student.fs.probability
    level, lc = SuspicionFusion.get_level(prob)

    # ── Bounding box — thin normally, thick when flagged ──────────
    thickness = 3 if prob >= Config.LEVEL_HIGH else 1
    cv2.rectangle(frame, (x1, y1), (x2, y2), color, thickness)

    # ── Pulsing red border on HIGH ────────────────────────────────
    if prob >= Config.LEVEL_HIGH:
        pt = int(3 + 3 * abs(math.sin(t * 4)))
        cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 0, 255), pt)

    # ── Top label bar — ID + score + level ───────────────────────
    bar_h = 22
    cv2.rectangle(frame, (x1, y1 - bar_h), (x2, y1), (12, 12, 18), -1)
    cv2.rectangle(frame, (x1, y1 - bar_h), (x2, y1), color, 1)

    if not student.calib.is_calibrated:
        prog_pct = int(student.calib.progress * 100)
        # Calibration progress bar fills the top bar
        prog_w = int((x2 - x1) * student.calib.progress)
        cv2.rectangle(frame, (x1, y1 - bar_h), (x1 + prog_w, y1), (0, 160, 100), -1)
        cv2.putText(
            frame,
            f"{student.label}  CAL {prog_pct}%",
            (x1 + 4, y1 - 6),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.42,
            (255, 255, 255),
            1,
            cv2.LINE_AA,
        )
    else:
        # Score bar inside the label strip
        score_w = int((x2 - x1) * (prob / 100.0))
        cv2.rectangle(frame, (x1, y1 - bar_h), (x1 + score_w, y1), lc, -1)
        label_txt = f"{student.label}  {int(prob)}%  {level}"
        cv2.putText(
            frame,
            label_txt,
            (x1 + 4, y1 - 6),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.42,
            (255, 255, 255),
            1,
            cv2.LINE_AA,
        )

    # ── Currently away indicator below box ────────────────────────
    if student.temporal.is_currently_away():
        away_dur = (
            t - student.temporal._away_start if student.temporal._away_start else 0
        )
        pulse = int(127 + 127 * math.sin(t * 4))
        cv2.putText(
            frame,
            f"AWAY {away_dur:.1f}s",
            (x1, y2 + 16),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.45,
            (0, pulse // 2, pulse),
            1,
            cv2.LINE_AA,
        )

    # ── Profile badge ─────────────────────────────────────────────
    if student.is_profile:
        cv2.putText(
            frame,
            "PROFILE",
            (x1, y2 + 32),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.38,
            (0, 180, 255),
            1,
            cv2.LINE_AA,
        )

    # ── Tracking dots ─────────────────────────────────────────────
    # (drawn in the main loop so we have the landmark object there)


def draw_summary_panel(
    frame, students: dict, active_ids: list, w: int, h: int, t: float
):
    """
    Right-side summary panel showing all students at a glance.
    Invigilator sees every student's status without reading face overlays.
    """
    pw = 260  # panel width
    px = w - pw  # panel x start
    ov = frame.copy()
    cv2.rectangle(ov, (px, 0), (w, h), (8, 10, 16), -1)
    cv2.addWeighted(ov, 0.82, frame, 0.18, 0, frame)
    cv2.line(frame, (px, 0), (px, h), (40, 40, 55), 1)

    # Header
    cv2.putText(
        frame,
        "PROCTOR PANEL",
        (px + 8, 28),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.58,
        (200, 200, 200),
        1,
        cv2.LINE_AA,
    )
    cv2.putText(
        frame,
        f"CPG-127  {len(active_ids)}/{MAX_STUDENTS} faces",
        (px + 8, 48),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.38,
        (120, 120, 120),
        1,
        cv2.LINE_AA,
    )
    cv2.line(frame, (px, 56), (w, 56), (40, 40, 55), 1)

    y = 76
    for sid, student in students.items():
        color = student.color
        prob = student.fs.probability
        level, lc = SuspicionFusion.get_level(prob)
        active = sid in active_ids

        # Dim inactive students (face not currently visible)
        dim = 1.0 if active else 0.4
        c = tuple(int(v * dim) for v in color)

        # Student row background on HIGH
        if prob >= Config.LEVEL_HIGH and active:
            pt = int(1 + 1 * abs(math.sin(t * 4)))
            cv2.rectangle(frame, (px + 4, y - 14), (w - 4, y + 58), (50, 0, 0), -1)
            cv2.rectangle(frame, (px + 4, y - 14), (w - 4, y + 58), (0, 0, 200), pt)

        # Student label + status
        status_txt = level if active else "NOT VISIBLE"
        cv2.putText(
            frame,
            f"{student.label}",
            (px + 10, y),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.60,
            c,
            2,
            cv2.LINE_AA,
        )
        cv2.putText(
            frame,
            status_txt,
            (px + 46, y),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.42,
            lc if active else (80, 80, 80),
            1,
            cv2.LINE_AA,
        )

        if active and student.calib.is_calibrated:
            # Score bar
            bx, bw_full = px + 10, pw - 20
            by = y + 8
            score_w = int(bw_full * (prob / 100.0))
            cv2.rectangle(frame, (bx, by), (bx + bw_full, by + 10), (30, 30, 30), -1)
            if score_w > 0:
                cv2.rectangle(frame, (bx, by), (bx + score_w, by + 10), lc, -1)
            cv2.rectangle(frame, (bx, by), (bx + bw_full, by + 10), (60, 60, 60), 1)
            cv2.putText(
                frame,
                f"{int(prob)}%",
                (bx + bw_full + 4, by + 9),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.38,
                lc,
                1,
                cv2.LINE_AA,
            )

            # Key stats in small text
            rc = student.temporal.recent_count(t)
            tc = student.temporal.total_count()
            ar = student.temporal.total_away_s / max(
                t - student.temporal._session_start, 1
            )
            cv2.putText(
                frame,
                f"Ev:{tc}  Win:{rc}  Away:{ar*100:.0f}%  Conf:{student.fs.confirmed_secs:.0f}s",
                (px + 10, y + 32),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.32,
                (140, 140, 140),
                1,
                cv2.LINE_AA,
            )

            # Verdict dot
            ev = (
                int(rc >= 3)
                + int(ar > 0.15)
                + int(student.fs.confirmed_secs > 5)
                + int(student.fs.max_probability >= Config.LEVEL_HIGH)
            )
            verdict_colors = [
                (50, 50, 50),
                (0, 200, 200),
                (0, 100, 255),
                (0, 0, 255),
                (0, 0, 255),
            ]
            verdict_labels = ["OK", "WATCH", "ALERT", "FLAG", "FLAG"]
            vc = verdict_colors[min(ev, 4)]
            vl = verdict_labels[min(ev, 4)]
            cv2.circle(frame, (px + 10, y + 48), 6, vc, -1, cv2.LINE_AA)
            cv2.putText(
                frame,
                f"Verdict: {vl}  (evidence {ev}/4)",
                (px + 22, y + 52),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.33,
                vc,
                1,
                cv2.LINE_AA,
            )

        elif active and not student.calib.is_calibrated:
            prog_pct = int(student.calib.progress * 100)
            bx, bw_full = px + 10, pw - 20
            by = y + 8
            prog_w = int(bw_full * student.calib.progress)
            cv2.rectangle(frame, (bx, by), (bx + bw_full, by + 10), (30, 30, 30), -1)
            cv2.rectangle(frame, (bx, by), (bx + prog_w, by + 10), (0, 160, 100), -1)
            cv2.putText(
                frame,
                f"Calibrating... {prog_pct}%",
                (px + 10, y + 30),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.35,
                (0, 200, 140),
                1,
                cv2.LINE_AA,
            )

        y += 72
        cv2.line(frame, (px + 4, y - 6), (w - 4, y - 6), (30, 30, 40), 1)

    # Global calibration overlay if ANY student still calibrating
    any_cal = any(
        sid in active_ids and not students[sid].calib.is_calibrated
        for sid in active_ids
    )
    if any_cal and active_ids:
        pulse_c = int(180 + 75 * abs(math.sin(t * 2)))
        cv2.putText(
            frame,
            "CALIBRATING — ALL STUDENTS LOOK FORWARD",
            (10, h - 20),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.55,
            (0, pulse_c, pulse_c),
            2,
            cv2.LINE_AA,
        )



"""
dev_panel.py — developer detail-mode diagnostic panel

Split out of module_headpose_multi_web.py during the multi-file refactor.
Code below is moved verbatim from the original module: dev-panel
constants/primitive helpers, Sections A-G (calibration, head/kinematic
state, signal breakdown, temporal/event state, session memory, final
decision, pipeline status), the mini yaw graph, and render_developer_view()
which assembles all of them into the 700x730 panel. Kept together as one
module since every piece here is called directly by render_developer_view()
and nothing outside this module needs them individually.
"""

import cv2
import math
import numpy as np

from config import Config
from suspicion import SuspicionFusion
from developer_ui_state import DeveloperUIState

# ═══════════════════════════════════════════════════════════════════
# DEV PANEL — constants and primitive helpers
# ═══════════════════════════════════════════════════════════════════

_DP_W = 700    # dev panel pixel width
_DP_H = 730    # dev panel pixel height

# Semantic colour palette for dev panel
_DC_HDR  = (210, 210, 210)   # section header
_DC_VAL  = (180, 180, 180)   # normal value
_DC_DIM  = ( 95,  95,  95)   # units / hints
_DC_OK   = ( 80, 220,  80)   # green — normal
_DC_MILD = (  0, 200, 200)   # cyan  — mild / watch
_DC_MED  = (  0, 140, 255)   # orange — moderate
_DC_HIGH = (  0,   0, 255)   # red   — high risk
_DC_CAL  = (  0, 200, 140)   # teal  — calibration


def _dp_txt(panel, text: str, x: int, y: int,
            color=_DC_VAL, scale: float = 0.40, bold: int = 1):
    """Write text onto the dev panel."""
    cv2.putText(panel, text, (x, y),
                cv2.FONT_HERSHEY_SIMPLEX, scale, color, bold, cv2.LINE_AA)


def _dp_bar(panel, x: int, y: int, value_pct: float,
            color: tuple, label: str = "",
            bar_w: int = 270, bar_h: int = 12) -> int:
    """
    Draw a labeled progress bar.
    value_pct is clamped 0–100.
    Returns the y coordinate immediately after the bar.
    """
    pct = max(0.0, min(value_pct / 100.0, 1.0))
    if label:
        _dp_txt(panel, label, x, y - 2, _DC_DIM, 0.32)
        y += 3
    cv2.rectangle(panel, (x, y), (x + bar_w, y + bar_h), (28, 28, 28), -1)
    if pct > 0:
        cv2.rectangle(panel, (x, y),
                      (x + int(bar_w * pct), y + bar_h), color, -1)
    cv2.rectangle(panel, (x, y), (x + bar_w, y + bar_h), (55, 55, 55), 1)
    _dp_txt(panel, f"{value_pct:.0f}%",
            x + bar_w + 5, y + bar_h - 1, color, 0.35)
    return y + bar_h + 4


def _dp_section(panel, x: int, y: int,
                title: str, color: tuple = (130, 160, 255)) -> int:
    """
    Draw a section header with a thin underline.
    Returns the y for the first content line.
    """
    _dp_txt(panel, title, x, y, color, 0.44, 2)
    cv2.line(panel, (x, y + 4), (x + 318, y + 4),
             tuple(max(0, c // 3) for c in color), 1)
    return y + 20


def _dp_kv(panel, x: int, y: int,
           key: str, value: str,
           vc=_DC_VAL, kc=_DC_DIM, scale: float = 0.38) -> int:
    """Draw a key: value pair. Returns next y."""
    _dp_txt(panel, key, x, y, kc, scale)
    _dp_txt(panel, value, x + 130, y, vc, scale)
    return y + 16


def _dc_z(z: float) -> tuple:
    """Colour for a z-score — mirrors draw_dashboard logic exactly."""
    if abs(z) < 1.0:
        return _DC_OK
    elif abs(z) < Config.K_SIGMA:
        return _DC_MED
    else:
        return _DC_HIGH


# ═══════════════════════════════════════════════════════════════════
# SECTION A — CALIBRATION
# ═══════════════════════════════════════════════════════════════════

def _draw_sec_calibration(panel, x: int, y: int,
                          student: "StudentPipeline",
                          is_visible: bool) -> int:
    """
    Displays existing calibration state.
    - During calibration : progress, Welford running mean/std preview,
                           raw yaw, Kalman yaw.
    - After calibration  : baseline, clamped std, K_SIGMA, threshold,
                           and a visual normal-range diagram.

    The 'preview std' during calibration is computed from calib.M2 / (n-1),
    which is the EXACT formula calib.feed() uses when it finalises.
    This is a read-only UI calculation — nothing is stored or changed.
    """
    calib = student.calib
    y = _dp_section(panel, x, y, "A  CALIBRATION", _DC_CAL)

    if not is_visible and not calib.is_calibrated:
        _dp_txt(panel, "No face — awaiting calibration", x, y, _DC_DIM, 0.36)
        return y + 22

    if not calib.is_calibrated:
        # ── Still calibrating ────────────────────────────────────
        _dp_txt(panel, "CALIBRATING", x, y, _DC_CAL, 0.42, 2)
        y += 20
        prog = calib.progress  # existing property: n / CALIBRATION_FRAMES
        ny = _dp_bar(panel, x, y, prog * 100, _DC_CAL, "", 240, 13)
        _dp_txt(panel,
                f"  {int(prog * 100)}%   {calib.n} / {Config.CALIBRATION_FRAMES} samples",
                x, ny + 11, _DC_DIM, 0.34)
        y = ny + 18

        # Preview std — read-only computation from Welford accumulators.
        # Identical formula to what calib.feed() will use when it completes.
        if calib.n > 1:
            _pvar = calib.M2 / (calib.n - 1)
            _pstd = math.sqrt(max(_pvar, 0.0))
        else:
            _pstd = 0.0

        y = _dp_kv(panel, x, y, "Running Mean:",
                   f"{calib.mean:+.2f} deg")
        y = _dp_kv(panel, x, y, "Preview STD:",
                   f"{_pstd:.2f} deg  "
                   f"(will clamp {Config.MIN_STD_DEG}–{Config.MAX_STD_DEG})",
                   _DC_DIM)
        y = _dp_kv(panel, x, y, "Raw Yaw:",
                   f"{student.raw_yaw:+.2f} deg", _DC_DIM)
        y = _dp_kv(panel, x, y, "Kalman Yaw:",
                   f"{student.yaw:+.2f} deg")
        face_str = "YES" if is_visible else "NO"
        y = _dp_kv(panel, x, y, "Face visible:", face_str,
                   _DC_OK if is_visible else _DC_HIGH)
        _dp_txt(panel, "[ Keep facing forward & stay still ]",
                x, y, _DC_DIM, 0.33)
        y += 16

    else:
        # ── Calibration complete ─────────────────────────────────
        _dp_txt(panel, "COMPLETE", x, y, _DC_OK, 0.42, 2)
        y += 20
        threshold = Config.K_SIGMA * calib.std
        lower     = calib.baseline - threshold
        upper     = calib.baseline + threshold

        y = _dp_kv(panel, x, y, "Samples:",
                   f"{min(calib.n, Config.CALIBRATION_FRAMES)}"
                   f" / {Config.CALIBRATION_FRAMES}")
        y = _dp_kv(panel, x, y, "Baseline:",
                   f"{calib.baseline:+.2f} deg", (100, 220, 255))
        y = _dp_kv(panel, x, y, "STD:",
                   f"{calib.std:.2f} deg"
                   f"  (raw clamped to {Config.MIN_STD_DEG}–{Config.MAX_STD_DEG})")
        y = _dp_kv(panel, x, y, "K-Sigma:",
                   f"{Config.K_SIGMA}")
        y = _dp_kv(panel, x, y, "Away threshold:",
                   f"+/-{threshold:.2f} deg  (K * STD)", (100, 220, 255))

        # Normal-range visualisation
        y += 4
        viz_w = 240
        cv2.rectangle(panel, (x, y), (x + viz_w, y + 15), (18, 18, 18), -1)
        # Green safe zone (centre third)
        z1 = x + viz_w // 3
        z2 = x + 2 * viz_w // 3
        cv2.rectangle(panel, (z1, y), (z2, y + 15), (0, 45, 15), -1)
        cv2.rectangle(panel, (x, y), (x + viz_w, y + 15), (38, 55, 38), 1)
        mid_x = x + viz_w // 2
        cv2.line(panel, (mid_x, y), (mid_x, y + 15), (0, 190, 90), 2)
        _dp_txt(panel, f"{lower:.1f}", x,         y + 25, _DC_DIM, 0.30)
        _dp_txt(panel, f"{calib.baseline:.1f}",
                mid_x - 10, y + 25, _DC_OK, 0.30)
        _dp_txt(panel, f"{upper:.1f}",
                x + viz_w - 22, y + 25, _DC_DIM, 0.30)
        _dp_txt(panel, "NORMAL RANGE  (lower — baseline — upper)",
                x, y - 5, _DC_DIM, 0.29)
        y += 34

    return y


# ═══════════════════════════════════════════════════════════════════
# SECTION B — HEAD / KINEMATIC STATE
# ═══════════════════════════════════════════════════════════════════

def _draw_sec_head_state(panel, x: int, y: int,
                         student: "StudentPipeline",
                         is_visible: bool) -> int:
    """
    Shows all kinematic values for the selected student.
    Reads: student.raw_yaw, student.yaw, student.z, student.calib.*,
           student.kinematic.*, student.is_profile, student.face_ar.
    Derives direction/state from the EXISTING calib.is_away() condition
    (|z| > K_SIGMA) — no new threshold introduced.
    """
    calib     = student.calib
    kinematic = student.kinematic
    y = _dp_section(panel, x, y, "B  HEAD / KINEMATIC STATE", (130, 160, 255))

    if not is_visible:
        _dp_txt(panel, "Student not visible this frame", x, y, _DC_DIM, 0.36)
        return y + 22

    if not calib.is_calibrated:
        _dp_txt(panel, "Calibrating — no z-score yet", x, y, _DC_DIM, 0.36)
        y = _dp_kv(panel, x, y + 18, "Raw Yaw:", f"{student.raw_yaw:+.2f} deg", _DC_DIM)
        y = _dp_kv(panel, x, y,       "Kalman Yaw:", f"{student.yaw:+.2f} deg")
        return y

    z         = student.z  # z-score already computed in StudentPipeline.process()
    diff      = student.yaw - calib.baseline
    zcol      = _dc_z(z)
    # Direction/state derived from existing is_away() condition (|z|>K_SIGMA)
    is_away_flag, direction = calib.is_away(student.yaw)
    state_str = "AWAY" if is_away_flag else "FORWARD"
    state_col = _DC_HIGH if is_away_flag else _DC_OK

    y = _dp_kv(panel, x, y, "Raw Yaw:",
               f"{student.raw_yaw:+.2f} deg", _DC_DIM)
    y = _dp_kv(panel, x, y, "Kalman Yaw:",
               f"{student.yaw:+.2f} deg")
    y = _dp_kv(panel, x, y, "Baseline:",
               f"{calib.baseline:+.2f} deg", (100, 220, 255))
    y = _dp_kv(panel, x, y, "Difference:",
               f"{diff:+.2f} deg",
               _DC_HIGH if abs(diff) > Config.K_SIGMA * calib.std else _DC_VAL)

    # Z-score line — slightly larger to draw the eye
    _dp_txt(panel, "Z-Score:", x, y, _DC_DIM, 0.38)
    _dp_txt(panel,
            f"{z:+.3f}   (threshold +/-{Config.K_SIGMA}  STD={calib.std:.2f})",
            x + 130, y, zcol, 0.38, 2)
    y += 18

    y = _dp_kv(panel, x, y, "Direction:", direction, zcol)
    y = _dp_kv(panel, x, y, "State:", state_str, state_col)
    y = _dp_kv(panel, x, y, "Angular Vel:",
               f"{kinematic.velocity:.1f} deg/s")
    y = _dp_kv(panel, x, y, "Max Vel:",
               f"{kinematic.max_velocity:.1f} deg/s", _DC_DIM)
    profile_str = "YES" if student.is_profile else "NO"
    profile_col = _DC_MED if student.is_profile else _DC_OK
    y = _dp_kv(panel, x, y, "Face Ratio:",
               f"{student.face_ar:.2f}   Profile: {profile_str}", profile_col)
    return y


# ═══════════════════════════════════════════════════════════════════
# SECTION C — SIGNAL BREAKDOWN
# ═══════════════════════════════════════════════════════════════════

def _draw_sec_signals(panel, x: int, y: int,
                      student: "StudentPipeline",
                      is_visible: bool) -> int:
    """
    Shows all five suspicion signal components and their configured weights.
    Reads existing SuspicionState components — no new calculation.
    Weights are read from Config, NOT hard-coded in strings.
    """
    fs = student.fs
    y  = _dp_section(panel, x, y, "C  SIGNAL BREAKDOWN", (200, 130, 255))

    if not student.calib.is_calibrated:
        _dp_txt(panel, "Awaiting calibration", x, y, _DC_DIM, 0.36)
        return y + 22

    prob  = fs.probability
    _, lc = SuspicionFusion.get_level(prob)

    y = _dp_bar(panel, x, y, prob, lc, "Overall Suspicion", 270, 15)
    y += 6

    # Signal bars — labels include actual Config weight values
    bars = [
        (fs.pos_component,     (100, 200, 255),
         f"Position Anomaly   (W={int(Config.W_POSITION*100)}%)"),
        (fs.vel_component,     (255, 165,  60),
         f"Angular Velocity   (W={int(Config.W_VELOCITY*100)}%)"),
        (fs.freq_component,    (180,  80, 255),
         f"Event Frequency    (W={int(Config.W_FREQUENCY*100)}%)"),
        (fs.dur_component,     (255,  80,  80),
         f"Away-Time Ratio    (W={int(Config.W_DURATION*100)}%)"),
        (fs.session_component, (255, 220,   0),
         f"Session Memory     (W={int(Config.W_SESSION*100)}%)"),
    ]
    for val, col, lbl in bars:
        y = _dp_bar(panel, x, y, val, col, lbl, 270, 11)
        y += 2

    return y + 4


# ═══════════════════════════════════════════════════════════════════
# SECTION D — TEMPORAL / EVENT STATE
# ═══════════════════════════════════════════════════════════════════

def _draw_sec_temporal(panel, x: int, y: int,
                       student: "StudentPipeline",
                       t: float, is_visible: bool) -> int:
    """
    Reads TemporalPatternEngine state — events, away timing, ratios.
    All values read from existing attributes; no new scoring computed.
    """
    temporal = student.temporal
    y = _dp_section(panel, x, y, "D  TEMPORAL / EVENTS", (255, 130, 130))

    if not student.calib.is_calibrated:
        _dp_txt(panel, "Awaiting calibration", x, y, _DC_DIM, 0.36)
        return y + 22

    # Current away state — read from existing _away_start
    if temporal.is_currently_away() and temporal._away_start is not None:
        cur_dur = t - temporal._away_start
        y = _dp_kv(panel, x, y, "Current State:", "AWAY", _DC_HIGH)
        y = _dp_kv(panel, x, y, "Away duration:",
                   f"{cur_dur:.1f} s  (ongoing)", _DC_HIGH)
    else:
        y = _dp_kv(panel, x, y, "Current State:", "FORWARD", _DC_OK)
        y = _dp_kv(panel, x, y, "Away duration:", "N/A", _DC_DIM)

    rc = temporal.recent_count(t)
    tc = temporal.total_count()
    rc_col = (_DC_HIGH if rc >= 5 else _DC_MILD if rc >= 2 else _DC_VAL)
    y = _dp_kv(panel, x, y, "Events (window):",
               f"{rc}   [{Config.TEMPORAL_WINDOW_SEC}s rolling window]", rc_col)
    y = _dp_kv(panel, x, y, "Total events:", f"{tc}")

    session_len = max(t - temporal._session_start, 1.0)
    raw_away_pct = temporal.total_away_s / session_len * 100.0
    y = _dp_kv(panel, x, y, "Total away time:",
               f"{temporal.total_away_s:.1f} s")
    y = _dp_kv(panel, x, y, "Raw away ratio:",
               f"{raw_away_pct:.1f}%  of session")

    # Last and longest event (from existing events list)
    if temporal.events:
        last_ev = temporal.events[-1]
        longest = max(temporal.events, key=lambda e: e.duration)
        y = _dp_kv(panel, x, y, "Last event:",
                   f"{last_ev.duration:.1f}s  ({last_ev.direction})", _DC_DIM)
        y = _dp_kv(panel, x, y, "Longest event:",
                   f"{longest.duration:.1f}s  ({longest.direction})", _DC_DIM)
    else:
        y = _dp_kv(panel, x, y, "Events:", "none yet", _DC_DIM)

    y = _dp_kv(panel, x, y, "Min event dur:",
               f"{Config.MIN_EVENT_DURATION}s  (shorter ignored)", _DC_DIM)
    return y


# ═══════════════════════════════════════════════════════════════════
# SECTION E — SESSION MEMORY
# ═══════════════════════════════════════════════════════════════════

def _draw_sec_session(panel, x: int, y: int,
                      student: "StudentPipeline",
                      t: float) -> int:
    """
    Displays session memory (cumulative evidence score).
    Explains what it means without changing how it works.
    """
    fs = student.fs
    y  = _dp_section(panel, x, y, "E  SESSION MEMORY", (200, 200,  60))

    mem = fs.session_component   # already 0–100%, read from SuspicionState
    if mem >= 99.0:
        mem_status = "MAXIMUM"
        mem_col    = _DC_HIGH
    elif mem >= 60.0:
        mem_status = "HIGH"
        mem_col    = _DC_MED
    elif mem >= 20.0:
        mem_status = "BUILDING"
        mem_col    = _DC_MILD
    else:
        mem_status = "LOW"
        mem_col    = _DC_OK

    y = _dp_bar(panel, x, y, mem, mem_col, "Memory Score", 270, 13)
    y += 2
    _dp_txt(panel, f"Status: {mem_status}", x, y, mem_col, 0.40, 2)
    y += 18

    tc  = student.temporal.total_count()
    session_len = max(t - student.temporal._session_start, 1.0)
    raw_ar = student.temporal.total_away_s / session_len * 100.0
    y = _dp_kv(panel, x, y, "Total events:", f"{tc}")
    y = _dp_kv(panel, x, y, "Raw away ratio:", f"{raw_ar:.1f}%")

    # Explanation text (UI only)
    _dp_txt(panel,
            "Accumulates all past suspicious events.",
            x, y, _DC_DIM, 0.33)
    y += 13
    _dp_txt(panel,
            "Never decays — past cheating stays on record.",
            x, y, _DC_DIM, 0.33)
    y += 16
    return y


# ═══════════════════════════════════════════════════════════════════
# SECTION F — FINAL DECISION
# ═══════════════════════════════════════════════════════════════════

def _draw_sec_verdict(panel, x: int, y: int,
                      student: "StudentPipeline",
                      t: float) -> int:
    """
    Shows the same verdict logic as draw_dashboard.
    ALL conditions read from existing state — no new thresholds.
    """
    fs       = student.fs
    temporal = student.temporal
    y        = _dp_section(panel, x, y, "F  FINAL DECISION", (255, 130,  60))

    prob  = fs.probability
    level, lc = SuspicionFusion.get_level(prob)

    # Level display — large text
    _dp_txt(panel, level, x, y, lc, 0.60, 2)
    y += 28

    y = _dp_kv(panel, x, y, "Overall suspicion:", f"{prob:.1f}%", lc)
    y = _dp_kv(panel, x, y, "Peak suspicion:",
               f"{fs.max_probability:.1f}%", lc)
    y = _dp_kv(panel, x, y, "Confirmed time:",
               f"{fs.confirmed_secs:.1f}s  (above {int(Config.CONFIRMED_THRESHOLD)}%)",
               _DC_HIGH if fs.confirmed_secs > 3 else _DC_DIM)
    y += 4

    # Evidence logic — identical to draw_dashboard
    rc  = temporal.recent_count(t)
    ar  = temporal.away_ratio(t)
    conf = fs.confirmed_secs
    ev_freq = 1 if rc >= 3 else 0
    ev_dur  = 1 if ar > 0.55 else 0
    ev_conf = 1 if conf > 5.0 else 0
    ev_peak = 1 if fs.max_probability >= Config.LEVEL_HIGH else 0
    evidence = ev_freq + ev_dur + ev_conf + ev_peak

    raw_ar = temporal.total_away_s / max(t - temporal._session_start, 1)

    evidence_items = [
        (ev_freq, f"Repeated looks   ({rc} in 20s, need 3)"),
        (ev_dur,  f"Away time        ({raw_ar*100:.1f}%, need 15%)"),
        (ev_conf, f"High suspicion   ({conf:.1f}s, need 5s)"),
        (ev_peak, f"Peak reached HIGH ({fs.max_probability:.0f}%)"),
    ]
    for passed, label in evidence_items:
        dot_c = _DC_OK if passed else (50, 50, 50)
        cv2.circle(panel, (x + 7, y - 4), 5, dot_c, -1, cv2.LINE_AA)
        _dp_txt(panel, label, x + 18, y,
                dot_c if passed else (80, 80, 80), 0.35)
        y += 15
    y += 4

    # Verdict box — same rules as draw_dashboard
    if evidence >= 3:
        vt, vc, vbg = "!! FLAGGED FOR REVIEW !!", _DC_HIGH, (55, 0, 0)
    elif evidence == 2:
        vt, vc, vbg = "ALERT INVIGILATOR", _DC_MED, (35, 18, 0)
    elif evidence == 1:
        vt, vc, vbg = "MONITOR CLOSELY", _DC_MILD, (0, 25, 25)
    else:
        vt, vc, vbg = "NO CONCERN", _DC_OK, (0, 18, 0)

    cv2.rectangle(panel, (x - 2, y - 2), (x + 316, y + 30), vbg, -1)
    cv2.rectangle(panel, (x - 2, y - 2), (x + 316, y + 30), vc, 1)
    _dp_txt(panel, vt, x + 4, y + 20, vc, 0.46, 2)
    y += 38
    return y


# ═══════════════════════════════════════════════════════════════════
# SECTION G — PIPELINE STATUS
# ═══════════════════════════════════════════════════════════════════

def _draw_sec_status(panel, x: int, y: int,
                     student: "StudentPipeline",
                     is_visible: bool) -> int:
    """
    Basic pipeline identity and visibility information.
    Clearly labels current identity method (position-based, left→right).
    """
    y = _dp_section(panel, x, y, "G  PIPELINE STATUS", (130, 130, 130))

    y = _dp_kv(panel, x, y, "Student ID:",    student.label, _DC_HDR)
    y = _dp_kv(panel, x, y, "Visible:",
               "YES" if is_visible else "NO",
               _DC_OK if is_visible else _DC_HIGH)
    y = _dp_kv(panel, x, y, "Calibrated:",
               "YES" if student.calib.is_calibrated else "NO",
               _DC_OK if student.calib.is_calibrated else _DC_CAL)
    y = _dp_kv(panel, x, y, "Cal samples:",
               f"{student.calib.n} / {Config.CALIBRATION_FRAMES}", _DC_DIM)
    y = _dp_kv(panel, x, y, "Identity mode:", "POSITION-BASED (left->right)", _DC_DIM)
    y = _dp_kv(panel, x, y, "Pipeline:",      "ACTIVE", _DC_OK)
    return y


# ═══════════════════════════════════════════════════════════════════
# MINI YAW GRAPH — UI-only, does NOT feed into algorithm
# ═══════════════════════════════════════════════════════════════════

def _draw_yaw_graph(panel, x: int, y: int,
                    ui_state: "DeveloperUIState",
                    student: "StudentPipeline") -> int:
    """
    Draws a rolling Kalman-yaw line graph (~5s history).
    Source: ui_state.yaw_history — a UI-only deque populated by
    DeveloperUIState.push_graph_sample(). This deque is NEVER read
    by any algorithm.
    Also draws the current baseline and +/-threshold lines when calibrated.
    """
    gw, gh = 300, 75
    _dp_txt(panel, "YAW HISTORY  (Kalman-smoothed, last 5s)", x, y, _DC_DIM, 0.32)
    y += 12

    cv2.rectangle(panel, (x, y), (x + gw, y + gh), (12, 12, 15), -1)
    cv2.rectangle(panel, (x, y), (x + gw, y + gh), (40, 40, 50), 1)

    hist = list(ui_state.yaw_history)
    base = list(ui_state.baseline_history)

    if len(hist) < 2:
        _dp_txt(panel, "accumulating...", x + 90, y + 35, _DC_DIM, 0.36)
        return y + gh + 6

    calib  = student.calib
    thr    = (Config.K_SIGMA * calib.std) if calib.is_calibrated else 0.0

    # Determine y-axis range from data + threshold bands
    all_v  = hist.copy()
    if calib.is_calibrated:
        b = calib.baseline
        all_v += [b + thr, b - thr]
    ymin   = min(all_v) - 2.0
    ymax   = max(all_v) + 2.0
    yrange = max(ymax - ymin, 4.0)

    def _vy(val: float) -> int:
        """Map a yaw value to a pixel y inside the graph box."""
        norm = (val - ymin) / yrange
        return y + gh - max(0, min(gh, int(norm * gh)))

    # Draw threshold bands and baseline
    if calib.is_calibrated:
        b = calib.baseline
        cv2.line(panel, (x, _vy(b + thr)), (x + gw, _vy(b + thr)),
                 (0, 70, 0), 1)
        cv2.line(panel, (x, _vy(b - thr)), (x + gw, _vy(b - thr)),
                 (0, 70, 0), 1)
        cv2.line(panel, (x, _vy(b)), (x + gw, _vy(b)),
                 (0, 140, 70), 1)

    # Draw yaw line
    n   = len(hist)
    pts = [
        (x + int(i * gw / max(n - 1, 1)), max(y, min(y + gh, _vy(v))))
        for i, v in enumerate(hist)
    ]
    for i in range(1, len(pts)):
        cv2.line(panel, pts[i - 1], pts[i], (90, 190, 255), 1, cv2.LINE_AA)
    if pts:
        cv2.circle(panel, pts[-1], 3, (255, 255, 255), -1)

    # Axis labels
    _dp_txt(panel, f"{ymax:.1f}", x + gw + 3, y + 8, _DC_DIM, 0.28)
    _dp_txt(panel, f"{ymin:.1f}", x + gw + 3, y + gh, _DC_DIM, 0.28)

    return y + gh + 6


# ═══════════════════════════════════════════════════════════════════
# RENDER DEVELOPER VIEW — assembles the full 700×730 dev panel
# ═══════════════════════════════════════════════════════════════════

def render_developer_view(students: dict, active_ids: list,
                          ui_state: "DeveloperUIState", t: float):
    """
    Creates a 700×730 numpy image containing all developer sections
    and shows it in a named OpenCV window (separate from the camera window).

    This function ONLY reads pipeline state. It never modifies it.
    It is safe to call every frame.
    """
    panel      = np.zeros((_DP_H, _DP_W, 3), dtype=np.uint8)
    sid        = ui_state.selected_sid
    student    = students[sid]
    is_visible = sid in active_ids

    # ── Header ────────────────────────────────────────────────────
    cv2.rectangle(panel, (0, 0), (_DP_W, 40), (12, 14, 20), -1)
    cv2.putText(panel,
                f"DEV MODE  |  CPG-127  |  Student {student.label}"
                f"  |  {'VISIBLE' if is_visible else 'NOT VISIBLE'}",
                (8, 26), cv2.FONT_HERSHEY_SIMPLEX, 0.50,
                student.color if is_visible else (80, 80, 80), 1, cv2.LINE_AA)
    cv2.putText(panel,
                "[0/D] Overview  [1-4] Switch  [ESC] Exit",
                (_DP_W - 310, 26), cv2.FONT_HERSHEY_SIMPLEX,
                0.34, _DC_DIM, 1, cv2.LINE_AA)
    cv2.line(panel, (0, 40), (_DP_W, 40), (40, 40, 55), 1)

    # ── NOT VISIBLE banner ─────────────────────────────────────────
    if not is_visible:
        cv2.rectangle(panel, (0, 41), (_DP_W, 110), (40, 10, 10), -1)
        cv2.putText(panel,
                    f"STUDENT {student.label}  —  NOT CURRENTLY VISIBLE",
                    (12, 72), cv2.FONT_HERSHEY_SIMPLEX,
                    0.60, _DC_HIGH, 2, cv2.LINE_AA)
        _dp_txt(panel,
                "Stale values shown below (last known pipeline state).",
                12, 95, _DC_DIM, 0.36)

    # ── Two-column layout ─────────────────────────────────────────
    lx = 8      # left column x
    rx = 358    # right column x
    y0 = 48

    # LEFT: A (Calibration) → B (Head State) → Mini-graph
    ly = y0
    ly = _draw_sec_calibration(panel, lx, ly, student, is_visible)
    ly += 8
    cv2.line(panel, (lx, ly), (lx + 330, ly), (30, 30, 40), 1)
    ly += 8
    ly = _draw_sec_head_state(panel, lx, ly, student, is_visible)
    ly += 8
    cv2.line(panel, (lx, ly), (lx + 330, ly), (30, 30, 40), 1)
    ly += 8
    ly = _draw_yaw_graph(panel, lx, ly, ui_state, student)

    # Centre divider
    cv2.line(panel, (350, 44), (350, _DP_H - 4), (35, 35, 50), 1)

    # RIGHT: C (Signals) → D (Temporal) → E (Session) → F (Verdict) → G (Status)
    ry = y0
    ry = _draw_sec_signals(panel, rx, ry, student, is_visible)
    ry += 6
    cv2.line(panel, (rx, ry), (rx + 330, ry), (30, 30, 40), 1)
    ry += 6
    ry = _draw_sec_temporal(panel, rx, ry, student, t, is_visible)
    ry += 6
    cv2.line(panel, (rx, ry), (rx + 330, ry), (30, 30, 40), 1)
    ry += 6
    ry = _draw_sec_session(panel, rx, ry, student, t)
    ry += 6
    cv2.line(panel, (rx, ry), (rx + 330, ry), (30, 30, 40), 1)
    ry += 6
    ry = _draw_sec_verdict(panel, rx, ry, student, t)
    ry += 6
    cv2.line(panel, (rx, ry), (rx + 330, ry), (30, 30, 40), 1)
    ry += 6
    ry = _draw_sec_status(panel, rx, ry, student, is_visible)

    cv2.imshow(DeveloperUIState._WIN_TITLE, panel)



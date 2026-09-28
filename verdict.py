"""
verdict.py — compute_verdict()

Centralizes the "evidence count / verdict" logic that already runs live
in camera_overlays.draw_summary_panel() (OK / WATCH / ALERT / FLAG), so
EventLogger and EvidenceCapture trigger off the *exact same* definition
of "suspicious" the invigilator already sees on screen — instead of a
second, separately-tuned threshold set that could drift from it.

This is a straight extraction of the verdict formula already used in
draw_summary_panel() (the panel actually shown in the multi-student main
loop) — same checks, same thresholds, same order. No new thresholds,
no algorithm change. Read-only: reads temporal/suspicion state that the
intelligence engine already computed, changes nothing.

Sep 2026: the four checks are now built in ONE place, verdict_checks(),
and both compute_verdict() (evidence capture, event log) and
verdict_payload() (the dashboard) count them from there. Before, the
dashboard never saw the verdict at all — only the score — so the one
thing that decides whether evidence is saved was invisible to the
invigilator, and nothing could say WHICH checks a student had tripped.
Same thresholds, same arithmetic; proven identical on randomised input
against the previous version (archive/17/verdict.py).
"""

from config import Config

VERDICT_LABELS = ["OK", "WATCH", "ALERT", "FLAG", "FLAG"]


def verdict_checks(student, t):
    """
    The four checks behind the verdict, each with the value it saw, so a
    reader can be told WHY a student is at WATCH rather than only THAT
    they are. Mirrors draw_summary_panel()'s 4-part check exactly:
      - recent_count(t) >= 3           (repeated looks in 20s window)
      - away ratio > 0.15              (raw away time / session time)
      - confirmed_secs > 5             (time spent above CONFIRMED_THRESHOLD)
      - max_probability >= LEVEL_HIGH  (ever hit HIGH RISK)
    Only meaningful once the student is calibrated.
    """
    rc = student.temporal.recent_count(t)
    ar = student.temporal.total_away_s / max(t - student.temporal._session_start, 1)
    cs = student.fs.confirmed_secs
    mp = student.fs.max_probability
    return [
        {"key": "repeated", "label": "Repeated looks away",
         "met": rc >= 3, "value": rc, "limit": 3,
         "window_sec": Config.TEMPORAL_WINDOW_SEC},
        {"key": "away_ratio", "label": "Share of the exam spent looking away",
         "met": ar > 0.15, "value": round(ar, 4), "limit": 0.15},
        {"key": "confirmed", "label": "Time spent with a high score",
         "met": cs > 5, "value": round(cs, 1), "limit": 5},
        {"key": "peak", "label": "Score has reached HIGH RISK",
         "met": mp >= Config.LEVEL_HIGH, "value": round(mp, 1),
         "limit": Config.LEVEL_HIGH},
    ]


def compute_verdict(student, t):
    """
    Returns (verdict_label: str, evidence_count: int) for one student at
    time t — how many of verdict_checks() are met.
    Returns ("CALIBRATING", 0) before the student's calibration finishes,
    same as the panel shows "Calibrating..." instead of a verdict.
    """
    if not student.calib.is_calibrated:
        return "CALIBRATING", 0

    ev = sum(1 for c in verdict_checks(student, t) if c["met"])
    return VERDICT_LABELS[min(ev, 4)], ev


def verdict_payload(student, t):
    """
    The verdict for the dashboard, with the checks behind it — the same
    label compute_verdict() gives the event log and evidence capture,
    because both count the same verdict_checks(). Read-only.
    """
    if not student.calib.is_calibrated:
        return {"label": "CALIBRATING", "evidence": 0, "checks": []}
    checks = verdict_checks(student, t)
    ev = sum(1 for c in checks if c["met"])
    return {"label": VERDICT_LABELS[min(ev, 4)], "evidence": ev, "checks": checks}

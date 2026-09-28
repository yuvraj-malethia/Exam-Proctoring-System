"""
developer_ui_state.py — DeveloperUIState

Split out of module_headpose_multi_web.py during the multi-file refactor.
Code below is moved verbatim from the original module. Pure view state —
zero intelligence, reads model state, never writes to it (see original
docstring below).
"""

import cv2
from collections import deque

# ═══════════════════════════════════════════════════════════════════
# DEVELOPER UI STATE — pure view state, zero intelligence
# ═══════════════════════════════════════════════════════════════════
# Separation principle:
#   MODEL (intelligence) ← StudentPipeline and its engines
#   VIEW  (UI state)     ← DeveloperUIState
# DeveloperUIState reads model state. It never writes to it.

class DeveloperUIState:
    """
    Holds only display/interaction state for the developer detail mode.
    - mode            : "overview" | "developer"
    - selected_sid    : 0-indexed student slot (0=S1 … 3=S4)
    - yaw_history     : UI-only deque for the mini-graph.
                        Populated by reading student.yaw each frame.
                        NEVER read back into any algorithm.
    - baseline_history: UI-only deque for drawing the baseline line on graph.
    """
    MODE_OVERVIEW  = "overview"
    MODE_DEVELOPER = "developer"
    _WIN_TITLE     = "DEV PANEL  |  CPG-127  |  Head Pose Intelligence"

    def __init__(self):
        self.mode           = self.MODE_OVERVIEW
        self.selected_sid   = 0
        # UI-only graph buffers — ~5 s at 30 fps
        self.yaw_history      = deque(maxlen=150)
        self.baseline_history = deque(maxlen=150)

    def set_developer(self, sid: int):
        """Enter developer mode for a specific student slot."""
        prev_sid       = self.selected_sid
        self.mode      = self.MODE_DEVELOPER
        self.selected_sid = sid
        if sid != prev_sid:          # clear graph when switching students
            self.yaw_history.clear()
            self.baseline_history.clear()

    def set_overview(self):
        """Return to overview mode and close the dev panel window."""
        self.mode = self.MODE_OVERVIEW
        try:
            cv2.destroyWindow(self._WIN_TITLE)
        except Exception:
            pass

    def push_graph_sample(self, student: "StudentPipeline"):
        """Called once per frame to feed the UI-only graph buffer."""
        self.yaw_history.append(student.yaw)
        self.baseline_history.append(
            student.calib.baseline if student.calib.is_calibrated else student.calib.mean
        )



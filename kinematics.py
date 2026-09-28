"""
kinematics.py — KinematicAnalyzer

Split out of module_headpose_multi_web.py during the multi-file refactor.
Code below is moved verbatim from the original module.
"""

import numpy as np
from collections import deque

from config import Config

# ═══════════════════════════════════════════════════════════════════
# KINEMATIC ANALYZER — Rolling-window velocity + acceleration
# ═══════════════════════════════════════════════════════════════════


class KinematicAnalyzer:
    """
    Uses a rolling window of (yaw, time) pairs to compute:
    - velocity  = mean of finite differences over window
    - acceleration = change in velocity over window

    Frame-diff velocity is O(noise). A rolling mean is much more stable.
    """

    def __init__(self):
        self._yaw = deque(maxlen=Config.VELOCITY_WINDOW)
        self._time = deque(maxlen=Config.VELOCITY_WINDOW)
        self.velocity = 0.0
        self.acceleration = 0.0
        self._prev_vel = 0.0
        self.max_velocity = 0.0

    def update(self, yaw: float, t: float):
        self._yaw.append(yaw)
        self._time.append(t)

        if len(self._yaw) >= 3:
            # Compute all finite differences in the window
            diffs = []
            for i in range(1, len(self._yaw)):
                dt = self._time[i] - self._time[i - 1]
                if dt > 1e-4:
                    diffs.append(abs((self._yaw[i] - self._yaw[i - 1]) / dt))
            if diffs:
                self.velocity = float(np.mean(diffs))
                dt_total = self._time[-1] - self._time[0]
                if dt_total > 1e-4:
                    self.acceleration = abs(self.velocity - self._prev_vel) / dt_total
                self._prev_vel = self.velocity
                self.max_velocity = max(self.max_velocity, self.velocity)

    @property
    def velocity_norm(self) -> float:
        return min(self.velocity / Config.VELOCITY_CAP, 1.0)

    @property
    def accel_norm(self) -> float:
        return min(self.acceleration / Config.ACCEL_CAP, 1.0)



"""
kalman_filter.py — KalmanFilter1D

Split out of module_headpose_multi_web.py during the multi-file refactor.
Code below is moved verbatim from the original module.
"""

import numpy as np
from typing import Tuple

# ═══════════════════════════════════════════════════════════════════
# KALMAN FILTER — 1D, tracks position + velocity
# ═══════════════════════════════════════════════════════════════════


class KalmanFilter1D:
    """
    State vector: [yaw_position, yaw_velocity]
    Gives us smooth yaw AND velocity as a byproduct of filtering —
    no need for a separate noisy frame-diff calculation.
    """

    def __init__(self, process_noise: float = 0.8, measurement_noise: float = 8.0):
        self.Q = process_noise  # Process noise covariance
        self.R = measurement_noise  # Measurement noise covariance
        self.x = np.array([0.0, 0.0])  # State: [position, velocity]
        self.P = np.eye(2) * 10.0  # Error covariance (start uncertain)
        self.initialized = False

    def update(self, measurement: float, dt: float) -> Tuple[float, float]:
        dt = max(dt, 0.001)
        F = np.array([[1.0, dt], [0.0, 1.0]])  # State transition
        H = np.array([[1.0, 0.0]])  # Measurement matrix
        Q = np.array(
            [
                [self.Q * dt**3 / 3, self.Q * dt**2 / 2],
                [self.Q * dt**2 / 2, self.Q * dt],
            ]
        )

        if not self.initialized:
            self.x[0] = measurement
            self.initialized = True
            return float(self.x[0]), float(self.x[1])

        # Predict
        x_pred = F @ self.x
        P_pred = F @ self.P @ F.T + Q

        # Update (Kalman gain)
        S = H @ P_pred @ H.T + self.R
        K = (P_pred @ H.T) / S[0, 0]
        innovation = float(measurement - (H @ x_pred)[0])
        self.x = x_pred + K.flatten() * innovation
        self.P = (np.eye(2) - np.outer(K, H)) @ P_pred

        return float(self.x[0]), float(self.x[1])



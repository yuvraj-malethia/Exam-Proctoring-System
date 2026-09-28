"""
calibration.py — CalibrationEngine

Split out of module_headpose_multi_web.py during the multi-file refactor.
Code below is moved verbatim from the original module.
"""

import math
from typing import Tuple

from config import Config

# ═══════════════════════════════════════════════════════════════════
# CALIBRATION ENGINE — Welford online mean/variance
# ═══════════════════════════════════════════════════════════════════


class CalibrationEngine:
    """
    Phase 1 (Hard calibration): Welford's algorithm — numerically stable,
    single-pass, no sample storage. Runs for CALIBRATION_FRAMES frames.

    Phase 2 (Adaptive drift): EMA update of baseline, only when the
    student is clearly looking forward (z < 1.0). This way the baseline
    slowly adapts to natural posture shifts without being gamed.
    """

    def __init__(self, freeze_after_calibration: bool = None):
        self.n = 0
        self.mean = 0.0
        self.M2 = 0.0  # Running sum of squared deviations
        self.std = Config.MIN_STD_DEG
        self.is_calibrated = False
        self.baseline = 0.0  # Adaptive mean (post-calibration)
        # Baseline at the moment calibration completed; never updated
        # afterwards. Added Sep 2026 and READ-ONLY: nothing in scoring
        # uses it. The 3D view draws "straight ahead" from it, because
        # .baseline itself moves after calibration (slowly through
        # adapt(), or — with the freeze below switched off — as the
        # running mean of every sample since the student sat down).
        self.initial_baseline = None
        # Whether feed() stops once calibration completes. None means
        # Config.CALIBRATION_FREEZE_ENABLED (the yaw setting); the pitch
        # pipeline passes Config.PITCH_CALIBRATION_FREEZE_ENABLED instead.
        # Read once here, so flip the Config value BEFORE creating a
        # StudentPipeline when comparing old and new behaviour.
        self.freeze_after_calibration = (
            Config.CALIBRATION_FREEZE_ENABLED
            if freeze_after_calibration is None else freeze_after_calibration)

    def feed(self, yaw: float) -> bool:
        """Feed a yaw sample during calibration. Returns True when done.

        Frozen (the default for yaw since Sep 2026): once calibrated this
        does nothing, so the baseline changes only through adapt(), as
        the class docstring always described. Not frozen: every call
        keeps updating mean and std and resets .baseline to the running
        mean of all samples — the original behaviour, which let a
        student's own repeated looks drag the baseline toward them. See
        Config.CALIBRATION_FREEZE_ENABLED for the measured difference.
        """
        if self.is_calibrated and self.freeze_after_calibration:
            return True
        self.n += 1
        delta = yaw - self.mean
        self.mean += delta / self.n
        delta2 = yaw - self.mean
        self.M2 += delta * delta2

        if self.n >= Config.CALIBRATION_FRAMES:
            variance = self.M2 / (self.n - 1) if self.n > 1 else 1.0
            raw_std = math.sqrt(variance)
            self.std = max(Config.MIN_STD_DEG, min(raw_std, Config.MAX_STD_DEG))
            if not self.is_calibrated:
                self.initial_baseline = self.mean
            self.baseline = self.mean
            self.is_calibrated = True

        return self.is_calibrated

    def adapt(self, yaw: float, z: float):
        """Slowly drift baseline — only when clearly forward.
        Was z < 1.0 → changed to z < 0.5 so the baseline does NOT
        drift when student is even slightly away. Prevents the system
        from slowly 'accepting' a turned head as normal."""
        if z < 0.5:
            self.baseline = (
                1 - Config.ADAPTIVE_ALPHA
            ) * self.baseline + Config.ADAPTIVE_ALPHA * yaw

    def z_score(self, yaw: float) -> float:
        """Signed z-score: positive = right of baseline, negative = left."""
        return (yaw - self.baseline) / self.std

    def is_away(self, yaw: float) -> Tuple[bool, str]:
        """True if yaw exceeds the adaptive threshold."""
        z = self.z_score(yaw)
        if abs(z) > Config.K_SIGMA:
            return True, ("Right" if z > 0 else "Left")
        return False, "Forward"

    @property
    def progress(self) -> float:
        return min(self.n / Config.CALIBRATION_FRAMES, 1.0)



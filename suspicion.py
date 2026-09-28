"""
suspicion.py — SuspicionState, SuspicionFusion

Split out of module_headpose_multi_web.py during the multi-file refactor.
Code below is moved verbatim from the original module.
"""

from dataclasses import dataclass
from typing import Tuple

from config import Config

# ═══════════════════════════════════════════════════════════════════
# SUSPICION FUSION — Weighted multi-signal EMA scoring
# ═══════════════════════════════════════════════════════════════════


@dataclass
class SuspicionState:
    probability: float = 0.0
    max_probability: float = 0.0
    confirmed_secs: float = 0.0  # Time above HIGH threshold
    # Component breakdown (for dashboard bars)
    pos_component: float = 0.0
    vel_component: float = 0.0
    freq_component: float = 0.0
    dur_component: float = 0.0
    session_component: float = 0.0  # Cumulative memory score


class SuspicionFusion:
    """
    Fuses 4 normalized (0–1) signals into a single 0–100% probability.

    Formula:
        raw = W_pos * pos + W_vel * vel + W_freq * freq + W_dur * dur
        prob = EMA(raw * 100, alpha)

    EMA smoothing prevents the score from flickering with every frame.
    Confirmed seconds accumulate only when probability > HIGH threshold.
    """

    def __init__(self):
        self.state = SuspicionState()
        self._smoothed = 0.0

    def update(
        self,
        pos_score: float,
        vel_score: float,
        freq_score: float,
        dur_score: float,
        session_score: float,
        dt: float,
    ) -> SuspicionState:

        raw = (
            Config.W_POSITION * pos_score
            + Config.W_VELOCITY * vel_score
            + Config.W_FREQUENCY * freq_score
            + Config.W_DURATION * dur_score
            + Config.W_SESSION * session_score
        ) * 100.0

        # EMA
        a = Config.SUSPICION_EMA_ALPHA
        self._smoothed = a * raw + (1 - a) * self._smoothed
        prob = max(0.0, min(self._smoothed, 100.0))

        # Accumulate confirmed time
        if prob > Config.CONFIRMED_THRESHOLD:
            self.state.confirmed_secs += dt

        # Store
        self.state.probability = prob
        self.state.max_probability = max(self.state.max_probability, prob)
        self.state.pos_component = pos_score * 100
        self.state.vel_component = vel_score * 100
        self.state.freq_component = freq_score * 100
        self.state.dur_component = dur_score * 100
        self.state.session_component = session_score * 100

        return self.state

    @staticmethod
    def get_level(prob: float) -> Tuple[str, Tuple[int, int, int]]:
        if prob >= Config.LEVEL_HIGH:
            return "HIGH RISK", (0, 0, 255)
        elif prob >= Config.LEVEL_MODERATE:
            return "MODERATE", (0, 100, 255)
        elif prob >= Config.LEVEL_MILD:
            return "MILD", (0, 200, 200)
        elif prob >= Config.LEVEL_LOW:
            return "LOW", (0, 220, 100)
        else:
            return "NORMAL", (0, 255, 60)



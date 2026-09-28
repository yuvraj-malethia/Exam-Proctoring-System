"""
temporal.py — DeviationEvent, TemporalPatternEngine

Split out of module_headpose_multi_web.py during the multi-file refactor.
Code below is moved verbatim from the original module.
"""

import time
import math
import numpy as np
from dataclasses import dataclass
from typing import List, Optional

import clock
from config import Config

# ═══════════════════════════════════════════════════════════════════
# TEMPORAL PATTERN ENGINE — Rolling window behavioral statistics
# ═══════════════════════════════════════════════════════════════════


@dataclass
class DeviationEvent:
    timestamp: float
    duration: float
    direction: str
    z_score: float


class TemporalPatternEngine:
    """
    Tracks deviation events in a rolling time window and computes:
    - frequency_score : events per minute, normalized
    - away_ratio      : fraction of session time spent looking away
    - duration_score  : average event duration, normalized

    Single events are noise. Patterns are signal.
    """

    def __init__(self):
        self.events: List[DeviationEvent] = []
        self._away_start: Optional[float] = None
        self._away_dir: str = "Forward"
        self._away_z: float = 0.0
        self.total_away_s: float = 0.0
        # clock.now() rather than time.time(): identical with a live camera,
        # but during a video replay it is the footage's own time, so the
        # away ratio (and the verdict built on it) does not depend on how
        # fast the clip happens to be analysed. See clock.py.
        self._session_start = clock.now()

    # ── State transitions ──────────────────────────────────────────

    def mark_away(self, t: float, direction: str, z: float):
        if self._away_start is None:
            self._away_start = t
            self._away_dir = direction
            self._away_z = z
        else:
            # Update direction/z if still away
            self._away_z = max(self._away_z, z)

    def mark_forward(self, t: float):
        if self._away_start is not None:
            dur = t - self._away_start
            if dur >= Config.MIN_EVENT_DURATION:
                self.events.append(
                    DeviationEvent(
                        timestamp=t,
                        duration=dur,
                        direction=self._away_dir,
                        z_score=self._away_z,
                    )
                )
                self.total_away_s += dur
            self._away_start = None

    def is_currently_away(self) -> bool:
        return self._away_start is not None

    # ── Scoring ───────────────────────────────────────────────────

    def _recent(self, t: float) -> List[DeviationEvent]:
        cutoff = t - Config.TEMPORAL_WINDOW_SEC
        return [e for e in self.events if e.timestamp > cutoff]

    def frequency_score(self, t: float) -> float:
        """
        First event is free — only repeated looks build score.
        0=0%, 1=0%, 2=25%, 3=50%, 5+=100%.
        """
        n = len(self._recent(t))
        return min(max(n - 1, 0) / 4.0, 1.0)

    def away_ratio(self, t: float) -> float:
        """
        NON-LINEAR away ratio score.
        Old: 20% away time → 20% score (linear, too forgiving)
        New: 20% away time → 67% score using x^0.5 curve
             This means spending 1/5 of exam looking away is already HIGH evidence.
        Thresholds that feel right:
          5% away  → 32% score  (minor)
          15% away → 58% score  (concerning)
          25% away → 75% score  (high)
          40%+ away → 95%+ score (extreme)
        """
        total = t - self._session_start
        if total < 1.0:
            return 0.0
        away = self.total_away_s
        if self._away_start:
            away += t - self._away_start
        raw_ratio = min(away / total, 1.0)
        # Square-root curve: score = sqrt(raw_ratio / 0.40)
        # At 40% away time → score = 1.0 (100%)
        return min(math.sqrt(raw_ratio / 0.40), 1.0)

    def session_score(self, t: float) -> float:
        """
        SESSION MEMORY — long-term behavioural memory with decay.

        Purpose (unchanged): the other signals are all short-window, so
        after 2-3 minutes of looking straight they all fall back to
        zero. Session memory is what stops the system from completely
        forgetting that a student was repeatedly looking away earlier.

        WHAT CHANGED (mentor review, Aug 2026):
        This score used to be permanent — every event counted forever at
        full strength. In a 3-hour exam that meant a student who looked
        away a handful of times in the first 10 minutes was pinned at
        100% session memory for the remaining 2h50m regardless of how
        they behaved. Since W_SESSION (0.30) is the heaviest fusion
        weight, that one saturated signal then dominated the verdict for
        the whole exam.

        Now each event's contribution is weighted by its AGE using
        exponential decay:

            contribution = min(duration / 4.0, 1.0) * 0.5 ** (age / halflife)
            score        = sum(contributions) / SESSION_MEMORY_DIVISOR

        So a 4-second event counts fully right now, half as much one
        half-life later, a quarter after two, and so on. Behaviour from
        an hour ago is effectively forgotten (~3%), while behaviour from
        the last few minutes still counts nearly in full.

        Chosen over a hard periodic reset deliberately: every other
        signal here already decays smoothly (see SUSPICION_EMA_ALPHA),
        and a scheduled reset is gameable — misbehave right after one
        and you get a clean window. Decay has no exploitable boundary.

        CRITICAL: only the SCORE decays. self.events is never pruned —
        the complete event history stays available for evidence, audit,
        and the session report. Use total_event_contribution() for the
        undecayed lifetime figure.
        """
        if not self.events:
            return 0.0

        if not Config.SESSION_MEMORY_DECAY_ENABLED:
            # Legacy permanent-memory behaviour, kept switchable so the
            # before/after difference can be demonstrated directly.
            total_score = sum(min(e.duration / 4.0, 1.0) for e in self.events)
            return min(total_score / Config.SESSION_MEMORY_DIVISOR, 1.0)

        halflife = max(Config.SESSION_MEMORY_HALFLIFE_SEC, 1e-6)
        total_score = 0.0
        for e in self.events:
            age = max(t - e.timestamp, 0.0)
            weight = 0.5 ** (age / halflife)
            total_score += min(e.duration / 4.0, 1.0) * weight
        return min(total_score / Config.SESSION_MEMORY_DIVISOR, 1.0)

    def total_event_contribution(self) -> float:
        """
        Undecayed lifetime event contribution — the same sum
        session_score() used to return before decay was introduced.

        Not used in any scoring decision. Exposed purely so the report
        and dashboard can show "this is what the student accumulated
        across the whole exam" alongside the decayed live score, so no
        history is hidden just because it no longer affects the verdict.
        """
        if not self.events:
            return 0.0
        return sum(min(e.duration / 4.0, 1.0) for e in self.events)

    def session_memory_age_profile(self, t: float) -> dict:
        """
        Diagnostic breakdown of how much of the current session memory
        comes from recent vs older behaviour. Read-only, feeds the
        dashboard/report explanation only — never used in scoring.
        """
        buckets = {"last_5min": 0.0, "5_to_15min": 0.0, "older": 0.0}
        for e in self.events:
            age = max(t - e.timestamp, 0.0)
            contrib = min(e.duration / 4.0, 1.0)
            if age <= 300:
                buckets["last_5min"] += contrib
            elif age <= 900:
                buckets["5_to_15min"] += contrib
            else:
                buckets["older"] += contrib
        return {k: round(v, 3) for k, v in buckets.items()}

    def duration_score(self, t: float) -> float:
        """Mean event duration in window, normalized. 6s avg = 1.0"""
        recent = self._recent(t)
        if not recent:
            return 0.0
        avg = float(np.mean([e.duration for e in recent]))
        return min(avg / 6.0, 1.0)

    def recent_count(self, t: float) -> int:
        return len(self._recent(t))

    def total_count(self) -> int:
        return len(self.events)



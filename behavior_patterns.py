"""
behavior_patterns.py — BehaviorPatternEngine

Turns a sequence of deviation events into NAMED, EXPLAINABLE behavioural
patterns.

WHY THIS EXISTS
───────────────
Until now the engine could only say "suspicion 73%". A number tells an
invigilator that something is wrong but not WHAT, and it gives them
nothing to act on or defend. Two students can reach the same score for
completely different reasons — one glancing repeatedly at a neighbour,
one reading something under the desk, one simply restless.

Cheating has a SHAPE in time, and so does innocence. This module reads
the event history the engine already produces and recognises those
shapes, each with a plain-English description a teacher can understand
without knowing anything about z-scores.

Crucially it can also EXONERATE: the "restless" pattern exists
specifically to say "lots of movement, but undirected and brief — this
does not look like cheating." A system that can only ever accuse is a
worse system, and an invigilator who is told why something is probably
innocent will trust the alerts that matter more.

READ-ONLY
─────────
Nothing here writes to any pipeline, changes any score, or influences
any threshold. It reads completed events and reports what it sees. The
suspicion score is computed exactly as before; these patterns explain
it rather than alter it.
"""

import math

from config import Config


# Pattern keys are stable identifiers; labels are what humans read.
BURST = "burst"
SUSTAINED = "sustained"
DIRECTIONAL_LOCK = "directional_lock"
DOWNWARD = "downward"
RESTLESS = "restless"
METRONOMIC = "metronomic"
ESCALATING = "escalating"


class BehaviorPatternEngine:
    """
    Stateless analyser. Call analyze(student, t) as often as you like —
    it derives everything from the event history each time, so it cannot
    drift out of sync with the pipeline it is describing.
    """

    def analyze(self, student, t: float) -> list:
        """
        Returns a list of detected patterns, highest confidence first.
        Each entry:
            key         stable identifier
            label       short human label ("Answer-checking pattern")
            confidence  0.0-1.0, how strongly the evidence fits
            severity    "suspicious" | "neutral" | "exonerating"
            description one plain-English sentence for a teacher
            evidence    the numbers behind it, for the report
        """
        if not student.calib.is_calibrated:
            return []

        events = list(student.temporal.events)
        window = Config.PATTERN_WINDOW_SEC
        recent = [e for e in events if (t - e.timestamp) <= window]

        if len(events) < Config.PATTERN_MIN_EVENTS:
            return []

        found = []
        for detector in (
            self._burst, self._sustained, self._directional_lock,
            self._downward, self._metronomic, self._escalating,
            self._restless,
        ):
            try:
                res = detector(student, events, recent, t, window)
            except Exception:
                res = None  # a detector must never break the pipeline
            if res:
                found.append(res)

        found.sort(key=lambda p: -p["confidence"])
        return self._resolve_conflicts(found)

    @staticmethod
    def _resolve_conflicts(found: list) -> list:
        """
        Some patterns describe the same events from different angles, and
        reporting both misleads. Where a benign explanation already
        accounts for the evidence, the accusatory one is dropped.

        Specifically: if the behaviour is scattered, brief and
        undirected (restless), then "regular intervals" is just the
        incidental timing of fidgeting, not evidence of working through
        a list. Testing showed an innocent restless student being
        headlined with suspicious labels that outranked their own
        exoneration — the opposite of what should happen.
        """
        keys = {p["key"] for p in found}
        if RESTLESS in keys:
            found = [p for p in found if p["key"] != METRONOMIC]
            # With nothing suspicious left standing, the exonerating
            # finding is the whole story and should read that way.
            if not any(p["severity"] == "suspicious" for p in found):
                for p in found:
                    if p["key"] == RESTLESS:
                        p["confidence"] = round(min(p["confidence"] * 1.5, 1.0), 3)
        return found

    # ── individual patterns ────────────────────────────────────────

    def _burst(self, student, events, recent, t, window):
        """
        Several SHORT looks packed close together, MOSTLY IN ONE
        DIRECTION. Classic answer-checking: glance, read a word, come
        back, write, glance again.

        The direction requirement matters for fairness. Without it, an
        ordinary restless student producing scattered brief looks was
        being reported as "answer-checking" — testing showed exactly
        that, and it outranked the exonerating pattern. Checking a
        specific thing means looking at a specific place; movement in
        all directions is fidgeting, which _restless handles instead.
        """
        short = [e for e in recent if e.duration <= Config.PATTERN_SHORT_EVENT_SEC]
        if len(short) < 3:
            return None
        counts = {}
        for e in short:
            counts[e.direction] = counts.get(e.direction, 0) + 1
        dom_dir, dom_n = max(counts.items(), key=lambda kv: kv[1])
        dom_share = dom_n / len(short)
        if dom_share < Config.PATTERN_BURST_MIN_DIRECTION_SHARE:
            return None  # scattered — restlessness, not targeted checking
        span = max(e.timestamp for e in short) - min(e.timestamp for e in short)
        if span <= 0:
            return None
        rate = len(short) / max(span, 1.0)  # events per second
        if rate < (3.0 / window):
            return None
        conf = min(len(short) / 6.0, 1.0) * min(rate / (6.0 / window), 1.0) * dom_share
        if conf < 0.25:
            return None
        return {
            "key": BURST, "label": "Answer-checking pattern",
            "confidence": round(conf, 3), "severity": "suspicious",
            "description": (
                f"{len(short)} brief looks away within {span:.0f} seconds, each "
                f"under {Config.PATTERN_SHORT_EVENT_SEC:.0f}s and mostly "
                f"{dom_dir.lower()} — the short, repeated, targeted rhythm "
                f"typical of checking an answer rather than reading."
            ),
            "evidence": {"short_events": len(short), "span_sec": round(span, 1),
                          "events_per_min": round(rate * 60, 1),
                          "dominant_direction": dom_dir,
                          "direction_share": round(dom_share, 2)},
        }

    def _sustained(self, student, events, recent, t, window):
        """
        One long, continuous look away. Reading takes time; a glance
        does not. Duration alone is the signal here.
        """
        longest = max(events, key=lambda e: e.duration)
        if longest.duration < Config.PATTERN_LONG_EVENT_SEC:
            return None
        conf = min(longest.duration / (Config.PATTERN_LONG_EVENT_SEC * 2.5), 1.0)
        return {
            "key": SUSTAINED, "label": "Prolonged reference pattern",
            "confidence": round(conf, 3), "severity": "suspicious",
            "description": (
                f"A single continuous look away lasting {longest.duration:.1f} "
                f"seconds ({longest.direction.lower()}) — long enough to read "
                f"from something rather than simply glance."
            ),
            "evidence": {"longest_event_sec": round(longest.duration, 2),
                          "direction": longest.direction},
        }

    def _directional_lock(self, student, events, recent, t, window):
        """
        Repeated looks in the SAME direction. A fixed external reference
        — a neighbour's paper, a note on the wall — produces a
        consistent bearing. Genuine restlessness does not.
        """
        pool = recent if len(recent) >= 3 else events[-6:]
        sideways = [e for e in pool if e.direction in ("Left", "Right")]
        if len(sideways) < 3:
            return None
        counts = {}
        for e in sideways:
            counts[e.direction] = counts.get(e.direction, 0) + 1
        top_dir, top_n = max(counts.items(), key=lambda kv: kv[1])
        share = top_n / len(sideways)
        if share < 0.75 or top_n < 3:
            return None
        conf = min(top_n / 5.0, 1.0) * share
        return {
            "key": DIRECTIONAL_LOCK, "label": "Consistent-direction pattern",
            "confidence": round(conf, 3), "severity": "suspicious",
            "description": (
                f"{top_n} of {len(sideways)} recent looks went the same way "
                f"({top_dir.lower()}) — a consistent bearing suggests a fixed "
                f"reference point rather than random movement."
            ),
            "evidence": {"dominant_direction": top_dir, "same_direction": top_n,
                          "of_sideways": len(sideways), "share": round(share, 2)},
        }

    def _downward(self, student, events, recent, t, window):
        """
        Looking below the desk line. Only detectable at all since pitch
        tracking was added — this behaviour used to be invisible.
        """
        downs = [e for e in events if e.direction == "Down"]
        if not downs:
            return None
        recent_downs = [e for e in recent if e.direction == "Down"]
        total_down = sum(e.duration for e in downs)
        if len(downs) < 2 and total_down < Config.PATTERN_LONG_EVENT_SEC:
            return None
        conf = min(len(downs) / 5.0, 1.0) * 0.5 + min(total_down / 20.0, 1.0) * 0.5
        return {
            "key": DOWNWARD, "label": "Below-desk-line pattern",
            "confidence": round(conf, 3), "severity": "suspicious",
            "description": (
                f"{len(downs)} downward looks totalling {total_down:.1f} seconds, "
                f"beyond this student's own normal reading angle — consistent "
                f"with material held below the desk, such as a phone."
            ),
            "evidence": {"down_events": len(downs),
                          "recent_down_events": len(recent_downs),
                          "total_down_sec": round(total_down, 1)},
        }

    def _metronomic(self, student, events, recent, t, window):
        """
        Suspiciously REGULAR spacing. Human restlessness is irregular;
        working through a list of copied answers tends not to be.
        """
        pool = recent if len(recent) >= 4 else events[-8:]
        if len(pool) < 4:
            return None
        times = sorted(e.timestamp for e in pool)
        gaps = [b - a for a, b in zip(times, times[1:])]
        if len(gaps) < 3:
            return None
        mean_gap = sum(gaps) / len(gaps)
        if mean_gap <= 0.5:
            return None
        var = sum((g - mean_gap) ** 2 for g in gaps) / len(gaps)
        cv = math.sqrt(var) / mean_gap  # coefficient of variation
        if cv > Config.PATTERN_METRONOME_CV:
            return None
        conf = min((Config.PATTERN_METRONOME_CV - cv) /
                   Config.PATTERN_METRONOME_CV, 1.0) * min(len(gaps) / 5.0, 1.0)
        if conf < 0.25:
            return None
        return {
            "key": METRONOMIC, "label": "Regular-interval pattern",
            "confidence": round(conf, 3), "severity": "suspicious",
            "description": (
                f"{len(pool)} looks away spaced almost evenly, about every "
                f"{mean_gap:.1f} seconds — an unusually regular rhythm, more "
                f"like working through a list than natural restlessness."
            ),
            "evidence": {"events": len(pool), "mean_gap_sec": round(mean_gap, 1),
                          "regularity_cv": round(cv, 3)},
        }

    def _escalating(self, student, events, recent, t, window):
        """
        Rate increasing over the session. Often seen as time pressure
        builds near the end of an exam.
        """
        if len(events) < 6:
            return None
        span = events[-1].timestamp - events[0].timestamp
        if span < window:
            return None
        mid = events[0].timestamp + span / 2.0
        first = [e for e in events if e.timestamp < mid]
        second = [e for e in events if e.timestamp >= mid]
        if len(first) < 2 or len(second) < 2:
            return None
        r1 = len(first) / (span / 2.0)
        r2 = len(second) / (span / 2.0)
        if r2 < r1 * Config.PATTERN_ESCALATION_RATIO:
            return None
        conf = min((r2 / max(r1, 1e-6)) / (Config.PATTERN_ESCALATION_RATIO * 2), 1.0)
        return {
            "key": ESCALATING, "label": "Escalating pattern",
            "confidence": round(conf, 3), "severity": "suspicious",
            "description": (
                f"Looking away has become more frequent as the session has gone "
                f"on — {len(second)} events in the second half against "
                f"{len(first)} in the first."
            ),
            "evidence": {"first_half_events": len(first),
                          "second_half_events": len(second),
                          "rate_increase": round(r2 / max(r1, 1e-6), 2)},
        }

    def _restless(self, student, events, recent, t, window):
        """
        EXONERATING. Frequent but brief, undirected, scattered movement.

        This pattern exists deliberately. A system that can only accuse
        is worse than one that can also say "this looks like an ordinary
        fidgety student, not a cheat" — and an invigilator who sees the
        system correctly clear someone will trust its real alerts more.
        """
        if len(recent) < 4:
            return None
        durations = [e.duration for e in recent]
        if max(durations) > Config.PATTERN_SHORT_EVENT_SEC:
            return None
        dirs = {e.direction for e in recent}
        if len(dirs) < 2:
            return None  # a single direction is a lock, not restlessness
        # Scattered means no direction dominates.
        counts = {}
        for e in recent:
            counts[e.direction] = counts.get(e.direction, 0) + 1
        share = max(counts.values()) / len(recent)
        if share > 0.6:
            return None
        conf = min(len(recent) / 6.0, 1.0) * (1.0 - share)
        if conf < 0.2:
            return None
        return {
            "key": RESTLESS, "label": "Restless but undirected",
            "confidence": round(conf, 3), "severity": "exonerating",
            "description": (
                f"{len(recent)} short looks spread across {len(dirs)} different "
                f"directions, none dominant and none longer than "
                f"{Config.PATTERN_SHORT_EVENT_SEC:.0f}s — this resembles ordinary "
                f"restlessness rather than referencing anything."
            ),
            "evidence": {"events": len(recent), "directions": sorted(dirs),
                          "max_direction_share": round(share, 2),
                          "longest_sec": round(max(durations), 2)},
        }

    # ── summary helper ─────────────────────────────────────────────

    @staticmethod
    def headline(patterns: list):
        """
        The single pattern most worth showing on a crowded dashboard
        card. Prefers the strongest suspicious pattern; falls back to an
        exonerating one so a cleared student still gets an explanation
        rather than a blank space.
        """
        if not patterns:
            return None
        susp = [p for p in patterns if p["severity"] == "suspicious"]
        if susp:
            return susp[0]
        return patterns[0]

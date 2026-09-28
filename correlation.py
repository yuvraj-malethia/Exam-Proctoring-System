"""
correlation.py — CrossStudentCorrelator

Looks for RELATIONSHIPS between students, rather than analysing each one
in isolation.

WHY THIS MATTERS
────────────────
Every signal in this system so far scores one student on their own
behaviour. But exam cheating is usually social — it involves two people,
and the giveaway is often not what either of them does but the fact that
they do it TOGETHER. Two students each glancing sideways a few times is
unremarkable. The same two students glancing TOWARDS EACH OTHER, within
a second of each other, repeatedly, is a different thing entirely — and
no amount of per-student scoring can see it, because neither student's
own record looks unusual.

This is the part of the system that is genuinely novel rather than
merely well-engineered: most proctoring tools treat each subject
independently and therefore cannot detect collaboration at all.

THE GEOMETRY THAT MAKES IT WORK
───────────────────────────────
Who sits to the left of whom comes from each student's real position in
the (mirrored) camera frame, passed in by the main loop as seat_x. Until
Sep 2026 this simply assumed slot 0 was leftmost — which the identity
tracker only guarantees for students first detected in the same frame
(see analyze()). So for two students A and B where A sits to the LEFT of B:

    A looking Right  →  towards B
    B looking Left   →  towards A

A pair of those, close together in time, is a mutual glance. The
direction check is what separates "both happened to look away" from
"they looked at each other", and it is why this cannot be done without
knowing the seating order.

WHAT IT DELIBERATELY DOES NOT DO
────────────────────────────────
It produces no accusation and changes no student's suspicion score. It
reports a correlation, a confidence, and the evidence behind it, for a
human to interpret — exactly like the behavioural patterns. Correlation
is not proof: two friends may simply be distracted by the same noise,
and a report that forgets this would be dangerous.
"""

import math

from config import Config


def _nm(student) -> str:
    """
    Human-facing name for a student, falling back to the seat label.
    Correlation findings name two specific people and go straight into
    the report, so they must read as people rather than seat numbers.
    """
    return getattr(student, "display_name", None) or getattr(student, "label", "?")


MUTUAL_GLANCE = "mutual_glance"
SYNCHRONISED = "synchronised"
FOLLOWER = "follower"

# How INFORMATIVE each finding is, independent of how confident it is.
# A vague finding is often easier to be confident about than a precise
# one — "they looked away at the same moment" is arithmetically simpler
# than "they looked at each other" — so ranking by confidence alone
# would systematically discard the better evidence. Specificity decides
# first; confidence only breaks ties within the same kind.
_SPECIFICITY = {
    MUTUAL_GLANCE: 3,   # directed at each other — the strongest claim
    FOLLOWER: 2,        # one-way, consistent lag
    SYNCHRONISED: 1,    # merely simultaneous
}


class CrossStudentCorrelator:
    """
    Stateless pairwise analyser. Reads completed events from each
    student's temporal engine and reports relationships. Never writes.
    """

    def analyze(self, students: dict, active_ids: list, t: float,
                seat_x: dict = None) -> list:
        """
        students   : {slot_id: StudentPipeline}
        active_ids : slots currently being monitored
        seat_x     : optional {slot_id: last face-centre x in the mirrored
                     frame} from the identity tracker. When known for both
                     students of a pair, it decides who sits on the left.
                     Without it the lower slot id is assumed to be on the
                     left (the original behaviour).
        Returns a list of findings, strongest first. Empty is the normal,
        expected result — this should be quiet unless something real is
        happening.

        WHY seat_x (Sep 2026): slot numbers follow left-to-right seating
        only for students first detected in the same frame. The identity
        tracker gives anyone who sits down later whichever slot is free,
        so slot 0 can easily be the student on the RIGHT — and then "A
        looked Right, B looked Left" means they looked AWAY from each
        other, not towards. Reading positions instead of slot numbers stops
        that from ever being reported as mutual glancing.
        """
        window = Config.CORRELATION_WINDOW_SEC
        # Only consider students with enough history to say anything.
        usable = []
        for sid in sorted(students.keys()):
            s = students[sid]
            if not s.calib.is_calibrated:
                continue
            evs = [e for e in s.temporal.events if (t - e.timestamp) <= window]
            if len(evs) >= Config.CORRELATION_MIN_EVENTS:
                usable.append((sid, s, evs))

        findings = []
        seat_x = seat_x or {}
        for i in range(len(usable)):
            for j in range(i + 1, len(usable)):
                a, b = usable[i], usable[j]
                # The detectors assume A sits to the LEFT of B. Known
                # positions win over slot order (see docstring above).
                xa, xb = seat_x.get(a[0]), seat_x.get(b[0])
                if xa is not None and xb is not None and xb < xa:
                    a, b = b, a
                sid_a, sa, ea = a
                sid_b, sb, eb = b
                for detector in (self._mutual_glance, self._synchronised,
                                  self._follower):
                    try:
                        res = detector(sid_a, sa, ea, sid_b, sb, eb, t)
                    except Exception:
                        res = None
                    if res:
                        findings.append(res)

        findings = self._dedupe_pairs(findings)
        findings.sort(key=lambda f: (-_SPECIFICITY.get(f["key"], 0), -f["confidence"]))
        return findings[: Config.CORRELATION_MAX_FINDINGS]

    @staticmethod
    def _dedupe_pairs(findings: list) -> list:
        """
        Keep only the most INFORMATIVE finding per pair of students.

        The detectors overlap by design — a genuine mutual glance also
        registers as synchronised, and often as a short consistent lag
        too. Reporting all three for one pair triples the apparent
        weight of a single piece of evidence, which would mislead an
        invigilator into thinking three separate things were observed.

        Selection is by SPECIFICITY first, not confidence. A vague
        finding can easily score higher than a precise one — "they
        looked away at the same time" is arithmetically easier to be
        certain about than "they looked at each other" — but the precise
        finding is the one worth reading. Keeping the higher number
        would systematically discard the better evidence.
        """
        best = {}
        for f in findings:
            key = tuple(sorted(f["slots"]))
            cur = best.get(key)
            rank = (_SPECIFICITY.get(f["key"], 0), f["confidence"])
            if cur is None or rank > (_SPECIFICITY.get(cur["key"], 0), cur["confidence"]):
                best[key] = f
        return list(best.values())

    # ── detectors ──────────────────────────────────────────────────

    def _mutual_glance(self, sid_a, sa, ea, sid_b, sb, eb, t):
        """
        The strongest signal: they look TOWARDS each other, close in
        time, more than once.

        Slot order gives the geometry — the student with the lower slot
        id sits to the left, so "towards" means Right for them and Left
        for the other. Requiring the correct pair of directions is what
        makes this meaningful rather than coincidental.
        """
        # analyze() always passes the student on the LEFT as A — by real
        # seat position when known, otherwise by slot order.
        toward_b = [e for e in ea if e.direction == "Right"]
        toward_a = [e for e in eb if e.direction == "Left"]
        if not toward_b or not toward_a:
            return None

        tol = Config.CORRELATION_TIME_TOLERANCE_SEC
        pairs = []
        used_b = set()
        for e1 in toward_b:
            best = None
            for k, e2 in enumerate(toward_a):
                if k in used_b:
                    continue
                gap = abs(e1.timestamp - e2.timestamp)
                if gap <= tol and (best is None or gap < best[0]):
                    best = (gap, k, e2)
            if best:
                used_b.add(best[1])
                pairs.append((e1, best[2], best[0]))

        if len(pairs) < Config.CORRELATION_MIN_PAIRS:
            return None

        mean_gap = sum(p[2] for p in pairs) / len(pairs)
        # Confidence rises with how many coincidences there were and how
        # tight they were in time.
        conf = min(len(pairs) / 4.0, 1.0) * (1.0 - min(mean_gap / tol, 1.0) * 0.5)
        return {
            "key": MUTUAL_GLANCE,
            "label": "Possible mutual glancing",
            "pair": [_nm(sa), _nm(sb)],
            "slots": [sid_a, sid_b],
            "confidence": round(conf, 3),
            "severity": "suspicious",
            "description": (
                f"{_nm(sa)} and {_nm(sb)} looked towards each other "
                f"{len(pairs)} times within {mean_gap:.1f}s of one another on "
                f"average. {_nm(sa)} sits to the left of {_nm(sb)}, so these "
                f"were {_nm(sa)} looking right and {_nm(sb)} looking left — "
                f"directed at each other rather than away at random."
            ),
            "evidence": {
                "coincidences": len(pairs),
                "mean_time_gap_sec": round(mean_gap, 2),
                "a_toward_b_events": len(toward_b),
                "b_toward_a_events": len(toward_a),
            },
            "caveat": (
                "Correlation is not proof — students may be reacting to the "
                "same distraction. Requires human review."
            ),
        }

    def _synchronised(self, sid_a, sa, ea, sid_b, sb, eb, t):
        """
        Their looking-away happens at the same moments, whatever the
        direction. Weaker than a mutual glance but still notable: two
        people working off the same source tend to pause together.
        """
        tol = Config.CORRELATION_TIME_TOLERANCE_SEC
        hits = 0
        used = set()
        for e1 in ea:
            for k, e2 in enumerate(eb):
                if k in used:
                    continue
                if abs(e1.timestamp - e2.timestamp) <= tol:
                    used.add(k)
                    hits += 1
                    break
        if hits < Config.CORRELATION_MIN_PAIRS:
            return None
        # Normalise against how often they each look away at all, so two
        # very active students do not register purely by chance.
        expected = (len(ea) * len(eb) * (2 * tol)) / max(
            Config.CORRELATION_WINDOW_SEC, 1.0
        )
        if expected <= 0 or hits <= expected * Config.CORRELATION_CHANCE_MULTIPLE:
            return None  # no more coincidence than chance would give
        conf = min(hits / 5.0, 1.0) * min(hits / max(expected * 3.0, 1e-6), 1.0)
        if conf < 0.2:
            return None
        return {
            "key": SYNCHRONISED,
            "label": "Synchronised activity",
            "pair": [_nm(sa), _nm(sb)],
            "slots": [sid_a, sid_b],
            "confidence": round(conf, 3),
            "severity": "neutral",
            "description": (
                f"{_nm(sa)} and {_nm(sb)} looked away at the same moments "
                f"{hits} times — noticeably more often than their individual "
                f"rates would produce by chance ({expected:.1f} expected). "
                f"Direction was not consistently towards each other."
            ),
            "evidence": {"coincidences": hits,
                          "expected_by_chance": round(expected, 2),
                          "a_events": len(ea), "b_events": len(eb)},
            "caveat": (
                "A shared distraction — a noise, an invigilator walking past — "
                "produces this too. Notable, not incriminating."
            ),
        }

    def _follower(self, sid_a, sa, ea, sid_b, sb, eb, t):
        """
        One student's looks consistently FOLLOW the other's by a
        similar short delay. That asymmetry is what copying looks like:
        one person produces an answer, the other collects it.
        """
        lag_min = Config.CORRELATION_LAG_MIN_SEC
        lag_max = Config.CORRELATION_LAG_MAX_SEC
        for leader, follower, le, fe in (
            (sa, sb, ea, eb), (sb, sa, eb, ea),
        ):
            lags = []
            used = set()
            for e1 in le:
                best = None
                for k, e2 in enumerate(fe):
                    if k in used:
                        continue
                    lag = e2.timestamp - e1.timestamp
                    if lag_min <= lag <= lag_max and (best is None or lag < best[0]):
                        best = (lag, k)
                if best:
                    used.add(best[1])
                    lags.append(best[0])
            if len(lags) < Config.CORRELATION_MIN_PAIRS:
                continue
            mean_lag = sum(lags) / len(lags)
            var = sum((l - mean_lag) ** 2 for l in lags) / len(lags)
            cv = math.sqrt(var) / max(mean_lag, 1e-6)
            # A CONSISTENT delay is the signal; scattered delays are just
            # two people independently looking around.
            if cv > Config.CORRELATION_LAG_CV:
                continue
            conf = min(len(lags) / 4.0, 1.0) * (1.0 - min(cv / Config.CORRELATION_LAG_CV, 1.0) * 0.6)
            if conf < 0.2:
                continue
            return {
                "key": FOLLOWER,
                "label": "Possible one-way copying",
                "pair": [_nm(leader), _nm(follower)],
                "slots": [sid_a, sid_b],
                "confidence": round(conf, 3),
                "severity": "suspicious",
                "description": (
                    f"{_nm(follower)}'s looks away followed {_nm(leader)}'s "
                    f"{len(lags)} times, each after a similar delay of about "
                    f"{mean_lag:.1f}s. A consistent one-way lag is the shape "
                    f"copying takes — one student produces, the other collects."
                ),
                "evidence": {"occurrences": len(lags),
                              "mean_lag_sec": round(mean_lag, 2),
                              "lag_consistency_cv": round(cv, 3),
                              "leader": _nm(leader),
                              "follower": _nm(follower)},
                "caveat": (
                    "A consistent lag can also arise from a shared external "
                    "cue. Requires human review."
                ),
            }
        return None

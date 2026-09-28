"""
narrative.py — SessionNarrator

Turns the numbers this system produces into plain English a teacher can
read, act on, and if necessary defend to a disciplinary committee.

WHY THIS EXISTS
───────────────
Everything upstream ends in figures: 73%, z=4.1, away-ratio 0.58. Those
are meaningless to the person who actually has to make a decision about
a student, and worse than meaningless in a formal setting — "the
computer said 73%" is not a finding anyone can act on or contest.

This module writes what a careful human proctor would write if they had
watched the whole session and taken notes. It states what was observed,
how often, for how long, and what it is and is not consistent with.

DELIBERATELY WRITTEN TO UNDER-CLAIM
───────────────────────────────────
The narrative never says a student cheated. It reports observations and
the patterns they fit, and it says plainly when the evidence is weak,
ambiguous, or actively points the other way. Three reasons:

  1. The system genuinely cannot know. It sees head angles, not intent.
     A confident accusation from a tool that cannot see the desk would
     be dishonest.
  2. An over-claiming report is worse than none in a disciplinary
     setting — one demonstrably wrong assertion discredits everything
     else in the document.
  3. A system that also says "this looks innocent" earns trust for the
     times it says something is worth looking at.

Everything here is generated deterministically from recorded data. No
figure is invented, and no sentence appears without the numbers behind
it being present in the same report.
"""

from config import Config


class SessionNarrator:
    """Generates plain-English prose from recorded session data."""

    # ── per-student ────────────────────────────────────────────────

    def student_summary(self, student, patterns, t, verdict=None) -> str:
        """
        The main paragraph for one student. Reads as observation notes,
        not as a verdict.
        """
        if not student.calib.is_calibrated:
            return (
                f"{_name(student)} was not monitored for long enough to establish "
                f"a personal baseline, so no behavioural assessment can be made. "
                f"Calibration reached "
                f"{int(student.calib.progress * 100)}% of the frames required."
            )

        temporal = student.temporal
        total = temporal.total_count()
        away = temporal.total_away_s
        peak = student.fs.max_probability
        session_len = max(t - temporal._session_start, 1.0)

        if total == 0:
            return (
                f"{_name(student)} was monitored for {_dur(session_len)} and no "
                f"deviation from their established baseline was recorded at any "
                f"point. Nothing in this session warrants attention."
            )

        parts = []

        # 1. What was observed, factually.
        dirs = {}
        for e in temporal.events:
            dirs[e.direction] = dirs.get(e.direction, 0) + 1
        dir_txt = ", ".join(
            f"{n} {d.lower()}" for d, n in
            sorted(dirs.items(), key=lambda kv: -kv[1])
        )
        pct_away = (away / session_len) * 100
        parts.append(
            f"Over {_dur(session_len)} of monitoring, {_name(student)} looked away "
            f"from their baseline position {total} "
            f"time{'s' if total != 1 else ''} ({dir_txt}), totalling "
            f"{away:.1f} seconds, or {pct_away:.1f}% of the session."
        )

        # 2. The most notable single event, if there is one.
        if temporal.events:
            longest = max(temporal.events, key=lambda e: e.duration)
            if longest.duration >= Config.PATTERN_LONG_EVENT_SEC:
                parts.append(
                    f"The longest single deviation lasted {longest.duration:.1f} "
                    f"seconds ({longest.direction.lower()}), which is long enough "
                    f"to read from something rather than merely glance."
                )

        # 3. What the patterns say — the interpretive part.
        susp = [p for p in patterns if p["severity"] == "suspicious"]
        exon = [p for p in patterns if p["severity"] == "exonerating"]
        if susp:
            top = susp[0]
            parts.append(
                f"This behaviour fits a {top['label'].lower()} "
                f"({int(top['confidence'] * 100)}% confidence): "
                f"{top['description']}"
            )
            if len(susp) > 1:
                others = ", ".join(p["label"].lower() for p in susp[1:3])
                n_others = len(susp[1:3])
                parts.append(
                    f"{'A further pattern was' if n_others == 1 else 'Further patterns were'} "
                    f"also recognised: {others}."
                )
        elif exon:
            top = exon[0]
            parts.append(
                f"Despite the activity above, the shape of it does not resemble "
                f"referencing: {top['description']} On the evidence recorded, "
                f"this looks like ordinary restlessness."
            )
        else:
            parts.append(
                "No recognisable behavioural pattern emerged from these events. "
                "Isolated deviations without a repeating shape are weak evidence "
                "on their own."
            )

        # 4. Peak score in context, with an honest reading of what it means.
        parts.append(
            f"Peak suspicion score reached {peak:.0f}%"
            + (f", and the student spent {student.fs.confirmed_secs:.1f} seconds "
               f"above the confirmed-concern threshold."
               if student.fs.confirmed_secs > 0 else
               ", and the score never remained above the confirmed-concern "
               "threshold for a sustained period.")
        )

        # 5. Explicit limits. Always present, never optional.
        parts.append(
            "This system observes head orientation only. It cannot see the desk, "
            "and it cannot establish intent. The above records what was observed "
            "and what it is consistent with, not what occurred."
        )
        return " ".join(parts)

    def recommendation(self, student, patterns, verdict) -> tuple:
        """
        Returns (action, reasoning). Deliberately conservative — the
        strongest recommendation available is "review", never "penalise".
        The decision belongs to a person.
        """
        if not student.calib.is_calibrated:
            return ("No assessment",
                    "Insufficient monitoring time to establish a baseline.")

        susp = [p for p in patterns if p["severity"] == "suspicious"]
        exon = [p for p in patterns if p["severity"] == "exonerating"]
        peak = student.fs.max_probability
        strong = [p for p in susp if p["confidence"] >= 0.6]

        if verdict == "FLAG" or (len(strong) >= 2 and peak >= Config.LEVEL_HIGH):
            return ("Refer for review",
                    f"Multiple independent patterns were recognised "
                    f"({', '.join(p['label'].lower() for p in strong[:3])}) alongside "
                    f"a peak score of {peak:.0f}%. Corroborating signals of this "
                    f"kind merit a human looking at the recorded evidence.")
        if verdict == "ALERT" or strong:
            return ("Worth a closer look",
                    f"One clear pattern was recognised "
                    f"({strong[0]['label'].lower() if strong else 'elevated activity'}) "
                    f"with a peak score of {peak:.0f}%. Not conclusive on its own, "
                    f"but enough to justify reviewing the evidence captured.")
        if exon and not susp:
            return ("No action recommended",
                    f"Activity was recorded, but its shape actively argues against "
                    f"referencing: {exon[0]['label'].lower()}. Flagging this student "
                    f"would most likely be a false positive.")
        if student.temporal.total_count() == 0:
            return ("No action recommended",
                    "No deviation from baseline was recorded at any point.")
        return ("No action recommended",
                f"Activity was recorded but did not form any recognisable pattern, "
                f"and the peak score of {peak:.0f}% stayed within the range produced "
                f"by ordinary exam behaviour.")

    # ── session level ──────────────────────────────────────────────

    def session_overview(self, students, active_ids, t, session_start,
                          verdicts, patterns_by_slot) -> str:
        """The executive summary paragraph at the top of the report."""
        dur = max(t - session_start, 1.0)
        monitored = [s for s in students.values() if s.calib.is_calibrated]
        if not monitored:
            return (
                f"The session ran for {_dur(dur)}, but no student was monitored "
                f"long enough to establish a baseline, so no assessment is "
                f"available."
            )

        flagged = [
            s for s in monitored
            if verdicts.get(s.sid) in ("ALERT", "FLAG")
        ]
        total_events = sum(s.temporal.total_count() for s in monitored)

        lines = [
            f"This session ran for {_dur(dur)} and monitored "
            f"{len(monitored)} student{'s' if len(monitored) != 1 else ''}. "
            f"{total_events} deviation event"
            f"{'s were' if total_events != 1 else ' was'} recorded in total."
        ]

        if not flagged:
            lines.append(
                "No student reached a level warranting review. Activity recorded "
                "during the session was consistent with ordinary exam behaviour."
            )
        else:
            names = ", ".join(_name(s) for s in flagged)
            highest = max(flagged, key=lambda s: s.fs.max_probability)
            lines.append(
                f"{len(flagged)} student{'s' if len(flagged) != 1 else ''} "
                f"({names}) reached a level warranting review. The highest peak "
                f"score was {highest.fs.max_probability:.0f}% "
                f"({_name(highest)})."
            )
            pats = patterns_by_slot.get(highest.sid) or []
            susp = [p for p in pats if p["severity"] == "suspicious"]
            if susp:
                lines.append(
                    f"The strongest single finding was a {susp[0]['label'].lower()} "
                    f"for {_name(highest)}."
                )
        return " ".join(lines)

    def correlation_summary(self, correlations) -> str:
        """Explains the cross-student section, including when it is empty."""
        if not correlations:
            return (
                "No behavioural relationship was detected between any pair of "
                "students. Each student's activity was independent of the others, "
                "which is the expected result."
            )
        susp = [c for c in correlations if c["severity"] == "suspicious"]
        lines = [
            f"{len(correlations)} behavioural relationship"
            f"{'s were' if len(correlations) != 1 else ' was'} detected between "
            f"students. This analysis looks for activity that is correlated "
            f"between two people, which no individual score can reveal."
        ]
        if susp:
            top = susp[0]
            lines.append(
                f"The strongest was between {top['pair'][0]} and {top['pair'][1]}: "
                f"{top['description']}"
            )
        lines.append(
            "Correlation is not proof of collaboration. Students may react to the "
            "same external event. These findings identify pairs worth reviewing, "
            "nothing more."
        )
        return " ".join(lines)

    def methodology_note(self) -> str:
        """Plain-English description of how the system reaches its numbers."""
        return (
            "Each student is monitored individually. During the first "
            f"{Config.CALIBRATION_FRAMES} frames the system learns that student's "
            "own natural head position, so a person who habitually sits at an "
            "angle is not penalised for it. Afterwards, deviation is measured in "
            "standard deviations from that personal baseline rather than against "
            "any fixed rule.\n\n"
            "A suspicion score combines five weighted signals: how far the head "
            "has turned, how fast it moved, how often deviations occur, how long "
            "they last, and a decaying memory of earlier behaviour in the "
            "session. Older behaviour counts progressively less, so a difficult "
            "few minutes early in a long exam does not follow a student for its "
            "entire duration.\n\n"
            "Both left-right head turn and downward tilt are tracked, the latter "
            "specifically so that material held below the desk line is not "
            "invisible to the system.\n\n"
            "Single events are deliberately weak evidence. The system is built "
            "around repeated patterns, because one glance is normal behaviour and "
            "a repeated, directed, timed sequence is not."
        )


def _name(student) -> str:
    """
    The human-facing name for a student: their registered name if the
    invigilator entered one, otherwise the seat label.

    Reports refer to people, not seat positions. "S3 is recommended for
    review" is useless to whoever has to act on it, because nothing in
    the document says who S3 was.
    """
    return getattr(student, "display_name", None) or getattr(student, "label", "?")


def _dur(seconds: float) -> str:
    """Human-readable duration, e.g. '1 hour 12 minutes'."""
    seconds = max(int(seconds), 0)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h} hour{'s' if h != 1 else ''} {m} minute{'s' if m != 1 else ''}"
    if m:
        return f"{m} minute{'s' if m != 1 else ''} {s} second{'s' if s != 1 else ''}"
    return f"{s} second{'s' if s != 1 else ''}"

"""
room_context.py — RoomContext

THE GAP THIS CLOSES: every student in this system is judged against
their own baseline and nothing else. That is the right default — it is
what makes the engine fair to a person who naturally sits slightly
turned — but it leaves one thing invisible. When something happens in
the ROOM, everyone reacts at once. The invigilator announces the time, a
door slams, someone drops a pencil case: four heads come up together,
four position scores rise together, and the engine treats that as four
independent students each deciding to look away.

A human invigilator would not. They would notice that everyone looked up
and think nothing of it, and they would notice the one student who
looked away while nobody else did. That second thing is the interesting
one, and the engine had no way to express it.

WHAT IS MEASURED. Each frame, for every calibrated student in view, how
many of the OTHERS are currently away. Leave-one-out, deliberately: a
student must never be part of the evidence that excuses their own
behaviour, or a single student alone in frame would permanently excuse
themselves.

    room_away(me) = (number of other students away) / (number of others)

With one student in the room there are no others, the fraction is
undefined, and nothing changes at all. This feature only exists when
there is a room to compare against.

WHAT IT DOES WITH IT. One thing, and only to one signal: it damps
`pos_score`, the "how far is this head turned right now" term in the
fusion. That is exactly the term a room-wide event inflates for
everybody simultaneously, and it is the term least able to tell the
difference on its own.

    pos_score *= 1 - (ROOM_DAMP * room_away)

So a glance nobody else shares counts fully. A glance the whole room
shares counts for much less. The other four fusion signals — velocity,
frequency, away-ratio and session memory — are untouched, so a student
who keeps looking away still accumulates, shared moments or not.

WHAT IT DELIBERATELY DOES NOT DO:

  - It does not suppress the event. The deviation is still recorded,
    still appears in the timeline and the log, still counts toward
    frequency and away-ratio. Nothing is hidden from the invigilator;
    only the instantaneous position term is discounted. Hiding data
    would break the rule that this system never deletes, only annotates.
  - It does not look for synchronised cheating. Two students glancing at
    each other at the same moment would damp each other here, which
    sounds wrong until you remember that `correlation.py` exists
    precisely to catch that pattern and reads the same events, which
    this module does not touch.
  - It does not use the suspicion score of other students, only their
    away state. Scores are slow, smoothed and path-dependent; "is this
    person's head turned right now" is immediate and is the thing that
    actually moves together during a room event.

ONE FRAME OF LAG, ON PURPOSE. The engine processes students one at a
time inside a single loop, so the away state of student 4 does not exist
yet when student 1 is being processed. Rather than restructure the loop
into two passes — a real change to the order in which the intelligence
runs — this uses the away states recorded on the PREVIOUS frame. At
25-30 frames per second that is 30-40 milliseconds. A room event lasts
seconds.

OFF BY DEFAULT (`Config.ROOM_CONTEXT_ENABLED`). It changes scores, so
the same rule applies as to every other scoring change in this project:
False reproduces the old behaviour exactly, and turning it on is a
decision made against evidence.

Holds no pipeline state and mutates nothing. It reads away flags and
hands back a number.
"""

from config import Config


class RoomContext:
    """One instance per session. Call `observe()` once per frame, after
    the per-student loop; read `away_fraction(sid)` during the next."""

    def __init__(self, max_students):
        self.max_students = max_students
        self._away = {}        # slot -> was away on the last observed frame
        self._present = set()  # slots calibrated and in view last frame
        self.last_fraction = {}  # slot -> what each student was told, for reporting

    # ── read, during the per-student loop ─────────────────────────────
    def away_fraction(self, sid):
        """How much of the rest of the room was away on the previous
        frame. None when there is nobody else to compare against, which
        is the signal to change nothing."""
        if not Config.ROOM_CONTEXT_ENABLED:
            return None
        others = [s for s in self._present if s != sid]
        if len(others) < Config.ROOM_MIN_OTHERS:
            return None
        frac = sum(1 for s in others if self._away.get(s)) / float(len(others))
        self.last_fraction[sid] = frac
        return frac

    # ── write, once per frame after the loop ──────────────────────────
    def observe(self, students, active_ids):
        """Record who was away this frame, for the next one to read."""
        if not Config.ROOM_CONTEXT_ENABLED:
            return
        present, away = set(), {}
        for sid in active_ids:
            st = students.get(sid)
            if st is None or not st.calib.is_calibrated:
                continue
            present.add(sid)
            # The same question the fusion asks: is this head outside its
            # own normal range right now. Read, never computed here.
            try:
                is_away, _ = st.calib.is_away(st.yaw)
            except Exception:
                is_away = False
            away[sid] = bool(is_away) or bool(getattr(st, "looking_down", False)) \
                or bool(getattr(st, "eyes_aside", False))
        self._present, self._away = present, away

    # ── reporting ─────────────────────────────────────────────────────
    def payload(self, sid):
        """What this student was told, for the dashboard. None when the
        feature is off or there was no room to compare against, so the
        browser can tell those apart from 'nobody else was away'."""
        if not Config.ROOM_CONTEXT_ENABLED:
            return None
        frac = self.last_fraction.get(sid)
        if frac is None:
            return None
        return {
            "away_fraction": round(frac, 3),
            "others": max(len(self._present) - (1 if sid in self._present else 0), 0),
            "shared": frac >= Config.ROOM_SHARED_FRAC,
            "damping": round(Config.ROOM_DAMP * frac, 3),
        }

    def room_payload(self):
        """The room as a whole, for the chart's second line."""
        if not Config.ROOM_CONTEXT_ENABLED:
            return None
        n = len(self._present)
        away = sum(1 for s in self._present if self._away.get(s))
        return {
            "students": n,
            "away": away,
            "away_fraction": round(away / n, 3) if n else 0.0,
            "shared": bool(n >= Config.ROOM_MIN_OTHERS + 1
                           and (away / n) >= Config.ROOM_SHARED_FRAC),
        }

"""
presence.py — PresenceTracker

THE GAP THIS CLOSES: until now the engine had exactly two states for a
student — a face it could measure, or nothing at all. "Nothing at all"
produced no score, no event, no entry in any log and no word to the
invigilator. It was silent.

That silence is not harmless. Measured on Yuvraj's own validation clips
(5 Oct 2026), the clip recorded as "phone held in the lap" spent 29
seconds in ONE unbroken stretch with no measurable face, and 96 of its
129 seconds unmeasured in total. The engine's verdict for that clip was
OK with a peak score of 3.5%, because almost nothing reached the
scoring pipeline at all. A student whose face cannot be seen scored
*better* than one whose face could — the exact wrong way round.

The cause is physical, not a bug: when a head tips far enough down, the
camera sees the top of the skull and there is no face in the image to
measure. No face model fixes that. What CAN be fixed is the silence.

So this module tracks, per seat, the thing the engine already knew but
never said:

    seen        — a face was measured this frame
    unmeasured  — the seat is occupied but no face has been measurable
                  for longer than PRESENCE_UNMEASURED_SEC
    gone        — unmeasured for longer than PRESENCE_GONE_SEC

and how long each spell lasted, both the current one and the session
totals.

DELIBERATELY NOT A SUSPICION SIGNAL. Nothing here feeds suspicion.py,
temporal.py or verdict.py, and nothing here changes a score. An
unmeasured student is not a suspicious student: ordinary writing loses
the face in brief flickers, and on the same clips an INNOCENT clip
(posture_shift) had a 13-second unmeasured stretch of its own. Treating
absence as guilt would be exactly the kind of unfair detector this
project has twice had to fix. What this produces is a statement of fact
for a human — "this seat could not be seen for 29 seconds" — which is
the same contract as the rest of the system: it flags, a person decides.

Brief losses are not reported. A face that vanishes for a third of a
second while someone writes is noise, and reporting it would bury the
real thing. Only a spell that lasts past PRESENCE_UNMEASURED_SEC is
opened, logged and counted.

Kill switch: Config.PRESENCE_TRACKING_ENABLED. Off reproduces the old
behaviour exactly — update() does nothing and payload() returns None.

Pure bookkeeping, like identity_tracker.py: it owns no scoring state and
mutates no pipeline. It reads IdentityTracker's slots (occupied, on
break, occupant serial) to know which seats are even supposed to have
somebody in them, and it is told separately whether a body was seen, by
whichever body check the caller is running (see body_presence.py). It
never guesses at a body on its own.
"""

from collections import deque

from config import Config


class _SeatPresence:
    """One seat's visibility bookkeeping. No scoring state."""

    __slots__ = ("state", "since", "last_seen", "total_unmeasured",
                 "longest_unmeasured", "spells", "serial", "body_seen",
                 "reported", "hands")

    def __init__(self):
        self.state = "seen"
        self.since = None            # when the current state began
        self.last_seen = None        # last time a face was measured here
        self.total_unmeasured = 0.0  # seconds this session, reported spells only
        self.longest_unmeasured = 0.0
        self.spells = deque(maxlen=Config.PRESENCE_MAX_SPELLS)
        self.serial = 0              # identity occupant_serial, to spot handover
        self.body_seen = None        # last body-check answer for this seat
        # What the hands were doing when the face could not be read.
        # REPORTING ONLY, never scored — see body_presence.py for the
        # measurements that rule it out as a detector.
        self.hands = None
        self.reported = False        # has the current spell been announced?

    def reset(self):
        """A new person took this seat — their predecessor's spells are
        not theirs. Called on handover, mirroring StudentPipeline.reset()."""
        self.state = "seen"
        self.since = None
        self.last_seen = None
        self.total_unmeasured = 0.0
        self.longest_unmeasured = 0.0
        self.spells.clear()
        self.body_seen = None
        self.hands = None
        self.reported = False


class PresenceTracker:
    """
    Call update() once per frame, after identity assignment.

    Returns a list of transition dicts for the caller to log — the same
    arrangement identity_tracker.assign() uses, so every announcement
    still happens in the main loop where it can be seen, rather than
    being buried inside a module.
    """

    def __init__(self, max_students):
        self.max_students = max_students
        self.seats = {i: _SeatPresence() for i in range(max_students)}

    # ── queries ───────────────────────────────────────────────────────
    def state(self, sid):
        return self.seats[sid].state

    def unmeasured_ids(self):
        """Seats currently in a reported spell. The body check runs only
        for these, which is what keeps it off the hot path while
        everybody's face is visible."""
        return [sid for sid, s in self.seats.items() if s.state != "seen"]

    def unmeasured_for(self, sid, t):
        """Seconds the current unmeasured spell has lasted, 0.0 if seen."""
        s = self.seats[sid]
        if s.state == "seen" or s.since is None:
            return 0.0
        return max(0.0, t - s.since)

    # ── the one write path ────────────────────────────────────────────
    def update(self, t, active_ids, identity, body=None, hands=None):
        """
        t          : engine time (clock.now()), seconds
        active_ids : slots that had a measurable face THIS frame
        identity   : IdentityTracker, read-only (occupied / break / serial)
        body       : optional {slot_id: bool} from a body check, or None
                     when no body check is running. None means "unknown",
                     never "nobody there".

        Returns [{slot, kind, seconds, body_seen}, ...] for the caller to
        log. kind is "lost" when a spell opens or "returned" when it
        closes.
        """
        if not Config.PRESENCE_TRACKING_ENABLED:
            return []

        transitions = []
        active = set(active_ids)

        for sid, seat in self.seats.items():
            slot = identity.slots.get(sid) if hasattr(identity, "slots") else None

            # A seat handed to a new person starts over. The outgoing
            # occupant's figures are not inherited by the newcomer —
            # the same rule the pipeline and the archive already follow.
            if slot is not None and slot.occupant_serial != seat.serial:
                seat.reset()
                seat.serial = slot.occupant_serial

            if body is not None and sid in body:
                seat.body_seen = bool(body[sid])
            if hands is not None and sid in hands:
                seat.hands = hands[sid]

            # Seats nobody has ever sat in, and seats whose student is on
            # an authorised break, are not "unmeasured" — there is
            # nobody there to measure. An empty chair must never be
            # reported as a student who cannot be seen.
            occupied = bool(slot.occupied) if slot is not None else False
            on_break = bool(slot.on_break) if slot is not None else False
            if not occupied or on_break:
                if seat.state != "seen":
                    seat.state, seat.since, seat.reported = "seen", None, False
                continue

            if sid in active:
                seat.last_seen = t
                if seat.state != "seen":
                    spell = max(0.0, t - (seat.since or t))
                    if seat.reported:
                        transitions.append({
                            "slot": sid, "kind": "returned",
                            "seconds": round(spell, 1),
                            "body_seen": seat.body_seen,
                        })
                    seat.state, seat.since, seat.reported = "seen", None, False
                continue

            # No face for this seat this frame.
            if seat.last_seen is None:
                # Occupied but never yet measured (still arriving). Not
                # a loss — there is nothing to have lost.
                continue

            gap = t - seat.last_seen
            if gap < Config.PRESENCE_UNMEASURED_SEC:
                continue  # a flicker; ordinary writing does this constantly

            if seat.state == "seen":
                seat.state = "unmeasured"
                seat.since = seat.last_seen
                seat.reported = True
                self._open_spell(seat, t, gap)
                transitions.append({
                    "slot": sid, "kind": "lost",
                    "seconds": round(gap, 1),
                    "body_seen": seat.body_seen,
                })
            else:
                self._extend_spell(seat, t, gap)
                if (seat.state == "unmeasured"
                        and gap >= Config.PRESENCE_GONE_SEC):
                    seat.state = "gone"
                    transitions.append({
                        "slot": sid, "kind": "lost",
                        "seconds": round(gap, 1),
                        "body_seen": seat.body_seen,
                    })

        return transitions

    # ── spell bookkeeping ─────────────────────────────────────────────
    def _open_spell(self, seat, t, gap):
        seat.spells.append({"start": seat.last_seen, "sec": round(gap, 1),
                            "body_seen": seat.body_seen})
        seat.total_unmeasured += gap
        seat.longest_unmeasured = max(seat.longest_unmeasured, gap)

    def _extend_spell(self, seat, t, gap):
        if not seat.spells:
            return
        last = seat.spells[-1]
        grown = gap - last["sec"]
        if grown <= 0:
            return
        last["sec"] = round(gap, 1)
        last["body_seen"] = seat.body_seen
        seat.total_unmeasured += grown
        seat.longest_unmeasured = max(seat.longest_unmeasured, gap)

    # ── read-only payload for the dashboard / report / clip summary ───
    def payload(self, sid, t):
        """
        One seat's visibility, or None when the feature is switched off
        so every consumer can tell "nothing to show" from "all fine".
        """
        if not Config.PRESENCE_TRACKING_ENABLED:
            return None
        seat = self.seats[sid]
        return {
            "state": seat.state,
            "current_sec": round(self.unmeasured_for(sid, t), 1),
            "total_unmeasured_sec": round(seat.total_unmeasured, 1),
            "longest_unmeasured_sec": round(seat.longest_unmeasured, 1),
            "spells": len(seat.spells),
            "body_seen": seat.body_seen,
            "hands": seat.hands,
            "reportable": seat.state != "seen",
        }

    def summary(self, t):
        """Session totals for every seat, for the clip summary and the
        PDF report. Seats never occupied are left out."""
        if not Config.PRESENCE_TRACKING_ENABLED:
            return {}
        out = {}
        for sid, seat in self.seats.items():
            if seat.last_seen is None and not seat.spells:
                continue
            out[sid] = {
                "total_unmeasured_sec": round(seat.total_unmeasured, 1),
                "longest_unmeasured_sec": round(seat.longest_unmeasured, 1),
                "spells": [dict(s) for s in seat.spells],
                "state": seat.state,
                # Last hand reading taken while this seat could not be
                # read. Reporting only; see body_presence.py.
                "hands": seat.hands,
            }
        return out

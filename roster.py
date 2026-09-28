"""
roster.py — StudentRoster

Lets the invigilator declare who is actually in the room before (or
during) an exam: real names, roll numbers, and which seat each person
occupies.

WHY THIS EXISTS
───────────────
Two problems, one answer.

1. REPORTS WERE ANONYMOUS. Everything the system produced referred to
   "S1", "S2" — seat positions, not people. A report that says "S3 is
   recommended for review" is useless to anyone who has to act on it,
   because nothing in the document says who S3 was. A name turns the
   output from a technical readout into an actual record.

2. IDENTITY WAS BEING GUESSED. Every mechanism for working out who is
   who — position tracking, grace periods, face signatures, the
   uncertainty questions — exists because the system has to infer
   something it was never told. The invigilator knows. Asking them once,
   at the start, is more reliable than any amount of inference, and it
   makes the roster cap meaningful: with three students registered, a
   fourth detected "face" is definitionally not a student.

DELIBERATELY OPTIONAL
─────────────────────
Registration is never required. With no roster the system behaves
exactly as before, labelling by seat. This matters because a quick test
run should not demand paperwork first, and because a half-registered
session must degrade gracefully rather than break — unregistered seats
simply keep their seat label.

PRIVACY
───────
A name and roll number are stored for the duration of the session and
written into the session folder alongside the report, because a report
that identifies a student is the entire purpose. No photograph is
stored. The face signature captured at registration is the same list of
facial-measurement ratios used elsewhere (see face_signature.py) — it
cannot be turned back into an image, and it is discarded with the
session.
"""

import json
import os
import threading
import time


class RegisteredStudent:
    """One registered person. Plain data."""

    __slots__ = ("slot", "name", "roll_no", "registered_at", "signature",
                  "seat_note")

    def __init__(self, slot, name, roll_no=None, registered_at=None,
                 signature=None, seat_note=None):
        self.slot = slot
        self.name = name
        self.roll_no = roll_no
        self.registered_at = registered_at or time.time()
        self.signature = signature
        self.seat_note = seat_note

    def to_dict(self, include_signature=False):
        d = {
            "slot": self.slot,
            "name": self.name,
            "roll_no": self.roll_no,
            "registered_at": self.registered_at,
            "registered_at_str": time.strftime(
                "%H:%M:%S", time.localtime(self.registered_at)),
            "seat_note": self.seat_note,
            "has_face_signature": bool(self.signature),
        }
        if include_signature:
            d["signature"] = self.signature
        return d


class StudentRoster:
    """
    Thread-safe registry of who is in the room.

    Written by Flask request threads (the invigilator registering
    people) and read by the capture loop, so the same lock discipline as
    web_state.SharedSnapshot applies.
    """

    def __init__(self, max_students: int, session_dir=None):
        self.max_students = max_students
        self._lock = threading.Lock()
        self._students = {}      # slot -> RegisteredStudent
        self._locked = False
        self._session_dir = session_dir
        self._path = (os.path.join(session_dir, "roster.json")
                       if session_dir else None)

    # ── registration ───────────────────────────────────────────────

    def register(self, slot: int, name: str, roll_no=None, signature=None,
                  seat_note=None):
        """
        Assign a real person to a seat. Returns (ok, error).

        Registering over an existing entry is allowed and replaces it —
        an invigilator correcting a typo or a mis-seated student should
        not have to unregister first.
        """
        name = (name or "").strip()
        if not name:
            return False, "A name is required."
        if len(name) > 60:
            return False, "Name is too long (60 characters maximum)."
        if not isinstance(slot, int) or not (0 <= slot < self.max_students):
            return False, f"Seat must be between 0 and {self.max_students - 1}."
        with self._lock:
            if self._locked:
                return False, ("The roster is locked for this exam. Unlock it "
                                "before making changes.")
            self._students[slot] = RegisteredStudent(
                slot=slot, name=name,
                roll_no=(roll_no or "").strip() or None,
                signature=signature, seat_note=seat_note,
            )
            snapshot = self._snapshot_locked()
        self._flush(snapshot)
        return True, None

    def unregister(self, slot: int):
        with self._lock:
            if self._locked:
                return False, "The roster is locked for this exam."
            if slot not in self._students:
                return False, f"Seat {slot} is not registered."
            del self._students[slot]
            snapshot = self._snapshot_locked()
        self._flush(snapshot)
        return True, None

    def attach_signature(self, slot: int, signature):
        """
        Store the face signature captured for a registered student.

        Kept on the roster rather than only in the live gallery so it
        survives a seat reset — a registered student who leaves and
        returns can be matched back against what was recorded at
        registration, which is the most reliable sample available
        (frontal, unhurried, taken while they were sitting still).
        """
        with self._lock:
            rec = self._students.get(slot)
            if rec is None or not signature:
                return False
            rec.signature = list(signature)
            snapshot = self._snapshot_locked()
        self._flush(snapshot)
        return True

    # ── locking ────────────────────────────────────────────────────

    def lock(self):
        """
        Freeze the roster for the exam. After this the list of people in
        the room is fixed, which is what makes the roster cap
        meaningful: with three students registered, a fourth detected
        face is definitionally not a student.
        """
        with self._lock:
            self._locked = True
            n = len(self._students)
        return n

    def unlock(self):
        with self._lock:
            self._locked = False

    @property
    def is_locked(self):
        with self._lock:
            return self._locked

    # ── reading ────────────────────────────────────────────────────

    def get(self, slot: int):
        with self._lock:
            return self._students.get(slot)

    def display_name(self, slot: int, fallback: str) -> str:
        """
        The name to show for a seat. Falls back to the seat label when
        nobody is registered there, so a partly-registered session reads
        sensibly rather than showing blanks.
        """
        with self._lock:
            rec = self._students.get(slot)
        return rec.name if rec else fallback

    def registered_count(self) -> int:
        with self._lock:
            return len(self._students)

    def registered_slots(self) -> list:
        with self._lock:
            return sorted(self._students.keys())

    def all_students(self, include_signature=False) -> list:
        with self._lock:
            return [s.to_dict(include_signature)
                    for s in sorted(self._students.values(),
                                     key=lambda x: x.slot)]

    def to_payload(self) -> dict:
        """Compact form for the dashboard."""
        with self._lock:
            students = {
                s.slot: {
                    "name": s.name, "roll_no": s.roll_no,
                    "registered_at_str": time.strftime(
                        "%H:%M:%S", time.localtime(s.registered_at)),
                    "has_face_signature": bool(s.signature),
                }
                for s in self._students.values()
            }
            locked = self._locked
        return {
            "enabled": True,
            "locked": locked,
            "registered": len(students),
            "max_students": self.max_students,
            "students": students,
        }

    # ── persistence ────────────────────────────────────────────────

    def _snapshot_locked(self):
        """Caller must already hold the lock."""
        return {
            "locked": self._locked,
            "students": [s.to_dict(include_signature=False)
                          for s in sorted(self._students.values(),
                                           key=lambda x: x.slot)],
        }

    def _flush(self, snapshot):
        """
        Persist to the session folder so the report and any later audit
        can say who was in the room, even if the process dies. Face
        signatures are deliberately NOT written to disk — they are
        working data for one session, not a record to keep.
        """
        if not self._path:
            return
        try:
            os.makedirs(os.path.dirname(self._path), exist_ok=True)
            tmp = self._path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(snapshot, f, indent=2)
            os.replace(tmp, self._path)  # atomic; never a half-written file
        except Exception as e:
            print(f"[WARN] Roster could not be saved: {e}")

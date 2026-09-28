"""
identity_review.py — IdentityReviewQueue

Holds identity questions the system cannot answer on its own, so an
invigilator can answer them instead.

WHY THIS EXISTS
───────────────
Face re-identification (face_signature.py) has three outcomes, not two.
A confident match is acted on automatically; a face with no resemblance
to anyone is treated as a new person automatically. The middle case —
"this looks like Student 2, but I am not sure" — used to be silently
lumped in with "definitely a new person". That quietly turned "I don't
know" into "I know it isn't them", and a student who genuinely returned
was permanently treated as a stranger with nobody ever being told.

Identity here is an accusation-relevant decision: getting it wrong means
either losing a real record or attaching one person's behaviour to
another. When the system has already admitted it is unsure, that
decision belongs to the invigilator, who actually knows who left the
room. This queue is how the system asks.

This deliberately mirrors how the rest of the engine already works: the
suspicion score never punishes anyone by itself either — it flags, and a
human decides. The system should not be making judgements that outrank
the teacher supervising the room.

HOW A QUESTION RESOLVES
───────────────────────
While a question is open, the person is treated as a NEW student and
monitored normally from a fresh calibration — no special state, nothing
paused. Mid-exam a stranger walking in is very unlikely, so this costs
little in the common case and is safe in the uncommon one.

  * Invigilator answers YES → the archived record of the student they
    were before leaving is merged back into their live pipeline, so the
    session ends with ONE continuous record instead of two orphaned
    fragments (see StudentPipeline.merge_archived_record).
  * Invigilator answers NO  → nothing happens. They stay a new
    occupant, exactly as they already were.
  * Nobody answers          → the question expires and is treated as
    NO. Expiry is harmless by construction: it produces the same result
    that was already in effect while the question was open.

No images are stored here — a question holds slot numbers, a similarity
score, and a reference to an archived record.
"""

import threading
import time

from config import Config


class IdentityReviewQueue:
    """
    Thread-safe store of open and resolved identity questions.

    Written by the CV loop (raise_question) and read/resolved by Flask
    request threads (open_questions / resolve), so the same
    lock-and-copy discipline as web_state.SharedSnapshot applies.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._open = {}       # review_id -> question dict
        self._resolved = []   # history, kept for the session report
        self._next_id = 1

    # ── raising ────────────────────────────────────────────────────

    def raise_question(self, slot: int, label: str, candidate_slot: int,
                        candidate_label: str, score: float, reason: str,
                        archived_record: dict, t: float) -> dict:
        """
        Ask whether the person now occupying `slot` is in fact the
        student described by `archived_record`.

        Returns the created question, or None if a question for this
        slot is already open (never stack duplicates for the same seat —
        one seat can only have one occupant, so a second question about
        it would be noise) or the open cap has been reached.
        """
        with self._lock:
            for q in self._open.values():
                if q["slot"] == slot:
                    return None
            if len(self._open) >= Config.IDENTITY_REVIEW_MAX_OPEN:
                return None

            rid = self._next_id
            self._next_id += 1
            q = {
                "id": rid,
                "slot": slot,
                "label": label,
                "candidate_slot": candidate_slot,
                "candidate_label": candidate_label,
                "score": round(score, 4),
                "reason": reason,
                "created_at": t,
                "created_at_str": _clock(t),
                "expires_at": t + Config.IDENTITY_REVIEW_EXPIRY_SEC,
                "status": "open",
                # Enough of the archived record for the invigilator to
                # answer without digging: who they were, how long they
                # sat, what their behaviour looked like.
                "archived": {
                    "record_id": (archived_record or {}).get("record_id"),
                    "occupant_serial": (archived_record or {}).get("occupant_serial"),
                    "started_at_str": (archived_record or {}).get("started_at_str"),
                    "ended_at_str": (archived_record or {}).get("ended_at_str"),
                    "duration_sec": (archived_record or {}).get("duration_sec"),
                    "peak_probability": (archived_record or {}).get("peak_probability"),
                    "total_events": (archived_record or {}).get("total_events"),
                },
            }
            self._open[rid] = q
            return dict(q)

    # ── resolving ──────────────────────────────────────────────────

    def resolve(self, review_id: int, confirmed: bool, t: float,
                resolved_by: str = "invigilator"):
        """
        Answer a question. Returns (question, error_message).
        A question that has already been answered cannot be answered
        again — the first answer stands, so the audit trail has exactly
        one decision per question.
        """
        with self._lock:
            q = self._open.pop(review_id, None)
            if q is None:
                return None, f"No open identity question with id {review_id}"
            q["status"] = "confirmed" if confirmed else "rejected"
            q["resolved_at"] = t
            q["resolved_at_str"] = _clock(t)
            q["resolved_by"] = resolved_by
            self._resolved.append(q)
            return dict(q), None

    def expire_due(self, t: float) -> list:
        """
        Close out questions nobody answered in time. Expiry means the
        same thing as "no" — the occupant simply carries on as a new
        student, which is what was already happening. Returns the
        questions that expired so the caller can log them.
        """
        expired = []
        with self._lock:
            for rid in [r for r, q in self._open.items() if t >= q["expires_at"]]:
                q = self._open.pop(rid)
                q["status"] = "expired"
                q["resolved_at"] = t
                q["resolved_at_str"] = _clock(t)
                q["resolved_by"] = "timeout"
                self._resolved.append(q)
                expired.append(dict(q))
        return expired

    def cancel_for_slot(self, slot: int, t: float, note: str) -> list:
        """
        Drop open questions about a slot whose situation has changed —
        e.g. the seat changed hands again, so the original question is
        no longer meaningful. Recorded rather than silently deleted.
        """
        cancelled = []
        with self._lock:
            for rid in [r for r, q in self._open.items() if q["slot"] == slot]:
                q = self._open.pop(rid)
                q["status"] = "cancelled"
                q["resolved_at"] = t
                q["resolved_at_str"] = _clock(t)
                q["resolved_by"] = note
                self._resolved.append(q)
                cancelled.append(dict(q))
        return cancelled

    # ── reading ────────────────────────────────────────────────────

    def open_questions(self) -> list:
        with self._lock:
            return [dict(q) for q in self._open.values()]

    def resolved_questions(self) -> list:
        with self._lock:
            return [dict(q) for q in self._resolved]

    def stats(self) -> dict:
        with self._lock:
            counts = {}
            for q in self._resolved:
                counts[q["status"]] = counts.get(q["status"], 0) + 1
            return {
                "open": len(self._open),
                "resolved": counts,
                "total_raised": self._next_id - 1,
            }


def _clock(ts):
    try:
        return time.strftime("%H:%M:%S", time.localtime(ts))
    except Exception:
        return None

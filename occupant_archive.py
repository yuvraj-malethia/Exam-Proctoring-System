"""
occupant_archive.py — OccupantArchive

Preserves the full record of everyone who has occupied a seat during a
session, instead of throwing it away when the seat changes hands.

WHY THIS EXISTS
───────────────
When a seat is handed to a different person, StudentPipeline.reset()
wipes that slot's calibration, suspicion score and event history so the
newcomer does not inherit someone else's record. That is correct — but
on its own it means the previous occupant's data is simply GONE. Two
problems follow:

  1. The session report can only ever show the LAST person to sit in
     each seat. Everything earlier silently disappears.

  2. It is exploitable. A student with a high suspicion score could
     deliberately leave the room long enough for their slot to expire,
     come back, and return to a clean record. Deleting on reset makes
     walking out an effective way to erase evidence.

The archive closes both. Nothing is ever deleted — it is FILED. Reset
still protects the newcomer from inheriting a record, and the outgoing
record still exists in full for the report and for audit.

WHAT IS STORED
──────────────
For each completed occupancy: which slot, which occupant number, when
they arrived and left, why the record closed, their final and peak
suspicion, confirmed seconds, total events and away time, plus the full
list of their deviation events. Numbers and timestamps only — no
images, no video, no face data.

Written to disk as JSON as soon as each record closes, so a crash mid
exam cannot lose the history of people who already left.
"""

import json
import os
import threading
import time


class OccupantArchive:
    """
    Thread-safe store of completed occupancy records.

    One writer (the CV loop, via close_record) and possibly several
    readers (Flask request threads via all_records/for_slot), so the
    same lock-and-copy pattern used by web_state.SharedSnapshot applies
    here too.
    """

    def __init__(self, session_dir=None):
        self._lock = threading.Lock()
        self._records = []
        self._next_record_id = 1
        self._session_dir = session_dir
        self._path = (
            os.path.join(session_dir, "occupancy_records.json")
            if session_dir else None
        )
        if session_dir:
            os.makedirs(session_dir, exist_ok=True)

    def close_record(self, slot: int, label: str, summary: dict,
                      events: list, started_at: float, ended_at: float,
                      reason: str, face_signature=None) -> dict:
        """
        File a completed occupancy. summary is the dict returned by
        StudentPipeline.reset(); events is that occupant's deviation
        history, copied out before the pipeline is wiped.

        Returns the stored record so the caller can log/report it.
        """
        record = {
            # Stable id so an identity question can point at exactly
            # this record, and so a later merge can mark it as reunited
            # rather than leaving a duplicate in the report.
            "record_id": self._next_record_id,
            "slot": slot,
            "label": label,
            "occupant_serial": summary.get("outgoing_occupant_serial"),
            "started_at": started_at,
            "ended_at": ended_at,
            "duration_sec": round(max(ended_at - started_at, 0.0), 1),
            "started_at_str": _clock(started_at),
            "ended_at_str": _clock(ended_at),
            "reason": reason,
            "final_probability": summary.get("final_probability"),
            "peak_probability": summary.get("peak_probability"),
            "confirmed_secs": summary.get("confirmed_secs"),
            "total_events": summary.get("total_events"),
            "total_away_s": summary.get("total_away_s"),
            "was_calibrated": summary.get("was_calibrated"),
            # Averaged face signature of the departing occupant, kept so
            # that if someone later sits here, their OWN averaged
            # signature can be compared against this one. Comparing two
            # averages is far more reliable than comparing a single
            # noisy frame to an average — which is what made the first
            # version ask the invigilator far more often than needed.
            # This is a list of ratios between facial measurements; it
            # is not an image and cannot be turned back into one.
            "face_signature": list(face_signature) if face_signature else None,
            "events": [
                {
                    "time": _clock(e.timestamp),
                    "timestamp": e.timestamp,
                    "duration": round(e.duration, 2),
                    "direction": e.direction,
                    "z_score": round(e.z_score, 2),
                }
                for e in (events or [])
            ],
        }
        with self._lock:
            self._next_record_id += 1
            self._records.append(record)
            snapshot = list(self._records)
        self._flush(snapshot)
        return record

    def get_record(self, record_id):
        with self._lock:
            for r in self._records:
                if r.get("record_id") == record_id:
                    return dict(r)
        return None

    def latest_for_slot(self, slot: int):
        """Most recently closed record for a seat — the occupant a
        returning student is most likely to actually be."""
        with self._lock:
            for r in reversed(self._records):
                if r["slot"] == slot and not r.get("merged_into_slot"):
                    return dict(r)
        return None

    def mark_merged(self, record_id, into_slot: int, t: float, score=None):
        """
        Flag an archived record as reunited with a live student, so the
        session report shows one continuous person rather than counting
        them twice. The record is NOT deleted — the fact that they left
        and came back is itself part of the audit trail.
        """
        with self._lock:
            for r in self._records:
                if r.get("record_id") == record_id:
                    r["merged_into_slot"] = into_slot
                    r["merged_at"] = t
                    r["merged_at_str"] = _clock(t)
                    r["merge_confidence"] = score
                    snapshot = list(self._records)
                    break
            else:
                return False
        self._flush(snapshot)
        return True

    def all_records(self) -> list:
        with self._lock:
            return list(self._records)

    def for_slot(self, slot: int) -> list:
        with self._lock:
            return [r for r in self._records if r["slot"] == slot]

    def summary_by_slot(self) -> dict:
        """
        Compact per-slot view for the dashboard: how many people have
        used this seat, and the peak suspicion reached by any of the
        PREVIOUS occupants. Lets the UI show "this seat also had an
        earlier occupant who peaked at N%" instead of hiding it.
        """
        out = {}
        with self._lock:
            for r in self._records:
                s = out.setdefault(r["slot"], {
                    "previous_occupants": 0,
                    "max_previous_peak": 0.0,
                    "total_previous_events": 0,
                })
                if r.get("merged_into_slot") is not None:
                    continue  # reunited with a live student — not a
                    # separate previous occupant any more.
                s["previous_occupants"] += 1
                s["max_previous_peak"] = max(
                    s["max_previous_peak"], r.get("peak_probability") or 0.0
                )
                s["total_previous_events"] += r.get("total_events") or 0
        return out

    def _flush(self, snapshot):
        """Write to disk immediately so an archived record survives a crash."""
        if not self._path:
            return
        try:
            tmp = self._path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(snapshot, f, indent=2)
            os.replace(tmp, self._path)  # atomic — never a half-written file
        except Exception as e:
            print(f"[WARN] Occupancy archive write failed: {e}")


def _clock(ts):
    try:
        return time.strftime("%H:%M:%S", time.localtime(ts))
    except Exception:
        return None

"""
event_logger.py — EventLogger

In-memory, thread-safe rolling log of session-level events: verdict
transitions (OK -> WATCH -> ALERT -> FLAG and back) plus lifecycle
markers (calibration complete, exam started/ended). This is deliberately
separate from the raw "look away" events already reconstructed live in
dashboard.js (ingestEvents() there already diffs temporal.total_events
per poll for the Classroom Activity feed / Classroom Timeline — that
part of the "event timeline" idea already exists and needs no backend
change). EventLogger covers the layer that doesn't exist yet: verdict-
level and session-level events, which need to be tracked server-side
because they depend on state changing over time, not a single snapshot.

Same lock-and-swap thread-safety pattern as web_state.SharedSnapshot —
one writer (the CV loop thread), one or more readers (Flask request
threads).
"""

import threading
from collections import deque


class EventLogger:
    def __init__(self, max_events=500):
        self._lock = threading.Lock()
        self._events = deque(maxlen=max_events)

    def add_event(self, student_label, event, score=0.0, duration=0.0, timestamp=None):
        """
        Record one event. timestamp: wall-clock string (HH:MM:SS); if not
        given, uses the current local time.
        """
        import time
        with self._lock:
            self._events.append({
                "student": student_label,
                "time": timestamp or time.strftime("%H:%M:%S"),
                "event": event,
                "score": round(score, 1),
                "duration": round(duration, 1),
            })

    def recent(self, n=20):
        """Returns up to the last n events, oldest first."""
        with self._lock:
            return list(self._events)[-n:]

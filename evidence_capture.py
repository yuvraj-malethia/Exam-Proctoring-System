"""
evidence_capture.py — EvidenceCapture

Saves an annotated frame + JSON metadata to disk whenever a student's
verdict crosses into ALERT or FLAG (see verdict.compute_verdict — the
same check already drawn on the live panel, so evidence always matches
what the invigilator saw on screen). Writes happen on a background
thread via a queue, never inline on the CV loop, for the same reason
the dashboard server itself runs on its own thread: a slow disk write
must never stall the camera loop.

Folder layout:
    evidence/
        session_2026_07_30_101530/
            S2/
                event_001.jpg
                event_001.json
            S3/
                event_002.jpg   (per-student numbering, not global)
                event_002.json
"""

import os
import json
import queue
import threading
import time
import cv2


class EvidenceCapture:
    def __init__(self, session_dir):
        self.session_dir = session_dir
        os.makedirs(session_dir, exist_ok=True)
        self._counters = {}          # student_label -> next event number
        self._queue = queue.Queue()
        self._latest = {}            # student_label -> latest evidence record
        self._lock = threading.Lock()
        threading.Thread(
            target=self._writer_loop, daemon=True, name="evidence-writer"
        ).start()

    def save_event(self, frame, student_label, suspicion, yaw, duration,
                    reason, pitch=None):
        """
        Non-blocking: copies the frame and queues the write. Safe to call
        from the CV loop every frame an event fires — the actual
        cv2.imwrite()/json.dump() happen on the background thread.
        """
        self._queue.put((frame.copy(), {
            "student": student_label,
            "time": time.strftime("%H:%M:%S"),
            "score": round(suspicion, 1),
            "reason": reason,
            "yaw": round(yaw, 1),
            "pitch": round(pitch, 1) if pitch is not None else None,
            "duration": round(duration, 1),
        }))

    def latest(self):
        """Returns {student_label: latest_evidence_record} for the dashboard."""
        with self._lock:
            return dict(self._latest)

    def queue_depth(self):
        """
        Diagnostic only: how many writes are currently queued but not
        yet flushed to disk. A number that keeps climbing means the
        background writer can't keep up with how often evidence is
        being triggered. Does not affect capture behavior in any way.
        """
        return self._queue.qsize()

    def _writer_loop(self):
        while True:
            frame, meta = self._queue.get()
            try:
                self._write(frame, meta)
            except Exception as e:
                print(f"[WARN] Evidence write failed: {e}")

    def _write(self, frame, meta):
        student = meta["student"]
        student_dir = os.path.join(self.session_dir, student)
        os.makedirs(student_dir, exist_ok=True)

        n = self._counters.get(student, 0) + 1
        self._counters[student] = n
        stem = f"event_{n:03d}"

        session_name = os.path.basename(os.path.normpath(self.session_dir))
        img_rel = os.path.join(session_name, student, f"{stem}.jpg")
        json_rel = os.path.join(session_name, student, f"{stem}.json")
        img_path = os.path.join(self.session_dir, student, f"{stem}.jpg")
        json_path = os.path.join(self.session_dir, student, f"{stem}.json")

        ok, jpg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
        if ok:
            with open(img_path, "wb") as f:
                f.write(jpg.tobytes())

        record = dict(meta)
        record["image"] = img_rel.replace("\\", "/")
        with open(json_path, "w") as f:
            json.dump(record, f, indent=2)

        with self._lock:
            self._latest[student] = record

"""
frame_faults.py — FrameFaults

THE BUG THIS CLOSES, and it is a real one that cost an hour on 13 Oct
2026. The main loop wraps each frame in a broad `except` so that one bad
frame cannot end an exam in progress. That is the right behaviour. What
was wrong is what happened next: it printed a single line and carried on,
with no memory that anything had gone wrong.

A typo made `StudentRoster.is_locked` — a `@property` — get called as
`is_locked()`, which raised `'bool' object is not callable` on EVERY
frame. All 2,955 of them. The run then finished, announced "Session
ended", wrote its results file and reported **0 students tracked and
0.0% of frames with a face**, as though it had simply watched an empty
room. Nothing anywhere said "every single frame failed".

That is the same failure shape CLAUDE.md already warns about for the PDF
report's evidence section: a broad except turning a real failure into a
quiet "no data" result. The lesson is not "remove the except" — the
except is protecting a live exam. The lesson is that **swallowing an
error must never be silent, and a run where frames failed must say so in
its summary rather than reporting a clean empty result.**

So this module remembers:
  - how many frames raised, out of how many
  - each DISTINCT fault, grouped by exception type and message, with the
    first frame it appeared on, the last, and how many times
  - the full traceback of each distinct fault, printed once when first
    seen rather than a one-line summary repeated thousands of times

and it can answer one question plainly: was this run healthy, or should
nobody trust its numbers? Past `Config.FRAME_FAULT_ALERT_FRAC` of frames
the run is reported as DEGRADED everywhere it is reported at all — the
console summary, the clip results JSON, the batch table and the
dashboard.

It holds no scoring state and makes no decisions. It is bookkeeping.
"""

import threading
import traceback

from config import Config


class FrameFaults:
    """Counts and groups the errors a frame-level `except` swallowed."""

    def __init__(self, max_distinct=40):
        # RLock, not Lock: payload() holds it and then reads .degraded,
        # which reads .fraction, which takes it again. With a plain Lock
        # that is a deadlock, and it hangs at the exact moment a run is
        # trying to report its faults — the worst possible time.
        self._lock = threading.RLock()
        self.frames = 0          # frames attempted
        self.failed = 0          # frames lost entirely
        self.subsystem = 0       # faults a narrower except caught
        self.max_distinct = max_distinct
        self.faults = {}         # signature -> record
        self._overflow = 0       # distinct faults past the cap

    # ── the write path ────────────────────────────────────────────────
    def saw_frame(self):
        with self._lock:
            self.frames += 1

    def note(self, frame_no, exc, where="frame"):
        """Record one swallowed exception. Returns True if this is the
        first time this particular fault has been seen, so the caller can
        print the full traceback once instead of once per frame.

        `where` names the subsystem whose own narrower except caught it.
        The main loop is not the only place that swallows: the dashboard
        serializer, the analytics pass, the behavioural pass and the
        profiler each have one, and each printed a line per frame and was
        otherwise invisible. A fault in any of them can hollow out a run
        just as quietly, so they are all counted here and the report says
        which one.
        """
        sig = f"[{where}] {type(exc).__name__}: {exc}"
        tb = traceback.format_exc()
        with self._lock:
            # `failed` means "the whole frame was lost". A subsystem
            # fault is recorded and reported but does not claim the
            # frame, or the degraded fraction would stop meaning
            # anything.
            if where == "frame":
                self.failed += 1
            else:
                self.subsystem += 1
            rec = self.faults.get(sig)
            if rec is None:
                if len(self.faults) >= self.max_distinct:
                    self._overflow += 1
                    return False
                self.faults[sig] = {
                    "signature": sig, "where": where, "count": 1,
                    "first_frame": frame_no, "last_frame": frame_no,
                    "traceback": tb,
                }
                return True
            rec["count"] += 1
            rec["last_frame"] = frame_no
            return False

    # ── reading ───────────────────────────────────────────────────────
    @property
    def fraction(self):
        with self._lock:
            return (self.failed / self.frames) if self.frames else 0.0

    @property
    def any_faults(self):
        return (self.failed + self.subsystem) > 0

    @property
    def degraded(self):
        """True when enough frames failed that the run's numbers should
        not be trusted without looking at why."""
        return self.failed > 0 and self.fraction >= Config.FRAME_FAULT_ALERT_FRAC

    def payload(self):
        """Read-only summary for the results file and the dashboard."""
        with self._lock:
            worst = max(self.faults.values(), key=lambda r: r["count"], default=None)
            return {
                "frames": self.frames,
                "failed": self.failed,
                "subsystem": self.subsystem,
                "fraction": round((self.failed / self.frames) if self.frames else 0.0, 4),
                "degraded": self.degraded,
                "distinct": len(self.faults) + self._overflow,
                "worst": ({"signature": worst["signature"], "count": worst["count"],
                           "first_frame": worst["first_frame"]} if worst else None),
                "faults": [
                    {k: r[k] for k in ("signature", "where", "count", "first_frame", "last_frame")}
                    for r in sorted(self.faults.values(), key=lambda r: -r["count"])
                ],
            }

    def report_lines(self):
        """The block printed at session end. Deliberately loud: the whole
        point of this module is that a run which failed must not look
        like a run that found nothing."""
        if not self.any_faults:
            return []
        pct = 100.0 * self.fraction
        if self.degraded:
            head = "RUN DEGRADED"
        elif self.failed:
            head = "frames were skipped"
        else:
            head = "errors were swallowed"
        out = [
            "",
            "=" * 60,
            f"  {head}: {self.failed} of {self.frames} frames lost ({pct:.1f}%)"
            + (f", {self.subsystem} subsystem errors" if self.subsystem else ""),
            "=" * 60,
        ]
        if self.degraded:
            out.append("  These numbers are NOT a clean result. Enough frames")
            out.append("  failed that what the engine reported may be missing")
            out.append("  most of the session. Read the faults below first.")
        for r in sorted(self.faults.values(), key=lambda r: -r["count"]):
            out.append("")
            out.append(f"  {r['count']}x  {r['signature']}")
            out.append(f"       frames {r['first_frame']}..{r['last_frame']}")
            for line in r["traceback"].rstrip().split("\n")[-6:]:
                out.append(f"       {line}")
        if self._overflow:
            out.append(f"  (+{self._overflow} further distinct faults not kept)")
        out.append("=" * 60)
        return out

"""
frame_profiler.py — FrameProfiler

Pure diagnostic instrumentation. Does NOT change any existing behavior,
algorithm, threshold, or control flow anywhere else in the project — it
only wraps existing stages with timers and records what it observes.
The system stays exactly as slow/laggy/wrong as it already is; this
module's only job is to turn that into numbers you can look at.

Phase 1 of the black-box flight recorder (see system_monitor.py for the
new pieces this phase added):
  - Garbage collector pauses, hooked directly via gc.callbacks — not
    inferred from gaps, actually measured. A GC pause freezes the WHOLE
    process (every thread), which is exactly the signature seen in real
    data from this project (even the background heartbeat thread
    stalling, not just the main loop).
  - System-wide CPU/memory/swap, not just this process's own usage — a
    process can look fine on its own numbers while the whole machine is
    thrashing because of something else.
  - Swap activity as a RATE (bytes swapped in/out since the last
    sample), which is what actually indicates active thrashing, not
    just a static "some swap is in use" percentage.
  - A much faster heartbeat (default 50ms, was 1s) that also measures
    its OWN scheduling drift — how much later than requested its sleep()
    actually returned. That drift, on its own, independent of anything
    else in the log, is direct proof of the whole process being starved
    of CPU time by the OS, with millisecond-level precision on exactly
    when and how long.
  - psutil is now auto-installed if missing (best-effort, silent on
    failure) so resource data isn't just absent because of one missed
    setup step — and either way, the outcome is logged as the very
    first record, so any file makes clear whether resource data exists.

Still records, per frame: stage timings, dt/fps, per-student algorithm
state, evidence queue depth — all read-only, nothing recomputed or
altered from what the engine already produced.

No video, images, or frames are ever written to the log — only numbers
and short text labels.

Writing to disk never blocks the main loop: every record is put on an
in-memory queue and flushed by a background thread. Every record is
flushed to the OS immediately; a full fsync (forcing it to physical
disk) happens periodically rather than on every record, since the
heartbeat alone can now produce ~20 records/second and fsync-per-record
at that rate would make the profiler itself a meaningful cost — which
would defeat its entire purpose.

Also prints a one-second rolling console summary: mean / min / max per
stage, so a single slow frame is visible instead of averaged away.
"""

import os
import json
import queue
import threading
import time

from system_monitor import ensure_psutil, GCMonitor, ResourceSampler


class FrameProfiler:
    def __init__(self, log_dir="diagnostics", console_interval=1.0,
                 heartbeat_interval=0.05, session_id=None):
        os.makedirs(log_dir, exist_ok=True)
        ts = session_id or time.strftime("%Y_%m_%d_%H%M%S")
        self.log_path = os.path.join(log_dir, f"profile_{ts}.jsonl")

        self._queue = queue.Queue()
        threading.Thread(
            target=self._writer_loop, daemon=True, name="profiler-writer"
        ).start()

        self.console_interval = console_interval
        self._window_start = time.perf_counter()
        self._window_stage_samples = {}   # stage name -> list of ms this window
        self._window_frame_count = 0
        self._window_gc_ms = 0.0
        self._window_gc_count = 0

        print(f"[PROFILER] Logging every frame to: {self.log_path}")

        # ── Resource sampling (new: system-wide, not just this process) ──
        psutil_mod, psutil_status = ensure_psutil()
        self._resource_sampler = ResourceSampler(psutil_mod)
        self._last_resource_sample_t = 0.0
        self._last_resources = None
        print(f"[PROFILER] {psutil_status}")
        self._queue.put({
            "type": "startup",
            "t_wall": time.time(),
            "psutil_status": psutil_status,
            "psutil_available": psutil_mod is not None,
        })

        # ── Garbage collector monitor (new) ──────────────────────────
        # Every GC pause gets its own record on the same timeline via
        # self._queue.put — see system_monitor.GCMonitor. Registered
        # last-ish in __init__ so it never fires on the setup above.
        self._gc_monitor = GCMonitor(sink=self._on_gc_record)

        # ── Heartbeat (upgraded: was 1s, now default 50ms) ──────────
        # Writes a small "still alive" record on its own thread,
        # independent of whether the main loop is calling log_frame().
        # If the main loop fully hangs, frame records stop — the
        # heartbeat keeps going until the process itself dies, so the
        # log shows when things stopped instead of just going silent.
        # Also measures its own sleep drift: how much LATER than
        # requested it actually woke up. On a healthy, responsive
        # process this should be ~0. A large drift is direct, precise
        # evidence the OS didn't schedule ANY of this process's threads
        # for that long — not a guess based on gaps between frames.
        self.heartbeat_interval = heartbeat_interval
        self._last_frame_no = 0
        self._last_frame_t = time.time()
        self._hb_lock = threading.Lock()
        threading.Thread(
            target=self._heartbeat_loop, daemon=True, name="profiler-heartbeat"
        ).start()

    def _on_gc_record(self, record):
        self._queue.put(record)
        if record.get("duration_ms") is not None:
            self._window_gc_ms += record["duration_ms"]
            self._window_gc_count += 1

    def _heartbeat_loop(self):
        next_tick = time.perf_counter() + self.heartbeat_interval
        while True:
            now_pc = time.perf_counter()
            sleep_for = max(0.0, next_tick - now_pc)
            time.sleep(sleep_for)
            actual_pc = time.perf_counter()
            drift_ms = (actual_pc - next_tick) * 1000.0  # how late this tick was
            next_tick = actual_pc + self.heartbeat_interval

            with self._hb_lock:
                last_frame_no = self._last_frame_no
                last_frame_t = self._last_frame_t
            now_wall = time.time()
            self._queue.put({
                "type": "heartbeat",
                "t_wall": now_wall,
                "last_frame": last_frame_no,
                "seconds_since_last_frame": round(now_wall - last_frame_t, 3),
                "sleep_drift_ms": round(drift_ms, 2),
            })

    # ── resource sampling (throttled internally to ~1/sec) ─────────
    def _maybe_sample_resources(self, t_wall):
        if t_wall - self._last_resource_sample_t < 1.0:
            return self._last_resources
        self._last_resource_sample_t = t_wall
        self._last_resources = self._resource_sampler.sample()
        return self._last_resources

    # ── per-frame record ────────────────────────────────────────────
    def log_frame(self, frame_no, t_wall, dt, fps_instant, stage_ms,
                  active_students, student_snapshot, evidence_queue_depth,
                  identity_snapshot=None):
        """
        stage_ms: dict of {stage_name: milliseconds_this_frame}. Stages
        that didn't run this frame (e.g. "jpeg" on a throttled-out
        frame, "dev_panel" outside developer mode) should be passed as
        0.0, not omitted — so the console/file both show they ran, but
        cost nothing, rather than looking absent.
        student_snapshot: dict of {student_label: {...}} — read-only
        values already computed by the engine this frame.
        """
        record = {
            "type": "frame",
            "frame": frame_no,
            "t_wall": t_wall,
            "dt": round(dt, 5),
            "fps_instant": round(fps_instant, 2),
            "stages_ms": {k: round(v, 3) for k, v in stage_ms.items()},
            "active_students": active_students,
            "students": student_snapshot,
            "evidence_queue_depth": evidence_queue_depth,
            # Per-frame identity state (slot occupancy, face-match
            # counters, open questions). Added after real testing showed
            # identity errors were the hardest thing to reconstruct
            # afterwards — they depend on how state evolved over time,
            # which no single moment reveals.
            "identity": identity_snapshot,
            "resources": self._maybe_sample_resources(t_wall),
        }
        self._queue.put(record)

        with self._hb_lock:
            self._last_frame_no = frame_no
            self._last_frame_t = t_wall

        for name, ms in stage_ms.items():
            self._window_stage_samples.setdefault(name, []).append(ms)
        self._window_frame_count += 1

        now = time.perf_counter()
        if now - self._window_start >= self.console_interval:
            self._print_console_summary()
            self._window_start = now
            self._window_stage_samples = {}
            self._window_frame_count = 0
            self._window_gc_ms = 0.0
            self._window_gc_count = 0

    def log_event(self, t_wall, frame_no, student, event, score=0.0, duration=0.0):
        """Same timeline as log_frame — for verdict changes, evidence
        saves, calibration-complete, exam start/end, so they can be
        lined up against the per-frame timing/resource data later."""
        self._queue.put({
            "type": "event",
            "t_wall": t_wall,
            "frame": frame_no,
            "student": student,
            "event": event,
            "score": round(score, 1),
            "duration": round(duration, 1),
        })

    # ── console summary ─────────────────────────────────────────────
    def _print_console_summary(self):
        if self._window_frame_count == 0:
            return
        avg_fps = self._window_frame_count / self.console_interval
        print(f"\n[PROFILE] {self._window_frame_count} frames / "
              f"{self.console_interval:.1f}s window  (avg {avg_fps:.1f} fps)")
        print(f"  {'stage':<20}{'avg ms':>9}{'min ms':>9}{'max ms':>9}{'n':>6}")
        total_avg = 0.0
        for name, samples in self._window_stage_samples.items():
            nz = [s for s in samples if s > 0]  # min/max over frames the stage actually ran
            avg = sum(samples) / len(samples)
            total_avg += avg
            mn = min(nz) if nz else 0.0
            mx = max(nz) if nz else 0.0
            print(f"  {name:<20}{avg:>9.2f}{mn:>9.2f}{mx:>9.2f}{len(samples):>6}")
        print(f"  {'(sum of stage avgs)':<20}{total_avg:>9.2f}")
        if self._window_gc_count:
            print(f"  gc: {self._window_gc_count} pause(s) totaling "
                  f"{self._window_gc_ms:.1f}ms this window")
        if self._last_resources:
            proc = self._last_resources.get("process")
            sysr = self._last_resources.get("system")
            if proc:
                print(f"  process: cpu={proc['cpu_percent']:.1f}%  "
                      f"rss={proc['rss_mb']:.1f}MB  threads={proc['num_threads']}")
            if sysr:
                swap_note = ""
                if sysr.get("swap_in_delta_mb") or sysr.get("swap_out_delta_mb"):
                    swap_note = (f"  swap_in={sysr['swap_in_delta_mb']}MB "
                                 f"swap_out={sysr['swap_out_delta_mb']}MB")
                print(f"  system:  cpu={sysr['cpu_percent']:.1f}%  "
                      f"mem={sysr['mem_percent']:.1f}%  "
                      f"swap={sysr['swap_percent']:.1f}%{swap_note}")

    # ── background writer (never blocks the caller, never dies) ─────
    def _writer_loop(self):
        """
        Runs forever on its own thread. Must NEVER exit and must NEVER
        let a write failure stop future writes — a diagnostic log that
        can silently stop is worse than none, since it fails exactly
        when something is going wrong and you need it most.

        - Every record is written + flushed to the OS immediately.
        - A full fsync (force to physical disk) happens periodically
          (every ~0.5s) rather than on every single record — the fast
          heartbeat alone can now produce ~20 records/second, and an
          fsync syscall on every one of those would make the profiler
          itself a real cost, which would defeat its entire purpose.
          This trades "the very last ~0.5s might be lost on a hard
          kill" for "the profiler doesn't measurably add to the lag
          it's trying to diagnose."
        - If the file handle itself goes bad (disk full, permissions
          change, drive unmounted, etc.), it's closed and reopened
          automatically rather than left broken forever.
        - Failures are still reported, but rate-limited to once every
          5 seconds so a sustained failure doesn't spam the console —
          it still gets your attention without drowning everything else.
        """
        f = None
        fail_count = 0
        last_fail_report = 0.0
        last_fsync = 0.0
        fsync_interval = 0.5

        def _open():
            return open(self.log_path, "a", buffering=1)

        try:
            f = _open()
        except Exception as e:
            print(f"[WARN] Profiler could not open log file: {e}")

        while True:
            record = self._queue.get()
            try:
                if f is None:
                    f = _open()
                f.write(json.dumps(record) + "\n")
                f.flush()
                now = time.monotonic()
                if now - last_fsync >= fsync_interval:
                    try:
                        os.fsync(f.fileno())
                    except Exception:
                        pass  # not available/needed on every platform — best effort
                    last_fsync = now
                fail_count = 0
            except Exception as e:
                fail_count += 1
                now = time.time()
                if now - last_fail_report > 5.0:
                    print(f"[WARN] Profiler write failing ({fail_count} since last "
                          f"report): {e} — will keep retrying, no data loss beyond "
                          f"what's already been dropped while failing")
                    last_fail_report = now
                try:
                    if f is not None:
                        f.close()
                except Exception:
                    pass
                f = None  # force a reopen attempt next record

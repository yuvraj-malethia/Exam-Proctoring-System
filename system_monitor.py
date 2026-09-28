"""
system_monitor.py — GCMonitor, ResourceSampler, ensure_psutil()

Phase 1 of the black-box flight recorder: fills the gaps the first
version of frame_profiler.py couldn't see —
  - Python's own garbage collector, which pauses the ENTIRE process
    (every thread, not just the one running Python code) while it runs.
    This is instrumented directly via gc.callbacks, not inferred.
  - System-wide CPU/memory/swap — not just this process's own usage.
    A process can look perfectly healthy on its own numbers while the
    whole machine is swapping to disk because of something else running
    alongside it; only the system-wide numbers can show that.
  - Swap activity as a RATE (bytes swapped in/out since the last
    sample), not just a static percentage — a rate is what actually
    indicates active thrashing happening right now, not just "some
    swap is in use" (which can be normal and harmless on its own).

Pure diagnostics. Never changes any behavior anywhere else in the
project — only observes and records.
"""

import os
import sys
import subprocess
import time


def ensure_psutil():
    """
    Tries to import psutil. If it's missing, makes ONE attempt to
    install it quietly via pip (so you don't have to remember to do
    this yourself before every session), then retries the import.
    Never raises — always returns (module_or_None, status_message).
    """
    try:
        import psutil
        return psutil, "psutil already available"
    except ImportError:
        pass

    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--quiet", "psutil"],
            capture_output=True, timeout=30, text=True,
        )
        if result.returncode != 0:
            return None, (
                "psutil not installed and auto-install failed "
                f"(pip exit {result.returncode}): {result.stderr[-300:] if result.stderr else 'no error output'}"
            )
    except Exception as e:
        return None, f"psutil not installed and auto-install could not run: {e}"

    try:
        import psutil
        return psutil, "psutil was missing — auto-installed successfully this run"
    except ImportError:
        return None, "psutil still unavailable after attempted auto-install"


class GCMonitor:
    """
    Hooks Python's garbage collector directly via gc.callbacks, so every
    collection is recorded as its own event: which generation, how long
    it paused the process, how many objects were collected/uncollectable.

    Why this matters specifically for this project: a GC pause freezes
    EVERY thread in the process, including background threads like the
    profiler's own heartbeat — which is exactly the signature already
    seen in real data from this project (a heartbeat thread stalling
    for seconds, not just the main loop). This turns "we suspect GC" into
    something directly checkable against the timeline.

    sink(record) is called for each start/stop pair with a plain dict —
    the caller (FrameProfiler) decides how to queue/log/print it.
    """

    def __init__(self, sink, warn_threshold_ms=50.0):
        self._sink = sink
        self._warn_threshold_ms = warn_threshold_ms
        self._starts = {}  # generation -> perf_counter timestamp
        import gc
        self._gc = gc
        gc.callbacks.append(self._on_gc_event)

    def _on_gc_event(self, phase, info):
        gen = info.get("generation")
        now = time.perf_counter()
        if phase == "start":
            self._starts[gen] = now
            return
        # phase == "stop"
        t0 = self._starts.pop(gen, None)
        duration_ms = (now - t0) * 1000.0 if t0 is not None else None
        record = {
            "type": "gc",
            "t_wall": time.time(),
            "generation": gen,
            "duration_ms": round(duration_ms, 3) if duration_ms is not None else None,
            "collected": info.get("collected"),
            "uncollectable": info.get("uncollectable"),
        }
        try:
            self._sink(record)
        except Exception:
            pass  # the sink's own queue must never raise back into gc's callback path
        if duration_ms is not None and duration_ms >= self._warn_threshold_ms:
            print(f"[GC] generation {gen} pause: {duration_ms:.1f}ms "
                  f"(collected={info.get('collected')})")

    def close(self):
        try:
            self._gc.callbacks.remove(self._on_gc_event)
        except ValueError:
            pass


class ResourceSampler:
    """
    Samples BOTH this process's own resource usage AND system-wide
    usage, plus swap activity as a delta (rate), not just a snapshot.
    Returns None (gracefully) if psutil isn't available at all.
    """

    def __init__(self, psutil_module):
        self._psutil = psutil_module
        self._proc = psutil_module.Process(os.getpid()) if psutil_module else None
        if self._proc is not None:
            self._proc.cpu_percent(None)  # prime psutil's internal counter
        self._prev_swap = None
        self._prev_swap_t = None

    def sample(self):
        if self._psutil is None:
            return None
        p = self._psutil
        try:
            proc_mem = self._proc.memory_info().rss / (1024 * 1024)
            proc_cpu = self._proc.cpu_percent(None)
            proc_threads = self._proc.num_threads()

            vm = p.virtual_memory()
            sm = p.swap_memory()
            sys_cpu = p.cpu_percent(None)

            now = time.time()
            swap_in_delta_mb = swap_out_delta_mb = None
            if self._prev_swap is not None:
                dt = max(now - self._prev_swap_t, 1e-6)
                sin_delta = max(sm.sin - self._prev_swap[0], 0)
                sout_delta = max(sm.sout - self._prev_swap[1], 0)
                swap_in_delta_mb = round(sin_delta / (1024 * 1024), 3)
                swap_out_delta_mb = round(sout_delta / (1024 * 1024), 3)
            self._prev_swap = (sm.sin, sm.sout)
            self._prev_swap_t = now

            return {
                "process": {
                    "cpu_percent": proc_cpu,
                    "rss_mb": round(proc_mem, 1),
                    "num_threads": proc_threads,
                },
                "system": {
                    "cpu_percent": sys_cpu,
                    "mem_percent": vm.percent,
                    "mem_available_mb": round(vm.available / (1024 * 1024), 1),
                    "swap_percent": sm.percent,
                    "swap_used_mb": round(sm.used / (1024 * 1024), 1),
                    # deltas since the LAST sample — the actual thrashing
                    # signal. A high static swap_used can be totally
                    # harmless; ongoing swap_in/out activity is not.
                    "swap_in_delta_mb": swap_in_delta_mb,
                    "swap_out_delta_mb": swap_out_delta_mb,
                },
            }
        except Exception:
            return None

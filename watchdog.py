"""
watchdog.py — external, independent process monitor (Phase 2)

Runs as its OWN operating-system process, entirely separate from
module_headpose_multi_web.py — specifically so it keeps recording even
if the main process hard-crashes or freezes completely, including
freezes severe enough that even the main process's OWN background
threads (its heartbeat) stop responding, which real data from this
project has already shown happening. Nothing inside the main process
— no thread, no try/except — can protect against the main process
itself being frozen or killed outright. Only something outside it can.

Two jobs, every tick:

  1. LIVENESS of the main process, from the outside. Not just "is the
     PID still there" but its actual OS-level state — "running",
     "sleeping", "disk-sleep", "zombie". "Disk-sleep" specifically
     means the OS has that process blocked waiting on disk I/O, which
     is exactly what active swap-in/swap-out looks like. Catching the
     main process in that state during a stall is about as close to a
     smoking gun for swap-thrashing as user-space monitoring gets.

  2. SYSTEM-WIDE BREAKDOWN — a "Task Manager" view: the top processes
     on the WHOLE machine by RAM and, separately, by CPU. Real data
     from this project already showed the main process itself using
     only ~1.5% of total system memory while the system sat at ~90%
     used — this is the piece that can show where the other ~88% is
     actually going, something no amount of instrumenting the Python
     app itself could ever answer, because the answer isn't inside it.

Runs independently: can be started by hand, but is normally spawned
automatically as a subprocess by module_headpose_multi_web.py at
startup, so nothing extra needs to be run manually.

Writes its own JSONL log (diagnostics/watchdog_<session_id>.jsonl), on
the SAME wall-clock timeline (time.time()) as the main profiler's log,
so the two can be lined up afterward.

No video, images, or frames are ever recorded — process names, PIDs,
and numbers only.
"""

import argparse
import json
import os
import sys
import time

try:
    import psutil
except ImportError:
    print("[WATCHDOG] psutil is required but not installed. "
          "Run: pip install psutil", file=sys.stderr)
    sys.exit(1)

TOP_N = 12


def _open_log(log_dir, session_id):
    os.makedirs(log_dir, exist_ok=True)
    path = os.path.join(log_dir, f"watchdog_{session_id}.jsonl")
    return open(path, "a", buffering=1), path


def _prime_all_process_cpu():
    """
    A process's FIRST cpu_percent() call is meaningless (no prior
    sample to compare against — psutil returns 0.0). Call it once for
    every process up front so the first real tick already has a
    baseline to measure a delta against, instead of reporting every
    process as 0% CPU on tick one.
    """
    for p in psutil.process_iter():
        try:
            p.cpu_percent(None)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass


def _top_processes(n=TOP_N):
    """Task-Manager-style snapshot: top N processes by RSS memory, and
    separately by CPU%, across the WHOLE machine — not just this app."""
    rows = []
    for p in psutil.process_iter(["pid", "name"]):
        try:
            with p.oneshot():
                rss_mb = p.memory_info().rss / (1024 * 1024)
                cpu = p.cpu_percent(None)
            rows.append({
                "pid": p.pid,
                "name": p.info.get("name") or "?",
                "rss_mb": round(rss_mb, 1),
                "cpu_percent": cpu,
            })
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue  # process ended or is unreadable between iteration and read — skip it

    by_mem = sorted(rows, key=lambda r: -r["rss_mb"])[:n]
    by_cpu = sorted(rows, key=lambda r: -r["cpu_percent"])[:n]
    return by_mem, by_cpu, len(rows)


def _main_process_status(main_pid):
    """Reads the MAIN process's own state from the outside — this still
    works even if the main process is completely frozen/unresponsive,
    since it's the OS reporting the state, not the process reporting
    on itself."""
    try:
        mp_ = psutil.Process(main_pid)
        with mp_.oneshot():
            return {
                "alive": True,
                "status": mp_.status(),  # 'running'/'sleeping'/'disk-sleep'/'zombie'/etc
                "rss_mb": round(mp_.memory_info().rss / (1024 * 1024), 1),
                "cpu_percent": mp_.cpu_percent(None),
                "num_threads": mp_.num_threads(),
            }
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return {"alive": False}


def run(main_pid, session_id, log_dir, interval):
    f, path = _open_log(log_dir, session_id)
    print(f"[WATCHDOG] Watching PID {main_pid}. Logging to: {path}")

    f.write(json.dumps({
        "type": "watchdog_startup", "t_wall": time.time(),
        "main_pid": main_pid, "interval": interval,
    }) + "\n")
    f.flush()

    _prime_all_process_cpu()
    time.sleep(0.2)  # let the priming call above establish a real baseline

    missing_since = None
    grace_period = 5.0  # avoid flagging "died" from one unlucky tick

    while True:
        t0 = time.time()

        main_status = _main_process_status(main_pid)
        vm = psutil.virtual_memory()
        sm = psutil.swap_memory()
        by_mem, by_cpu, total_procs = _top_processes()

        record = {
            "type": "watchdog_tick",
            "t_wall": t0,
            "main_process": main_status,
            "system": {
                "cpu_percent": psutil.cpu_percent(None),
                "mem_percent": vm.percent,
                "mem_available_mb": round(vm.available / (1024 * 1024), 1),
                "swap_percent": sm.percent,
                "swap_used_mb": round(sm.used / (1024 * 1024), 1),
            },
            "total_processes": total_procs,
            "top_by_memory": by_mem,
            "top_by_cpu": by_cpu,
        }

        try:
            f.write(json.dumps(record) + "\n")
            f.flush()
            try:
                os.fsync(f.fileno())
            except Exception:
                pass
        except Exception as e:
            print(f"[WATCHDOG WARN] write failed: {e}", file=sys.stderr)

        if not main_status["alive"]:
            if missing_since is None:
                missing_since = t0
            elif t0 - missing_since >= grace_period:
                try:
                    f.write(json.dumps({
                        "type": "watchdog_main_ended", "t_wall": time.time(),
                        "note": "Main process no longer running — watchdog exiting.",
                    }) + "\n")
                    f.flush()
                except Exception:
                    pass
                print("[WATCHDOG] Main process ended. Exiting.")
                return
        else:
            missing_since = None

        elapsed = time.time() - t0
        time.sleep(max(0.0, interval - elapsed))


def main():
    ap = argparse.ArgumentParser(description="External watchdog for module_headpose_multi_web.py")
    ap.add_argument("--pid", type=int, required=True, help="PID of the main process to watch")
    ap.add_argument("--session-id", type=str, required=True, help="Shared session timestamp, matches profile_<id>.jsonl")
    ap.add_argument("--log-dir", type=str, default="diagnostics")
    ap.add_argument("--interval", type=float, default=1.0, help="Seconds between ticks")
    args = ap.parse_args()
    try:
        run(args.pid, args.session_id, args.log_dir, args.interval)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

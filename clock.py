"""
clock.py — the engine's single answer to "what time is it now?"

WHY THIS EXISTS
───────────────────────────────────────────────────────────────────────
Almost every algorithm here is handed the current time `t` by the main
loop. A few places took it themselves with time.time() instead — most
importantly TemporalPatternEngine, which stamps its session start that
way, and verdict.py / away_ratio(), which measure "fraction of the
session spent looking away" from it.

That is invisible with a live camera, where frame time and wall time are
the same thing. It breaks the moment a recorded video is replayed: a
20-minute clip analysed in 5 minutes would have its session length
measured in wall-clock minutes while every event is stamped in video
minutes, so the away ratio — and with it the verdict — would depend on
how fast the computer happened to be. Replaying the same clip twice
could give two different answers, which defeats the whole point of
replaying clips to validate changes.

So everything the engine calls "now" comes from here:
  - live camera (default): exactly time.time(), so live behaviour is
    unchanged byte for byte
  - video replay: the timestamp of the frame being analysed, set by
    video_run.VideoSource

Deliberately NOT used for performance timing (time.perf_counter in the
profiler) or for file names — those really are about the wall clock.
"""

import time

_source = None


def now() -> float:
    """Current engine time in epoch seconds."""
    return _source() if _source is not None else time.time()


def use_source(fn) -> None:
    """Take "now" from fn() instead of the wall clock (video replay)."""
    global _source
    _source = fn


def use_wall_clock() -> None:
    """Back to the wall clock (the default)."""
    global _source
    _source = None

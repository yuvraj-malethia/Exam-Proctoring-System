"""
session_replay.py — turn a profiler log into a scrubbable session replay.

WHY THIS EXISTS
───────────────────────────────────────────────────────────────────────
Every live session already writes a black-box log (frame_profiler.py):
each student's head angle, baseline, spread, suspicion score and verdict,
every frame, plus every logged event. That log IS a recording of the
session — without a single frame of video being stored. This module
builds a compact, down-sampled timeline from it, which the dashboard's
Session Replay (Reports page, session_replay.js) plays back through the
same 3D room code the Live Monitor uses (digital_twin.js). An invigilator
can scrub to the moment a verdict changed and watch where heads turned.

READ-ONLY: it reads log files and writes only its own cache. Nothing here
recomputes a score, a verdict or a finding — it replays what the engine
recorded at the time.

HONEST ABOUT WHAT OLDER LOGS DO NOT CONTAIN
  - pitch (up/down) was only logged from Sep 2026 -> heads stay level and
    the readout says "Not recorded"
  - seat positions (identity cx) only from Sep 2026 -> desks fall back to
    seat-number order
  - initial_baseline only from Sep 2026 -> taken as the baseline on the
    first calibrated sample of each occupant, which is what
    CalibrationEngine.initial_baseline records
  - away thresholds are rebuilt as K_SIGMA x the logged spread using the
    settings in force TODAY; a log from before a threshold change is
    replayed against the new value (the notes say so)
  - cross-student findings are not logged -> no link beams in a replay
  - logs from before the Sep 2026 verdict-logging fix repeat
    "Verdict: X" on every frame; repeats are collapsed back to changes
Each of these is stated to the viewer in meta["notes"].

PERFORMANCE
A 22-minute log is ~86 MB. Only frames at the replay rate (default 4 per
second) are JSON-parsed; the rest are skipped after a cheap timestamp
read. The result is cached in diagnostics/replay_cache/, keyed by the
log's size and modification time, so a session opens instantly the
second time and a log that is still being written is never served stale.
"""

import json
import os
import re
from datetime import datetime

from config import Config
from student_pipeline import STUDENT_COLORS, MAX_STUDENTS

CACHE_VERSION = 1
DEFAULT_HZ = 4
_ID_RE = re.compile(r"^profile_\d{4}_\d{2}_\d{2}_\d{6}$")
_TWALL_RE = re.compile(r'"t_wall":\s*([0-9]+(?:\.[0-9]+)?)')
_REGISTERED_RE = re.compile(r"^(S\d) registered as (.+?)(?: \(roll .*\))?$")
VERDICTS = ["CALIBRATING", "OK", "WATCH", "ALERT", "FLAG"]
SLOT_STATES = ["tracked", "held", "empty", "expired", "on_break"]
LABELS = [f"S{i + 1}" for i in range(MAX_STUDENTS)]


def _hex(bgr) -> str:
    b, g, r = bgr
    return f"#{r:02x}{g:02x}{b:02x}"


def _cache_path(diag_dir, stem, st, hz):
    return os.path.join(diag_dir, "replay_cache",
                        f"{stem}_{hz}hz_{st.st_size}_{int(st.st_mtime)}_v{CACHE_VERSION}.json")


def _first_last_t(path):
    """Epoch time of the first and last timestamped records, read from the
    head and tail of the file only — never the whole log."""
    with open(path, "rb") as f:
        head = f.read(65536).decode("utf-8", "replace")
        size = f.seek(0, 2)
        f.seek(max(0, size - 262144))
        tail = f.read().decode("utf-8", "replace")
    m = _TWALL_RE.search(head)
    ms = _TWALL_RE.findall(tail)
    return (float(m.group(1)) if m else None), (float(ms[-1]) if ms else None)


def list_sessions(diag_dir):
    """Recorded sessions available to replay, newest first."""
    out = []
    if not os.path.isdir(diag_dir):
        return out
    for name in os.listdir(diag_dir):
        stem, ext = os.path.splitext(name)
        if ext != ".jsonl" or not _ID_RE.match(stem):
            continue
        path = os.path.join(diag_dir, name)
        try:
            first, last = _first_last_t(path)
            st = os.stat(path)
        except OSError:
            continue
        out.append({
            "id": stem,
            "started": (datetime.fromtimestamp(first).strftime("%d %b %Y, %H:%M")
                        if first else stem),
            "duration_sec": round(last - first, 1) if first and last else None,
            "size_mb": round(st.st_size / 1e6, 1),
            "cached": os.path.exists(_cache_path(diag_dir, stem, st, DEFAULT_HZ)),
        })
    out.sort(key=lambda s: s["id"], reverse=True)
    return out


def build_timeline(diag_dir, session_id, hz=DEFAULT_HZ) -> str:
    """JSON text of the replay timeline for one session (cached)."""
    if not _ID_RE.match(session_id or ""):
        raise ValueError("unknown session id")
    path = os.path.join(diag_dir, session_id + ".jsonl")
    if not os.path.isfile(path):
        raise FileNotFoundError(session_id)
    st = os.stat(path)
    cache = _cache_path(diag_dir, session_id, st, hz)
    if os.path.exists(cache):
        with open(cache, encoding="utf-8") as f:
            return f.read()
    text = json.dumps(_build(path, session_id, hz), separators=(",", ":"))
    os.makedirs(os.path.dirname(cache), exist_ok=True)
    tmp = cache + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, cache)
    return text


def _event_kind(who, text):
    if text.startswith("Verdict: "):
        return "verdict"
    if text.upper().startswith("EVIDENCE SAVED"):
        return "evidence"
    if "Calibration complete" in text:
        return "calibration"
    if who == "SYSTEM":
        return "system"
    return "identity"


def _build(path, stem, hz):
    step = 1.0 / hz
    r2 = lambda v: None if v is None else round(float(v), 2)
    fields = ("vis", "cal", "yaw", "z", "base", "init", "std", "prob", "verdict",
              "slot", "cx", "pitch", "pcal", "pbase", "down")
    cols = {l: {k: [] for k in fields} for l in LABELS}
    times, events = [], []
    t0 = next_t = None
    init_base = {l: None for l in LABELS}
    prev_cal = {l: False for l in LABELS}
    last_verdict = {}
    seen = set()
    frames = 0
    pitch_seen = cx_seen = False
    names = {}

    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if line.startswith('{"type": "frame"'):
                frames += 1
                m = _TWALL_RE.search(line, 0, 240)
                if not m:
                    continue
                tw = float(m.group(1))
                if t0 is None:
                    t0 = next_t = tw
                if tw < next_t:
                    continue
                # After a stall, restart the grid rather than emitting a burst.
                next_t = tw + step if tw - next_t > step else next_t + step
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                times.append(round(tw - t0, 2))
                studs = rec.get("students") or {}
                slots = (rec.get("identity") or {}).get("slots") or {}
                for i, l in enumerate(LABELS):
                    s = studs.get(l) or {}
                    c = cols[l]
                    vis, cal = bool(s.get("visible")), bool(s.get("calibrated"))
                    if vis:
                        seen.add(l)
                    if cal and not prev_cal[l]:
                        init_base[l] = s.get("initial_baseline", s.get("baseline"))
                    elif not cal:
                        init_base[l] = None
                    prev_cal[l] = cal
                    c["vis"].append(1 if vis else 0)
                    c["cal"].append(1 if cal else 0)
                    c["yaw"].append(r2(s.get("yaw")))
                    c["z"].append(r2(s.get("z")))
                    c["base"].append(r2(s.get("baseline")))
                    c["init"].append(r2(init_base[l]))
                    c["std"].append(r2(s.get("std")))
                    c["prob"].append(round(float(s.get("probability") or 0.0), 1))
                    v = s.get("verdict")
                    c["verdict"].append(VERDICTS.index(v) if v in VERDICTS else None)
                    slot = slots.get(str(i)) or {}
                    ss = slot.get("state")
                    c["slot"].append(SLOT_STATES.index(ss) if ss in SLOT_STATES else None)
                    if slot.get("cx") is not None:
                        cx_seen = True
                    c["cx"].append(r2(slot.get("cx")))
                    if "pitch" in s:
                        pitch_seen = True
                    c["pitch"].append(r2(s.get("pitch")))
                    c["pcal"].append(1 if s.get("pitch_calibrated") else 0)
                    c["pbase"].append(r2(s.get("pitch_baseline")))
                    c["down"].append(1 if s.get("looking_down") else 0)
            elif line.startswith('{"type": "event"'):
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                who, text, tw = ev.get("student", ""), str(ev.get("event", "")), ev.get("t_wall")
                if tw is None:
                    continue
                if text.startswith("Verdict: "):
                    if last_verdict.get(who) == text:
                        continue      # pre-fix logs repeated this every frame
                    last_verdict[who] = text
                # Roster registrations name the student ("S1 registered as
                # Yuvraj (roll ...)"), so the replay can show people rather
                # than seat numbers, exactly as the live dashboard did.
                reg = _REGISTERED_RE.match(text)
                if reg:
                    names[reg.group(1)] = reg.group(2).strip()
                events.append({"t": tw, "student": who, "text": text[:180],
                               "kind": _event_kind(who, text), "score": ev.get("score")})

    t0 = t0 or (events[0]["t"] if events else 0.0)
    kept_events = []
    for e in events:
        # Seats nobody ever sat in only ever logged "Verdict: CALIBRATING".
        if e["kind"] == "verdict" and e["student"] in LABELS and e["student"] not in seen:
            continue
        e["t"] = round(max(e["t"] - t0, 0.0), 2)
        kept_events.append(e)

    students = {}
    for l in LABELS:
        if l not in seen:
            continue
        c = cols[l]
        if not pitch_seen:
            for k in ("pitch", "pcal", "pbase", "down"):
                c.pop(k)
        if not cx_seen:
            c.pop("cx")
        students[l] = c

    notes = []
    if not pitch_seen:
        notes.append("Up/down head angle was not recorded in this log (it was added to "
                     "the profiler in Sep 2026), so heads are shown level.")
    if not cx_seen:
        notes.append("Seat positions were not recorded in this log, so desks are ordered "
                     "by seat number rather than by where students actually sat.")
    notes.append(f"Away thresholds are rebuilt from the logged baseline and spread using "
                 f"today's setting (K_SIGMA = {Config.K_SIGMA}). Cross-student findings are "
                 f"not stored in logs, so no link beams are shown in a replay.")

    return {
        "meta": {
            "id": stem,
            "started": datetime.fromtimestamp(t0).strftime("%d %b %Y, %H:%M:%S") if t0 else None,
            "duration_sec": times[-1] if times else 0.0,
            "hz": hz,
            "frames_in_log": frames,
            "samples": len(times),
            "students": [l for l in LABELS if l in seen],
            "labels": LABELS,
            "colors": {l: _hex(STUDENT_COLORS[i]) for i, l in enumerate(LABELS)},
            "pitch_recorded": pitch_seen,
            "positions_recorded": cx_seen,
            "names": names,
            "verdicts": VERDICTS,
            "slot_states": SLOT_STATES,
            "config": {
                "k_sigma": Config.K_SIGMA,
                "calibration_frames": Config.CALIBRATION_FRAMES,
                "pitch_down_sign": Config.PITCH_DOWN_SIGN,
                # Same thresholds and names as SuspicionFusion.get_level().
                "levels": [
                    [Config.LEVEL_HIGH, "HIGH RISK", "high"],
                    [Config.LEVEL_MODERATE, "MODERATE", "moderate"],
                    [Config.LEVEL_MILD, "MILD", "mild"],
                    [Config.LEVEL_LOW, "LOW", "low"],
                    [0, "NORMAL", "normal"],
                ],
            },
            "notes": notes,
        },
        "t": times,
        "students": students,
        "events": kept_events,
    }

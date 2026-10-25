"""
demo.py — one command to run the demo

THE GAP THIS CLOSES: by October 2026 the strongest things this project
can show — four students tracked at once, eye tracking, the room line on
the risk chart, evidence appearing live — each needed the presenter to
know something first. The four-student clip needs
`Config.FACE_RESCUE_ENABLED = True` or the face model finds nobody; the
room line needs `Config.ROOM_CONTEXT_ENABLED = True`; the clip has to be
built with exactly the right tile sizes or every angle comes out
compressed. Remembering all of that in front of an evaluation panel is
how a demo goes wrong.

So each demo is one word:

    python demo.py            the four-student room (the main demo)
    python demo.py sync       four students reacting together (room context)
    python demo.py eyes       one student, head still, eyes moving
    python demo.py live       the real webcam

WHAT IT CHANGES, AND WHAT IT DOES NOT. The settings a demo needs are
switched on INSIDE THIS RUN ONLY, by setting them on Config before the
engine starts. config.py on disk is never edited, so the project's
defaults — and every validation number recorded against them — stay
exactly as they were. Every override is printed when the demo starts, so
nothing is switched on silently.

Sign-in stays as configured. If the dashboard asks for a password, it is
the invigilator account you created.

When a recorded clip finishes, the dashboard stays up showing the final
state until you press Ctrl+C. It will say live data has stopped arriving,
which is the truth: the recording is over.
"""

import argparse
import os
import sys
import threading
import time
import webbrowser

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)
sys.path.insert(0, HERE)

from config import Config  # noqa: E402

# The composite clips, with the exact settings that were verified to work.
# --tile 320 matters: a wider composite compresses every measured angle
# (see make_room_clip.py), and these values keep that ratio at 1.25.
ROOM_CLIPS = {
    "room": {
        "out": "demo/room4.mp4",
        "clips": ["innocent_steady", "innocent_fidget", "suspicious_side_glance", "eyes_only"],
        "mirror": {3},
    },
    "sync": {
        "out": "demo/room_sync.mp4",
        "clips": ["suspicious_side_glance"] * 4,
        "mirror": {2, 4},
    },
}

MODES = {
    "room": {
        "what": "Four students in one room. One person recorded four times, laid side by side.",
        "video": "demo/room4.mp4",
        "overrides": {"FACE_RESCUE_ENABLED": True, "ROOM_CONTEXT_ENABLED": True},
        "why": {
            "FACE_RESCUE_ENABLED": "the four faces are too small a share of the frame for the normal face model",
            "ROOM_CONTEXT_ENABLED": "draws 'the room' as a second line on the risk chart",
        },
    },
    "sync": {
        "what": "Four copies of the same glancing clip, so the whole room moves together.",
        "video": "demo/room_sync.mp4",
        "overrides": {"FACE_RESCUE_ENABLED": True, "ROOM_CONTEXT_ENABLED": True},
        "why": {
            "FACE_RESCUE_ENABLED": "the four faces are too small a share of the frame for the normal face model",
            "ROOM_CONTEXT_ENABLED": "this is the clip that shows room context working",
        },
    },
    "eyes": {
        "what": "One student, head held still, eyes flicking to one side.",
        "video": "clips/eyes_only.mp4",
        "overrides": {},
        "why": {},
    },
    "group": {
        "what": "The real webcam, with several real people in front of it.",
        "video": None,
        "overrides": {"FACE_RESCUE_ENABLED": True, "ROOM_CONTEXT_ENABLED": True},
        "why": {
            "FACE_RESCUE_ENABLED": "four people share one frame, so each face is small; rescue finds the ones the normal model misses",
            "ROOM_CONTEXT_ENABLED": "with real people, a moment the whole room shares can finally be seen",
        },
    },
    "live": {
        "what": "The real webcam.",
        "video": None,
        "overrides": {"ROOM_CONTEXT_ENABLED": True},
        "why": {
            "ROOM_CONTEXT_ENABLED": "harmless with one person; it only acts when others are in view",
        },
    },
}


def ensure_clip(mode):
    """Build a composite clip if it is not already on disk."""
    spec = ROOM_CLIPS.get(mode)
    if spec is None or os.path.exists(spec["out"]):
        return
    missing = [c for c in spec["clips"] if not os.path.exists(os.path.join("clips", c + ".mp4"))]
    if missing:
        raise SystemExit("cannot build the demo clip; these recordings are missing from clips/: "
                         + ", ".join(sorted(set(missing))))
    import make_room_clip
    print(f"Building {spec['out']} (first run only, about a minute)...")
    make_room_clip.build(spec["clips"], spec["out"], mirror=spec["mirror"],
                         seconds=115, tile_cap=320, tile_cap_h=360)


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Run a CPG-127 demo in one command.",
        epilog="Modes: room (default), sync, eyes, live, group. "
               "Or --video PATH to replay any recording with the group settings.")
    ap.add_argument("mode", nargs="?", default="room", choices=sorted(MODES))
    ap.add_argument("--no-browser", action="store_true", help="do not open the dashboard automatically")
    ap.add_argument("--window", action="store_true", help="also open the OpenCV camera window")
    ap.add_argument("--video", help="replay any recording (e.g. a group clip) with the group settings")
    a = ap.parse_args(argv)
    m = MODES[a.mode]
    if a.video:
        # Any recording, played with the settings a multi-person clip
        # needs. This is how a real group recording gets shown.
        if not os.path.exists(a.video):
            raise SystemExit(f"missing {a.video}")
        m = dict(MODES["group"])
        m["what"] = f"Recording: {a.video}"
        m["video"] = a.video

    if m["video"] and not a.video:
        ensure_clip(a.mode)
        if not os.path.exists(m["video"]):
            raise SystemExit(f"missing {m['video']}")

    print()
    print("=" * 64)
    print(f"  CPG-127 demo: {a.mode}")
    print(f"  {m['what']}")
    print("=" * 64)
    if m["overrides"]:
        print("  Switched on for this run only (config.py is not edited):")
        for k, v in m["overrides"].items():
            setattr(Config, k, v)
            print(f"    {k} = {v}  -- {m['why'].get(k, '')}")
    else:
        print("  No settings changed: this runs on the project's defaults.")
    url = f"http://127.0.0.1:{Config.DASHBOARD_PORT}/#live"
    print(f"\n  Dashboard: {url}")
    if m["video"]:
        print("  This is a RECORDING. The dashboard says so on every page.")
    print("=" * 64 + "\n")

    if not a.no_browser:
        threading.Timer(4.0, lambda: webbrowser.open(url)).start()

    import module_headpose_multi_web as engine
    engine.main(video_path=m["video"], show_window=a.window or m["video"] is None,
                dashboard=True, paced=True if m["video"] else None)

    if m["video"]:
        print("\nThe recording has finished. The dashboard stays up on its final")
        print("state, and the Review page has the report. Press Ctrl+C to quit.")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())

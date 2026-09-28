"""
record_clip.py — record a labelled test clip from the webcam.

WHY THIS EXISTS
───────────────────────────────────────────────────────────────────────
Clips recorded once can be replayed through the engine after every change
(video_batch.py) instead of re-acting the same behaviour live each time.
This records one clip and writes its label next to it in one step, so a
clip can never end up in the folder without anyone knowing what it was
meant to show.

Frames are saved exactly as the camera delivers them (not mirrored),
because the engine mirrors every frame itself, live or replayed — so a
replayed clip goes through precisely what the live camera would have.

USAGE
    python record_clip.py innocent_steady --label innocent --seconds 120
    python record_clip.py phone_in_lap --label suspicious --note "looks down at phone 5 times"
    python record_clip.py --list        the suggested set, and which are recorded

THE SUGGESTED SET (SUGGESTED_CLIPS below; about 2 minutes each)
Four innocent and four suspicious, so a result can say both how much it
catches and how often it wrongly flags ordinary behaviour. Two were added
in Sep 2026 for specific open questions: innocent_writing (does a frozen
up/down baseline flag ordinary writing? — PITCH_CALIBRATION_FREEZE_ENABLED)
and suspicious_quick_checks (the answer-checking pattern). The dashboard's
Validation page shows the same list and ticks off what exists.

Sit normally for the first ~10 seconds of every clip: that is when the
engine learns the student's normal head position.
"""

import argparse
import json
import os
import time
from datetime import datetime

import cv2

COUNTDOWN_SEC = 3
CALIBRATION_HINT_SEC = 10

# name, label, what to do while recording
SUGGESTED_CLIPS = [
    ("innocent_steady", "innocent",
     "Normal exam posture: read, write, think, the odd brief glance up."),
    ("innocent_writing", "innocent",
     "Head down writing for long stretches, as when answering at length."),
    ("innocent_fidget", "innocent",
     "Restless: stretch, rub your neck, short looks in different directions."),
    ("innocent_posture_shift", "innocent",
     "Settle, then lean back or shift in the chair once, and carry on."),
    ("suspicious_side_glance", "suspicious",
     "Repeated 3-4 s looks to the SAME side, as if at a neighbour's paper."),
    ("suspicious_quick_checks", "suspicious",
     "Bursts of very short looks to one side, like checking an answer."),
    ("suspicious_phone_lap", "suspicious",
     "Repeatedly look down at a phone held in your lap."),
    ("suspicious_long_look", "suspicious",
     "One or two long looks (10 s or more) at notes to one side."),
]


def print_plan(folder):
    labels = {}
    path = os.path.join(folder, "labels.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            labels = json.load(f)
    print(f"Suggested clips (folder: {folder})\n")
    for name, label, what in SUGGESTED_CLIPS:
        done = os.path.exists(os.path.join(folder, name + ".mp4"))
        print(f"  [{'x' if done else ' '}] {name:24s} {label:10s} {what}")
        if not done:
            print(f"      python record_clip.py {name} --label {label}")
    extra = sorted(k for k in labels if os.path.splitext(k)[0] not in {n for n, _, _ in SUGGESTED_CLIPS})
    if extra:
        print("\n  Also recorded: " + ", ".join(extra))


def main():
    ap = argparse.ArgumentParser(
        description="Record a labelled webcam clip for video replay testing.",
        epilog="See the suggested set with: python record_clip.py --list")
    ap.add_argument("name", nargs="?", help="clip name, e.g. phone_in_lap (saved as <folder>/<name>.mp4)")
    ap.add_argument("--label", choices=["innocent", "suspicious"],
                    help="what the clip is meant to show (required when recording)")
    ap.add_argument("--list", action="store_true", help="show the suggested clips and which exist")
    ap.add_argument("--seconds", type=float, default=120, help="length to record (default 120)")
    ap.add_argument("--note", default="", help="what actually happens in the clip")
    ap.add_argument("--camera", type=int, default=0, help="camera index (default 0, same as the engine)")
    ap.add_argument("--folder", default="clips", help="where clips are kept (default: clips)")
    ap.add_argument("--overwrite", action="store_true", help="replace an existing clip with this name")
    args = ap.parse_args()
    if args.list:
        print_plan(args.folder)
        return
    if not args.name or not args.label:
        ap.error("give a clip name and --label innocent|suspicious (or --list)")

    name = "".join(c if c.isalnum() or c in "-_" else "_" for c in args.name)
    os.makedirs(args.folder, exist_ok=True)
    path = os.path.join(args.folder, f"{name}.mp4")
    if os.path.exists(path) and not args.overwrite:
        print(f"[STOP] {path} already exists. Pick another name or add --overwrite.")
        return

    cap = cv2.VideoCapture(args.camera)
    if not cap.isOpened():
        print("[ERROR] Could not open the webcam.")
        return

    # Countdown doubles as a frame-rate measurement: webcams often deliver
    # fewer frames than they advertise (especially in dim light), and the
    # file must be written at the rate frames REALLY arrive or replayed
    # time would run fast or slow.
    window = "Recording clip - ESC to stop"
    frames, started = 0, time.perf_counter()
    while True:
        ok, frame = cap.read()
        if not ok:
            print("[ERROR] Camera stopped delivering frames.")
            cap.release()
            return
        frames += 1
        elapsed = time.perf_counter() - started
        if elapsed >= COUNTDOWN_SEC:
            break
        view = cv2.flip(frame, 1)
        cv2.putText(view, f"Starting in {COUNTDOWN_SEC - int(elapsed)}", (30, 60),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.4, (0, 210, 255), 3, cv2.LINE_AA)
        cv2.putText(view, "Sit normally for the first 10 seconds", (30, 105),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (230, 230, 230), 2, cv2.LINE_AA)
        cv2.imshow(window, view)
        if cv2.waitKey(1) & 0xFF == 27:
            cap.release()
            cv2.destroyAllWindows()
            print("Cancelled before recording started.")
            return
    fps = max(5.0, min(60.0, round(frames / elapsed, 1)))
    h, w = frame.shape[:2]
    writer = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"mp4v"), fps, (w, h))
    if not writer.isOpened():
        print(f"[ERROR] Could not create {path}")
        cap.release()
        return

    print(f"Recording {path}: {w}x{h} at {fps} fps for up to {args.seconds:.0f}s (ESC stops early)")
    written, rec_start = 0, time.perf_counter()
    while True:
        ok, frame = cap.read()
        if not ok:
            print("[WARN] Camera stopped delivering frames; keeping what was recorded.")
            break
        writer.write(frame)          # saved un-mirrored, exactly as captured
        written += 1
        elapsed = time.perf_counter() - rec_start
        view = cv2.flip(frame, 1)    # preview mirrored, like a mirror
        cv2.circle(view, (30, 30), 10, (0, 0, 255), -1)
        cv2.putText(view, f"REC {elapsed:5.1f}s / {args.seconds:.0f}s", (50, 40),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 2, cv2.LINE_AA)
        if elapsed < CALIBRATION_HINT_SEC:
            cv2.putText(view, "Calibrating: sit normally", (30, 80),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 210, 255), 2, cv2.LINE_AA)
        cv2.imshow(window, view)
        if (cv2.waitKey(1) & 0xFF) == 27 or elapsed >= args.seconds:
            break
    writer.release()
    cap.release()
    cv2.destroyAllWindows()

    real_fps = written / max(time.perf_counter() - rec_start, 1e-6)
    if abs(real_fps - fps) > 0.15 * fps:
        print(f"[WARN] Frames arrived at {real_fps:.1f} fps but the file says {fps} fps; "
              f"replayed timing will be off by about {abs(real_fps / fps - 1) * 100:.0f}%. "
              f"Better light usually steadies the camera's frame rate — consider re-recording.")

    labels_path = os.path.join(args.folder, "labels.json")
    labels = {}
    if os.path.exists(labels_path):
        with open(labels_path, encoding="utf-8") as f:
            labels = json.load(f)
    labels[os.path.basename(path)] = {
        "label": args.label,
        "note": args.note,
        "recorded_at": datetime.now().isoformat(timespec="seconds"),
        "seconds": round(written / fps, 1),
        "fps": fps,
        "resolution": f"{w}x{h}",
    }
    with open(labels_path, "w", encoding="utf-8") as f:
        json.dump(labels, f, indent=2)

    print(f"Saved {written} frames ({written / fps:.1f}s) to {path}, labelled '{args.label}'.")
    print(f"Watch it replay:  python module_headpose_multi_web.py --video {path}")
    print(f"Check all clips:  python video_batch.py {args.folder}")


if __name__ == "__main__":
    main()

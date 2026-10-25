"""
make_room_clip.py — build a four-student clip from four single-student ones

THE GAP THIS CLOSES: this engine was built to watch a row of students. It
tracks four seats, holds a seat when its occupant leaves, archives the
outgoing record when the seat changes hands, orders desks by real
left-to-right position, and looks for relationships BETWEEN students.
None of that has ever been seen working, because every clip and every
live test has had exactly one person in it — Yuvraj, alone, with a
webcam. A demo that shows one face in a box makes a four-student system
look like a one-student toy.

So: take four recordings of one person and lay them side by side into a
single wide frame. The engine is told nothing. It sees four faces at four
positions, moving independently, and runs the multi-student path it has
always had.

BE STRAIGHT ABOUT WHAT THIS IS. It is four recordings of the same person
composited into one frame, not four people. It proves the multi-student
machinery runs on real faces; it proves nothing about telling different
people apart, and the cross-student findings it produces are findings
about four recordings that never shared a room. Say so when showing it.

WHY THE LAYOUT IS TWO STAGGERED ROWS. Three constraints collide:

  1. MediaPipe cannot read an extremely wide frame. Four 640x480 tiles
     laid in one row give 2560x480, and the face model squashes that to
     its square input until the faces are too small to find. MEASURED:
     zero faces on the 2560x480 composite, one face on a single tile cut
     out of the very same frame. This was the first thing tried and it
     failed outright.
  2. Seat order and cross-student geometry are read from horizontal
     position (`identity.slots[sid].cx`), so four students sharing two
     x positions — which a plain 2x2 grid gives — would stack two desks
     on one spot in the 3D room and leave correlation.py's left/right
     reasoning ambiguous.
  3. Duplicate-detection filtering uses full 2D distance
     (`IDENTITY_MIN_FACE_SEPARATION_FRAC` of the frame width), so tiles
     must be far enough apart in the plane as well.

Two rows offset by half a tile satisfies all three: a 1600x960 frame
that the face model handles comfortably, four distinct x positions
(320, 640, 960, 1280 for 640x480 tiles) each at least a fifth of the
frame apart, and clear 2D separation. It also happens to look like a
real hall, where rows are usually staggered so nobody sits directly
behind anyone.

NOTE THE SEAT ORDER. Because the second row is offset, the engine's
left-to-right order is not the order the tiles are listed in: tile 1,
tile 3, tile 2, tile 4. The summary prints the on-screen order so the
two are never confused.

THE REAL CONSTRAINT IS HOW BIG A FACE IS RELATIVE TO THE FRAME, not the
frame's shape. MediaPipe's face detector resizes whatever it is given
down to a small square input, so what matters is the face's share of the
frame width. MEASURED on this footage: a face is about 127px wide, which
is 20% of a single 640px tile and survives the resize, but only 8% of a
1600px composite and does not. Four tiles at full size is therefore
impossible however they are arranged — 2560x480 found zero faces, and so
did 1600x960.

So each source is CROPPED to a window around that clip's own face before
being laid out. The window is sized from the actual range the head
travels across the whole clip, with margin, so the student never leaves
their tile. Cropping (not scaling) is what keeps the face large in
pixels while making the frame small enough that the face is still a
healthy fraction of it. Nothing is resized, so every face reaches the
engine at exactly the pixel size it was recorded at.

`Config.MIN_FACE_AREA_FRAC` compares each detection against the LARGEST
one in frame rather than against the frame, so four similar faces are
all kept.

THE COMPOSITE SHRINKS EVERY MEASURED ANGLE, BY A KNOWN FACTOR.
`extract_head_pose()` builds its camera matrix from the FRAME WIDTH:
`cam = [[w, 0, h/2], [0, w, w/2], [0, 0, 1]]`. Widening the frame tells
solvePnP the lens got longer, so the same head turn reads as a smaller
angle. MEASURED, same landmarks, only the declared frame size changed:

    yaw   -19.60 deg alone  ->   -7.83 deg inside a 1597px composite
    pitch  14.76 deg alone  ->    3.08 deg

A 2.5x compression, not a rounding error. The first build of this tool
hit it and scored all four students OK with peaks near zero. The ratio
is composite_width / source_width, so the fix is to keep the composite
narrow — hence --tile, which caps each crop. At 320px tiles the
composite is 800px against a 640px source, a ratio of 1.25, and
`eyes_only` scores ALERT at 45.1% here against ALERT at 53.7%
standalone. A clip whose head travels further than the tile is wide
will be clipped and score lower: `suspicious_side_glance` drops from
FLAG 80.2% to WATCH 48.3%. Treat composite scores as indicative, never
as comparable to the single-clip batch.

THIS NEEDS `Config.FACE_RESCUE_ENABLED = True`. Even cropped, the faces
are a smaller share of the frame than FaceMesh wants, so the normal path
finds nothing and the YuNet-assisted rescue path recovers all four. That
is what face_rescue.py exists for, and it is already proven to agree
with the normal path to within 0.15 degrees of yaw.

MIRRORING. A clip can be flipped horizontally with --mirror. That turns a
student who glances left into one who glances right, which is what makes
two of them look TOWARD each other instead of both looking the same way —
the arrangement cross-student correlation exists to find. It is a real
transformation of the data and it is printed in the output summary, so
nobody later mistakes a mirrored tile for what was recorded.

Output goes to demo/ rather than clips/ on purpose: video_batch.py walks
clips/, and a composite has no single honest label — it contains both
innocent and suspicious behaviour at once. It must never be marked in
the validation scoreboard.

    python make_room_clip.py --out demo/room4.mp4 \
        innocent_steady innocent_fidget suspicious_side_glance eyes_only \
        --mirror 3
    python module_headpose_multi_web.py --video demo/room4.mp4
"""

import argparse
import os
import sys

import cv2
import numpy as np

CLIPS_DIR = "clips"
OUT_DIR = "demo"


def face_window(cap, meta, samples=40, margin=0.55, min_w=300, max_w=None, max_h=None):
    """Where does this clip's face live, across the whole clip?

    Returns a crop box wide enough to hold every position the head
    reaches, plus margin. Sampling the whole clip rather than the first
    few seconds matters: innocent_posture_shift and the glance clips
    move the head a long way, and a window fitted to the opening frames
    would cut the student in half later on.

    Falls back to a centred window if no face is ever found, so a clip
    the detector dislikes still produces something rather than crashing.
    """
    import cv2 as _cv
    W, H, n = meta["w"], meta["h"], meta["frames"]
    det = _cv.FaceDetectorYN.create(
        os.path.join("models", "face_detection_yunet_2023mar.onnx"), "", (W, H), 0.6)
    xs0, ys0, xs1, ys1 = [], [], [], []
    for i in range(samples):
        cap.set(_cv.CAP_PROP_POS_FRAMES, int(n * i / samples))
        ok, frame = cap.read()
        if not ok:
            continue
        _, faces = det.detect(frame)
        for b in (faces if faces is not None else []):
            x, y, w, h = (float(v) for v in b[:4])
            xs0.append(x); ys0.append(y); xs1.append(x + w); ys1.append(y + h)
    cap.set(_cv.CAP_PROP_POS_FRAMES, 0)
    if not xs0:
        cw = max(min_w, W // 2)
        return ((W - cw) // 2, 0, cw, H)

    fx0, fy0, fx1, fy1 = min(xs0), min(ys0), max(xs1), max(ys1)
    mw, mh = (fx1 - fx0) * margin, (fy1 - fy0) * margin
    x0 = int(max(0, fx0 - mw)); y0 = int(max(0, fy0 - mh))
    x1 = int(min(W, fx1 + mw)); y1 = int(min(H, fy1 + mh))
    if x1 - x0 < min_w:
        cx = (x0 + x1) // 2
        x0 = max(0, cx - min_w // 2); x1 = min(W, x0 + min_w); x0 = max(0, x1 - min_w)
    # A hard ceiling on the window. The composite's width divided by the
    # source's width is exactly how much every measured angle shrinks
    # (see the docstring), so keeping tiles small is not cosmetic — it
    # is the difference between a demo that scores like the originals
    # and one that does not. Centre the capped window on the face.
    if max_w and x1 - x0 > max_w:
        cx = (x0 + x1) // 2
        x0 = max(0, min(W - max_w, cx - max_w // 2)); x1 = x0 + max_w
    if max_h and y1 - y0 > max_h:
        cy = (y0 + y1) // 2
        y0 = max(0, min(H - max_h, cy - max_h // 2)); y1 = y0 + max_h
    return (x0, y0, x1 - x0, y1 - y0)


def _open(name):
    path = name if os.path.exists(name) else os.path.join(CLIPS_DIR, name + ".mp4")
    if not os.path.exists(path):
        raise SystemExit(f"clip not found: {path}")
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        raise SystemExit(f"could not open: {path}")
    return path, cap


def build(names, out_path, mirror=(), seconds=None, fps=None, crop=True,
          tile_cap=None, tile_cap_h=None):
    """Lay the clips side by side into one wide video. Returns a summary."""
    paths, caps, metas = [], [], []
    for n in names:
        p, c = _open(n)
        paths.append(p)
        caps.append(c)
        metas.append({
            "fps": c.get(cv2.CAP_PROP_FPS) or 25.0,
            "w": int(c.get(cv2.CAP_PROP_FRAME_WIDTH)),
            "h": int(c.get(cv2.CAP_PROP_FRAME_HEIGHT)),
            "frames": int(c.get(cv2.CAP_PROP_FRAME_COUNT) or 0),
        })

    # Clips were recorded in separate sittings and their frame rates
    # differ slightly (25.7 to 27.1 on the existing set). Picking one
    # timebase and sampling every source BY TIME keeps them in step; just
    # pairing frame N with frame N would let them drift apart by seconds
    # over a two-minute clip.
    out_fps = fps or min(m["fps"] for m in metas)
    span = min(m["frames"] / m["fps"] for m in metas)
    if seconds:
        span = min(span, float(seconds))
    n_out = int(span * out_fps)

    windows = None
    if crop:
        print("  finding where each face sits...", flush=True)
        windows = [face_window(caps[k], metas[k], max_w=tile_cap, max_h=tile_cap_h)
                   for k in range(len(names))]
        for k, w in enumerate(windows):
            print(f"    {os.path.basename(paths[k])}: crop {w[2]}x{w[3]} at ({w[0]},{w[1]})")
        tile_w = max(w[2] for w in windows)
        tile_h = max(w[3] for w in windows)
    else:
        tile_w = max(m["w"] for m in metas)
        tile_h = max(m["h"] for m in metas)
    # Two staggered rows; see the module docstring for why not one row
    # and why not a plain grid.
    places = [(0, 0), (tile_w, 0), (tile_w // 2, tile_h), (tile_w + tile_w // 2, tile_h)]
    places = places[:len(names)]
    frame_w = max(x for x, _ in places) + tile_w
    frame_h = max(y for _, y in places) + tile_h

    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    writer = cv2.VideoWriter(out_path, cv2.VideoWriter_fourcc(*"mp4v"),
                             out_fps, (frame_w, frame_h))
    if not writer.isOpened():
        raise SystemExit(f"could not write: {out_path}")

    canvas = np.zeros((frame_h, frame_w, 3), dtype=np.uint8)
    last = [None] * len(names)

    for i in range(n_out):
        t = i / out_fps
        for k, cap in enumerate(caps):
            want = int(t * metas[k]["fps"])
            cap.set(cv2.CAP_PROP_POS_FRAMES, want)
            ok, frame = cap.read()
            if not ok:
                # A source that ends early or drops a frame holds its last
                # one rather than going black: a sudden black tile would
                # look to the engine like that student vanished, and the
                # seat-handover path would fire for no reason.
                if last[k] is None:
                    continue
                frame = last[k]
            if windows is not None:
                wx, wy, ww, wh = windows[k]
                frame = frame[wy:wy + wh, wx:wx + ww]
            if frame.shape[0] != tile_h or frame.shape[1] != tile_w:
                # Pad rather than resize: resizing would change the face's
                # pixel size, which is the one thing this whole layout
                # exists to protect.
                pad = np.zeros((tile_h, tile_w, 3), dtype=np.uint8)
                fh, fw = frame.shape[:2]
                fh, fw = min(fh, tile_h), min(fw, tile_w)
                pad[:fh, :fw] = frame[:fh, :fw]
                frame = pad
            if (k + 1) in mirror:
                frame = cv2.flip(frame, 1)
            last[k] = frame
            ox, oy = places[k]
            canvas[oy:oy + tile_h, ox:ox + tile_w] = frame
        writer.write(canvas)
        if i % 250 == 0:
            print(f"  {i}/{n_out} frames", flush=True)

    writer.release()
    for c in caps:
        c.release()
    seats = [{"seat": k + 1, "clip": os.path.basename(paths[k]),
              "mirrored": (k + 1) in mirror,
              "cx": places[k][0] + tile_w // 2,
              "row": 1 if places[k][1] == 0 else 2} for k in range(len(names))]
    return {
        "out": out_path, "fps": round(out_fps, 2), "frames": n_out,
        "seconds": round(span, 1), "size": f"{frame_w}x{frame_h}",
        "seats": seats,
        "left_to_right": [s["clip"] for s in sorted(seats, key=lambda q: q["cx"])],
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("clips", nargs="+", help="clip names (without .mp4) or paths, left to right")
    ap.add_argument("--out", default=os.path.join(OUT_DIR, "room.mp4"))
    ap.add_argument("--mirror", default="", help="1-based seat numbers to flip, e.g. 3 or 2,4")
    ap.add_argument("--seconds", type=float, help="trim to this many seconds")
    ap.add_argument("--fps", type=float, help="output frame rate (default: the slowest source)")
    ap.add_argument("--tile", type=int, help="cap each crop's width in px")
    ap.add_argument("--tile-h", type=int, help="cap each crop's height in px")
    ap.add_argument("--no-crop", action="store_true",
                    help="lay out whole frames instead of cropping around each face. "
                         "The face model will almost certainly find nothing; see the "
                         "module docstring.")
    a = ap.parse_args(argv)

    if len(a.clips) > 4:
        raise SystemExit("the engine tracks at most 4 seats (student_pipeline.MAX_STUDENTS)")
    mirror = {int(x) for x in a.mirror.replace(" ", "").split(",") if x}

    print(f"Building {a.out} from {len(a.clips)} clips...")
    info = build(a.clips, a.out, mirror=mirror, seconds=a.seconds, fps=a.fps,
                 crop=not a.no_crop, tile_cap=a.tile, tile_cap_h=a.tile_h)
    print(f"\nWrote {info['out']}  {info['size']}  {info['seconds']}s @ {info['fps']}fps")
    for s in info["seats"]:
        print(f"  tile {s['seat']} (row {s['row']}, x={s['cx']}): {s['clip']}"
              + ("  (MIRRORED)" if s["mirrored"] else ""))
    print("  on screen, left to right: " + ", ".join(info["left_to_right"]))
    print("\nThis is one person, recorded four times, laid side by side.")
    print("It exercises the multi-student path on real faces. It does NOT")
    print("show four people, and any between-student finding it produces is")
    print("a finding about four recordings that never shared a room.")
    print("")
    print("Set Config.FACE_RESCUE_ENABLED = True before replaying this: the")
    print("faces are small enough that the normal face model finds none, and")
    print("the rescue path is what recovers them. Scores are compressed by")
    print("roughly composite_width / source_width and are NOT comparable to")
    print("the single-clip validation batch.")
    print(f"\n  python module_headpose_multi_web.py --video {info['out']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

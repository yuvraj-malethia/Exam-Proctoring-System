"""
face_rescue.py — FaceRescue

THE GAP THIS CLOSES: MediaPipe's FaceMesh drops the face far more often
than the face is actually gone. Measured on Yuvraj's own clips
(5 Oct 2026), with the frames flipped exactly as the engine flips them:

    clip                    FaceMesh   a plain detector sees a face
    innocent_writing          69.9%            96.4%
    innocent_posture_shift    41.2%            69.6% (YuNet 97.7%)
    suspicious_phone_lap      25.1%            62.5% (YuNet 98.1%)

The landmark model's own internal face detector is what gives up on a low
head; the landmarks themselves are usually recoverable if something else
points at where the face is. So: when FaceMesh comes back short, YuNet
(already inside OpenCV, 232 KB, about 2 ms) is asked for a box, and
FaceMesh is run a second time on that crop alone.

WHAT IT ACTUALLY BUYS, measured on the full clips:
    innocent_posture_shift   41.2% -> 60.0%   (32% of dropped frames back)
    suspicious_phone_lap     25.1% -> 28.5%   ( 4% of dropped frames back)

So it is worth real frames on a student leaning low over the page, and
almost nothing on a head tipped right down into a lap — which is the
expected result, not a disappointment: YuNet still reports a box there,
but it is finding the crown of a head, and a crown has no face geometry
to measure. Nothing recovers an angle that is not in the image.

ALSO MEASURED AND REJECTED: MediaPipe's newer Face Landmarker (Tasks
API) as a straight replacement for FaceMesh. It was WORSE on this
footage — 32.7% against FaceMesh's 86.4% on innocent_writing. Recorded
here so it is not tried again as an obvious idea.

THIS CHANGES SCORES, so it is OFF by default
(Config.FACE_RESCUE_ENABLED). Recovered frames reach the scoring
pipeline exactly like any other frame, which is the point — and also why
turning it on is a decision with evidence behind it, not a default.
Rescued landmarks are remapped back into full-frame coordinates before
anybody sees them, so identity tracking, the bounding boxes and
extract_head_pose() all work in the one coordinate space they always
have.

Fails soft and loudly, like body_presence.py: a missing model file
disables the feature and says so once.
"""

import os

from config import Config


class _Point:
    """One landmark in full-frame normalised coordinates."""
    __slots__ = ("x", "y", "z")

    def __init__(self, x, y, z):
        self.x = x
        self.y = y
        self.z = z


class _Remapped:
    """Stands in for MediaPipe's landmark list. Same attribute shape
    (`.landmark[i].x/.y/.z`), so every existing reader works unchanged."""
    __slots__ = ("landmark",)

    def __init__(self, points):
        self.landmark = points


class FaceRescue:
    def __init__(self, max_students, model_path=None):
        self.max_students = max_students
        self.model_path = model_path or Config.FACE_RESCUE_MODEL
        self.available = False
        self.reason = "not started"
        self._yunet = None
        self._mesh = None
        self._size = None
        self.attempts = 0
        self.recovered = 0

    def start(self):
        if not Config.FACE_RESCUE_ENABLED:
            self.reason = "disabled in Config"
            return False
        path = self.model_path
        if not os.path.isabs(path):
            path = os.path.join(os.getcwd(), path)
        if not os.path.exists(path):
            self.reason = f"model file not found: {path}"
            print(f"[RESCUE] face rescue off — {self.reason}")
            return False
        try:
            import cv2
            import mediapipe as mp
            self._cv2 = cv2
            self._yunet = cv2.FaceDetectorYN.create(
                path, "", (320, 320), Config.FACE_RESCUE_MIN_CONF)
            # static_image_mode: each crop is a fresh image with no
            # tracking history to carry over, so force a real detection
            # instead of letting it look for last frame's landmarks.
            self._mesh = mp.solutions.face_mesh.FaceMesh(
                static_image_mode=True, max_num_faces=1, refine_landmarks=False,
                min_detection_confidence=Config.FACE_RESCUE_MESH_CONF)
        except Exception as exc:  # noqa: BLE001 — reported, never swallowed
            self.reason = f"{type(exc).__name__}: {exc}"
            print(f"[RESCUE] face rescue off — {self.reason}")
            return False
        self.available = True
        self.reason = "ready"
        print(f"[RESCUE] face rescue on — {os.path.basename(path)}")
        return True

    def close(self):
        if self._mesh is not None:
            try:
                self._mesh.close()
            except Exception:  # noqa: BLE001 — shutdown only
                pass
            self._mesh = None

    # ── the rescue ────────────────────────────────────────────────────
    def rescue(self, rgb, bgr, w, h, found, expected):
        """
        rgb/bgr : this frame, flipped as the engine flips it
        found   : landmark lists FaceMesh already returned
        expected: how many faces we have reason to expect
        Returns the extra landmark lists, in FULL-FRAME coordinates.
        """
        if not self.available or len(found) >= expected:
            return []
        self.attempts += 1
        try:
            if self._size != (w, h):
                self._yunet.setInputSize((w, h))
                self._size = (w, h)
            _, boxes = self._yunet.detect(bgr)
        except Exception as exc:  # noqa: BLE001 — one failure disables it
            self.available = False
            self.reason = f"{type(exc).__name__}: {exc}"
            print(f"[RESCUE] face rescue stopped — {self.reason}")
            return []
        if boxes is None or not len(boxes):
            return []

        # Where FaceMesh already has a face, leave it alone — a second
        # copy of one student would look to the identity tracker exactly
        # like a phantom detection, the bug class this project has
        # already had to defend against twice.
        taken = []
        for lmk in found:
            xs = [p.x for p in lmk.landmark]
            ys = [p.y for p in lmk.landmark]
            taken.append(((min(xs) + max(xs)) / 2 * w, (min(ys) + max(ys)) / 2 * h))

        margin = Config.FACE_RESCUE_MARGIN
        out = []
        for box in boxes:
            if len(found) + len(out) >= expected:
                break
            bx, by, bw, bh = (float(v) for v in box[:4])
            cx, cy = bx + bw / 2, by + bh / 2
            if any(abs(cx - tx) < bw and abs(cy - ty) < bh for tx, ty in taken):
                continue
            mx, my = bw * margin, bh * margin
            x0 = max(0, int(bx - mx)); y0 = max(0, int(by - my))
            x1 = min(w, int(bx + bw + mx)); y1 = min(h, int(by + bh + my))
            cw, ch = x1 - x0, y1 - y0
            if cw < 24 or ch < 24:
                continue
            res = self._mesh.process(rgb[y0:y1, x0:x1])
            if not res.multi_face_landmarks:
                continue
            crop = res.multi_face_landmarks[0]
            # Back into full-frame normalised space. z is a depth in
            # units of the image width it was measured in, so it is
            # rescaled by the same factor as x.
            scale = cw / float(w)
            out.append(_Remapped([
                _Point((x0 + p.x * cw) / w, (y0 + p.y * ch) / h, p.z * scale)
                for p in crop.landmark
            ]))
            self.recovered += 1
        return out

"""
body_presence.py — BodyPresence

THE GAP THIS CLOSES: presence.py can say "this seat has had no
measurable face for 29 seconds", but on its own it cannot say whether
that is a student bent low over their paper or an empty chair. Those two
need very different responses from an invigilator, and a face model
cannot tell them apart — in both cases there is no face.

A body model can. Measured on Yuvraj's eight validation clips (5 Oct
2026), MediaPipe's pose model tracked him in 100% of frames, including
every single frame in which no face could be found by any of the three
face models tried. Shoulders and torso stay visible long after the face
has tipped out of view.

WHAT THIS DELIBERATELY DOES NOT DO: it does not classify posture and it
does not feed suspicion. TWO separate attempts to read "looking into the
lap" from pose landmarks have now been tested and FAILED, and both are
recorded here so nobody rediscovers them as good ideas:

  1. "Head below the shoulder line" — the innocent writing clip scored
     HIGHER on it (64.9% of frames) than the lap-phone clip (37.8%).

  2. "Hands below the desk" (13 Oct 2026). Refuted outright by its own
     data. Wrist height measured against the shoulder line and divided
     by shoulder width, median over each clip:

         innocent_steady   1.49      innocent_fidget   1.35
         innocent_writing  1.44      eyes_only         1.41
         side_glance       1.34      PHONE IN LAP      0.91

     Larger means lower in frame. The hands sit LOWER while writing than
     while using a phone in the lap, and MediaPipe reports them as LESS
     visible too (median wrist visibility 0.13 writing against 0.31 for
     the lap clip). Any rule of the form "the hands dropped out of sight,
     so something is happening" flags writing HARDER than it flags the
     thing it was built to catch.

     A different feature does separate the lap clip — wrist-to-face
     distance, normalised by shoulder width, below 1.5 — firing on 83.4%
     of the lap clip against 1.7% of steady, 2.2% of writing, 0.2% of
     side_glance and 0.0% of eyes_only. But it fires on **34.5% of
     innocent_fidget**, because stretching, scratching your head and
     rubbing your neck all put a hand near your face. A detector built on
     it would flag a restless innocent student a third of the time, which
     is precisely the unfairness this project has twice had to fix.
     It is also one suspicious clip, from one person: fitting a rule to
     that is overfitting with extra steps.

     This agrees with the literature rather than contradicting it. The
     one published system claiming under-desk hand features (improved
     YOLOv8, Springer 2025) notes that comparison models "frequently
     misclassify page-turning actions for cheating", and MediaPipe's own
     pose accuracy is documented as degrading under occlusion — a desk
     being exactly that. No commercial proctoring vendor documents
     hands-below-desk detection at all; the nearest things, Honorlock's
     secondary-device and Apple-Handoff detection, are network and
     operating-system signals, not vision of hands.

So the hand measurements below are REPORTING ONLY. They are recorded and
shown next to a seat the engine could not read, so an invigilator can see
what the hands were doing at that moment, and so that a future clip set
with more than one suspicious example has data to be judged against.
Nothing here reaches suspicion.py, temporal.py or verdict.py.

All this module decides is: is there a person in this seat, yes or no.

COST. This is a second model in a loop whose frame rate has already been
investigated at length (see the diagnostics section of CLAUDE.md), so it
is off unless Config.PRESENCE_BODY_CHECK_ENABLED is set, and even then:
  - it runs only while at least one seat is actually unmeasured
  - at most Config.PRESENCE_BODY_CHECK_HZ times a second
A hall where every face is visible never runs it at all.

FAILS SOFT, LOUDLY. A missing model file or an unavailable Tasks API
disables the check and prints why, once. It must never take down a live
exam, and it must never fail quietly either — a silent failure here
would look exactly like "nobody is ever absent", which is the same class
of bug as the swallowed exception that once emptied the PDF report's
evidence section.
"""

import os

from config import Config


def _hand_features(pose):
    """Where the hands are, in units that do not depend on how close the
    student sits: everything is divided by their own shoulder width.

    Returns None when the shoulders cannot be located, because every
    number here is relative to them. Reporting only — see the module
    docstring for the measurements that rule these out as a detector.
    """
    try:
        ls, rs = pose[11], pose[12]
        lw, rw, nose = pose[15], pose[16], pose[0]
    except (IndexError, TypeError):
        return None
    shw = ((ls.x - rs.x) ** 2 + (ls.y - rs.y) ** 2) ** 0.5
    if shw < 1e-4:
        return None
    scx, scy = (ls.x + rs.x) / 2.0, (ls.y + rs.y) / 2.0
    height = (((lw.y - scy) / shw) + ((rw.y - scy) / shw)) / 2.0
    near = min(
        (((lw.x - nose.x) ** 2 + (lw.y - nose.y) ** 2) ** 0.5) / shw,
        (((rw.x - nose.x) ** 2 + (rw.y - nose.y) ** 2) ** 0.5) / shw,
    )
    vis = min(getattr(lw, "visibility", 0.0), getattr(rw, "visibility", 0.0))
    return {
        "height": round(height, 3),    # larger = lower in frame
        "near_face": round(near, 3),   # smaller = closer to the head
        "visibility": round(vis, 3),   # as the model reports it
    }


def describe_hands(h):
    """One plain phrase for a reader. Deliberately descriptive and never
    a judgement: "near their face" is a fact, "reaching for a phone" is
    an accusation the measurements do not support."""
    if not h:
        return None
    if h["visibility"] < 0.10:
        where = "out of the camera's view"
    elif h["near_face"] < 1.5:
        where = "up near their face"
    elif h["height"] > 1.3:
        where = "low, around desk height"
    else:
        where = "in front of them"
    return "hands " + where


class BodyPresence:
    """
    One instance per session. Call maybe_check() each frame; it decides
    for itself whether this is a frame worth spending the model on.
    """

    def __init__(self, max_students, model_path=None):
        self.max_students = max_students
        self.model_path = model_path or Config.PRESENCE_BODY_MODEL
        self.available = False
        self.reason = "not started"
        self._landmarker = None
        self._last_run = None
        self.runs = 0
        # Latest hand descriptors per slot, reporting only. See the
        # module docstring for why these are not a detector.
        self.hands = {}

    # ── lifecycle ─────────────────────────────────────────────────────
    def start(self):
        """Load the model. Returns True if the check can run. Safe to
        call when the feature is off — it simply reports why not."""
        if not Config.PRESENCE_BODY_CHECK_ENABLED:
            self.reason = "disabled in Config"
            return False
        path = self.model_path
        if not os.path.isabs(path):
            path = os.path.join(os.getcwd(), path)
        if not os.path.exists(path):
            self.reason = f"model file not found: {path}"
            print(f"[PRESENCE] body check off — {self.reason}")
            return False
        try:
            import mediapipe as mp
            from mediapipe.tasks import python as mp_python
            from mediapipe.tasks.python import vision
        except ImportError as exc:
            self.reason = f"mediapipe tasks unavailable ({exc})"
            print(f"[PRESENCE] body check off — {self.reason}")
            return False
        try:
            # Loaded as bytes, not by path: the Tasks API resolves a
            # relative model path against its own package directory, so
            # a Windows absolute path gets mangled into nonsense like
            # site-packages/D:\...\model.task.
            with open(path, "rb") as fh:
                blob = fh.read()
            self._mp = mp
            self._landmarker = vision.PoseLandmarker.create_from_options(
                vision.PoseLandmarkerOptions(
                    base_options=mp_python.BaseOptions(model_asset_buffer=blob),
                    running_mode=vision.RunningMode.IMAGE,
                    num_poses=self.max_students,
                    min_pose_detection_confidence=Config.PRESENCE_BODY_MIN_CONF,
                    min_pose_presence_confidence=Config.PRESENCE_BODY_MIN_CONF,
                )
            )
        except Exception as exc:  # noqa: BLE001 — reported, never swallowed
            self.reason = f"{type(exc).__name__}: {exc}"
            print(f"[PRESENCE] body check off — could not load model: {self.reason}")
            return False

        self.available = True
        self.reason = "ready"
        print(f"[PRESENCE] body check on — {os.path.basename(path)}")
        return True

    def close(self):
        if self._landmarker is not None:
            try:
                self._landmarker.close()
            except Exception:  # noqa: BLE001 — shutdown only
                pass
            self._landmarker = None

    # ── the check ─────────────────────────────────────────────────────
    def maybe_check(self, rgb, t, identity, unmeasured_slots, frame_w):
        """
        rgb              : the frame already converted for MediaPipe,
                           flipped exactly as the face model sees it
        unmeasured_slots : slots presence.py currently cannot measure
        Returns {slot_id: bool} for those slots, or None when the check
        did not run this frame (off, not due yet, or failed).
        """
        if not self.available or not unmeasured_slots:
            return None
        interval = 1.0 / max(Config.PRESENCE_BODY_CHECK_HZ, 0.1)
        if self._last_run is not None and (t - self._last_run) < interval:
            return None
        self._last_run = t

        try:
            image = self._mp.Image(image_format=self._mp.ImageFormat.SRGB, data=rgb)
            result = self._landmarker.detect(image)
        except Exception as exc:  # noqa: BLE001 — one failure disables it
            self.available = False
            self.reason = f"{type(exc).__name__}: {exc}"
            print(f"[PRESENCE] body check stopped — {self.reason}")
            return None

        self.runs += 1
        bodies_x = []
        for pose in (result.pose_landmarks or []):
            # Shoulders if we have them, nose otherwise: a seated person
            # bent over a desk always shows shoulders, which is the
            # whole reason this check exists.
            xs = [pose[i].x for i in (11, 12) if i < len(pose)]
            if not xs and pose:
                xs = [pose[0].x]
            if xs:
                bodies_x.append(sum(xs) / len(xs) * frame_w)

        radius = Config.IDENTITY_MATCH_RADIUS_FRAC * frame_w
        answer = {}
        for sid in unmeasured_slots:
            slot = identity.slots.get(sid) if hasattr(identity, "slots") else None
            if slot is None or slot.cx is None:
                continue  # never seen here; no position to compare against
            best, best_d = None, None
            for idx, bx in enumerate(bodies_x):
                d = abs(bx - slot.cx)
                if d <= radius and (best_d is None or d < best_d):
                    best, best_d = idx, d
            answer[sid] = best is not None
            if best is not None:
                h = _hand_features(result.pose_landmarks[best])
                if h:
                    self.hands[sid] = h
        return answer or None

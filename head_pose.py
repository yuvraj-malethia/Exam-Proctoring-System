"""
head_pose.py — landmark IDs + extract_yaw()

Split out of module_headpose_multi_web.py during the multi-file refactor.
Code below is moved verbatim from the original module.
"""

import cv2
import numpy as np
from typing import Tuple, Optional

# ═══════════════════════════════════════════════════════════════════
# HEAD POSE EXTRACTION — SolvePnP from MediaPipe landmarks
# ═══════════════════════════════════════════════════════════════════

_LANDMARK_IDS = [1, 33, 263, 199, 61, 291]
_BBOX_IDS = [10, 152, 234, 454]


def extract_head_pose(landmarks, w: int, h: int):
    """
    Returns (yaw_deg, pitch_deg, is_profile, face_aspect_ratio).

    WHY THIS EXISTS: extract_yaw() below has always run solvePnP and
    RQDecomp3x3, which produce ALL THREE head angles — then thrown two
    of them away and returned only yaw. Pitch was sitting there, already
    paid for, unused.

    That omission was the system's biggest blind spot: a student looking
    straight DOWN at a phone in their lap barely changes yaw at all, so
    the most common real-world cheating posture was close to invisible.
    Reading angles[0] costs nothing and closes it.

    pitch_deg convention: RQDecomp3x3's sign depends on the solve, so
    "which sign means looking down" is left to
    Config.PITCH_DOWN_SIGN rather than hardcoded here — it can be
    flipped after real-camera testing without touching this function.

    extract_yaw() is kept unchanged below so existing callers and the
    equivalence tests are unaffected.
    """
    bbox_pts = [landmarks.landmark[i] for i in _BBOX_IDS]
    fx = [p.x * w for p in bbox_pts]
    fy = [p.y * h for p in bbox_pts]
    face_w = max(fx) - min(fx)
    face_h = max(fy) - min(fy)
    far = face_w / face_h if face_h > 1 else 1.0
    is_profile = far < 0.52

    face_2d, face_3d = [], []
    for idx in _LANDMARK_IDS:
        pt = landmarks.landmark[idx]
        x, y = int(pt.x * w), int(pt.y * h)
        face_2d.append([x, y])
        face_3d.append([x, y, pt.z])

    face_2d = np.array(face_2d, dtype=np.float64)
    face_3d = np.array(face_3d, dtype=np.float64)
    cam = np.array([[w, 0, h / 2], [0, w, w / 2], [0, 0, 1]], dtype=np.float64)
    dist = np.zeros((4, 1))

    success, rot_vec, _ = cv2.solvePnP(
        face_3d, face_2d, cam, dist, flags=cv2.SOLVEPNP_ITERATIVE
    )
    if not success:
        return None, None, is_profile, far

    rmat, _ = cv2.Rodrigues(rot_vec)
    angles, *_ = cv2.RQDecomp3x3(rmat)
    pnp_yaw = float(angles[1] * 180.0)
    pnp_pitch = float(angles[0] * 180.0)  # was discarded until now

    if is_profile:
        # Profile fallback for yaw is unchanged. Pitch is unreliable on a
        # near-profile face (the landmarks used to infer it are heavily
        # foreshortened), so it is reported as None rather than guessed —
        # the pitch pipeline simply idles for those frames.
        nose_x = landmarks.landmark[1].x
        left_eye_x = landmarks.landmark[33].x
        right_eye_x = landmarks.landmark[263].x
        eye_mid_x = (left_eye_x + right_eye_x) / 2.0
        dir_sign = 1.0 if nose_x < eye_mid_x else -1.0
        return dir_sign * 32.0, None, True, far

    return pnp_yaw, pnp_pitch, False, far


def extract_yaw(landmarks, w: int, h: int) -> Tuple[Optional[float], bool, float]:
    """Returns (yaw_deg, is_profile, face_aspect_ratio)."""
    bbox_pts = [landmarks.landmark[i] for i in _BBOX_IDS]
    fx = [p.x * w for p in bbox_pts]
    fy = [p.y * h for p in bbox_pts]
    face_w = max(fx) - min(fx)
    face_h = max(fy) - min(fy)
    far = face_w / face_h if face_h > 1 else 1.0
    is_profile = far < 0.52

    face_2d, face_3d = [], []
    for idx in _LANDMARK_IDS:
        pt = landmarks.landmark[idx]
        x, y = int(pt.x * w), int(pt.y * h)
        face_2d.append([x, y])
        face_3d.append([x, y, pt.z])

    face_2d = np.array(face_2d, dtype=np.float64)
    face_3d = np.array(face_3d, dtype=np.float64)
    cam = np.array([[w, 0, h / 2], [0, w, w / 2], [0, 0, 1]], dtype=np.float64)
    dist = np.zeros((4, 1))

    success, rot_vec, _ = cv2.solvePnP(
        face_3d, face_2d, cam, dist, flags=cv2.SOLVEPNP_ITERATIVE
    )
    if not success:
        return None, is_profile, far

    rmat, _ = cv2.Rodrigues(rot_vec)
    angles, *_ = cv2.RQDecomp3x3(rmat)
    pnp_yaw = float(angles[1] * 180.0)

    if is_profile:
        nose_x = landmarks.landmark[1].x
        left_eye_x = landmarks.landmark[33].x
        right_eye_x = landmarks.landmark[263].x
        eye_mid_x = (left_eye_x + right_eye_x) / 2.0
        dir_sign = 1.0 if nose_x < eye_mid_x else -1.0
        return dir_sign * 32.0, True, far

    return pnp_yaw, False, far



"""
eye_gaze.py — extract_eye_gaze()

THE GAP THIS CLOSES: every head-pose proctoring system has the same
obvious hole, and it is the first thing anyone asks about one — a
student can keep their head still and move only their eyes. Until now
this engine measured head orientation and nothing else, so that
behaviour was invisible to it by construction.

MediaPipe's face mesh can produce iris landmarks (468–477), but only
when it is built with refine_landmarks=True, which this project had
switched off. Turning it on costs one flag and a little CPU.

WHAT IS MEASURED. For each eye, where the iris centre sits between that
eye's own inner and outer corner, as a fraction of the eye's width:

        outer corner |-----●-----| inner corner
                       iris

      0  = iris centred in the eye
    -50  = iris hard against one side
    +50  = hard against the other

REPORTED AS PERCENT OF EYE WIDTH, NOT A 0-1 FRACTION, AND THAT MATTERS.
The eye signal is fed through the same CalibrationEngine the yaw and
pitch pipelines use, and that engine floors the standard deviation it
learns at Config.MIN_STD_DEG (3.5) and caps it at MAX_STD_DEG (5.0) —
numbers chosen for a signal measured in degrees. Handed a signal whose
whole range is about 0.5, the floor swamps it: every z-score comes out
near 0.03 and no threshold can ever be crossed. That is not a
hypothesis — it was built that way first, and across all eight real
clips the eye path fired exactly zero times while appearing to work.
Scaling to percent puts the signal in the same numeric range as degrees,
so the floor means "eyes that wobble less than 3.5% of an eye width
count as perfectly steady", which is a sensible statement rather than an
accident.

Both eyes are averaged. Measuring against the eye's OWN corners is what
makes this a different signal from head yaw: when the head turns, the
corners travel with it and the ratio barely moves. Only the eye moving
inside its socket changes it.

MEASURED ON REAL FOOTAGE (Yuvraj's 8 clips, 5 Oct 2026). Typical range
over a clip, 5th percentile of the offset:
    innocent_steady          -16.4
    innocent_writing         -13.8
    suspicious_quick_checks  -34.1
    suspicious_side_glance   -41.4
The two clips recorded as cheating sit more than twice as far to one
side as the two recorded as normal. (Negative is the viewer's left,
which is the side he was told to glance toward.)

THE HONEST LIMIT, AND HOW IT IS HANDLED. The separation is not purely
eye movement. Correlation between this offset and head yaw was -0.11 and
+0.18 on the innocent clips but +0.63 and +0.69 on the suspicious ones —
because when he glanced at a neighbour his head went too, and a turned
head also shifts the iris against the corners through plain perspective
(about 1 percentage point of offset per degree of yaw). So a large offset measured
while the head is already turned says very little that yaw did not
already say, and says it less reliably.

student_pipeline.py therefore only acts on this signal while the head is
WITHIN its normal range. That is both the honest choice and the useful
one: it is exactly the case yaw cannot see, and exactly the case where
perspective contamination is smallest.

Returns None rather than guessing when the landmarks are not usable —
no iris points (refine off), a profile view where one eye is
foreshortened to nothing, or eyes too closed to locate an iris in.
"""

from config import Config

# MediaPipe face-mesh indices. Present only with refine_landmarks=True.
_L_IRIS, _R_IRIS = 468, 473          # iris centres
_L_OUT, _L_IN = 33, 133              # left eye, outer and inner corner
_R_IN, _R_OUT = 362, 263             # right eye
_L_TOP, _L_BOT = 159, 145            # left eyelid, for openness
_R_TOP, _R_BOT = 386, 374            # right eyelid

_EYES = (
    (_L_IRIS, _L_OUT, _L_IN, _L_TOP, _L_BOT),
    (_R_IRIS, _R_OUT, _R_IN, _R_TOP, _R_BOT),
)


def has_iris(landmarks) -> bool:
    """True when this face mesh was built with refine_landmarks=True."""
    try:
        return len(landmarks.landmark) > _R_IRIS
    except (AttributeError, TypeError):
        return False


def extract_eye_gaze(landmarks, w: int, h: int):
    """
    Returns (across, down, openness), any of which may be None.

    across   : iris position between the eye corners, as a percent of
               eye width. Negative = toward the viewer's left.
    down     : iris position relative to the line between the corners,
               same units. Positive = looking downward. This exists for
               the case neither head signal can see — a level head with
               the eyes dropped to something on the desk.
    openness : eyelid gap as a fraction of eye width, averaged. Used to
               reject blinks, where an iris position is meaningless.
    """
    if not has_iris(landmarks):
        return None, None, None
    pts = landmarks.landmark

    across, down, openings = [], [], []
    for iris_i, out_i, in_i, top_i, bot_i in _EYES:
        ox, oy = pts[out_i].x * w, pts[out_i].y * h
        ix, iy = pts[in_i].x * w, pts[in_i].y * h
        eye_w = ((ox - ix) ** 2 + (oy - iy) ** 2) ** 0.5
        # A foreshortened eye (head well turned, or a partly hidden face)
        # gives a tiny width and a wildly unstable ratio. Dropping it is
        # better than reporting a number that happens to be large.
        if eye_w < Config.EYE_MIN_WIDTH_PX:
            continue
        centre_x = (ox + ix) / 2.0
        centre_y = (oy + iy) / 2.0
        across.append(100.0 * ((pts[iris_i].x * w) - centre_x) / eye_w)
        # Vertical is measured against the LINE BETWEEN THE CORNERS, not
        # against the eyelids. The lids move whenever the eye opens or
        # narrows, so an eyelid reference would read a squint as a look
        # downward. The corners do not move with the lid.
        # Divided by eye WIDTH, not height, for the same reason: height
        # collapses during a blink and would make the ratio explode.
        down.append(100.0 * ((pts[iris_i].y * h) - centre_y) / eye_w)
        openings.append((abs(pts[top_i].y - pts[bot_i].y) * h) / eye_w)

    if not across:
        return None, None, None
    openness = sum(openings) / len(openings)
    if openness < Config.EYE_MIN_OPENNESS:
        # Blinking or squinting: there is no usable iris position. The
        # openness is still returned so a caller can tell "eye shut" from
        # "no face at all".
        return None, None, openness
    return (sum(across) / len(across), sum(down) / len(down), openness)


def direction_label(across: float, down: float = None) -> str:
    """Which way the eyes are looking, in the mirrored view the engine
    and the dashboard both work in — the same convention the yaw
    directions already use, so "Left" means the same thing in both.

    Whichever axis deviates further names the direction. A glance is
    rarely purely sideways or purely downward, and calling it by the
    smaller component would mislabel it.
    """
    if down is not None and abs(down) > abs(across):
        return "Eyes Down" if down > 0 else "Eyes Up"
    return "Eyes Left" if across < 0 else "Eyes Right"

"""
face_signature.py — FaceSignature, SignatureGallery

Lightweight face re-identification so a student who leaves and comes
back is recognised and given their OWN record back, instead of being
treated as a stranger and reset.

WHAT PROBLEM THIS SOLVES
────────────────────────
identity_tracker.py matches people by WHERE THEY ARE SITTING. That
handles the common cases (brief disappearance, leaning, two students
crossing) but it has two holes it cannot close on its own:

  * A student gone longer than the continuity timeout (bathroom break)
    comes back and is treated as a new person — their calibration and
    history are reset even though they are the same student.
  * Because leaving resets the record, walking out becomes a way to
    wipe a high suspicion score.

Recognising the face closes both.

WHY THIS APPROACH (and its honest limits)
─────────────────────────────────────────
This is deliberately NOT general face recognition ("who is this out of
millions of people?"), which needs a heavy deep-learning model. The
question here is far narrower and far easier: there are at most 4
enrolled students in this room, and we only need "which of these 4 is
this, if any?"

So the signature is built from geometry that MediaPipe already gives us
every frame — no new model, no new dependency, no extra inference cost.
It takes a fixed set of stable facial landmarks, measures every pairwise
distance between them in 3D, and divides by the inter-ocular distance.
Dividing makes it independent of how close the student is to the camera;
using 3D coordinates rather than the flat 2D projection makes it far
less sensitive to head rotation.

LIMITS — state these plainly, do not oversell them:
  * Geometric signatures are weaker than deep face embeddings. They
    separate a handful of people in stable conditions; they are not
    identity-proof and are not a security mechanism.
  * They degrade when the head is turned far from frontal, which is why
    enrolment and matching both REQUIRE a near-frontal face.
  * Two people with genuinely similar face proportions may score close
    together. This is why a match must clear both an absolute
    similarity threshold AND beat the runner-up by a margin — an
    ambiguous match is rejected rather than guessed, because wrongly
    restoring one student's record onto another is worse than making
    them recalibrate.
  * Every rejection falls back to the existing position-based logic, so
    a failure here degrades to the previous behaviour rather than
    breaking anything.

PRIVACY
───────
No images are stored, ever. A signature is a short list of ratios
between facial measurements — it cannot be turned back into a picture
of anyone. Signatures live in memory for the duration of one session
and are discarded when the process exits; nothing is written to disk
and nothing leaves the machine.
"""

import math

from config import Config

# Stable landmarks used to build the signature. Chosen for being
# structural (bone/socket geometry) rather than expressive — mouth and
# brow points move when someone talks or frowns, which would make the
# signature drift within a single session.
_SIG_LANDMARKS = [
    33,   # right eye outer corner
    133,  # right eye inner corner
    362,  # left eye inner corner
    263,  # left eye outer corner
    1,    # nose tip
    168,  # nose bridge (between eyes)
    6,    # nose bridge lower
    197,  # nose bridge mid
    10,   # forehead centre top
    152,  # chin bottom
    234,  # right cheek edge
    454,  # left cheek edge
    93,   # right jaw
    323,  # left jaw
]

# Reference pair for scale normalisation: outer corners of both eyes.
# Inter-ocular distance is the standard anthropometric reference — it
# barely changes with expression, unlike face height or width.
_REF_A, _REF_B = 33, 263


def _dist3(p, q):
    return math.sqrt((p.x - q.x) ** 2 + (p.y - q.y) ** 2 + (p.z - q.z) ** 2)


def compute_signature(landmarks):
    """
    Build a scale-normalised geometric signature from one frame's
    landmarks. Returns a list of floats, or None if the landmarks are
    unusable (degenerate reference distance).
    """
    try:
        lm = landmarks.landmark
        ref = _dist3(lm[_REF_A], lm[_REF_B])
        if ref <= 1e-6:
            return None
        pts = [lm[i] for i in _SIG_LANDMARKS]
        sig = []
        for i in range(len(pts)):
            for j in range(i + 1, len(pts)):
                sig.append(_dist3(pts[i], pts[j]) / ref)
        return sig
    except (IndexError, AttributeError, TypeError):
        return None


def similarity(a, b):
    """
    1.0 = identical geometry, falling towards 0 as they diverge.

    Uses mean relative difference per measurement rather than raw
    Euclidean distance, so every measurement contributes proportionally
    instead of the physically largest ones dominating the score.
    """
    if not a or not b or len(a) != len(b):
        return 0.0
    total = 0.0
    for x, y in zip(a, b):
        denom = max(abs(x), abs(y), 1e-6)
        total += abs(x - y) / denom
    mean_rel_diff = total / len(a)
    return max(0.0, 1.0 - mean_rel_diff)


class _Enrolment:
    """Accumulating signature record for one slot."""

    __slots__ = ("samples", "mean_sig", "complete", "label")

    def __init__(self, label):
        self.samples = []
        self.mean_sig = None
        self.complete = False
        self.label = label

    def add(self, sig):
        self.samples.append(sig)
        if len(self.samples) > Config.FACE_SIG_ENROL_SAMPLES:
            self.samples.pop(0)
        n = len(self.samples[0])
        self.mean_sig = [
            sum(s[i] for s in self.samples) / len(self.samples) for i in range(n)
        ]
        if len(self.samples) >= Config.FACE_SIG_ENROL_SAMPLES:
            self.complete = True


class SignatureGallery:
    """
    Holds one enrolled signature per slot and answers "which enrolled
    student is this face, if any?".

    Enrolment happens passively: while a student sits at their slot
    looking roughly forward, samples accumulate. No separate enrolment
    step is required from the operator, and a student who never faces
    the camera simply never enrols — in which case the system falls
    back to position-based identity exactly as before.
    """

    def __init__(self, max_students: int):
        self.max_students = max_students
        self._enrol = {i: None for i in range(max_students)}
        # Signatures of students who have left a seat, kept so they can
        # be recognised wherever they sit next — identity follows the
        # PERSON, not the chair.
        self._departed = []
        self.match_attempts = 0
        self.match_successes = 0
        self.match_rejections = 0
        self.match_uncertain = 0  # times a human was asked instead

    # ── enrolment ──────────────────────────────────────────────────

    def observe(self, slot: int, landmarks, yaw_deg, is_profile: bool, label=None):
        """
        Feed one frame for a slot. Ignored unless the face is near
        frontal — a signature built from a turned head would poison the
        gallery and cause false matches later.
        """
        if not Config.FACE_SIG_ENABLED:
            return
        if is_profile:
            return
        if yaw_deg is None or abs(yaw_deg) > Config.FACE_SIG_MAX_ENROL_YAW:
            return
        sig = compute_signature(landmarks)
        if sig is None:
            return
        rec = self._enrol.get(slot)
        if rec is None:
            rec = _Enrolment(label or f"S{slot + 1}")
            self._enrol[slot] = rec
        rec.add(sig)

    def mean_signature(self, slot: int):
        """The averaged signature for a slot, or None if not yet enrolled.
        Averaged over many frames, so it is far less noisy than any
        single frame — which is why the deferred check below compares
        average-to-average rather than one frame to an average."""
        rec = self._enrol.get(slot)
        if not rec or not rec.complete:
            return None
        return list(rec.mean_sig)

    def compare_signatures(self, sig_a, sig_b):
        """Raw similarity between two stored signatures, no thresholds."""
        return similarity(sig_a, sig_b)

    def classify_score(self, score: float, runner_up: float = 0.0):
        """
        Apply the same three-tier rules used by identify(), to a score
        obtained some other way (e.g. the deferred average-to-average
        check). Returns (outcome, reason) with outcome in
        match / uncertain / none.
        """
        if score < Config.FACE_SIG_UNCERTAIN_THRESHOLD:
            return "none", f"no resemblance (best {score:.3f})"
        if score < Config.FACE_SIG_MATCH_THRESHOLD:
            return "uncertain", (
                f"possible match {score:.3f}, below the "
                f"{Config.FACE_SIG_MATCH_THRESHOLD} needed to act automatically"
            )
        if (score - runner_up) < Config.FACE_SIG_MATCH_MARGIN:
            return "uncertain", (
                f"two students score too closely ({score:.3f} vs {runner_up:.3f})"
            )
        return "match", f"confident match {score:.3f}"

    def is_enrolled(self, slot: int) -> bool:
        rec = self._enrol.get(slot)
        return bool(rec and rec.complete)

    def enrolled_slots(self) -> list:
        return [s for s in range(self.max_students) if self.is_enrolled(s)]

    def clear_slot(self, slot: int):
        """Forget a slot's enrolment — used when a seat genuinely changes
        hands, so the newcomer is not matched against the old occupant."""
        self._enrol[slot] = None

    def retire_slot(self, slot: int, record_id, t: float, label=None):
        """
        A seat has changed hands. Move the departing occupant's enrolled
        signature into the DEPARTED registry instead of deleting it, and
        clear the slot so the newcomer is not matched against them.

        WHY THIS MATTERS (found in real testing): signatures used to be
        stored per SEAT and thrown away when the seat changed hands. A
        student who left and later sat somewhere else was therefore
        compared against whoever used to sit in their NEW seat — a
        different person entirely — which produced confidently wrong
        identifications. People are not seats. A departing student's
        signature now follows the PERSON, so they can be recognised
        wherever in the room they sit down.
        """
        rec = self._enrol.get(slot)
        if rec and rec.complete:
            self._departed.append({
                "record_id": record_id,
                "label": label or rec.label,
                "from_slot": slot,
                "left_at": t,
                "sig": list(rec.mean_sig),
            })
            # Keep the registry bounded; oldest departures drop first.
            if len(self._departed) > 32:
                self._departed.pop(0)
        self._enrol[slot] = None

    def identify_departed(self, sig, exclude_record_ids=()):
        """
        Compare an averaged signature against every student who has
        left a seat this session, regardless of WHICH seat they left.

        Returns (outcome, entry, score, reason) using the same
        three-tier rules as identify(). entry carries the archived
        record_id so the caller knows exactly whose history to reunite.
        """
        if not Config.FACE_SIG_ENABLED or not sig:
            return "none", None, 0.0, "face signatures disabled"
        pool = [d for d in self._departed
                if d["record_id"] not in exclude_record_ids and d.get("sig")]
        if not pool:
            return "none", None, 0.0, "no departed students to compare against"

        scored = sorted(
            ((similarity(sig, d["sig"]), d) for d in pool),
            key=lambda x: x[0], reverse=True,
        )
        best_score, best = scored[0]
        runner_up = scored[1][0] if len(scored) > 1 else 0.0
        outcome, reason = self.classify_score(best_score, runner_up)
        if outcome == "match":
            self.match_successes += 1
        elif outcome == "uncertain":
            self.match_uncertain += 1
        else:
            self.match_rejections += 1
        return outcome, best, best_score, reason

    def departed_count(self) -> int:
        return len(self._departed)

    def enrolment_progress(self, slot: int) -> float:
        rec = self._enrol.get(slot)
        if not rec:
            return 0.0
        return min(len(rec.samples) / float(Config.FACE_SIG_ENROL_SAMPLES), 1.0)

    # ── matching ───────────────────────────────────────────────────

    def identify(self, landmarks, yaw_deg, is_profile: bool, exclude=()):
        """
        Try to identify a face against enrolled slots.

        Returns (outcome, slot, score, reason) where outcome is one of:

          "match"     — confident enough to act on automatically.
          "uncertain" — looks like someone we know, but not sure enough.
                        The caller must NOT act on this by itself; it
                        should ask the invigilator, who actually knows
                        who left the room. slot is the best guess.
          "none"      — no meaningful resemblance to anyone enrolled.
                        Safe to treat as a genuinely new person with no
                        prompt, because there is nothing to be unsure
                        about.

        The middle outcome exists deliberately. Previously anything
        short of confident was silently treated as "definitely a new
        person", which conflated "I don't know" with "I know it isn't
        them" — and a returning student measured slightly differently
        was permanently reassigned with nobody ever being told. Identity
        is an accusation-relevant decision; when the system is unsure,
        it raises the question rather than settling it.

        An AMBIGUOUS result (two enrolled students scoring close
        together) is also returned as "uncertain" rather than rejected
        outright — the system genuinely does not know which of them it
        is, which is exactly the case a human should decide.
        """
        if not Config.FACE_SIG_ENABLED:
            return "none", None, 0.0, "face signatures disabled"
        if is_profile:
            return "none", None, 0.0, "face turned too far to identify"
        if yaw_deg is None or abs(yaw_deg) > Config.FACE_SIG_MAX_MATCH_YAW:
            return "none", None, 0.0, "face not frontal enough to identify"

        sig = compute_signature(landmarks)
        if sig is None:
            return "none", None, 0.0, "could not compute signature"

        self.match_attempts += 1

        scored = []
        for slot in range(self.max_students):
            if slot in exclude:
                continue
            rec = self._enrol.get(slot)
            if not rec or not rec.complete:
                continue
            scored.append((similarity(sig, rec.mean_sig), slot))

        if not scored:
            self.match_rejections += 1
            return "none", None, 0.0, "no enrolled students to compare against"

        scored.sort(reverse=True)
        best_score, best_slot = scored[0]
        runner_up = scored[1][0] if len(scored) > 1 else 0.0

        # Nothing like anyone we know — genuinely a new person.
        if best_score < Config.FACE_SIG_UNCERTAIN_THRESHOLD:
            self.match_rejections += 1
            return "none", None, best_score, (
                f"no resemblance to any enrolled student "
                f"(best {best_score:.3f})"
            )

        # Looks like someone, but not confident enough to act alone.
        if best_score < Config.FACE_SIG_MATCH_THRESHOLD:
            self.match_uncertain += 1
            return "uncertain", best_slot, best_score, (
                f"possible match {best_score:.3f}, below the "
                f"{Config.FACE_SIG_MATCH_THRESHOLD} needed to act "
                f"automatically — invigilator confirmation required"
            )

        # Strong score, but two people look too similar to tell apart.
        if (best_score - runner_up) < Config.FACE_SIG_MATCH_MARGIN:
            self.match_uncertain += 1
            return "uncertain", best_slot, best_score, (
                f"two students score too closely ({best_score:.3f} vs "
                f"{runner_up:.3f}) — invigilator confirmation required"
            )

        self.match_successes += 1
        return "match", best_slot, best_score, f"confident match {best_score:.3f}"

    def stats(self) -> dict:
        return {
            "enabled": Config.FACE_SIG_ENABLED,
            "enrolled_slots": self.enrolled_slots(),
            "match_attempts": self.match_attempts,
            "match_successes": self.match_successes,
            "match_rejections": self.match_rejections,
            "match_uncertain": self.match_uncertain,
            "departed_known": len(self._departed),
        }

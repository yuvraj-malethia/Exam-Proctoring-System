"""
identity_tracker.py — IdentityTracker

Fixes the seat-assignment identity bug.

THE BUG THIS REPLACES
─────────────────────
The main loop used to rebuild face→slot assignment from scratch every
single frame, by sorting the visible faces left→right:

    faces_with_x.sort(key=lambda fx: fx[0])
    sorted_faces = [lm for _, lm in faces_with_x]   # slot 0 = leftmost

Nothing remembered which face was S1 a moment earlier. Consequences:

  * If the student in seat 1 stood up, everyone to their right shifted
    up one slot. S2's face started feeding S1's pipeline — inheriting
    S1's calibration baseline, accumulated suspicion score, event
    history, and evidence trail, silently, under S1's label.
  * That is not a data-loss bug, it is a FALSE-ACCUSATION bug: an
    innocent student can end up wearing another student's record.
  * Two students crossing left/right order (leaning across a desk) had
    the same effect, momentarily, every frame it happened.

THE FIX
───────
Three mechanisms, all backend-only — the dashboard still sees the same
S1..S4 slots and needs no changes:

  1. CONTINUITY MATCHING. Each slot remembers where its face was last
     frame. A detected face is matched to the slot whose last known
     position it is nearest to (greedy nearest-neighbour, capped by
     IDENTITY_MATCH_RADIUS_FRAC). Small shifts, leaning, and two
     students crossing horizontally no longer scramble identity,
     because matching is by proximity-to-last-known-position rather
     than by rank order.

  2. GRACE PERIOD. When a face disappears (looked down, hand over
     face, leaned out of frame, momentary detection failure) its slot
     is HELD VACANT for IDENTITY_GRACE_PERIOD_SEC. During that window
     nobody else can be given the slot, so a neighbour cannot absorb
     the missing student's identity. If the original student reappears
     near their old position within the window, they simply resume.

  3. EXPLICIT REASSIGNMENT + RESET. If the grace period expires and a
     genuinely different face takes the seat, that is reported as a
     reassignment. The caller then resets that slot's pipeline (see
     Config.IDENTITY_RESET_ON_REASSIGN) so the newcomer starts from a
     clean calibration and zero suspicion instead of inheriting the
     previous occupant's history — and the handover is written to the
     event log and the black-box recorder, so it is auditable rather
     than silent.

KNOWN LIMIT (deliberate, worth stating plainly)
───────────────────────────────────────────────
This tracks by POSITION, not by appearance. It cannot tell that two
students deliberately swapped seats with each other — from the
tracker's point of view each simply stayed near a plausible position.
Closing that hole requires true face recognition (a face-embedding
identity check), which is named as future work rather than implemented
here. What this DOES close is the common, accidental, and by far most
frequent case: someone leaving, returning, being briefly undetected, or
two people crossing horizontally.
"""

from config import Config


class _Slot:
    """Last-known state of one seat. Pure bookkeeping — no scoring."""

    __slots__ = ("cx", "cy", "last_seen", "occupied", "occupant_serial",
                 "on_break", "break_started", "occupied_since")

    def __init__(self):
        self.cx = None            # last known face centre x (pixels)
        self.cy = None            # last known face centre y (pixels)
        self.last_seen = None     # wall-clock time this slot last had a face
        self.occupied = False     # has this slot ever been claimed?
        self.occupant_serial = 0  # increments each time a NEW person takes it
        self.on_break = False     # invigilator marked this student away
        self.break_started = None # when the break began
        self.occupied_since = None  # when the CURRENT occupant arrived —
        # used by the archive to record how long they sat here.


class IdentityTracker:
    """
    Maps detected faces to stable student slots across frames.

    Call assign() once per frame. It returns:
        assignment      : {slot_id: landmarks} for slots seen this frame
        reassigned      : [dict, ...] one per slot handed to a new person
        held            : [slot_id, ...] slots vacant but inside grace

    The tracker owns NO scoring state and never touches StudentPipeline.
    Resetting a reassigned slot is the caller's job, kept there on
    purpose so all pipeline mutation stays visible in the main loop.
    """

    # A gap between frames larger than this means the pipeline itself
    # stalled (this project has measured multi-second freezes, and the
    # external watchdog has recorded gaps of tens of seconds under
    # system memory pressure). Time during which we were not looking at
    # the camera must NOT count as a student being absent.
    STALL_THRESHOLD_SEC = 2.0

    def __init__(self, max_students: int):
        self.max_students = max_students
        self.slots = {i: _Slot() for i in range(max_students)}
        self.total_reassignments = 0
        self._last_assign_t = None
        self.total_stall_compensations = 0
        self.total_stalled_sec = 0.0
        # Optional face_signature.SignatureGallery. When present, a face
        # that cannot be placed by position is offered to it for
        # re-identification before being treated as a stranger. Kept
        # optional so the tracker still works standalone.
        self.gallery = None
        self.total_face_restores = 0
        # Candidate faces that match no existing student yet. A face must
        # persist here for IDENTITY_NEW_TRACK_FRAMES before it is granted
        # an identity, which is what prevents a single-frame phantom
        # detection from stealing a slot (see Config docs).
        self._pending = []
        self.total_phantoms_rejected = 0
        self.total_duplicates_dropped = 0
        self.total_small_faces_dropped = 0
        self.total_over_cap_dropped = 0
        # How many people the invigilator says are actually in the room.
        # None = automatic. See set_expected_students().
        self.expected_students = Config.EXPECTED_STUDENTS

    def _compensate_for_stall(self, t: float):
        """
        If the main loop froze, every slot's "seconds since last seen"
        would balloon purely because no frames were processed — and on
        the next frame every student would look long-gone, be treated as
        a new occupant, and have their calibration and suspicion history
        wiped. That would turn a performance problem into data loss.

        A student can only be judged absent during time we were actually
        watching. So when a frame arrives after an abnormally long gap,
        that gap is added back to every slot's last_seen, freezing their
        apparent age across the stall.
        """
        if self._last_assign_t is None:
            self._last_assign_t = t
            return
        gap = t - self._last_assign_t
        if gap > self.STALL_THRESHOLD_SEC:
            for s in self.slots.values():
                if s.last_seen is not None:
                    s.last_seen += gap
            self.total_stall_compensations += 1
            self.total_stalled_sec += gap
        self._last_assign_t = t

    # ── helpers ────────────────────────────────────────────────────

    @staticmethod
    def _centre(lm, w: int, h: int, bbox_ids):
        xs = [lm.landmark[i].x * w for i in bbox_ids]
        ys = [lm.landmark[i].y * h for i in bbox_ids]
        return (min(xs) + max(xs)) / 2.0, (min(ys) + max(ys)) / 2.0

    @staticmethod
    def _area(lm, w: int, h: int, bbox_ids):
        """Bounding-box area of a detection, used to tell a student at
        the desk from a smaller, further-away background phantom."""
        xs = [lm.landmark[i].x * w for i in bbox_ids]
        ys = [lm.landmark[i].y * h for i in bbox_ids]
        return max(max(xs) - min(xs), 1.0) * max(max(ys) - min(ys), 1.0)

    def set_expected_students(self, n):
        """
        Invigilator override: how many people are actually in the room.

        The system then refuses to track more than this many, keeping the
        largest faces. A stable background phantom — which real logs
        showed persisting for up to 27 seconds, long enough to defeat any
        automatic persistence test — can no longer take an identity.
        Pass None to return to automatic behaviour.
        """
        if n is None:
            self.expected_students = None
            return True
        try:
            n = int(n)
        except (TypeError, ValueError):
            return False
        if not (1 <= n <= self.max_students):
            return False
        self.expected_students = n
        return True

    def _cap_faces(self, faces, centres, areas):
        """
        Apply size filtering and the expected-student cap. Returns the
        indices to keep, largest first — because the student sitting at
        the desk is nearer the camera, and therefore bigger, than
        anything in the background.
        """
        idx = sorted(range(len(faces)), key=lambda i: -areas[i])
        if not idx:
            return []

        # Size filter: drop detections far smaller than the biggest face.
        if Config.MIN_FACE_AREA_FRAC > 0:
            biggest = areas[idx[0]]
            keep = [i for i in idx
                    if areas[i] >= biggest * Config.MIN_FACE_AREA_FRAC]
            dropped = len(idx) - len(keep)
            if dropped:
                self.total_small_faces_dropped += dropped
            idx = keep

        # Roster cap: never track more people than are actually present.
        cap = self.expected_students or self.max_students
        if len(idx) > cap:
            self.total_over_cap_dropped += (len(idx) - cap)
            idx = idx[:cap]
        return idx

    def _is_held(self, sid: int, t: float) -> bool:
        """
        True if this slot is vacant but still reserved for its owner.

        A slot marked as on an authorised break is held INDEFINITELY —
        the invigilator has explicitly stated this student is coming
        back, which is more reliable than any timeout we could guess.
        """
        s = self.slots[sid]
        if s.on_break:
            return True
        if not s.occupied or s.last_seen is None:
            return False
        return (t - s.last_seen) < Config.IDENTITY_GRACE_PERIOD_SEC

    def set_break(self, sid: int, on_break: bool, t: float) -> bool:
        """
        Invigilator override: mark a student as away on an authorised
        break (or back from one).

        While on break the slot is held indefinitely, so nobody else can
        take their seat or inherit their identity, and the caller skips
        processing for that slot so an empty chair never pollutes their
        calibration or accrues suspicion. Their record is untouched —
        they resume exactly where they left off.

        Returns True if the state actually changed.
        """
        if sid not in self.slots:
            return False
        s = self.slots[sid]
        if s.on_break == on_break:
            return False
        s.on_break = on_break
        if on_break:
            s.break_started = t
        else:
            s.break_started = None
            # Treat the return as "just seen" so the normal grace period
            # applies from now, rather than the slot instantly looking
            # long-abandoned because last_seen is stale from before the
            # break.
            s.last_seen = t
        return True

    def is_on_break(self, sid: int) -> bool:
        return bool(self.slots[sid].on_break)

    def break_slots(self) -> list:
        return [i for i, s in self.slots.items() if s.on_break]

    def slot_status(self, t: float) -> dict:
        """Read-only snapshot for logging / dashboard. Never used in scoring."""
        out = {}
        for sid, s in self.slots.items():
            if s.on_break:
                state = "on_break"
            elif not s.occupied:
                state = "empty"
            elif s.last_seen is not None and (t - s.last_seen) < 0.5:
                state = "tracked"
            elif self._is_held(sid, t):
                state = "held"
            else:
                state = "expired"
            out[sid] = {
                "state": state,
                "seconds_since_seen": (
                    round(t - s.last_seen, 2) if s.last_seen is not None else None
                ),
                "occupant_serial": s.occupant_serial,
                "on_break": s.on_break,
                # Last known face-centre x in pixels of the (mirrored)
                # frame. Read-only, added Sep 2026 so the 3D room can sit
                # students left-to-right in the same order as the camera
                # feed. Only the ORDER is meaningful to it.
                "cx": (round(s.cx, 1) if s.cx is not None else None),
                "break_seconds": (
                    round(t - s.break_started, 1)
                    if s.on_break and s.break_started else None
                ),
                "occupied_since": s.occupied_since,
            }
        return out

    # ── main entry point ───────────────────────────────────────────

    def assign(self, faces, w: int, h: int, t: float, bbox_ids,
               yaw_lookup=None, profile_lookup=None):
        """
        faces    : list of MediaPipe landmark objects detected this frame
        w, h     : frame dimensions in pixels
        t        : wall-clock time of this frame
        bbox_ids : landmark indices used to compute a face's centre
                   (passed in rather than imported so this module stays
                   independent of head_pose.py)
        yaw_lookup / profile_lookup : optional callables taking a face
                   index and returning that face's yaw in degrees / its
                   profile flag. Only used for face re-identification,
                   which refuses to act on a non-frontal face. Omitting
                   them simply disables re-identification.

        Returns (assignment, reassigned, held, restored):
            assignment : {slot_id: landmarks} seen this frame
            reassigned : slots handed to a genuinely NEW person
            held       : slots vacant but reserved (grace or on break)
            restored   : slots returned to their original owner by face
                         re-identification after a long absence
        """
        # Must run before ANY aging decision below, so a pipeline stall
        # is never mistaken for students leaving the room.
        self._compensate_for_stall(t)

        assignment = {}
        reassigned = []
        restored = []
        new_occupant_uncertain = []

        if not faces:
            return (assignment, reassigned,
                    [i for i in range(self.max_students) if self._is_held(i, t)],
                    restored)

        # Never accept more faces than we have slots for. MediaPipe is
        # already configured with max_num_faces=MAX_STUDENTS, so this is
        # belt-and-braces against an out-of-range slot index.
        faces = list(faces)[: self.max_students]

        centres = [self._centre(lm, w, h, bbox_ids) for lm in faces]
        radius = Config.IDENTITY_MATCH_RADIUS_FRAC * w

        # Drop duplicate detections of the same face before anything
        # else — a duplicate is a free chance for identity to jump.
        keep = self._dedupe(faces, centres, w)
        if len(keep) != len(faces):
            faces = [faces[i] for i in keep]
            centres = [centres[i] for i in keep]

        # Size filter + roster cap. This is the defence that actually
        # works against STABLE phantoms — real logs showed background
        # detections persisting 27 seconds, which no persistence test
        # can distinguish from a real student. Keeping only the largest
        # N faces, where N is what the invigilator says is in the room,
        # removes them regardless of how convincing they look.
        areas = [self._area(lm, w, h, bbox_ids) for lm in faces]
        kept = self._cap_faces(faces, centres, areas)
        if len(kept) != len(faces):
            kept_set = set(kept)
            faces = [faces[i] for i in range(len(faces)) if i in kept_set]
            centres = [centres[i] for i in range(len(centres)) if i in kept_set]

        # ── Pass 1: continuity matching ─────────────────────────────
        # Greedy nearest-neighbour between this frame's faces and every
        # slot that has a known last position. Sorting all candidate
        # pairs by distance and taking the closest first means the most
        # confident match wins, rather than whichever slot happens to be
        # checked first.
        #
        # A slot that has been vacant longer than
        # IDENTITY_CONTINUITY_TIMEOUT_SEC is deliberately EXCLUDED from
        # this pass. Position stops being evidence of identity once a
        # seat has been empty that long — otherwise a genuinely new
        # person sitting down in a vacated seat would silently continue
        # the previous occupant's track and inherit their record, which
        # is the exact bug this module exists to prevent. Such a slot
        # falls through to pass 2 and is handled as a new occupant.
        # THE OSCILLATION BUG THIS FIXES (found in real testing):
        # matching used to consider every slot together and simply pick
        # the closest by distance. A slot last seen 20 MINUTES ago was
        # weighed against a slot last seen 40 MILLISECONDS ago purely on
        # pixels. So a student merely shifting position in their own
        # chair could land nearer some long-dead slot's stale position
        # than their own current one — and their identity flipped, over
        # and over, as they leaned side to side.
        #
        # Now matching runs in two passes with a strict priority:
        #   Pass A — slots being ACTIVELY tracked right now. Someone
        #            already on camera keeps their identity; a stale
        #            slot can never take them.
        #   Pass B — only then, stale-but-not-expired slots, for people
        #            genuinely returning to a seat.
        #
        # The radius also scales with how long a slot has been unseen: a
        # face seen 40ms ago cannot have moved far, so its match radius
        # stays tight, while a slot vacant for seconds is allowed a
        # wider one. A fixed radius was both too loose for the first
        # case and too tight for the second.
        def _radius_for(sid):
            s = self.slots[sid]
            if s.last_seen is None:
                return radius
            gap = max(t - s.last_seen, 0.0)
            # 1x at 0s, growing to a cap of 2.5x for a long absence.
            return radius * min(1.0 + gap * 0.6, 2.5)

        active_ids_set = set()
        stale_ids_set = set()
        for sid, s in self.slots.items():
            if s.cx is None or not s.occupied or s.last_seen is None:
                continue
            gap = t - s.last_seen
            if gap > Config.IDENTITY_CONTINUITY_TIMEOUT_SEC:
                continue  # too long — position no longer implies identity
            if gap <= Config.IDENTITY_ACTIVE_TRACK_SEC:
                active_ids_set.add(sid)
            else:
                stale_ids_set.add(sid)

        used_faces = set()
        used_slots = set()

        def _match_pass(slot_ids):
            cands = []
            for fi, (cx, cy) in enumerate(centres):
                if fi in used_faces:
                    continue
                for sid in slot_ids:
                    if sid in used_slots:
                        continue
                    s = self.slots[sid]
                    d = ((cx - s.cx) ** 2 + (cy - s.cy) ** 2) ** 0.5
                    if d <= _radius_for(sid):
                        cands.append((d, fi, sid))
            cands.sort(key=lambda c: c[0])
            for d, fi, sid in cands:
                if fi in used_faces or sid in used_slots:
                    continue
                used_faces.add(fi)
                used_slots.add(sid)
                assignment[sid] = faces[fi]
                s = self.slots[sid]
                s.cx, s.cy = centres[fi]
                s.last_seen = t

        _match_pass(active_ids_set)   # Pass A — protect live tracks
        _match_pass(stale_ids_set)    # Pass B — then returning students

        # ── Pass 2: place unmatched faces ───────────────────────────
        # A face that matched no existing slot is either the very first
        # frame (no history yet) or a genuinely new person. Sorting the
        # leftovers left→right preserves the original left-to-right
        # seat convention for the initial assignment, so S1 is still the
        # leftmost student when a session starts.
        leftover = sorted(
            (fi for fi in range(len(faces)) if fi not in used_faces),
            key=lambda fi: centres[fi][0],
        )

        for fi in leftover:
            # ── Face re-identification (see face_signature.py) ───────
            # Before treating this face as a stranger, ask whether it
            # belongs to a student we have already enrolled. This is
            # what lets someone return from a 5-minute bathroom break
            # and get their OWN record back instead of a reset — and it
            # is what stops leaving the room being usable to wipe a
            # suspicion score. Only fires when the slot it points to is
            # actually free this frame; a confident match to a slot
            # someone else currently occupies is ignored rather than
            # trusted, since two people cannot be in one seat.
            restored_sid = None
            uncertain_info = None
            if self.gallery is not None:
                outcome, cand, score, reason = self.gallery.identify(
                    faces[fi],
                    yaw_lookup(fi) if yaw_lookup else None,
                    profile_lookup(fi) if profile_lookup else False,
                    exclude=used_slots,
                )
                if outcome == "match" and cand is not None and cand not in used_slots:
                    restored_sid = cand
                elif outcome == "uncertain" and cand is not None:
                    # The system thinks it recognises this person but is
                    # not sure. It must NOT decide either way on its own:
                    # silently treating them as new would erase a real
                    # student's record, and restoring on a weak match
                    # could attach one student's behaviour to another.
                    # Record the doubt so the caller can ask the
                    # invigilator, who actually knows who left the room.
                    # Meanwhile they proceed as a new occupant below —
                    # the safe default, and what happens anyway if the
                    # question is never answered.
                    uncertain_info = {
                        "candidate_slot": cand,
                        "score": round(score, 4),
                        "reason": reason,
                    }

            if restored_sid is not None:
                sid = restored_sid
                s = self.slots[sid]
                s.cx, s.cy = centres[fi]
                s.last_seen = t
                if s.on_break:
                    # Recognised while still flagged as on break — they
                    # are visibly back, so clear it automatically.
                    s.on_break = False
                    s.break_started = None
                used_slots.add(sid)
                assignment[sid] = faces[fi]
                self.total_face_restores += 1
                restored.append({
                    "slot": sid,
                    "score": round(score, 4),
                    "away_for_sec": (
                        round(t - s.last_seen, 2) if s.last_seen else None
                    ),
                })
                continue

            # PHANTOM GATE: a face that matches nobody must persist for
            # several frames before it is granted an identity. Real logs
            # showed single-frame phantom detections instantly claiming
            # slots and then capturing the real person's identity.
            # A TIGHTER radius than slot matching: between consecutive
            # frames a person barely moves, so candidates should only
            # cluster when they are practically on the same spot. Using
            # the full match radius here merged two people standing near
            # each other into a single candidate, so only one of them
            # ever got an identity.
            _pend_r = Config.IDENTITY_MIN_FACE_SEPARATION_FRAC * w
            if not self._pending_promote(centres[fi][0], centres[fi][1], t, _pend_r):
                continue

            sid = self._claim_slot(t, used_slots, centres[fi], radius)
            if sid is None:
                # Every slot is either taken this frame or inside its
                # grace window. Dropping the face is the correct and
                # safe outcome: better to not track someone than to
                # hand them an existing student's identity.
                continue

            s = self.slots[sid]
            was_occupied = s.occupied
            if was_occupied:
                s.occupant_serial += 1
                self.total_reassignments += 1
                reassigned.append({
                    "slot": sid,
                    "vacant_for_sec": (
                        round(t - s.last_seen, 2) if s.last_seen is not None else None
                    ),
                    "occupant_serial": s.occupant_serial,
                    "previous_occupied_since": s.occupied_since,
                    # Present when the face looked familiar but the
                    # system was not sure enough to act — the caller
                    # turns this into a question for the invigilator.
                    "uncertain_identity": uncertain_info,
                })
            else:
                s.occupied = True
                s.occupant_serial = 1
                if uncertain_info is not None:
                    # A fresh seat, but the face resembles someone who
                    # already left. Still worth asking about.
                    new_occupant_uncertain.append({
                        "slot": sid,
                        "uncertain_identity": uncertain_info,
                    })
            s.occupied_since = t
            s.on_break = False
            s.break_started = None

            s.cx, s.cy = centres[fi]
            s.last_seen = t
            used_slots.add(sid)
            assignment[sid] = faces[fi]

        held = [i for i in range(self.max_students)
                if i not in assignment and self._is_held(i, t)]
        # Uncertain matches on brand-new seats are reported alongside
        # reassignments so the caller has one place to look for
        # "someone the invigilator should be asked about".
        for u in new_occupant_uncertain:
            reassigned.append({
                "slot": u["slot"],
                "vacant_for_sec": None,
                "occupant_serial": self.slots[u["slot"]].occupant_serial,
                "previous_occupied_since": None,
                "uncertain_identity": u["uncertain_identity"],
                "first_occupant": True,  # nothing to archive or reset
            })
        return assignment, reassigned, held, restored

    def _dedupe(self, faces, centres, w):
        """
        Drop detections that sit almost on top of each other — the same
        face reported twice by the detector. Real log data showed this
        happening on thousands of frames with only one person present,
        and each duplicate was a chance for the person's identity to
        jump to a different slot.
        """
        min_sep = Config.IDENTITY_MIN_FACE_SEPARATION_FRAC * w
        keep = []
        for i, (cx, cy) in enumerate(centres):
            dup = False
            for j in keep:
                jx, jy = centres[j]
                if ((cx - jx) ** 2 + (cy - jy) ** 2) ** 0.5 < min_sep:
                    dup = True
                    break
            if dup:
                self.total_duplicates_dropped += 1
            else:
                keep.append(i)
        return keep

    def _pending_promote(self, cx, cy, t, radius):
        """
        Track a face that matches no existing student. Returns True only
        once it has been seen consistently enough to be believed.

        This is the phantom filter. A detection that appears for one
        frame and vanishes never reaches the threshold, so it can never
        take a slot. A real person who sits down clears it in a fraction
        of a second.
        """
        # Forget stale candidates first.
        self._pending = [
            p for p in self._pending
            if (t - p["last_seen"]) <= Config.IDENTITY_PENDING_TIMEOUT_SEC
        ]
        for p in self._pending:
            if ((cx - p["cx"]) ** 2 + (cy - p["cy"]) ** 2) ** 0.5 <= radius:
                # The required frames must arrive within a short window
                # of FIRST sighting, not merely add up eventually. A
                # sparse trickle of phantoms near one spot would
                # otherwise creep to the threshold over minutes — which
                # is exactly what testing at the observed phantom rate
                # produced. Restart the count instead.
                if (t - p["first_seen"]) > Config.IDENTITY_NEW_TRACK_WINDOW_SEC:
                    p["first_seen"] = t
                    p["hits"] = 1
                    p["cx"], p["cy"] = cx, cy
                    p["last_seen"] = t
                    return False
                p["cx"], p["cy"] = cx, cy
                p["last_seen"] = t
                p["hits"] += 1
                if p["hits"] >= Config.IDENTITY_NEW_TRACK_FRAMES:
                    self._pending.remove(p)
                    return True
                return False
        self._pending.append({"cx": cx, "cy": cy, "first_seen": t,
                               "last_seen": t, "hits": 1})
        self.total_phantoms_rejected += 1
        return False

    def _claim_slot(self, t: float, used_slots: set, centre, radius: float):
        """
        Pick a slot for a face that matched no active track. Never
        returns a slot that is in use this frame or still inside its
        grace period — that reservation is exactly what stops a
        neighbour from absorbing a temporarily-absent student.

        Preference order, and the reasoning for it:

          1. An EXPIRED slot whose last known position is nearest this
             face (within match radius). Keeps seat labels tied to
             physical seats — someone sitting where S2 sat becomes S2,
             which is how an invigilator reads the room. Safe despite
             the inheritance risk, because claiming an occupied slot
             always reports a reassignment, and the caller resets the
             pipeline before the newcomer accumulates anything.
          2. A never-used slot. A genuinely new person in a new place
             gets a clean seat rather than displacing a stored record.
          3. The longest-vacant expired slot — last resort when the
             room is full and none of the above apply.
        """
        available = [i for i in range(self.max_students)
                     if i not in used_slots and not self._is_held(i, t)]
        if not available:
            return None

        cx, cy = centre

        # 1. nearest expired slot by last known position
        expired_near = []
        for i in available:
            s = self.slots[i]
            if not s.occupied or s.cx is None:
                continue
            d = ((cx - s.cx) ** 2 + (cy - s.cy) ** 2) ** 0.5
            if d <= radius:
                expired_near.append((d, i))
        if expired_near:
            expired_near.sort()
            return expired_near[0][1]

        # 2. Any previously-occupied seat, before inventing a new one.
        #    In an exam the roster is essentially fixed, so someone who
        #    matched nothing is far more likely a student returning than
        #    a brand-new arrival. Preferring a fresh slot here is exactly
        #    what marched one person through S1 -> S3 -> S4 in testing.
        if Config.IDENTITY_PREFER_REUSE:
            reusable = [i for i in available if self.slots[i].occupied]
            if reusable:
                reusable.sort(key=lambda i: self.slots[i].last_seen or 0.0)
                return reusable[-1]  # most recently vacated

        # 3. never-used slot
        fresh = [i for i in available if not self.slots[i].occupied]
        if fresh:
            return fresh[0]

        # 3. longest-vacant expired slot
        available.sort(key=lambda i: self.slots[i].last_seen or 0.0)
        return available[0]

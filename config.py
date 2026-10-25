"""
config.py — Config

Split out of module_headpose_multi_web.py (Head Pose Intelligence Engine,
Capstone CPG-127) during the multi-file refactor. Code below is moved
verbatim from the original module — no algorithm, threshold, or naming
changes.
"""

# ═══════════════════════════════════════════════════════════════════
# CONFIG — All tunables in one place
# ═══════════════════════════════════════════════════════════════════


class Config:
    # Calibration
    CALIBRATION_FRAMES = 150  # ~5 seconds at 30fps
    ADAPTIVE_ALPHA = 0.0015  # Baseline drift speed (very slow)
    MIN_STD_DEG = 3.5
    MAX_STD_DEG = 5.0  # Cap — prevents huge threshold from wide calibration movement

    # ── Calibration freeze (Sep 2026) ──────────────────────────────
    # CalibrationEngine's docstring says calibration runs for
    # CALIBRATION_FRAMES frames, after which the baseline only drifts
    # slowly through adapt(). The code did not do that: feed() kept
    # running every frame, so the baseline became the running mean of
    # EVERY head position since the student sat down (matched to within
    # 0.003 against a real 22-minute log, 17 Sep 2026) and std widened
    # with every glance. Replaying that log and scripted scenarios, old
    # vs frozen:
    #   - looking +12 for 4 s every 15 s: 2 events old, 40 frozen — the
    #     student's own looks dragged the baseline along and widened the
    #     threshold until they barely registered
    #   - innocent: sat slightly turned for 2 min, then glanced +7:
    #     5 false events old, 0 frozen
    #   - the real 17 Sep sessions: 3 -> 2 and 2 -> 1 events, no new flags
    # Trade-off: a calibration spoiled by looking around during the first
    # few seconds now stays spoiled, because adapt() only corrects within
    # 0.5 std. Known limit either way: a head turned very slowly (over
    # minutes) is followed by adapt() and is not caught, old or new.
    CALIBRATION_FREEZE_ENABLED = True  # Yaw. False = old behaviour, byte-identical.
    PITCH_CALIBRATION_FREEZE_ENABLED = False  # Same fix for pitch, deliberately
    # OFF until real pitch data exists (pitch was first logged Sep 2026).
    # Students look down at their paper constantly, and whether a frozen
    # pitch baseline would start flagging ordinary writing cannot be
    # judged from synthetic data. Fairness to innocent students first.

    # Adaptive threshold
    # Direction = "Away" if |yaw - baseline| > K_SIGMA * std_dev
    K_SIGMA = 2.3  # Was 2.8 → 2.3. Threshold now ≈ 8° (was ≈ 10°).
    # 8° is realistic: looking at neighbour's paper = ~15°,
    # natural thinking glance = ~5°. This catches real cheating.

    # Kinematic
    VELOCITY_WINDOW = 7
    VELOCITY_CAP = 120.0
    ACCEL_CAP = 400.0

    # Temporal
    TEMPORAL_WINDOW_SEC = 20
    MIN_EVENT_DURATION = 1.2

    # ── Session memory decay ───────────────────────────────────────
    # PROBLEM THIS FIXES (mentor review, Aug 2026):
    #   session_score() used to be permanent — it summed every event's
    #   contribution and divided by a constant, with NO forgetting. In a
    #   3-hour exam a student who looked away a few times in the first
    #   10 minutes stayed pinned at 100% session memory for the
    #   remaining 2h50m, no matter how well they behaved afterwards.
    #   Because W_SESSION is the single heaviest fusion weight (0.30),
    #   that saturated signal then dominated the final verdict for the
    #   rest of the exam.
    #
    # THE FIX: exponential decay by event age. Recent behaviour counts
    # fully; older behaviour fades smoothly. Chosen over a hard periodic
    # reset because (a) every other signal in this system already decays
    # smoothly (see SUSPICION_EMA_ALPHA), so a sudden snap to zero would
    # be the only discontinuity in the model, and (b) a predictable
    # reset boundary is gameable — misbehave right after a reset and you
    # get a clean window. Decay has no such exploitable edge.
    #
    # IMPORTANT: only the SCORE decays. The raw event history in
    # TemporalPatternEngine.events is never deleted — it stays complete
    # for evidence, audit, and the session report.
    SESSION_MEMORY_HALFLIFE_SEC = 600.0  # 10 min: an event counts half
    # as much 10 minutes later, a quarter after 20, ~3% after an hour.
    SESSION_MEMORY_DIVISOR = 8.0  # Points needed to saturate at 100%.
    # Was 5.0 (an undocumented tweak that contradicted this module's own
    # docstring, which said 8). Restored to 8.0 per mentor feedback that
    # memory filled too fast. Sustained cheating still saturates easily;
    # occasional glances no longer do.
    SESSION_MEMORY_DECAY_ENABLED = True  # Kept as an explicit switch so
    # the old permanent-memory behaviour can be reproduced for
    # before/after comparison during evaluation.

    # ── Evidence capture triggers ──────────────────────────────────
    # Real testing found snapshots were far too rare: a student could
    # visibly look away again and again and produce nothing, because
    # verdict escalation needs several slow-moving conditions to line up
    # at once. Evidence now also fires on the raw suspicion score, which
    # is what actually moves during an episode.
    EVIDENCE_CAPTURE_VERDICTS = ("ALERT", "FLAG")  # verdict escalations
    # that capture a snapshot.
    EVIDENCE_SCORE_THRESHOLD = 45.0  # capture whenever the live score
    # reaches this, independent of verdict. Set near MODERATE so genuine
    # looking-away behaviour is caught while ordinary fidgeting is not.
    EVIDENCE_SCORE_COOLDOWN_SEC = 20.0  # minimum gap between
    # score-triggered snapshots for the SAME student, so one sustained
    # episode yields a handful of useful frames instead of hundreds of
    # near-identical ones filling the disk.

    # ── Identity tracking (seat assignment) ────────────────────────
    # PROBLEM THIS FIXES:
    #   Face→slot assignment used to be recomputed from scratch every
    #   frame by sorting faces left→right. Nothing remembered which face
    #   was S1 a moment ago. If a student left their seat, everyone to
    #   their right shifted up a slot — silently inheriting the missing
    #   student's calibration baseline, suspicion score, and evidence
    #   trail. That is a FALSE-ACCUSATION bug: an innocent student can
    #   end up wearing someone else's cheating record.
    #
    # THE FIX: match each detected face to the slot whose last known
    # position it is closest to, hold a vacated slot for a grace period
    # before letting anyone else take it, and reset + log the slot when
    # a genuinely new person does take it.
    INTELLIGENCE_INTERVAL_SEC = 0.5  # How often the pattern and
    # correlation engines re-run. They analyse minutes of history, so
    # recomputing every frame is wasted work — twice a second is far
    # quicker than the behaviour they describe can change.

    # ── Live risk timeline ─────────────────────────────────────────
    # The dashboard chart showing every student's suspicion score
    # evolving together. A single number tells you where a student is
    # now; the shape over time tells you whether it is climbing,
    # settling, or spiking - and lets one glance compare all four.
    RISK_TIMELINE_WINDOW_SEC = 300.0  # How much history the chart
    # shows (5 minutes). Long enough for a pattern to be visible,
    # short enough that recent movement is not squashed flat.
    RISK_TIMELINE_POINTS = 90  # Points sent per student per update.
    # The score buffer holds far more, but sending all of it eight
    # times a second to every browser would dwarf the entire rest of
    # the payload for detail a chart this size cannot render.

    # ── Session report (PDF) ───────────────────────────────────────
    # See session_report.py. The report is the artefact that outlives
    # the session — the thing a teacher can actually hand to someone.
    REPORT_ENABLED = True  # Generate automatically when a session ends.
    REPORT_MAX_TIMELINE_ROWS = 40  # Cap per student. A three-hour exam
    # can produce hundreds of events; a report nobody will read because
    # it is 90 pages long defeats the purpose. The full record stays in
    # the session log.
    REPORT_MAX_EVIDENCE_IMAGES = 4  # Per student. Same reasoning.
    REPORT_SCORE_SAMPLE_SEC = 2.0  # How often the suspicion score is
    # sampled for the history graph. Every frame would be 100x more data
    # than a printed chart can show.
    REPORT_SCORE_HISTORY_MAX = 3000  # Ring-buffer cap, about 100 minutes
    # at the sample rate above.

    # ── Cross-student correlation ─────────────────────────────────
    # See correlation.py. This is the layer that looks for RELATIONSHIPS
    # between students rather than scoring each in isolation — cheating
    # is usually social, and two people each looking sideways is
    # unremarkable until you notice they are looking at EACH OTHER,
    # within a second, repeatedly. No per-student score can see that.
    #
    # Nothing here alters anyone's suspicion score. It reports a
    # correlation with its evidence and an explicit caveat, for a human
    # to interpret. Correlation is not proof.
    CORRELATION_ENABLED = True
    CORRELATION_WINDOW_SEC = 180.0  # How far back pairwise history is
    # considered. Longer than the pattern window: a collaboration
    # signature emerges over minutes, not seconds.
    CORRELATION_MIN_EVENTS = 3  # Each student needs at least this many
    # events in the window before a pair is worth examining at all.
    CORRELATION_MIN_PAIRS = 3  # How many coincidences are required.
    # Two people looking away together once or twice is ordinary; the
    # signal is in repetition.
    CORRELATION_TIME_TOLERANCE_SEC = 1.5  # Two events this close count
    # as simultaneous. Wide enough for human reaction time, tight enough
    # that unrelated glances rarely land inside it.
    CORRELATION_CHANCE_MULTIPLE = 2.0  # Synchronised activity must
    # exceed what the pair's individual rates would produce by chance by
    # this factor. Two very fidgety students would otherwise correlate
    # with each other purely by arithmetic.
    CORRELATION_LAG_MIN_SEC = 0.4  # For one-way copying: the follower's
    CORRELATION_LAG_MAX_SEC = 6.0  # look must trail the leader's within
    # this range. Below the minimum is simultaneous rather than
    # following; above the maximum is unrelated.
    CORRELATION_LAG_CV = 0.4  # The lag must be CONSISTENT. Scattered
    # delays are just two people independently looking around; a
    # repeatable delay is what copying actually looks like.
    CORRELATION_MAX_FINDINGS = 6  # Cap so a busy room cannot flood the
    # dashboard with pairwise noise.

    # ── Behavioural pattern recognition ───────────────────────────
    # See behavior_patterns.py. These thresholds shape WHICH patterns
    # are recognised; they never alter the suspicion score itself, which
    # is computed exactly as before. Patterns explain the score, they do
    # not change it — so tuning here is safe.
    PATTERN_WINDOW_SEC = 120.0  # How far back "recent behaviour" looks.
    # Wider than TEMPORAL_WINDOW_SEC (20s) on purpose: a pattern is a
    # shape over minutes, whereas the suspicion signals are about the
    # last few seconds.
    PATTERN_MIN_EVENTS = 2  # Below this there is no shape to recognise,
    # only isolated incidents — and single events are not evidence.
    PATTERN_SHORT_EVENT_SEC = 2.5  # At or under this counts as a
    # "glance" (checking something) rather than reading.
    PATTERN_LONG_EVENT_SEC = 5.0  # At or over this counts as sustained
    # reading rather than a glance.
    PATTERN_BURST_MIN_DIRECTION_SHARE = 0.6  # For a flurry of brief
    # looks to count as targeted answer-checking, at least this share
    # must go the SAME way. Without this requirement an ordinary
    # restless student producing scattered glances was labelled
    # "answer-checking" — and it outranked their own exoneration.
    # Checking a specific thing means looking at a specific place.
    PATTERN_METRONOME_CV = 0.35  # Coefficient of variation in the gaps
    # between events, below which the timing is "suspiciously regular".
    # Human restlessness is irregular; working through copied answers
    # tends not to be.
    PATTERN_ESCALATION_RATIO = 1.8  # Second-half event rate must exceed
    # first-half by this factor to count as escalating.

    # ── Pitch / looking-down detection ─────────────────────────────
    # THE BLIND SPOT THIS CLOSES: the engine only ever tracked yaw
    # (left-right head turn). A student looking straight DOWN at a phone
    # in their lap barely changes yaw, so the single most common
    # real-world cheating posture was nearly invisible. The pitch angle
    # was already being computed by solvePnP every frame and discarded
    # (see head_pose.extract_head_pose).
    #
    # Pitch runs as a PARALLEL pipeline to yaw — its own Kalman filter
    # and its own calibrated baseline, because everyone holds their head
    # at a different natural reading angle. A downward deviation then
    # produces an ordinary deviation event tagged "Down", so the
    # existing frequency / duration / session signals pick it up with no
    # change to the fusion weights at all.
    PITCH_ENABLED = True  # Set False for byte-identical old behaviour.
    PITCH_DOWN_SIGN = -1.0  # Which sign of pitch deviation means
    # "looking down". RQDecomp3x3's sign convention depends on the solve,
    # so this is a flag rather than a hardcoded assumption — flip it
    # after real-camera testing if down/up come out reversed.
    PITCH_K_SIGMA = 2.6  # Deviations beyond this many standard
    # deviations below baseline count as looking down. Slightly higher
    # than K_SIGMA (2.3) for yaw because natural pitch varies more: a
    # student legitimately looks down at their paper constantly, so the
    # bar for "suspiciously down" has to sit above normal writing.
    PITCH_MIN_DOWN_DEG = 12.0  # Absolute floor. A deviation must ALSO
    # exceed this many degrees below baseline, so a student with an
    # unusually steady head (tiny std) is not flagged for a couple of
    # degrees of ordinary movement.

    # ── Expected student count (roster cap) ────────────────────────
    # WHY THIS EXISTS — real log evidence:
    #   In a session with exactly ONE person present, MediaPipe reported
    #   extra faces on 5.6% of frames. Crucially those phantoms were NOT
    #   brief noise: the longest ran 349 consecutive frames (27.6s), and
    #   several ran 5-20s. Something stable in the room (a doorway, a
    #   poster, a reflection, a person in the background) was being
    #   detected as a face for half a minute at a time.
    #
    #   No amount of persistence-checking can defeat that: a phantom
    #   lasting 27 seconds passes any "is it stable?" test a real
    #   student would pass. Geometry alone genuinely cannot separate a
    #   convincing fake face from a real student.
    #
    #   But the invigilator knows how many students are in the room. So
    #   they tell the system, and it simply refuses to track more than
    #   that many people. With 1 student expected, a background phantom
    #   can never take an identity, no matter how long it persists.
    #
    #   This is the same principle already used for the break button and
    #   identity questions: where the system cannot be sure, the human
    #   who does know decides, rather than the system guessing.
    EXPECTED_STUDENTS = None  # None = auto (track up to MAX_STUDENTS,
    # the old behaviour). Set to an integer, or use the dashboard
    # control, to cap how many people can ever be tracked at once.
    # When capped, the LARGEST faces win — a real student at the desk is
    # nearer the camera, and therefore bigger, than background objects.

    MIN_FACE_AREA_FRAC = 0.30  # Even in auto mode, drop any detection
    # whose bounding box area is below this fraction of the LARGEST
    # face's area in the same frame. Background phantoms are further
    # away and therefore markedly smaller than the student at the desk.
    # Set to 0 to disable size filtering entirely.

    # ── Phantom-detection defences ─────────────────────────────────
    # REAL LOG EVIDENCE: in a 21-minute session with exactly ONE person
    # in front of the camera, MediaPipe reported 2+ faces on 5.6% of
    # frames (1626 frames), and 3 faces on 4.4%. These are phantoms —
    # background texture, a doorway, a reflection, or the same face
    # detected twice.
    #
    # The damage they did: a phantom lasting a SINGLE FRAME immediately
    # claimed a brand-new slot. Half a second later the real person was
    # being matched to that phantom's slot instead of their own, and
    # their identity had silently moved S1 -> S3 -> S4. This, not the
    # matching maths, was the true cause of the identity churn.
    #
    # Two defences, both cheap:
    IDENTITY_MIN_FACE_SEPARATION_FRAC = 0.10  # Two detections closer
    # together than this (fraction of frame width) are the same person
    # detected twice. The extra one is discarded before matching.
    IDENTITY_NEW_TRACK_FRAMES = 8  # A face matching no existing student
    # must be seen this many frames in roughly the same place before it
    # is granted an identity of its own. A one-frame phantom can never
    # clear this; a real person walking in clears it in a third of a
    # second. This single rule is what stops phantoms from stealing
    # identities.
    IDENTITY_NEW_TRACK_WINDOW_SEC = 1.0  # The frames above must be
    # collected within this long of first sighting. Without this, sparse
    # phantoms trickling in near the same spot could slowly accumulate
    # hits over minutes and eventually earn an identity anyway — testing
    # at the real 5.6% phantom rate showed exactly that happening. A
    # genuine person is detected on essentially every frame and reaches
    # the threshold in a third of a second, so this costs them nothing.
    IDENTITY_PENDING_TIMEOUT_SEC = 1.5  # Forget a pending candidate not
    # seen for this long, so brief flickers never accumulate into an
    # identity over minutes.
    IDENTITY_PREFER_REUSE = True  # When an unmatched face DOES earn an
    # identity, prefer giving it a seat that a student previously
    # occupied over inventing a brand-new one. In an exam the roster is
    # essentially fixed, so a returning student is far more likely than
    # a genuinely new arrival — and this is what stops one person
    # marching through S1, S3, S4 as they come and go.

    IDENTITY_ACTIVE_TRACK_SEC = 1.0  # A slot seen within this long is
    # considered ACTIVELY tracked. Actively tracked slots are matched
    # first, in their own pass, before any stale slot is considered.
    #
    # WHY (found in real testing): matching used to weigh every slot
    # together purely on pixel distance, so a slot last seen 20 minutes
    # ago competed on equal terms with one seen 40 milliseconds ago. A
    # student simply shifting position in their own chair could end up
    # closer to some dead slot's stale position than to their own — and
    # their identity flipped back and forth as they leaned. Someone
    # already on camera must never be stolen by a stale slot.
    IDENTITY_MATCH_RADIUS_FRAC = 0.18  # Max match distance as a fraction
    # of frame width. A face must be within this of a slot's last known
    # position to be considered the same person continuing.
    IDENTITY_GRACE_PERIOD_SEC = 5.0  # How long a slot is held vacant
    # for its owner after they disappear (looked down, hand over face,
    # leaned out of frame) before it can be reassigned to a new person.
    IDENTITY_CONTINUITY_TIMEOUT_SEC = 300.0  # Outer bound on "same
    # person returning". Between the grace period and this timeout, a
    # face reappearing at a slot's old position is still assumed to be
    # its original owner coming back. Past this timeout that assumption
    # is no longer safe on position alone — so the slot is treated as a
    # genuinely new occupant: reset and logged.
    #
    # Raised 60s -> 300s (5 min): 60 seconds was far too short for a
    # real exam. A student going to the bathroom is gone 3-5 minutes and
    # would have been wrongly treated as a stranger and reset. Note the
    # timeout is a fallback, NOT the main protection — face
    # re-identification (face_signature.py) and the invigilator break
    # button both restore a returning student regardless of how long
    # they were away. This value only matters when both of those are
    # unavailable.
    #
    # Three tiers in total:
    #   0 → GRACE            : slot held exclusively, nobody else may take it
    #   GRACE → CONTINUITY   : original owner may resume by position
    #   beyond CONTINUITY    : new occupant, pipeline reset + audit logged
    #
    # IMPORTANT: expiring no longer DELETES anything. The outgoing
    # occupant's full record is filed in occupant_archive.py first, so
    # leaving the room can never erase a suspicion record.

    # ── Invigilator break (manual override) ────────────────────────
    # A human invigilator already knows when a student is on an
    # authorised break. Marking it explicitly is more reliable than any
    # amount of inference: while on break, a slot is held indefinitely,
    # monitoring is paused, and no suspicion accrues. Nothing about the
    # student's record is touched, so they resume exactly where they
    # left off.
    BREAK_PAUSES_SCORING = True  # While on break, skip pipeline
    # processing entirely for that slot rather than feeding it frames of
    # an empty chair — which would otherwise pollute their calibration.

    # ── Face re-identification (see face_signature.py) ─────────────
    # Closes the two holes position-tracking cannot: a student returning
    # after a long absence being treated as a stranger, and leaving the
    # room being usable to wipe a suspicion record.
    FACE_SIG_ENABLED = True
    FACE_SIG_ENROL_SAMPLES = 30  # Frames averaged into one enrolled
    # signature. Averaging suppresses per-frame landmark jitter.
    FACE_SIG_MAX_ENROL_YAW = 12.0  # Only enrol from near-frontal faces;
    # a signature built from a turned head would poison the gallery.
    FACE_SIG_MAX_MATCH_YAW = 20.0  # Slightly looser for matching than
    # enrolment — a returning student may not be perfectly square to the
    # camera, but a heavily turned face is still refused.
    FACE_SIG_MATCH_THRESHOLD = 0.93  # Absolute similarity floor for an
    # AUTOMATIC match — the system acts on its own only above this.
    FACE_SIG_MATCH_MARGIN = 0.02  # Best must beat runner-up by this to
    # be acted on automatically.
    FACE_SIG_UNCERTAIN_THRESHOLD = 0.80  # Lower edge of the "ask a
    # human" band. Between this and FACE_SIG_MATCH_THRESHOLD the system
    # thinks it recognises someone but is not sure enough to act.
    #
    # WHY THREE OUTCOMES, NOT TWO:
    #   The earlier design had only "confident enough → restore" and
    #   "not confident → silently treat as a new person". That quietly
    #   equated "I don't know" with "definitely a different student" —
    #   so a student genuinely returning, merely measured a little
    #   differently, was permanently and invisibly given a stranger's
    #   treatment and nobody ever found out.
    #
    #   Identity is an accusation-relevant decision. When the system has
    #   already admitted it is unsure, that decision belongs to the
    #   invigilator, who actually knows who left the room. The system's
    #   job is to raise the question, not to settle it. This mirrors how
    #   the suspicion score already works: the engine flags, a human
    #   judges.
    #
    #   >= MATCH_THRESHOLD (and margin ok) → restore automatically
    #   >= UNCERTAIN_THRESHOLD             → ASK the invigilator
    #   below that                         → treat as new, no prompt
    #
    # The middle band should be rare by design, so this does not turn
    # into the invigilator being pestered constantly.
    IDENTITY_REVIEW_EXPIRY_SEC = 1800.0  # How long an unanswered
    # identity question stays open (30 min). Expiry is harmless: it
    # means exactly the same as the invigilator answering "no" — the
    # student simply carries on as a new occupant, which is what was
    # already happening while the question was open.
    IDENTITY_REVIEW_MAX_OPEN = 12  # Cap on simultaneously open
    # questions, so a camera glitch producing many faces can never
    # flood the dashboard.
    # Both must pass. Restoring the WRONG student's history is worse
    # than making someone recalibrate, so ambiguity is always resolved
    # by refusing to identify rather than guessing. Every rejection
    # falls back to the existing position-based behaviour.
    IDENTITY_RESET_ON_REASSIGN = True  # When a slot IS handed to a new
    # person, wipe that slot's pipeline so the newcomer starts clean
    # instead of inheriting the previous occupant's history.

    # Suspicion fusion weights (must sum to 1.0)
    W_POSITION = 0.15  # Reduced — position alone is weak evidence
    W_VELOCITY = 0.10  # Unchanged
    W_FREQUENCY = 0.20  # Reduced slightly — window-based, can be gamed
    W_DURATION = 0.25  # Unchanged — away time is honest signal
    W_SESSION = 0.30  # Increased from 0.20 → 0.30.
    # Session memory now has most weight.
    # Past cheating STAYS on record and matters most.

    # Smoothing
    SUSPICION_EMA_ALPHA = 0.08  # Was 0.035 → 0.08. Score reacts faster.
    # Previous was so slow it felt dead.

    # Alert thresholds — lowered so they're actually reachable
    LEVEL_LOW = 18  # Was 20
    LEVEL_MILD = 35  # Was 38
    LEVEL_MODERATE = 50  # Was 58 — most important change
    LEVEL_HIGH = 65  # Was 75 — now reachable after sustained cheating

    # Confirmed event accumulation
    CONFIRMED_THRESHOLD = 75.0  # % above which time accumulates

    # ── Dashboard access (Sep 2026) ────────────────────────────────
    # None of these touch scoring. They decide who may open the
    # dashboard, which shows live camera frames of students and can
    # switch the whole page to demo data mid-exam (/api/mode) — both of
    # which were open to anyone who could reach the port.
    DASHBOARD_LOGIN_ENABLED = True  # Invigilator sign-in (see
    # dashboard_auth.py). False restores the old open dashboard for
    # solo development, and is then only ever served on this computer:
    # a non-local DASHBOARD_HOST is refused while login is off.
    DASHBOARD_HOST = "127.0.0.1"  # "127.0.0.1" = this computer only.
    # "0.0.0.0" lets other devices on the same network open it (e.g. a
    # panel member's laptop during a demo). Traffic is plain HTTP, so
    # use that only on a network you trust.
    DASHBOARD_PORT = 5000
    DASHBOARD_SESSION_HOURS = 12.0  # A sign-in lasts this long. Long
    # enough for any exam day; an invigilator is never signed out in
    # the middle of a three-hour paper.

    # ── Presence: seats the engine cannot measure ──────────────────
    # THE GAP THIS CLOSES: a seat with no measurable face used to be
    # completely silent — no score, no event, no word to the
    # invigilator. Measured on the 5 Oct 2026 validation clips, the
    # "phone in the lap" clip spent 29 seconds in one unbroken stretch
    # with no face the engine could read, 89 seconds unmeasured in
    # total, and still finished at verdict OK with a 3.5% peak. A
    # student the camera cannot see scored better than one it could.
    #
    # presence.py now reports those spells as plain fact. It is NOT a
    # suspicion signal and feeds nothing in the scoring pipeline: an
    # innocent clip (posture_shift, a student leaning low over the
    # page) has a 13-second spell of its own. The invigilator is told
    # the seat could not be seen; the invigilator decides.
    PRESENCE_TRACKING_ENABLED = True  # False = the old silence, exactly.
    PRESENCE_UNMEASURED_SEC = 3.0  # A spell must last this long before
    # it is opened, logged or counted. Ordinary writing drops the face
    # constantly in flickers — on innocent_writing the longest single
    # loss was 0.4s across two minutes — and reporting those would bury
    # the real thing. Measured spell counts on the 8 clips at this
    # value: writing 0, fidget 0, side_glance 0, long_look 0,
    # steady 2 (10.2s), posture_shift 8 (46.0s), quick_checks 3
    # (32.9s), phone_lap 8 (89.1s). Lowering it to 2.0s adds only
    # short spells on clips that are already clean.
    PRESENCE_GONE_SEC = 30.0  # Past this, a spell is called "gone"
    # rather than "not visible". Deliberately well under
    # IDENTITY_CONTINUITY_TIMEOUT_SEC (300s): the seat is still held
    # for its owner, this only changes what the invigilator is told.
    PRESENCE_MAX_SPELLS = 50  # Rolling cap per seat, same reasoning as
    # the event log's 500: a bounded buffer cannot grow without limit
    # over a three-hour exam.

    # ── Body check (optional, off by default) ──────────────────────
    # Tells "head down over the desk" apart from "the chair is empty",
    # which a face model alone cannot do. Measured on the clips: body
    # pose tracked 100% of frames, including every frame where no face
    # was findable at all.
    #
    # OFF by default because it is a second model in the live loop and
    # this project has already spent weeks investigating frame-rate
    # complaints. When on it runs only while some seat is actually
    # unmeasured, and at most PRESENCE_BODY_CHECK_HZ times a second —
    # so a hall where everyone's face is visible pays nothing.
    PRESENCE_BODY_CHECK_ENABLED = False
    PRESENCE_BODY_CHECK_HZ = 2.0  # Upper bound on how often the body
    # model runs. A chair does not empty in half a second.
    PRESENCE_BODY_MODEL = "models/pose_landmarker_lite.task"
    PRESENCE_BODY_MIN_CONF = 0.5

    # ── Face rescue (optional, off by default) ─────────────────────
    # MediaPipe's FaceMesh gives up on a low head far sooner than the
    # face actually leaves the frame. Measured on the 5 Oct 2026 clips:
    # FaceMesh saw 41.2% of innocent_posture_shift while YuNet found a
    # face in 97.7%. face_rescue.py crops to YuNet's box and runs the
    # landmark model again there.
    #
    # What it is measured to buy, on the whole clips:
    #   innocent_posture_shift  41.2% -> 60.0% of frames
    #   suspicious_phone_lap    25.1% -> 28.5%
    # The lap posture stays unmeasurable, as expected: YuNet's box there
    # is the crown of a head, and a crown has no face geometry.
    #
    # OFF by default because recovered frames go into scoring like any
    # other frame — this CHANGES SCORES, so switching it on should be a
    # decision with evidence behind it. Config.FACE_RESCUE_ENABLED =
    # False reproduces the old behaviour exactly.
    #
    # Also measured and rejected: MediaPipe's newer Face Landmarker
    # (Tasks API) as a replacement for FaceMesh. Worse on this footage —
    # 32.7% against 86.4% on innocent_writing.
    FACE_RESCUE_ENABLED = False
    FACE_RESCUE_MODEL = "models/face_detection_yunet_2023mar.onnx"
    FACE_RESCUE_MIN_CONF = 0.6  # YuNet score threshold.
    FACE_RESCUE_MESH_CONF = 0.4  # Looser than the live mesh's 0.55: the
    # crop has already been vouched for by a detector, so the second
    # pass does not need to re-earn that confidence from scratch.
    FACE_RESCUE_MARGIN = 0.6  # Crop padding as a fraction of the box.
    # Too tight and the landmark model has no context; too loose and the
    # crop is just the whole frame again.

    # ── Eye tracking (optional, off by default) ────────────────────
    # THE GAP THIS CLOSES: a student can keep their head still and move
    # only their eyes. A head-pose engine cannot see that at all, and it
    # is the first objection anyone raises about one.
    #
    # Measured on the 5 Oct 2026 clips, 5th percentile of iris offset:
    #   innocent_steady  -0.164   suspicious_quick_checks  -0.341
    #   innocent_writing -0.138   suspicious_side_glance   -0.414
    #
    # Turning this on also switches MediaPipe to refine_landmarks=True,
    # which on this footage DETECTS THE FACE FAR MORE OFTEN — a second,
    # unexpected benefit, larger than the YuNet rescue gives:
    #   innocent_steady          70.8% -> 98.3% of frames
    #   innocent_writing         69.9% -> 95.6%
    #   suspicious_quick_checks  70.4% -> 86.4%
    #   suspicious_side_glance   96.6% -> 100.0%
    # Existing head angles barely move with it on: yaw differed by a
    # median of 0.3-0.5 degrees and pitch by 0.65-0.85, against an away
    # threshold of about 8 degrees.
    #
    # ON since 8 Oct 2026, with Yuvraj's approval, after the one clip
    # that could settle it was recorded. `eyes_only` is two minutes of
    # him sitting upright with his head deliberately still — it moved
    # 2.0 degrees in total across the whole clip and never once crossed
    # the away threshold — while his eyes flicked to one side.
    #   eye tracking OFF -> OK,    peak  0.5%,  0 events, not flagged
    #   eye tracking ON  -> ALERT, peak 53.7%, 10 events, flagged
    # With the feature off the engine saw NOTHING: two minutes of
    # deliberate glancing scored half a percent. The miss was total, not
    # marginal. Across all 9 clips, switching it on takes 2 of 5 cheating
    # clips caught to 3 of 5, with still 0 of 4 innocent clips wrongly
    # flagged.
    # Setting it back to False reproduces the pre-eye behaviour exactly;
    # that was verified field-for-field on real clips before it was ever
    # turned on, and the seeded equivalence test is unaffected either way.
    EYE_TRACKING_ENABLED = True
    EYE_K_SIGMA = 2.6  # Deviations beyond this many standard deviations
    # of the student's own normal eye position count. Matches
    # PITCH_K_SIGMA rather than yaw's 2.3: eye position is noisier.
    EYE_MIN_OFFSET = 12.0  # Absolute floor, as a PERCENT of eye width,
    # ON TOP of the statistical bar — the same belt-and-braces rule
    # PITCH_MIN_DOWN_DEG applies, for the same reason: a student whose
    # eyes barely move has a tiny std, and a z-score alone would flag
    # ordinary reading. The innocent clips sit around 0.14 at their 5th
    # percentile, so this is deliberately not a generous threshold and
    # it is PROVISIONAL until a clip is recorded of eyes moving while
    # the head stays still.
    # Units matter here: eye_gaze.py reports the offset as a PERCENT of
    # eye width so it shares a numeric range with the degree-based
    # signals. CalibrationEngine floors the std it learns at MIN_STD_DEG
    # (3.5) whatever it is fed; on a 0-1 fraction that floor makes every
    # z-score about 0.03 and the threshold literally unreachable, which
    # is exactly what the first build did — silently, on all 8 clips.
    # ── Vertical eye channel (built, but OFF — see below) ─────────
    # Measured on the real clips and NOT trustworthy enough to act on
    # yet, which is why it has its own switch rather than riding on
    # EYE_TRACKING_ENABLED. Frames beyond a 6-point deviation:
    #   innocent_fidget   6.8%   suspicious_side_glance 41.0%
    #   innocent_steady  35.3%
    # An innocent clip sitting at 35% is not a usable separation. The
    # likely reason is a biased sample rather than a bad idea: on
    # innocent_steady only 411 of 3,533 frames had a readable iris at
    # all, because he spent the clip looking down at paper with his lids
    # covering his eyes, so the few readable frames are exactly the
    # moments he looked up and moved — and a "resting baseline" computed
    # from those is not his resting position.
    # Gating to a level head helps the clip with a healthy sample
    # (innocent_fidget falls to 1.8%) and does not rescue the starved
    # one. So: the channel is implemented, both gates are on, the floor
    # is conservative, and the switch stays OFF until a clip exists that
    # actually contains a level head with the eyes dropped.
    EYE_VERTICAL_ENABLED = False
    EYE_REQUIRE_HEAD_LEVEL = True  # Only act while the head is NOT
    # pitched. Same reasoning as EYE_REQUIRE_HEAD_FORWARD: a pitched
    # head is already covered by the pitch pipeline, and it also drags
    # the iris against the eye corners through perspective, so a reading
    # taken then is both redundant and contaminated.
    EYE_MIN_OFFSET_V = 15.0  # Same bar for the VERTICAL eye channel, in
    # the same percent-of-eye-width units. Lower than the sideways one
    # because an eye travels less far up and down before the lid gets in
    # the way, so the same absolute floor would make the vertical
    # channel unreachable. This closes the case neither head signal can
    # see: a level head with the eyes dropped to something on the desk —
    # pitch sees nothing because the head did not move, and the sideways
    # channel sees nothing because the eyes did not go sideways.
    # Set at the 99th percentile of the cleanest innocent sample rather
    # than at a level that would fire often, deliberately: an unproven
    # signal should start too quiet, not too loud.
    EYE_MIN_OPENNESS = 0.18  # Eyelid gap over eye width. Below this the
    # eye is blinking or squinting and the iris position means nothing.
    EYE_MIN_WIDTH_PX = 8.0  # An eye narrower than this on screen is
    # foreshortened past the point of a trustworthy ratio.
    EYE_REQUIRE_HEAD_FORWARD = True  # Only act on the eye signal while
    # the head is within its normal range. Two reasons, and both matter:
    # a turned head is already caught by yaw, and a turned head also
    # shifts the iris against the eye corners through plain perspective
    # (about 0.01 of offset per degree), so a reading taken then is both
    # redundant and contaminated. Leaving this on means the eye path
    # fires ONLY on the case yaw genuinely cannot see.

    # ── Frame faults ───────────────────────────────────────────────
    # The main loop swallows per-frame errors so one bad frame cannot
    # end an exam. That is right, but on 13 Oct 2026 a typo raised on
    # EVERY frame and the run still finished, announcing 0 students and
    # 0.0% face seen as though it had watched an empty room. See
    # frame_faults.py. Past this fraction of failed frames a run is
    # reported as DEGRADED in the console summary, the clip results, the
    # batch table and the dashboard — never again as a clean empty one.
    FRAME_FAULT_ALERT_FRAC = 0.02  # 2% of frames.

    # ── Room context (optional, off by default) ────────────────────
    # THE GAP THIS CLOSES: every student is judged only against their
    # own baseline, so when the whole room reacts to something — the
    # invigilator speaking, a door — four heads come up together and the
    # engine reads four independent decisions to look away. A human
    # invigilator would read it as one room event, and would instead
    # notice the student who looked away while nobody else did.
    #
    # room_context.py damps ONE fusion term, pos_score, by how much of
    # the rest of the room is away at that moment (leave-one-out). The
    # event is still recorded and still counts toward frequency,
    # away-ratio and session memory — nothing is hidden, only the
    # instantaneous "how far is this head turned" term is discounted.
    #
    # OFF by default: it changes scores, same rule as
    # EYE_TRACKING_ENABLED and FACE_RESCUE_ENABLED. False reproduces the
    # old behaviour exactly.
    ROOM_CONTEXT_ENABLED = False
    ROOM_MIN_OTHERS = 2  # Fewer other students than this and the room is
    # not a sample worth comparing against, so nothing changes. With one
    # student in frame this feature never does anything at all.
    ROOM_DAMP = 0.8  # How much a fully-shared moment discounts the
    # position term. At 0.8, a glance the entire room shares contributes
    # a fifth of what a solitary glance does. Not 1.0 on purpose: a
    # moment everyone shares is weaker evidence, never zero evidence.
    ROOM_SHARED_FRAC = 0.6  # At or above this fraction the moment is
    # labelled "shared" for the dashboard. Reporting only — the damping
    # itself is proportional and has no threshold.

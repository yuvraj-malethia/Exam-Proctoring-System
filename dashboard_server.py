"""
dashboard_server.py — Flask app: ROUTES ONLY. No HTML/CSS/JS strings live
in this file (they're in dashboard/index.html, styles.css, dashboard.js).

Two ways to run this:

1. Imported and driven by module_headpose_multi_web.py  (REAL mode)
   ─────────────────────────────────────────────────────────────────
   create_app(snapshot) is called with the live SharedSnapshot from the
   CV engine's main() thread. REAL data is served by default. An
   optional toggle (POST /api/mode) can switch the browser to DEMO DATA
   for UI comparison without touching the real engine underneath.

2. Run directly — `python dashboard_server.py`               (MOCK mode)
   ─────────────────────────────────────────────────────────────────
   No camera is opened and mediapipe is never imported. A MockEngine
   instance drives the dashboard so the UI can be designed/tested with
   no students in front of a webcam. Always labeled DEMO DATA, and the
   mode toggle is locked on (there is no real engine to switch to).

Sign-in (Sep 2026): every route except the sign-in page itself requires
an invigilator to be signed in (dashboard_auth.py holds the accounts,
checks and access log; this file only wires the routes). Every POST must
also carry the header X-Requested-With: exam-monitor, which a page on
another site cannot add without the browser asking this server first —
so no other website can press the dashboard's buttons on a signed-in
invigilator's behalf.
"""

import json
import os
import threading
import time
from datetime import timedelta
from urllib.parse import quote

from flask import (Flask, Response, jsonify, redirect, request,
                   send_from_directory, session)

from config import Config
import dashboard_auth
from mock_engine import MockEngine
import session_replay
import validation

_DASHBOARD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard")
# Output directories are resolved against the CURRENT WORKING DIRECTORY,
# not this file's location, because that is where the engine actually
# writes them: module_headpose_multi_web.py uses relative paths like
# os.path.join("evidence", session_id). Resolving them next to this
# source file instead meant that whenever the program was started from
# anywhere other than the project folder, the server looked in one place
# while the engine wrote to another — and every download returned 404
# while the files sat on disk perfectly intact.
_EVIDENCE_DIR = os.path.abspath("evidence")
# Profiler logs for Session Replay — same working-directory rule as above.
_DIAGNOSTICS_DIR = os.path.abspath("diagnostics")
# Labelled clips and their replay results (Validation page) — same rule:
# record_clip.py and video_batch.py write them relative to the working
# directory.
_CLIPS_DIR = os.path.abspath("clips")
_VIDEO_RESULTS_DIR = os.path.abspath("video_results")

_STREAM_HZ = 8    # state push rate to the browser (SSE)
_VIDEO_HZ = 15    # camera-frame push rate to the browser (MJPEG)

_EMPTY_STATE = {
    "timestamp": 0,
    "system": {
        "mock": False, "engine_connected": False,
        "fps": 0, "visible_students": 0, "calibrated_students": 0,
        "attention_students": 0, "highest_risk": None,
        "session_time": 0, "max_students": 4,
    },
    "config": {},
    "students": {},
}


# Reachable without signing in: the sign-in page, what it loads, and the
# endpoints it calls. Everything else is closed by default, so a route
# added later is protected without anyone having to remember to.
_PUBLIC_PATHS = {
    "/login", "/login.js", "/styles.css", "/favicon.svg",
    "/api/auth/state", "/api/auth/login", "/api/auth/setup",
}
_CSRF_HEADER = ("X-Requested-With", "exam-monitor")


def create_app(snapshot=None, identity=None, on_break=None,
                reviews=None, on_identity_review=None,
                on_generate_report=None, roster=None,
                on_roster_change=None, login_enabled=None, auth=None):
    """
    snapshot: a SharedSnapshot instance from the real engine, or None to
    run fully standalone (mock-only — see module docstring, mode 2).
    identity: the engine's IdentityTracker, so the invigilator break
        endpoint can hold/release a student's slot. Optional — the
        endpoint reports unavailable rather than failing if absent.
    on_break: optional callback(slot, on_break) fired after a successful
        break toggle, so the engine can log it to the event log and the
        black-box recorder.
    reviews: the engine's IdentityReviewQueue, holding identity
        questions the system could not answer confidently on its own.
    on_identity_review: callback(review_id, confirmed) -> (ok, payload)
        run when an invigilator answers one. Confirming is what merges a
        returning student's archived record back into their live one.
    login_enabled: None = Config.DASHBOARD_LOGIN_ENABLED.
    auth: a dashboard_auth.DashboardAuth to use instead of the default
        one (tests pass one that keeps its accounts in a scratch folder).
    """
    app = Flask(__name__, static_folder=_DASHBOARD_DIR, static_url_path="")

    auth = auth or dashboard_auth.DashboardAuth(
        enabled=Config.DASHBOARD_LOGIN_ENABLED if login_enabled is None else login_enabled)
    app.secret_key = auth.secret_key
    app.config.update(
        SESSION_COOKIE_NAME="exam_monitor_session",
        SESSION_COOKIE_HTTPONLY=True,       # page scripts can't read it
        SESSION_COOKIE_SAMESITE="Lax",      # not sent on other sites' requests
        PERMANENT_SESSION_LIFETIME=timedelta(hours=Config.DASHBOARD_SESSION_HOURS),
        # Browsers must re-check dashboard files on every load (a cheap
        # "304 not modified" when unchanged). Without this, Chrome kept a
        # stale styles.css after an update while loading the new
        # dashboard.js — a half-old page.
        SEND_FILE_MAX_AGE_DEFAULT=0,
    )

    @app.before_request
    def _guard():
        # Every POST, signed in or not, must come from this dashboard's own
        # scripts (see module docstring).
        if request.method == "POST" and request.headers.get(_CSRF_HEADER[0]) != _CSRF_HEADER[1]:
            return jsonify({"error": "Request refused: it did not come from the dashboard."}), 403
        if not auth.enabled or request.path in _PUBLIC_PATHS:
            return None
        if auth.session_user(session):
            return None
        if (request.path.startswith(("/api/", "/evidence/", "/reports/"))
                or request.path in ("/stream", "/video_feed")):
            return jsonify({"error": "Sign in required.", "signin": "/login"}), 401
        return redirect("/login?next=" + quote(request.full_path.rstrip("?")))

    # ── Sign-in ───────────────────────────────────────────────────────
    @app.route("/login")
    def login_page():
        if not auth.enabled or auth.session_user(session):
            return redirect("/")
        return send_from_directory(_DASHBOARD_DIR, "login.html")

    @app.route("/api/auth/state")
    def auth_state():
        """What the sign-in page should offer. setup_allowed: a first
        account may only be created from the computer running the server."""
        user = auth.session_user(session) if auth.enabled else None
        return jsonify({
            "enabled": auth.enabled,
            "user": user,
            "setup_needed": auth.enabled and not auth.has_accounts(),
            "setup_allowed": dashboard_auth.is_loopback(request.remote_addr),
        })

    def _sign_in(name, fp):
        session.clear()
        session.permanent = True
        session["user"] = name
        session["fp"] = fp

    @app.route("/api/auth/login", methods=["POST"])
    def auth_login():
        if not auth.enabled:
            return jsonify({"user": None})
        addr = request.remote_addr
        wait = auth.wait_seconds(addr)
        if wait > 0:
            return jsonify({"error": f"Too many attempts. Try again in {int(wait) + 1} s.",
                            "retry_after": int(wait) + 1}), 429
        body = request.get_json(silent=True) or {}
        name = str(body.get("username", "")).strip()
        ok, result = auth.check_password(name, str(body.get("password", "")), addr)
        if not ok:
            auth.log("failed sign-in", name, addr)
            return jsonify({"error": result}), 401
        _sign_in(name, result)
        auth.log("signed in", name, addr)
        return jsonify({"user": name})

    @app.route("/api/auth/setup", methods=["POST"])
    def auth_setup():
        """Create the FIRST account. Refused once any account exists, and
        refused from any other computer (see dashboard_auth docstring)."""
        if not auth.enabled:
            return jsonify({"error": "Sign-in is switched off."}), 400
        if auth.has_accounts():
            return jsonify({"error": "An account already exists. Sign in instead."}), 409
        if not dashboard_auth.is_loopback(request.remote_addr):
            return jsonify({"error": "The first account can only be created on the "
                                     "computer running the exam monitor."}), 403
        body = request.get_json(silent=True) or {}
        name = str(body.get("username", "")).strip()
        err = auth.add_account(name, str(body.get("password", "")))
        if err:
            return jsonify({"error": err}), 400
        ok, fp = auth.check_password(name, str(body.get("password", "")), request.remote_addr)
        _sign_in(name, fp)
        auth.log("account created", name, request.remote_addr, "first account, from the browser")
        return jsonify({"user": name})

    @app.route("/api/auth/logout", methods=["POST"])
    def auth_logout():
        name = session.get("user")
        session.clear()
        if name:
            auth.log("signed out", name, request.remote_addr)
        return jsonify({"ok": True})

    @app.route("/api/auth/me")
    def auth_me():
        return jsonify({"enabled": auth.enabled,
                        "user": auth.session_user(session) if auth.enabled else None})

    mock_engine = MockEngine()
    # Held in dicts rather than as bare locals so they stay rebindable
    # from the enclosing scope without needing `nonlocal` in each route.
    identity_ref = {"tracker": identity}
    on_break_cb = {"fn": on_break}
    reviews_ref = {"queue": reviews}
    review_cb = {"fn": on_identity_review}
    report_cb = {"fn": on_generate_report}
    roster_ref = {"roster": roster}
    roster_cb = {"fn": on_roster_change}
    # Same reasoning as _EVIDENCE_DIR above: match where the engine writes.
    _REPORTS_DIR = os.path.abspath("reports")
    # forced=True: no real engine was attached at all (standalone run) —
    # there is nothing else to show, so mock can never be switched off.
    state_lock = threading.Lock()
    mode = {"mock": snapshot is None, "forced": snapshot is None}

    def _current_payload():
        with state_lock:
            use_mock = mode["mock"] or mode["forced"]
        if use_mock:
            return mock_engine.tick()
        state, jpeg = snapshot.get()
        if state is None:
            # Real engine attached but hasn't published a frame yet —
            # e.g. the browser was opened before the webcam finished
            # starting up. Frontend shows "WAITING FOR ENGINE".
            waiting = dict(_EMPTY_STATE)
            waiting["timestamp"] = time.time()
            return waiting, None
        return state, jpeg

    @app.route("/")
    def index():
        return send_from_directory(_DASHBOARD_DIR, "index.html")

    @app.route("/api/mode", methods=["GET"])
    def get_mode():
        with state_lock:
            return jsonify({
                "mock": mode["mock"] or mode["forced"],
                "toggleable": not mode["forced"],
            })

    @app.route("/api/mode", methods=["POST"])
    def set_mode():
        if mode["forced"]:
            return jsonify({
                "error": "No real engine attached — this server is running "
                         "in standalone demo mode only."
            }), 400
        body = request.get_json(silent=True) or {}
        with state_lock:
            mode["mock"] = bool(body.get("mock", not mode["mock"]))
            result = mode["mock"]
        return jsonify({"mock": result})

    @app.route("/stream")
    def stream():
        """Server-Sent Events: one JSON snapshot per push, ~8x/sec.
        One-directional (engine -> browser) is all this dashboard needs,
        so SSE was chosen over WebSocket — it needs no extra dependency,
        runs over plain HTTP, and the browser's built-in EventSource
        reconnects automatically if the connection drops."""
        def gen():
            period = 1.0 / _STREAM_HZ
            while True:
                state, _ = _current_payload()
                yield f"data: {json.dumps(state)}\n\n"
                time.sleep(period)
        return Response(gen(), mimetype="text/event-stream",
                         headers={"Cache-Control": "no-cache"})

    @app.route("/api/break", methods=["POST"])
    def set_break():
        """
        Invigilator override: mark a student as away on an authorised
        break, or back from one.

        Why this exists: in a real exam hall a human already KNOWS when
        a student has been given permission to leave. Stating that
        explicitly is more reliable than any timeout or recognition the
        system could infer on its own. While on break the slot is held
        indefinitely (nobody can take their seat or inherit their
        identity), monitoring is paused so an empty chair cannot
        pollute their calibration or accrue suspicion, and their record
        is preserved untouched for when they return.

        Body: {"slot": 0-3, "on_break": true|false}
        """
        if snapshot is None:
            return jsonify({"error": "No engine attached — demo mode has no "
                                      "real students to mark."}), 400
        if identity_ref.get("tracker") is None:
            return jsonify({"error": "Identity tracker unavailable."}), 503

        body = request.get_json(silent=True) or {}
        slot = body.get("slot")
        if not isinstance(slot, int):
            return jsonify({"error": "'slot' must be an integer."}), 400
        tracker = identity_ref["tracker"]
        if slot not in tracker.slots:
            return jsonify({"error": f"Unknown slot {slot}."}), 400

        on_break = bool(body.get("on_break", not tracker.is_on_break(slot)))
        changed = tracker.set_break(slot, on_break, time.time())
        if changed and on_break_cb.get("fn") is not None:
            try:
                on_break_cb["fn"](slot, on_break)
            except Exception as e:
                print(f"[WARN] break callback failed: {e}")
        return jsonify({
            "slot": slot,
            "on_break": tracker.is_on_break(slot),
            "changed": changed,
        })

    @app.route("/api/roster/students", methods=["GET"])
    def list_roster():
        """Who the invigilator has registered for this exam."""
        r = roster_ref.get("roster")
        if r is None:
            return jsonify({"enabled": False, "students": {}})
        return jsonify(r.to_payload())

    @app.route("/api/roster/students", methods=["POST"])
    def change_roster():
        """
        Register a student to a seat, remove one, or lock the roster in
        for the exam.

        Registering gives reports real names instead of seat positions.
        Locking additionally caps face tracking to the number of people
        actually present, which is the only reliable defence against a
        stable background object being detected as a student.

        Body: {"action": "register"|"unregister"|"lock"|"unlock",
               "slot": 0-3, "name": str, "roll_no": str}
        """
        if roster_cb.get("fn") is None:
            return jsonify({"error": "No engine attached - demo mode has no "
                                      "real students to register."}), 400
        body = request.get_json(silent=True) or {}
        action = body.get("action")
        if action not in ("register", "unregister", "lock", "unlock"):
            return jsonify({"error": "'action' must be register, unregister, "
                                      "lock or unlock."}), 400
        ok, result = roster_cb["fn"](action, body)
        if not ok:
            return jsonify({"error": result}), 400
        return jsonify(result)

    @app.route("/api/report", methods=["POST"])
    def generate_report():
        """
        Build a PDF session report from everything observed so far,
        without ending the session.

        This is the artefact that outlives the session — the thing an
        invigilator can actually hand to somebody. It runs on this
        request thread rather than the capture loop, because building a
        PDF takes far longer than one video frame and monitoring must
        not stutter because a button was pressed.
        """
        if report_cb.get("fn") is None:
            return jsonify({"error": "No engine attached — demo mode has no "
                                      "session to report on."}), 400
        ok, result = report_cb["fn"]()
        if not ok:
            return jsonify({"error": result}), 500
        return jsonify({"report": result, "url": "/reports/" + result})

    @app.route("/reports/<path:filename>")
    def report_file(filename):
        """Serves generated PDF reports for download from the dashboard."""
        return send_from_directory(_REPORTS_DIR, filename)

    @app.route("/api/roster", methods=["POST"])
    def set_roster():
        """
        Invigilator states how many students are actually in the room.

        Real logs showed background objects being detected as faces for
        up to 27 seconds at a time — long enough that no automatic
        persistence test can tell them from a real student. Capping the
        roster removes them regardless: the system keeps only the
        largest N faces, and a student at the desk is always nearer, and
        therefore bigger, than something in the background.

        Body: {"count": 1-4} or {"count": null} for automatic.
        """
        tracker = identity_ref.get("tracker")
        if tracker is None:
            return jsonify({"error": "No engine attached — demo mode has no "
                                      "real students to count."}), 400
        body = request.get_json(silent=True) or {}
        if "count" not in body:
            return jsonify({"error": "'count' is required (1-4, or null for auto)."}), 400
        count = body.get("count")
        if not tracker.set_expected_students(count):
            return jsonify({"error": f"'count' must be null or 1-{tracker.max_students}."}), 400
        return jsonify({"expected_students": tracker.expected_students})

    @app.route("/api/identity/reviews", methods=["GET"])
    def list_identity_reviews():
        """
        Identity questions awaiting an invigilator's answer.

        These exist because face re-identification is not always sure,
        and "not sure" is not the same as "definitely a different
        person". Rather than silently deciding — which could either
        erase a real student's record or attach one student's behaviour
        to another — the system asks the person who actually knows who
        left the room.
        """
        q = reviews_ref.get("queue")
        if q is None:
            return jsonify({"reviews": [], "available": False})
        return jsonify({
            "reviews": q.open_questions(),
            "stats": q.stats(),
            "available": True,
        })

    @app.route("/api/identity/review", methods=["POST"])
    def resolve_identity_review():
        """
        Answer an identity question.

        Body: {"review_id": <int>, "confirm": true|false}

        confirm=true  → the archived record of who this student was
                        before they left is merged back into their live
                        record, so the session ends with one continuous
                        person rather than two fragments.
        confirm=false → nothing changes; they remain a new student,
                        exactly as they already were while the question
                        was open.
        """
        if reviews_ref.get("queue") is None or review_cb.get("fn") is None:
            return jsonify({"error": "Identity review unavailable — no engine "
                                      "attached (demo mode has no real students)."}), 400

        body = request.get_json(silent=True) or {}
        rid = body.get("review_id")
        if not isinstance(rid, int):
            return jsonify({"error": "'review_id' must be an integer."}), 400
        if "confirm" not in body:
            return jsonify({"error": "'confirm' (true/false) is required — this "
                                      "decision is never assumed."}), 400
        confirmed = bool(body.get("confirm"))

        try:
            ok, payload = review_cb["fn"](rid, confirmed)
        except Exception as e:
            print(f"[WARN] identity review callback failed: {e}")
            return jsonify({"error": f"Review could not be applied: {e}"}), 500

        if not ok:
            return jsonify({"error": payload}), 400
        return jsonify(payload)

    @app.route("/api/replay/sessions")
    def replay_sessions():
        """
        Recorded sessions available for Session Replay (Reports page):
        every profiler log in diagnostics/. Works in demo mode too — a
        replay needs no camera and no running engine, only the log.
        """
        return jsonify({"sessions": session_replay.list_sessions(_DIAGNOSTICS_DIR)})

    @app.route("/api/replay/<session_id>")
    def replay_session(session_id):
        """
        Down-sampled timeline of one recorded session (see
        session_replay.py). Built from the log on first request and cached,
        so a long session takes a moment once and opens instantly after.
        """
        try:
            text = session_replay.build_timeline(_DIAGNOSTICS_DIR, session_id)
        except ValueError:
            return jsonify({"error": "Unknown session."}), 400
        except FileNotFoundError:
            return jsonify({"error": "That session's log no longer exists."}), 404
        return Response(text, mimetype="application/json")

    @app.route("/api/validation")
    def validation_overview():
        """
        Validation page (Sep 2026): recorded clips, the suggested set,
        the latest replay batch compared with each clip's label, history,
        and any check running now. See validation.py. Read-only.
        """
        return jsonify(validation.overview(_CLIPS_DIR, _VIDEO_RESULTS_DIR))

    @app.route("/api/validation/run", methods=["POST"])
    def validation_run():
        """
        Replay every clip through the engine (video_batch.py) in the
        background. Refused while a live engine is attached: a batch runs
        several engines at full speed for minutes, and live monitoring
        must never be slowed down by a test.
        """
        if snapshot is not None:
            return jsonify({"error": "Stop the live session first: the check replays every "
                                     "clip at full speed and would slow live monitoring. "
                                     "Run it with `python dashboard_server.py` instead."}), 409
        ok, result = validation.start_batch(_CLIPS_DIR)
        if not ok:
            return jsonify({"error": result}), 400
        return jsonify({"job": result})

    @app.route("/api/validation/whatif", methods=["POST"])
    def validation_whatif():
        """
        Re-score the clips of one batch with one setting changed
        (whatif.py, always in a separate process so no Config value in this
        server or a live engine is ever touched).
        Body: {"batch": "batch_...", "setting": "K_SIGMA", "values": [2.0, 2.3]}
        """
        body = request.get_json(silent=True) or {}
        ok, result = validation.run_whatif(_VIDEO_RESULTS_DIR, str(body.get("batch", "")),
                                           str(body.get("setting", "")), body.get("values") or [])
        if not ok:
            return jsonify({"error": result}), 400
        return jsonify(result)

    @app.route("/evidence/<path:filename>")
    def evidence_file(filename):
        """
        Serves saved evidence images/JSON written by
        evidence_capture.EvidenceCapture (path is the "image"/"json"
        value already included in each student's "latest_evidence"
        payload from the /stream endpoint, e.g. "S2/event_001.jpg").
        Read-only static file serving — added for the evidence-capture
        feature, everything else in this file is unchanged.
        """
        return send_from_directory(_EVIDENCE_DIR, filename)

    @app.route("/video_feed")
    def video_feed():
        """MJPEG stream of the SAME fully-drawn frame the OpenCV window
        shows (or the labeled demo placeholder in mock mode). This route
        never opens a camera — it only reads whatever the CV loop (or the
        mock engine) already produced."""
        def gen():
            period = 1.0 / _VIDEO_HZ
            while True:
                _, jpeg = _current_payload()
                if jpeg is not None:
                    yield (b"--frame\r\nContent-Type: image/jpeg\r\n\r\n"
                           + jpeg + b"\r\n")
                time.sleep(period)
        return Response(gen(),
                         mimetype="multipart/x-mixed-replace; boundary=frame")

    return app


if __name__ == "__main__":
    # Standalone run: no camera, no mediapipe import — pure UI development
    # in mock mode. See "MOCK / DEMO mode" in the project brief.
    print("[INFO] Dashboard server — STANDALONE DEMO MODE (no camera opened)")
    host = dashboard_auth.safe_host(Config.DASHBOARD_HOST, Config.DASHBOARD_LOGIN_ENABLED)
    dashboard_auth.print_urls(host, Config.DASHBOARD_PORT)
    demo_app = create_app(snapshot=None)
    demo_app.run(host=host, port=Config.DASHBOARD_PORT, threaded=True,
                 use_reloader=False, debug=False)

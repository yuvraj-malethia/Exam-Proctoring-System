/**
 * session_replay.js — SESSION REPLAY (Review page; was "Reports" before the Sep 2026 redesign).
 *
 * WHY THIS EXISTS
 * ───────────────────────────────────────────────────────────────────
 * Every live session writes a black-box log of each student's head
 * angle, baseline, spread, score and verdict (frame_profiler.py). That
 * log is a recording of the exam with no video stored. This plays one
 * back, scrubbable, through the SAME 3D room the Live Monitor uses
 * (digital_twin.js), with verdict changes and evidence snapshots marked
 * on the timeline — so a reviewer can jump to the moment something was
 * flagged and see where heads were actually turned.
 *
 * RULES
 *  - ZERO INTELLIGENCE. It replays what the engine logged at the time.
 *    The only derived fields are the ones the room needs to draw, built
 *    the way the engine defines them from logged values: "away" is
 *    |logged z| > K_SIGMA (CalibrationEngine.is_away), the threshold is
 *    K_SIGMA × logged spread, and the level uses SuspicionFusion's own
 *    thresholds (sent by the server, never hardcoded here).
 *  - HONEST ABOUT GAPS. Whatever an older log did not record (pitch, seat
 *    positions, cross-student findings) is stated under the timeline, not
 *    papered over.
 *  - NEVER TOUCHES THE LIVE PANEL. Its own room instance, its own data;
 *    no 3D work at all until a session is opened.
 */
(function () {
  "use strict";

  const root = document.querySelector("[data-replay-root]");
  if (!root || !window.DigitalTwin) return;

  const SPEEDS = [1, 4, 16, 60];
  const TICK_MS = 125;   // the live stream's 8 Hz
  const num = function (v) { return typeof v === "number" && Number.isFinite(v); };

  const twin = window.DigitalTwin.create(root, {
    instance: "replay",
    eyebrow: "Session replay",
    title: "Watch a recorded session again",
    subtitle: "Played back from the session's log, with no video stored: every student's head as it was measured at the time. Jump to any marked moment on the timeline.",
    emptyMessage: "Nobody was in view at this point of the session.",
  });
  twin.refs.message.textContent = "Choose a recorded session below.";

  const PLAY_ICON = '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M4 2.5v11l9-5.5z"/></svg>';
  const PAUSE_ICON = '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M3.5 2.5h3v11h-3zM9.5 2.5h3v11h-3z"/></svg>';
  const bar = document.createElement("div");
  bar.className = "replay-bar";
  bar.innerHTML =
    '<div class="replay-row">' +
      '<label class="replay-pick"><span class="eyebrow">Session</span><select class="field" data-r="session"><option value="">Loading…</option></select></label>' +
      '<button type="button" class="btn btn-primary" data-r="open">Open</button>' +
      '<span class="replay-status" data-r="status"></span>' +
    '</div>' +
    '<div class="replay-row" data-r="transport" hidden>' +
      '<button type="button" class="btn replay-play" data-r="play" aria-label="Play" title="Play (Space)">' + PLAY_ICON + '</button>' +
      '<div class="seg" role="group" aria-label="Playback speed">' +
        SPEEDS.map(function (s) {
          return '<button type="button" class="seg-btn' + (s === 16 ? " is-active" : "") + '" data-speed="' + s + '">' + s + '×</button>';
        }).join("") +
      '</div>' +
      '<span class="replay-time" data-r="time">00:00 / 00:00</span>' +
    '</div>' +
    '<div class="replay-track" data-r="track" hidden>' +
      '<div class="replay-marks" data-r="marks"></div>' +
      '<input type="range" class="replay-scrub" data-r="scrub" min="0" max="0" step="1" value="0" aria-label="Position in the session">' +
    '</div>' +
    '<div class="replay-lower">' +
      '<div class="replay-events" data-r="events" hidden></div>' +
      '<div class="replay-notes" data-r="notes" hidden></div>' +
    '</div>';
  twin.refs.footer.appendChild(bar);
  const R = {};
  bar.querySelectorAll("[data-r]").forEach(function (el) { R[el.dataset.r] = el; });

  const P = { data: null, idx: 0, pos: 0, playing: false, speed: 16, timer: 0, lastWall: 0, eventsSig: null };

  function setText(el, t) { if (el && el.textContent !== t) el.textContent = t; }
  function clock(sec) {
    if (!num(sec) || sec < 0) sec = 0;
    const s = Math.floor(sec), h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), r = s % 60;
    const mm = String(m).padStart(2, "0") + ":" + String(r).padStart(2, "0");
    return h ? h + ":" + mm : mm;
  }
  function duration() { return P.data ? P.data.t[P.data.t.length - 1] || 0 : 0; }

  // ── Rebuild the payload the room expects, from one logged sample ──
  function levelFor(score) {
    const levels = P.data.meta.config.levels;
    for (let k = 0; k < levels.length; k++) {
      if (score >= levels[k][0]) return { label: levels[k][1], key: levels[k][2] };
    }
    return { label: "NORMAL", key: "normal" };
  }
  function stateAt(i) {
    const d = P.data, m = d.meta, cfg = m.config;
    const students = {}, identity = {};
    let hasIdentity = false;
    m.labels.forEach(function (label, slot) {
      const c = d.students[label];
      if (!c) return;
      const vis = !!c.vis[i], cal = !!c.cal[i], z = c.z[i], std = c.std[i];
      // Exactly CalibrationEngine.is_away(), applied to the logged z.
      const away = cal && num(z) && Math.abs(z) > cfg.k_sigma;
      const score = c.prob[i] || 0, level = levelFor(score);
      students[label] = {
        sid: slot, label: label, visible: vis,
        display_name: (m.names && m.names[label]) || label,
        color: m.colors[label],
        calibration: {
          complete: cal, baseline: c.base[i], initial_baseline: c.init[i], running_mean: c.base[i],
          threshold: cal && num(std) ? cfg.k_sigma * std : null,
        },
        head_pose: {
          filtered_yaw: c.yaw[i],
          state: cal ? (away ? "AWAY" : "FORWARD") : "CALIBRATING",
          direction: away ? (z > 0 ? "Right" : "Left") : "Forward",
        },
        suspicion: cal ? { score: score, level: level.label, level_key: level.key } : null,
        verdict: num(c.verdict[i]) ? { label: m.verdicts[c.verdict[i]] } : null,
        // The spread of pitch was never logged, so its threshold is unknown:
        // the pitch fan stays hidden rather than being guessed.
        pitch: m.pitch_recorded
          ? { enabled: true, recorded: true, calibrated: !!c.pcal[i], pitch: c.pitch[i],
              baseline: c.pbase[i], looking_down: !!c.down[i], down_sign: cfg.pitch_down_sign,
              down_threshold_deg: null }
          : { enabled: false, recorded: false },
      };
      const slotState = c.slot[i];
      const cx = c.cx ? c.cx[i] : null;
      if (num(slotState) || num(cx)) {
        hasIdentity = true;
        identity[slot] = { state: num(slotState) ? m.slot_states[slotState] : (vis ? "tracked" : "held"), cx: cx };
      }
    });
    return { students: students, identity: hasIdentity ? identity : null, correlations: [] };
  }

  // ── Playback ──────────────────────────────────────────────────────
  function indexAt(pos) {
    const t = P.data.t;
    let lo = 0, hi = t.length - 1;
    while (lo < hi) {
      const mid = (lo + hi + 1) >> 1;
      if (t[mid] <= pos) lo = mid; else hi = mid - 1;
    }
    return lo;
  }
  function show(i, fromScrub) {
    P.idx = i;
    if (!fromScrub) R.scrub.value = String(i);
    twin.update(stateAt(i));
    setText(R.time, clock(P.data.t[i]) + " / " + clock(duration()));
    renderEvents();
  }
  function seekTo(sec) {
    P.pos = Math.max(0, Math.min(sec, duration()));
    show(indexAt(P.pos));
  }
  function pageActive() {
    const page = root.closest(".page");
    return !page || page.classList.contains("is-active");
  }
  function tick() {
    const now = performance.now();
    const dt = (now - P.lastWall) / 1000;
    P.lastWall = now;
    if (!pageActive()) { pause(); return; }   // like a video: leaving the page pauses it
    P.pos = Math.min(P.pos + dt * P.speed, duration());
    const i = indexAt(P.pos);
    if (i !== P.idx) show(i);
    if (P.pos >= duration()) pause();
  }
  function play() {
    if (!P.data) return;
    if (P.pos >= duration()) seekTo(0);
    P.playing = true;
    P.lastWall = performance.now();
    setPlayButton();
    if (!P.timer) P.timer = setInterval(tick, TICK_MS);
  }
  function pause() {
    P.playing = false;
    setPlayButton();
    clearInterval(P.timer);
    P.timer = 0;
  }
  function setPlayButton() {
    R.play.innerHTML = P.playing ? PAUSE_ICON : PLAY_ICON;
    R.play.setAttribute("aria-label", P.playing ? "Pause" : "Play");
    R.play.title = (P.playing ? "Pause" : "Play") + " (Space)";
  }

  // ── Timeline marks and the recent-events list ─────────────────────
  function markClass(e) {
    if (e.kind === "verdict") return "is-v-" + String(e.text.split(": ")[1] || "").toLowerCase();
    return "is-" + e.kind;
  }
  function buildMarks() {
    R.marks.textContent = "";
    const dur = duration() || 1;
    P.data.events.forEach(function (e) {
      if (e.kind === "system") return;   // exam started / ended: the ends of the track
      const mk = document.createElement("button");
      mk.type = "button";
      mk.className = "replay-mark " + markClass(e);
      mk.style.left = (100 * Math.min(e.t / dur, 1)) + "%";
      mk.title = clock(e.t) + " · " + e.student + " · " + e.text;
      mk.dataset.t = String(e.t);
      R.marks.appendChild(mk);
    });
  }
  function renderEvents() {
    const now = P.data.t[P.idx], ev = P.data.events;
    let last = -1;
    while (last + 1 < ev.length && ev[last + 1].t <= now) last++;
    const sig = P.data.meta.id + ":" + last;
    if (sig === P.eventsSig) return;
    P.eventsSig = sig;
    R.events.hidden = false;
    R.events.textContent = "";
    const head = document.createElement("div");
    head.className = "replay-events-head eyebrow";
    head.textContent = "Recorded up to this point";
    R.events.appendChild(head);
    const recent = ev.slice(Math.max(0, last - 3), last + 1).reverse();
    if (!recent.length) {
      const none = document.createElement("div");
      none.className = "replay-event";
      none.textContent = "Nothing recorded yet.";
      R.events.appendChild(none);
    }
    recent.forEach(function (e) {
      const row = document.createElement("div");
      row.className = "replay-event";
      const time = document.createElement("b");
      time.textContent = clock(e.t);
      const text = document.createElement("span");
      text.textContent = (e.student === "SYSTEM" ? "" : e.student + " · ") + e.text;
      row.append(time, text);
      R.events.appendChild(row);
    });
  }

  // ── Loading ───────────────────────────────────────────────────────
  function loadSessions() {
    fetch("/api/replay/sessions")
      .then(function (r) { return r.json(); })
      .then(function (res) {
        const sessions = res.sessions || [];
        R.session.textContent = "";
        if (!sessions.length) {
          const o = document.createElement("option");
          o.value = "";
          o.textContent = "No recorded sessions yet";
          R.session.appendChild(o);
          R.open.disabled = true;
          setText(R.status, "Sessions are recorded automatically whenever the engine runs.");
          return;
        }
        sessions.forEach(function (s) {
          const o = document.createElement("option");
          o.value = s.id;
          o.textContent = s.started + " · " + clock(s.duration_sec) + " · " + s.size_mb + " MB";
          R.session.appendChild(o);
        });
        R.open.disabled = false;
      })
      .catch(function () { setText(R.status, "Could not list recorded sessions."); });
  }
  function openSession() {
    const id = R.session.value;
    if (!id) return;
    pause();
    R.open.disabled = true;
    setText(R.status, "Preparing replay… a long session can take a few seconds the first time.");
    fetch("/api/replay/" + encodeURIComponent(id))
      .then(function (r) { return r.json().then(function (body) { return { ok: r.ok, body: body }; }); })
      .then(function (res) {
        R.open.disabled = false;
        if (!res.ok) { setText(R.status, res.body.error || "Could not open that session."); return; }
        const d = res.body;
        if (!d.t || !d.t.length) { setText(R.status, "That log contains no recorded frames."); return; }
        P.data = d;
        P.eventsSig = null;
        const m = d.meta;
        setText(R.status, "Recorded " + m.started + " · " + clock(m.duration_sec) + " · " +
          m.students.length + " student" + (m.students.length === 1 ? "" : "s") + " seen");
        R.scrub.max = String(d.t.length - 1);
        R.transport.hidden = false;
        R.track.hidden = false;
        R.notes.hidden = !m.notes.length;
        R.notes.textContent = "";
        m.notes.forEach(function (n) {
          const line = document.createElement("div");
          line.textContent = n;
          R.notes.appendChild(line);
        });
        buildMarks();
        seekTo(0);
      })
      .catch(function () {
        R.open.disabled = false;
        setText(R.status, "Could not open that session.");
      });
  }

  R.open.addEventListener("click", openSession);
  R.play.addEventListener("click", function () { P.playing ? pause() : play(); });
  bar.querySelectorAll("[data-speed]").forEach(function (b) {
    b.addEventListener("click", function () {
      P.speed = Number(b.dataset.speed);
      bar.querySelectorAll("[data-speed]").forEach(function (o) { o.classList.toggle("is-active", o === b); });
    });
  });
  R.scrub.addEventListener("input", function () {
    const i = Number(R.scrub.value);
    P.pos = P.data.t[i];
    show(i, true);
  });
  R.marks.addEventListener("click", function (e) {
    const mk = e.target.closest(".replay-mark");
    if (mk) seekTo(Number(mk.dataset.t));
  });
  // Returning to the Review page redraws the paused frame (dashboard.js
  // announces every page change).
  document.addEventListener("examonitor:page", function () {
    setTimeout(function () { if (P.data && pageActive()) twin.update(stateAt(P.idx)); }, 60);
  });
  // Space plays / pauses; arrows step a paused replay by one sample (a
  // quarter-second of the session), Shift + arrow by ten seconds.
  document.addEventListener("keydown", function (e) {
    if (!P.data || !pageActive() || e.ctrlKey || e.metaKey || e.altKey) return;
    if (e.target.closest("input:not([type=range]), textarea, select, [contenteditable]")) return;
    if (e.key === " " && !e.target.closest("button")) {
      e.preventDefault();
      P.playing ? pause() : play();
    } else if ((e.key === "ArrowRight" || e.key === "ArrowLeft") && !e.target.closest("input[type=range]")) {
      e.preventDefault();
      pause();
      const dir = e.key === "ArrowRight" ? 1 : -1;
      if (e.shiftKey) seekTo(P.data.t[P.idx] + dir * 10);
      else { const i = Math.max(0, Math.min(P.data.t.length - 1, P.idx + dir)); P.pos = P.data.t[i]; show(i); }
    }
  });

  loadSessions();
  window.SessionReplay = {
    // Inspection only (tests / console). Changes nothing.
    debug: function () {
      return { loaded: P.data ? P.data.meta.id : null, idx: P.idx, pos: P.pos, playing: P.playing,
               speed: P.speed, room: twin.debug() };
    },
  };
})();

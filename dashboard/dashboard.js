/**
 * dashboard.js — PRESENTATION ONLY.  (rewritten Sep 2026, "Paper & Ink")
 *
 * WHY IT WAS REWRITTEN
 * ───────────────────────────────────────────────────────────────────
 * As features piled up, the Live page became a stack of equally loud
 * panels and it got hard to see what was actually happening. The new
 * page answers one question first — "does anyone need me right now?" —
 * in a sentence, then shows the seat plan in the order students really
 * sit, then the detail. Colour now means "needs attention" and nothing
 * else (see the rules at the top of styles.css).
 *
 * RULES THIS FILE KEEPS (unchanged from the original)
 * ───────────────────────────────────────────────────────────────────
 *  - ZERO INTELLIGENCE. Every number, level, verdict, threshold and
 *    pattern comes verbatim from the engine's payload. This file only
 *    arranges, formats and explains. The one thing it composes is the
 *    headline sentence, from verdicts and levels the engine already gave.
 *  - BUILD ONCE, UPDATE IN PLACE. The stream pushes ~8 updates a second.
 *    Structure is built when its SHAPE changes (a student appears,
 *    calibration completes); every tick only touches text, classes and
 *    styles. Rebuilding on every tick is the bug that once made the
 *    identity-confirmation buttons impossible to click — it has recurred
 *    more than once, so every panel with a button here is signature-
 *    checked before it is rebuilt.
 *  - THE SYSTEM FLAGS, A HUMAN DECIDES. Wording under-claims throughout:
 *    "needs a look", "consistent with", never "cheating".
 *
 * TALKS TO: /stream (SSE, one connection), /video_feed (MJPEG, only while
 * the Live page is open), the /api/* routes in dashboard_server.py. Every
 * POST carries X-Requested-With: exam-monitor (see dashboard_server.py).
 */
(function () {
  "use strict";

  // ── Constants ──────────────────────────────────────────────────
  const SEATS = ["S1", "S2", "S3", "S4"];
  const LEVEL_WORDS = {
    normal: "NORMAL", low: "LOW", mild: "MILD", moderate: "MODERATE",
    high: "HIGH RISK", off: "—", calib: "CALIBRATING",
  };
  const SIGNALS = [
    { key: "position",  label: "How far the head is turned" },
    { key: "velocity",  label: "How fast it turned" },
    { key: "frequency", label: "How often they looked away" },
    { key: "duration",  label: "Share of time spent away" },
    { key: "session",   label: "Session memory" },
  ];
  // The same five signals, named as the code names them. The Students
  // page says what they mean in words; the Developer page is where
  // somebody asks "how is this number actually computed?" and deserves
  // the variable, the Config constant and the weight, not a paraphrase.
  const TECH_SIGNALS = [
    { key: "position",  v: "pos_score",  c: "W_POSITION",
      note: "angular deviation from baseline, as a z-score, capped at K_SIGMA x 2.5" },
    { key: "velocity",  v: "vel_score",  c: "W_VELOCITY",
      note: "angular velocity, normalised (KinematicAnalyzer.velocity_norm)" },
    { key: "frequency", v: "freq_score", c: "W_FREQUENCY",
      note: "events inside TEMPORAL_WINDOW_SEC" },
    { key: "duration",  v: "dur_score",  c: "W_DURATION",
      note: "away time divided by session time" },
    { key: "session",   v: "sess_score", c: "W_SESSION",
      note: "decayed memory of past events (SESSION_MEMORY_HALFLIFE_SEC)" },
  ];
  const PIPELINE = [
    "Camera", "Raw yaw", "Kalman", "Filtered yaw", "Baseline / z",
    "Forward / away", "Event", "Temporal", "Session memory",
    "Fusion", "Suspicion", "Verdict",
  ];
  const PAGES = {
    live:        "Live",
    setup:       "Setup",
    review:      "Review",
    students:    "Students",
    calibration: "Calibration",
    validation:  "Validation",
    developer:   "Developer",
  };
  const OLD_PAGE_NAMES = { overview: "live", reports: "review" };
  const STALE_MS = 3000;
  const CHART_EVERY_MS = 450;     // charts redraw at ~2 Hz, not on every tick
  const APP_TITLE = "Exam Monitor · CPG-127";
  const CSRF = { "X-Requested-With": "exam-monitor" };

  // ── Formatting ─────────────────────────────────────────────────
  const isNum = function (v) { return typeof v === "number" && Number.isFinite(v); };
  function fmtNum(v, d, suffix) { return isNum(v) ? v.toFixed(d === undefined ? 1 : d) + (suffix || "") : "—"; }
  function fmtInt(v, suffix) { return isNum(v) ? Math.round(v) + (suffix || "") : "—"; }
  function fmtPct(v) { return fmtNum(v, 0, "%"); }
  function fmtSecs(v) { return fmtNum(v, 1, " s"); }
  function fmtDeg(v) { return fmtNum(v, 1, "°"); }
  function fmtClock(sec) {
    if (!isNum(sec) || sec < 0) return "00:00";
    const s = Math.floor(sec), h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), r = s % 60;
    const mm = String(m).padStart(2, "0") + ":" + String(r).padStart(2, "0");
    return h ? String(h).padStart(2, "0") + ":" + mm : mm;
  }
  function fmtDur(sec) {
    if (!isNum(sec)) return "";
    const s = Math.floor(sec);
    return s < 60 ? s + " s" : Math.floor(s / 60) + " min " + String(s % 60).padStart(2, "0") + " s";
  }
  function esc(s) {
    return String(s == null ? "" : s).replace(/&/g, "&amp;").replace(/</g, "&lt;")
      .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }
  function cap(s) { s = String(s || ""); return s.charAt(0).toUpperCase() + s.slice(1); }
  function listWords(names) {
    if (names.length <= 1) return names.join("");
    return names.slice(0, -1).join(", ") + " and " + names[names.length - 1];
  }
  const NUMBER_WORDS = ["no", "one", "two", "three", "four", "five", "six"];
  function countWord(n) { return NUMBER_WORDS[n] || String(n); }

  // ── DOM ────────────────────────────────────────────────────────
  const $ = function (id) { return document.getElementById(id); };
  function setText(node, t) { if (node && node.textContent !== t) node.textContent = t; }
  function setHTML(node, h) { if (node && node._html !== h) { node.innerHTML = h; node._html = h; } }
  function setCls(node, c) { if (node && node.className !== c) node.className = c; }
  function setData(node, k, v) { if (node && node.dataset[k] !== v) node.dataset[k] = v; }
  function setStyle(node, prop, v) { if (node && node.style[prop] !== v) node.style[prop] = v; }
  function setAttr(node, k, v) { if (node && node.getAttribute(k) !== v) node.setAttribute(k, v); }
  function toggle(node, cls, on) { if (node && node.classList.contains(cls) !== !!on) node.classList.toggle(cls, !!on); }
  function show(node, on) { if (node && node.hidden === !!on) node.hidden = !on; }
  function refsOf(root) {
    const r = {};
    root.querySelectorAll("[data-ref]").forEach(function (n) { r[n.dataset.ref] = n; });
    return r;
  }
  function cssVar(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }

  // ── App state ──────────────────────────────────────────────────
  const App = {
    state: null, prev: null,
    page: "live",
    selected: null,          // student shown on Students / Calibration / Developer
    inspecting: null,        // seat open in the inspector
    conn: "connecting", connected: false, lastTickAt: 0, firstTickAt: 0,
    es: null, reconnectTimer: null,
    looks: [], serverEvents: [], prevEventsRaw: [], seq: 0,
    logFilter: "all",
    devLog: [],
    built: {}, refs: {}, shapes: {}, sigs: {}, roomHist: [], eyeHist: {},
    lastChartAt: 0,
    user: null, loginEnabled: true, modeToggleable: false,
  };

  // ── Server calls ───────────────────────────────────────────────
  function goSignIn() {
    const next = location.pathname + location.hash;
    location.href = "/login?next=" + encodeURIComponent(next);
  }
  function api(url, opts) {
    opts = opts || {};
    const hasBody = opts.body !== undefined;
    return fetch(url, {
      method: opts.method || (hasBody ? "POST" : "GET"),
      headers: Object.assign({}, CSRF, hasBody ? { "Content-Type": "application/json" } : {}),
      body: hasBody ? JSON.stringify(opts.body) : undefined,
      credentials: "same-origin",
    }).then(function (res) {
      return res.json().catch(function () { return {}; }).then(function (j) {
        if (res.status === 401 && j && j.signin) goSignIn();
        return { ok: res.ok, status: res.status, j: j || {} };
      });
    }).catch(function () {
      // Server unreachable: callers see an ordinary failure; the banner
      // already tells the invigilator the connection is gone.
      return { ok: false, status: 0, j: { error: "Could not reach the exam monitor." } };
    });
  }
  window.ExamMonitor = { api: api };

  // ── Payload readers (no intelligence — just where things live) ──
  function students(state) { return (state && state.students) || {}; }
  function nameOf(p) { return p ? (p.display_name || p.label) : ""; }
  function nameForLabel(label) {
    const p = students(App.state)[label];
    return p ? nameOf(p) : label;
  }
  function levelKeyOf(p) {
    if (!p || !p.visible) return "off";
    if (!p.calibration || !p.calibration.complete) return "calib";
    return (p.suspicion && p.suspicion.level_key) || "off";
  }
  function levelWordOf(p) {
    const k = levelKeyOf(p);
    if (k === "off") return p && !p.visible ? "NOT IN VIEW" : "NO DATA";
    return LEVEL_WORDS[k] || "—";
  }
  function verdictOf(p) { return p && p.verdict && p.verdict.label ? p.verdict.label : null; }
  function isCalibrated(p) { return !!(p && p.calibration && p.calibration.complete); }
  function slotInfo(state, label) {
    const id = state && state.identity;
    return id ? (id[SEATS.indexOf(label)] || id[String(SEATS.indexOf(label))] || null) : null;
  }
  // How much attention a seat needs, from the engine's own verdict and
  // level: "look" = the verdict that triggers evidence capture (ALERT /
  // FLAG) or HIGH RISK; "watch" = WATCH, or the levels the engine itself
  // counts as needing attention (serialize_full_state: not normal/low).
  function tierOf(p, info) {
    if (!p) return "empty";
    if (info && info.on_break) return "break";
    if (!p.visible) return "away";
    if (!isCalibrated(p)) return "calib";
    const v = verdictOf(p), lk = levelKeyOf(p);
    if (v === "ALERT" || v === "FLAG" || lk === "high") return "look";
    if (v === "WATCH" || lk === "moderate" || lk === "mild") return "watch";
    return "calm";
  }
  function levelsCfg(state) {
    const l = (state && state.config && state.config.levels) || {};
    return {
      low: isNum(l.low) ? l.low : 18, mild: isNum(l.mild) ? l.mild : 35,
      moderate: isNum(l.moderate) ? l.moderate : 50, high: isNum(l.high) ? l.high : 65,
    };
  }
  function riskKey(score, state) {
    const L = levelsCfg(state || App.state);
    if (score >= L.high) return "high";
    if (score >= L.moderate) return "moderate";
    if (score >= L.mild) return "mild";
    if (score >= L.low) return "low";
    return "normal";
  }
  // Seats that belong on the plan: anyone in view, holding a seat, on a
  // break — not the empty pipelines the engine always keeps for 4 seats.
  function seatOnPlan(state, label) {
    const p = students(state)[label];
    if (!p) return false;
    if (p.visible) return true;
    const info = slotInfo(state, label);
    if (info) return info.on_break || info.state === "held" || info.state === "tracked";
    return isCalibrated(p) || (p.calibration && p.calibration.samples > 0);
  }
  // Left-to-right as the (mirrored) camera sees them: identity cx, the
  // same order the 3D room uses. Unknown positions go last, by seat.
  function seatOrder(state) {
    const items = SEATS.filter(function (l) { return students(state)[l]; }).map(function (l, i) {
      const info = slotInfo(state, l);
      return { l: l, i: SEATS.indexOf(l), cx: info && isNum(info.cx) ? info.cx : null };
    });
    const known = items.filter(function (o) { return o.cx !== null; }).sort(function (a, b) { return a.cx - b.cx; });
    const unknown = items.filter(function (o) { return o.cx === null; }).sort(function (a, b) { return a.i - b.i; });
    return known.concat(unknown).map(function (o) { return o.l; });
  }
  // A seat's colour (the same hue as its box on the camera feed), toned
  // down to an ink so four lines on a chart don't look like neon signs.
  const inkCache = {};
  function seatInk(p) {
    const hex = (p && p.color) || "#888888";
    const night = document.documentElement.dataset.theme === "night";
    const key = hex + night;
    if (inkCache[key]) return inkCache[key];
    const m = /^#?([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(hex);
    if (!m) return (inkCache[key] = "var(--ink-3)");
    const r = parseInt(m[1], 16) / 255, g = parseInt(m[2], 16) / 255, b = parseInt(m[3], 16) / 255;
    const max = Math.max(r, g, b), min = Math.min(r, g, b);
    let h = 0;
    if (max !== min) {
      const d = max - min;
      h = max === r ? ((g - b) / d) % 6 : max === g ? (b - r) / d + 2 : (r - g) / d + 4;
      h = Math.round(h * 60);
      if (h < 0) h += 360;
    }
    return (inkCache[key] = "hsl(" + h + " " + (night ? 55 : 58) + "% " + (night ? 68 : 38) + "%)");
  }
  // Where the student is looking, drawn exactly the way the 3D room draws
  // it (digital_twin.js owns the mapping, so the dial and the room agree).
  function gazeOf(p) {
    if (window.DigitalTwin && window.DigitalTwin.gazeView) {
      try { return window.DigitalTwin.gazeView(p); } catch (e) { /* fall through */ }
    }
    return { idle: true };
  }
  function directionText(p) {
    const t = p.temporal || {}, hp = p.head_pose || {};
    if (p.pitch && p.pitch.looking_down) return "Looking down";
    if (t.currently_away || hp.state === "AWAY") {
      const dir = hp.direction && hp.direction !== "Forward" && hp.direction !== "N/A" ? hp.direction.toLowerCase() : "away";
      return "Looking " + dir;
    }
    return "Facing forward";
  }

  // ═══════════════════════════════════════════════════════════════
  // THEME
  // ═══════════════════════════════════════════════════════════════
  function setTheme(t) {
    document.documentElement.dataset.theme = t;
    try { localStorage.setItem("em-theme", t); } catch (e) {}
    App.sigs.lanes = null;       // lane swatches carry inline colours
    App.lastChartAt = 0;
    if (window.DigitalTwin && window.DigitalTwin.retheme) {
      try { window.DigitalTwin.retheme(); } catch (e) { console.warn("[theme] 3D room:", e); }
    }
    if (App.state) renderAll();
  }
  function toggleTheme() {
    setTheme(document.documentElement.dataset.theme === "night" ? "paper" : "night");
  }

  // ═══════════════════════════════════════════════════════════════
  // ACCOUNT MENU
  // ═══════════════════════════════════════════════════════════════
  function loadAccount() {
    api("/api/auth/me").then(function (out) {
      if (!out.ok) return;
      App.loginEnabled = !!out.j.enabled;
      App.user = out.j.user;
      setText($("account-initial"), App.user ? App.user.charAt(0).toUpperCase() : "·");
      setText($("account-name"), App.user || "Nobody");
      setText($("account-label"), App.loginEnabled ? "Signed in as" : "Sign-in is off");
      show($("signout-btn"), App.loginEnabled);
      const note = $("account-note");
      if (!App.loginEnabled) {
        note.textContent = "Anyone who can open this page can use it. Turn sign-in on with Config.DASHBOARD_LOGIN_ENABLED.";
        show(note, true);
      }
    });
  }
  function refreshMode() {
    api("/api/mode").then(function (out) {
      if (!out.ok) return;
      App.modeToggleable = !!out.j.toggleable;
      const b = $("mock-toggle");
      show(b, App.modeToggleable);
      setText(b, out.j.mock ? "Switch back to the real engine" : "Switch to demo data");
    });
  }
  function toggleMode() {
    api("/api/mode").then(function (cur) {
      if (!cur.ok) return null;
      return api("/api/mode", { body: { mock: !cur.j.mock } });
    }).then(refreshMode);
  }
  function setMenu(open) {
    const m = $("account-menu"), b = $("account-btn");
    show(m, open);
    b.setAttribute("aria-expanded", open ? "true" : "false");
  }
  function signOut() {
    api("/api/auth/logout", { method: "POST" }).then(function () { location.href = "/login"; });
  }

  // ═══════════════════════════════════════════════════════════════
  // CONNECTION
  // ═══════════════════════════════════════════════════════════════
  function connectStream() {
    if (App.es) { try { App.es.close(); } catch (e) {} }
    setConn("connecting");
    App.es = new EventSource("/stream");
    App.es.onmessage = function (evt) {
      let data;
      try { data = JSON.parse(evt.data); } catch (e) { return; }
      App.prev = App.state;
      App.state = data;
      App.lastTickAt = performance.now();
      if (!App.firstTickAt) App.firstTickAt = App.lastTickAt;
      App.connected = true;
      ingest(data, App.prev);
      renderAll();
      setConn(connFromPayload(data));
    };
    App.es.onerror = function () {
      App.connected = false;
      // A signed-out session looks like a network error to EventSource;
      // ask the server which one it is.
      api("/api/auth/me").then(function (out) { if (out.status === 401) goSignIn(); });
      if (App.es && App.es.readyState === EventSource.CONNECTING) setConn("reconnecting");
      else { setConn("offline"); scheduleReconnect(); }
    };
  }
  function scheduleReconnect() {
    if (App.reconnectTimer) return;
    App.reconnectTimer = setTimeout(function () { App.reconnectTimer = null; connectStream(); }, 2000);
  }
  function connFromPayload(state) {
    const sys = state.system || {};
    if (sys.mock) return "demo";
    if (sys.replay) return "replay";      // a recorded clip is never shown as LIVE
    if (sys.engine_connected) return "live";
    return "connecting";
  }
  const CONN_TEXT = {
    connecting: "Connecting", reconnecting: "Reconnecting", live: "Live", demo: "Demo",
    replay: "Replay", stale: "Stale", offline: "Offline",
  };
  function setConn(state) {
    App.conn = state;
    const pill = $("conn-pill");
    setData(pill, "state", state);
    setText($("conn-pill-text"), CONN_TEXT[state] || state);
    renderBanner();
  }
  // Stale watchdog — independent of the stream, which is exactly what
  // stops arriving when something goes wrong.
  setInterval(function () {
    if (!App.firstTickAt) return;
    const ago = performance.now() - App.lastTickAt;
    if (ago > STALE_MS && App.connected && App.conn !== "stale") setConn("stale");
    if (App.conn === "stale" || App.conn === "offline" || App.conn === "reconnecting") renderBanner();
  }, 500);

  function renderBanner() {
    const b = $("banner");
    const sys = (App.state && App.state.system) || {};
    const dead = App.firstTickAt && (App.conn === "stale" || App.conn === "offline" || App.conn === "reconnecting");
    document.body.toggleAttribute("data-stale", !!dead);
    if (dead) {
      const ago = (performance.now() - App.lastTickAt) / 1000;
      setData(b, "kind", "stale");
      setText($("banner-stamp"), "No signal");
      setText($("banner-text"), "Live data stopped arriving " + fmtDur(ago) + " ago. Everything on screen is frozen at that moment; it is not the room as it is now.");
      show(b, true);
      return;
    }
    // A run that is failing frames outranks everything below: the
    // numbers on screen may be missing most of the session, and the one
    // thing worse than showing nothing is showing a confident empty
    // room. See frame_faults.py for the bug that taught this.
    const flt = sys.faults;
    if (flt && flt.degraded) {
      setData(b, "kind", "stale");
      setText($("banner-stamp"), "Degraded");
      setText($("banner-text"),
        Math.round(flt.fraction * 100) + "% of frames failed to process (" +
        flt.failed + " of " + flt.frames + "). What is shown may be missing most " +
        "of the session. See the Developer page for the cause.");
      show(b, true);
      return;
    }
    if (sys.replay) {
      const r = sys.replay;
      setData(b, "kind", "replay");
      setText($("banner-stamp"), "Recording");
      setText($("banner-text"), "Recorded clip " + r.clip + " (" + fmtClock(r.footage_sec) + " / " +
        fmtClock(r.footage_total_sec) + ") played through the engine. Not a live exam.");
      show(b, true);
      return;
    }
    if (sys.mock) {
      setData(b, "kind", "demo");
      setText($("banner-stamp"), "Specimen");
      setText($("banner-text"), "Demo data: four simulated students. Not a real exam session.");
      show(b, true);
      return;
    }
    show(b, false);
  }

  // ═══════════════════════════════════════════════════════════════
  // NAVIGATION — pages live in the URL hash, so a refresh keeps your
  // place and the browser's Back button works.
  // ═══════════════════════════════════════════════════════════════
  function pageFromHash() {
    let h = (location.hash || "").replace(/^#/, "");
    h = OLD_PAGE_NAMES[h] || h;
    return PAGES[h] ? h : "live";
  }
  function goToPage(page) {
    if (!PAGES[page]) page = "live";
    if (("#" + page) !== location.hash) history.replaceState(null, "", "#" + page);
    applyPage(page);
  }
  function applyPage(page) {
    const changed = page !== App.page || !App.built.pageApplied;
    App.page = page;
    App.built.pageApplied = true;
    document.querySelectorAll(".nav-item").forEach(function (a) {
      const on = a.dataset.page === page;
      toggle(a, "is-active", on);
      if (on) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
    });
    document.querySelectorAll("main .page").forEach(function (s) {
      toggle(s, "is-active", s.dataset.page === page);
    });
    if (page !== "live") closeInspector();
    setCameraStream(page === "live");
    if (changed) {
      document.dispatchEvent(new CustomEvent("examonitor:page", { detail: { page: page } }));
      App.lastChartAt = 0;
      window.scrollTo(0, 0);
      if (page === "validation") loadValidation();
    }
    if (App.state) renderAll();
  }

  // ═══════════════════════════════════════════════════════════════
  // INGEST — per-tick bookkeeping, before rendering
  // ═══════════════════════════════════════════════════════════════
  // A rolling history of how much of the room was away, kept in the
  // browser because the engine sends only the current frame's figure.
  // This is what the chart's second line is drawn from: an individual
  // score means little until you can see it against what everyone else
  // was doing at the same moment.
  function ingestRoom(state) {
    const r = (state.system && state.system.room) || null;
    if (!r) return;
    const now = performance.now();
    App.roomHist.push({ at: now, pct: (r.away_fraction || 0) * 100, shared: !!r.shared });
    const win = ((state.config && state.config.risk_timeline &&
                  state.config.risk_timeline.window_sec) || 300) * 1000;
    while (App.roomHist.length && now - App.roomHist[0].at > win) App.roomHist.shift();
  }

  // A rolling 20-second history of each student's eye position, kept in
  // the browser because the engine sends only the current frame. This is
  // what the eye strip under the head graph is drawn from. A reading of
  // null is stored too: "the iris could not be read" is information (head
  // down, blink, face too small), and must show as a gap in the line
  // rather than as the eyes sitting dead centre.
  function ingestEyes(state) {
    const now = performance.now() / 1000;
    SEATS.forEach(function (l) {
      const p = students(state)[l];
      if (!p) return;
      const h = App.eyeHist[l] || (App.eyeHist[l] = []);
      const e = p.eye;
      if (p.visible && e && isNum(e.offset)) {
        h.push({ t: now, v: e.offset - (isNum(e.baseline) ? e.baseline : 0), aside: !!e.aside });
      } else if (p.visible && h.length && h[h.length - 1].v !== null) {
        h.push({ t: now, v: null, aside: false });
      }
      while (h.length && now - h[0].t > 20) h.shift();
    });
  }

  function ingest(state, prev) {
    ingestRoom(state);
    ingestEyes(state);
    // Looks away: reconstructed from the engine's event count, as before.
    if (prev && prev.students) {
      SEATS.forEach(function (label) {
        const p = students(state)[label], pp = prev.students[label];
        if (!p || !pp || !p.temporal || !pp.temporal) return;
        if (p.temporal.total_events > pp.temporal.total_events && p.temporal.last_event) {
          const e = p.temporal.last_event;
          App.looks.push({
            seq: ++App.seq, at: Date.now(), label: label, time: e.time,
            duration: e.duration, direction: e.direction, z: e.z_score,
            level: riskKey(p.suspicion ? p.suspicion.score : 0, state),
          });
          if (App.looks.length > 160) App.looks.shift();
        }
      });
    }
    // Session events: the engine sends its last 20; keep a longer history
    // here by adding only the ones not seen before.
    const raw = state.events || [];
    const fresh = newTail(App.prevEventsRaw, raw);
    App.prevEventsRaw = raw;
    fresh.forEach(function (e) {
      App.serverEvents.push(Object.assign({ seq: ++App.seq }, e));
    });
    if (App.serverEvents.length > 300) App.serverEvents.splice(0, App.serverEvents.length - 300);

    // Developer page's rolling log.
    const sys = state.system || {};
    App.devLog.push({
      t: new Date().toLocaleTimeString("en-GB"), fps: sys.fps,
      msg: "vis=" + (sys.visible_students || 0) + " cal=" + (sys.calibrated_students || 0) + " " +
        SEATS.filter(function (l) { return students(state)[l]; }).map(function (l) {
          const p = students(state)[l];
          return l + ":" + (p.suspicion ? Math.round(p.suspicion.score) : "—");
        }).join(" "),
    });
    if (App.devLog.length > 100) App.devLog.shift();
  }
  function sameEvent(a, b) {
    return a.time === b.time && a.student === b.student && a.event === b.event && a.score === b.score;
  }
  // The entries of `next` that come after the part it shares with `prev`.
  function newTail(prev, next) {
    for (let n = Math.min(prev.length, next.length); n > 0; n--) {
      let ok = true;
      for (let k = 0; k < n && ok; k++) ok = sameEvent(prev[prev.length - n + k], next[k]);
      if (ok) return next.slice(n);
    }
    return next.slice();
  }

  // ═══════════════════════════════════════════════════════════════
  // RENDER
  // ═══════════════════════════════════════════════════════════════
  function renderAll() {
    const state = App.state;
    if (!state) return;
    if (state.config) window.CONFIG = state.config;
    renderMast(state);
    renderBanner();
    if (!App.selected || !students(state)[App.selected]) {
      const order = seatOrder(state).filter(function (l) { return seatOnPlan(state, l); });
      App.selected = order[0] || SEATS.find(function (l) { return students(state)[l]; }) || "S1";
    }
    switch (App.page) {
      case "live": renderLive(state); break;
      case "setup": renderSetup(state); break;
      case "review": renderReview(state); break;
      case "students": renderStudentsPage(state); break;
      case "calibration": renderCalibrationPage(state); break;
      case "developer": renderDeveloperPage(state); break;
    }
    if (App.inspecting) renderInspector(state);
  }

  function renderMast(state) {
    const sys = state.system || {};
    setText($("tb-visible"), fmtInt(sys.visible_students) + " / " + fmtInt(sys.max_students));
    setText($("tb-session"), fmtClock(sys.session_time));
    setText($("tb-fps"), fmtNum(sys.fps, 0));
    // Tab title and the Live tab's dot: someone needs a look, even when
    // you are on another page or another browser tab.
    let n = 0;
    SEATS.forEach(function (l) {
      const p = students(state)[l];
      if (p && tierOf(p, slotInfo(state, l)) === "look") n++;
    });
    const title = (n ? "(" + n + ") " : "") + APP_TITLE;
    if (document.title !== title) document.title = title;
    show($("nav-alert"), n > 0 && App.page !== "live");
  }

  // ═══════════════════════════════════════════════════════════════
  // LIVE
  // ═══════════════════════════════════════════════════════════════
  function renderLive(state) {
    renderHall(state);
    renderCorrelations(state);
    renderEvidence(state);
    renderBlind(state);
    renderIdentityReviews(state);
    renderSeats(state);
    renderCamera(state);
    if (window.DigitalTwin) {
      // Guarded: a WebGL or library failure must never stop the page.
      try { window.DigitalTwin.update(state); } catch (e) { console.warn("[3D room] update failed:", e); }
    }
    const now = performance.now();
    if (now - App.lastChartAt >= CHART_EVERY_MS) {
      App.lastChartAt = now;
      renderRiskChart(state);
      renderLanes(state);
    }
    renderLog(state);
  }

  // ── The hall line ────────────────────────────────────────────
  function renderHall(state) {
    const sys = state.system || {};
    const groups = { look: [], watch: [], calib: [], calm: [], away: [], "break": [] };
    seatOrder(state).forEach(function (l) {
      if (!seatOnPlan(state, l)) return;
      const p = students(state)[l];
      const t = tierOf(p, slotInfo(state, l));
      if (groups[t]) groups[t].push(p);
    });
    // Most serious first within each group.
    ["look", "watch"].forEach(function (k) {
      groups[k].sort(function (a, b) {
        const va = { FLAG: 2, ALERT: 1 }[verdictOf(a)] || 0, vb = { FLAG: 2, ALERT: 1 }[verdictOf(b)] || 0;
        return (vb - va) || ((b.suspicion ? b.suspicion.score : 0) - (a.suspicion ? a.suspicion.score : 0));
      });
    });
    const names = function (list, cls) { return list.map(function (p) { return '<span class="who' + (cls || "") + '">' + esc(nameOf(p)) + "</span>"; }); };
    const plain = function (list) { return list.map(function (p) { return esc(nameOf(p)); }); };
    const present = groups.look.length + groups.watch.length + groups.calib.length + groups.calm.length;

    let line, sub = [];
    if (!App.firstTickAt || (!sys.engine_connected && !sys.mock && !sys.replay)) {
      line = "Waiting for the engine…";
      sub.push("The page fills in as soon as the first frame arrives.");
    } else if (!present && !groups.away.length && !groups["break"].length) {
      line = "Nobody is in view yet.";
      sub.push("Students appear here as soon as the camera finds them.");
    } else if (groups.look.length) {
      line = listWords(names(groups.look)) + (groups.look.length === 1 ? " needs a look." : " need a look.");
      const p = groups.look[0];
      sub.push(reasonLine(p));
    } else if (groups.watch.length) {
      line = "Worth keeping an eye on " + listWords(names(groups.watch, " is-watch")) + ".";
      sub.push(reasonLine(groups.watch[0]));
    } else if (groups.calib.length && !groups.calm.length) {
      line = "Settling in: learning how " + (groups.calib.length === 1 ? esc(nameOf(groups.calib[0])) : countWord(groups.calib.length) + " students") + " normally sit.";
      sub.push("Ask students to look at their papers as usual for a few seconds.");
    } else if (groups.calm.length) {
      const n = groups.calm.length;
      line = n === 1 ? esc(nameOf(groups.calm[0])) + ' is <span class="ok">calm</span>.'
        : 'All ' + countWord(n) + ' students are <span class="ok">calm</span>.';
      sub.push("Nothing needs you right now.");
    } else {
      line = "No one is at their desk right now.";
    }
    const rest = [];
    if (groups.look.length && groups.watch.length) rest.push("keep an eye on " + listWords(plain(groups.watch)));
    if ((groups.look.length || groups.watch.length) && groups.calm.length) rest.push(listWords(plain(groups.calm)) + (groups.calm.length === 1 ? " is" : " are") + " calm");
    if (groups.calib.length && (groups.look.length || groups.watch.length || groups.calm.length)) rest.push(listWords(plain(groups.calib)) + " still calibrating");
    if (groups["break"].length) rest.push(listWords(plain(groups["break"])) + " on a break");
    if (groups.away.length) rest.push(listWords(plain(groups.away)) + " out of view");
    if (rest.length) sub.push(cap(rest.join("; ")) + ".");
    // A seat the engine cannot read belongs in the headline summary, not
    // only in its own callout: the one-sentence line is what an
    // invigilator glances at, and "nothing needs you right now" would be
    // a lie while a seat has gone unread for half a minute.
    const blindNow = (sys.unmeasured || []).filter(function (u) { return u.sec >= 3; });
    if (blindNow.length) {
      sub.push(listWords(blindNow.map(function (u) {
        const q = students(state)[u.label];
        return esc(q ? nameOf(q) : u.label);
      })) + (blindNow.length === 1 ? " cannot be seen" : " cannot be seen") +
        " (" + fmtDur(blindNow[0].sec) + ").");
    }

    setHTML($("hall-line"), line);
    setHTML($("hall-sub"), sub.join('<span class="sep">·</span>'));
    setText($("hall-eyebrow"), sys.replay ? "Replaying a recorded clip" : sys.mock ? "Demo · during the exam" : "During the exam");

    const tally = { OK: 0, WATCH: 0, ALERT: 0, FLAG: 0 };
    SEATS.forEach(function (l) {
      const p = students(state)[l];
      if (p && p.visible && isCalibrated(p) && tally[verdictOf(p)] !== undefined) tally[verdictOf(p)]++;
    });
    $("tally").querySelectorAll(".tally-cell").forEach(function (c) {
      const n = tally[c.dataset.v] || 0;
      setText(c.querySelector(".tally-n"), String(n));
      toggle(c, "has", n > 0);
    });
  }
  // "ALERT · 2 of 4 checks: repeated looks away, share of time away ·
  // Answer-checking pattern" — the reason, in the engine's own terms.
  function reasonLine(p) {
    const bits = [];
    const v = p.verdict;
    if (v && v.label && v.label !== "CALIBRATING") {
      const met = (v.checks || []).filter(function (c) { return c.met; }).map(function (c) { return c.label.toLowerCase(); });
      bits.push(esc(nameOf(p)) + " is at " + esc(v.label) + (met.length ? " (" + met.length + " of 4 checks: " + esc(met.join(", ")) + ")" : ""));
    } else if (p.suspicion) {
      bits.push(esc(nameOf(p)) + " is at " + esc(p.suspicion.level));
    }
    if (p.headline_pattern) bits.push(esc(p.headline_pattern.label));
    if (p.temporal && p.temporal.currently_away) bits.push(esc(directionText(p).toLowerCase()) + " now");
    return bits.join(" · ");
  }

  // ── Between students ─────────────────────────────────────────
  // ── Seats the engine cannot read ─────────────────────────────
  // Visibility, not suspicion. A student writing with their head low
  // disappears from a webcam just as surely as one looking into their
  // lap, so this says what happened and leaves the judgement alone.
  // Before this existed a seat could go unread for half a minute and
  // the page would show nothing at all.
  function renderBlind(state) {
    const panel = $("blind-panel"), list = $("blind-list");
    const sys = state.system || {};
    const items = sys.unmeasured || [];
    const sig = items.map(function (u) { return u.sid + ":" + Math.round(u.sec) + ":" + u.body_seen; }).join(",");
    if (sig === App.sigs.blind) return;
    App.sigs.blind = sig;
    show(panel, items.length > 0);
    if (!items.length) { list.innerHTML = ""; return; }
    list.innerHTML = items.map(function (u) {
      const who = (students(state)[u.label] ? nameOf(students(state)[u.label]) : u.label);
      let body = "";
      if (u.body_seen === true) body = "Someone is still sitting there.";
      else if (u.body_seen === false) body = "No one seen in that seat.";
      return '<div class="finding">' +
        '<p class="finding-title">' + esc(who) + '</p>' +
        '<span class="finding-conf is-neutral">' + fmtDur(u.sec) + '</span>' +
        '<p class="finding-desc">No readable face for ' + fmtDur(u.sec) +
        (body ? " " + esc(body) : "") + '</p>' +
      '</div>';
    }).join("") + '<p class="caveat">Not a suspicion. Writing with the head low does this too — it means the engine has nothing to measure, so nothing about these seats has been checked.</p>';
  }

  // The snapshots evidence_capture.py saved, shown while the session is
  // still running. Nothing new is computed here — the images already
  // existed, they were just buried in Review where nobody looks during
  // an exam. Signature-checked before rebuilding, like every panel that
  // updates from the live stream.
  function renderEvidence(state) {
    const panel = $("evidence-panel"), list = $("evidence-list");
    const shots = [];
    seatOrder(state).forEach(function (l) {
      const p = students(state)[l];
      const e = p && p.latest_evidence;
      if (e && e.image) shots.push({ p: p, e: e });
    });
    shots.sort(function (a, b) { return (b.e.timestamp || 0) - (a.e.timestamp || 0); });
    const top = shots.slice(0, 3);
    const sig = top.map(function (q) { return q.e.image; }).join(",");
    if (sig === App.sigs.shots) return;
    App.sigs.shots = sig;
    show(panel, top.length > 0);
    if (!top.length) { list.innerHTML = ""; return; }
    list.innerHTML = top.map(function (q) {
      const when = q.e.time || (q.e.timestamp ? fmtClockOfDay(q.e.timestamp) : "");
      return '<figure class="shot">' +
        '<a href="/evidence/' + encodeURI(q.e.image) + '" target="_blank" rel="noopener">' +
        '<img src="/evidence/' + encodeURI(q.e.image) + '" alt="Saved frame for ' +
        esc(nameOf(q.p)) + '" loading="lazy"></a>' +
        '<figcaption><b>' + esc(nameOf(q.p)) + '</b>' +
        (when ? '<span class="when">' + esc(String(when)) + '</span>' : '') +
        (isNum(q.e.suspicion) ? '<span class="num">' + fmtNum(q.e.suspicion, 0) + '%</span>' : '') +
        (q.e.reason ? '<span class="why">' + esc(q.e.reason) + '</span>' : '') +
        '</figcaption></figure>';
    }).join("") +
      '<p class="caveat">A saved frame is a prompt to look, not a finding. ' +
      'Open Review for the full record.</p>';
  }

  function fmtClockOfDay(ts) {
    try { return new Date(ts * 1000).toLocaleTimeString(); } catch (e) { return ""; }
  }

  function renderCorrelations(state) {
    const panel = $("correlation-panel"), list = $("correlation-list");
    const items = state.correlations || [];
    const sig = items.map(function (c) { return c.key + ":" + (c.slots || []).join("-") + ":" + c.confidence; }).join(",");
    if (sig === App.sigs.corr) return;
    App.sigs.corr = sig;
    show(panel, items.length > 0);
    if (!items.length) { list.innerHTML = ""; return; }
    list.innerHTML = items.map(function (c) {
      const pct = Math.round((c.confidence || 0) * 100);
      return '<div class="finding">' +
        '<p class="finding-title">' + esc(c.label) + ' · <span class="pair">' + esc((c.pair || []).join(" & ")) + '</span></p>' +
        '<span class="finding-conf' + (c.severity === "suspicious" ? "" : " is-neutral") + '">' + pct + '%</span>' +
        '<p class="finding-desc">' + esc(c.description || "") + '</p>' +
      '</div>';
    }).join("") + '<p class="caveat">' + esc(items[0].caveat || "Correlation is not proof. Requires human review.") + '</p>';
  }

  // ── Identity questions ───────────────────────────────────────
  // THE FLICKER BUG THIS GUARDS AGAINST (found in real testing): this
  // panel once rebuilt on every push, ~8×/s, destroying the Yes/No buttons
  // before a click could land. It is rebuilt only when the SET of open
  // questions changes.
  function renderIdentityReviews(state) {
    const reviews = state.identity_reviews || [];
    const sig = reviews.map(function (q) { return q.id; }).join(",");
    if (sig === App.sigs.reviews) return;
    const hadNew = reviews.some(function (q) { return (App.sigs.reviews || "").split(",").indexOf(String(q.id)) < 0; });
    App.sigs.reviews = sig;
    const panel = $("identity-review-panel");
    if (!reviews.length) {
      show(panel, false);
      $("identity-review-list").innerHTML = "";
      show($("identity-alert"), false);
      return;
    }
    show(panel, true);
    const html = reviews.map(questionHTML).join("");
    $("identity-review-list").innerHTML = html;
    $("identity-alert-body").innerHTML = html;
    setText($("identity-alert-count"), reviews.length > 1
      ? reviews.length + " identity questions need you" : "An identity question needs you");
    if (hadNew) show($("identity-alert"), true);
  }
  function questionHTML(q) {
    const a = q.archived || {};
    const pct = Math.round((q.score || 0) * 100);
    const prior = [];
    if (a.started_at_str) prior.push("sat " + a.started_at_str + "–" + (a.ended_at_str || "?"));
    if (a.peak_probability != null) prior.push("peak " + fmtNum(a.peak_probability, 0) + "%");
    if (a.total_events != null) prior.push(a.total_events + " look" + (a.total_events === 1 ? "" : "s") + " away");
    const who = q.candidate_label && q.candidate_label !== q.label
      ? "the student who left <b>" + esc(nameForLabel(q.candidate_label)) + "</b>" : "the student who sat here earlier";
    return '<div class="question" data-review-id="' + q.id + '">' +
      '<p class="question-text">Is the person now at <b>' + esc(q.label) + '</b> the same as ' + who + '?</p>' +
      '<p class="question-detail">Face similarity <span class="num">' + pct + '%</span>: not sure enough to decide alone.' +
        (prior.length ? ' Earlier occupant: ' + esc(prior.join(" · ")) + '.' : '') + '</p>' +
      '<p class="question-note">Until you answer, they are monitored as a new student. Nothing is merged without you.</p>' +
      '<div class="question-actions">' +
        '<button type="button" class="btn btn-primary btn-sm" data-review-confirm="' + q.id + '">Yes, same student</button>' +
        '<button type="button" class="btn btn-sm" data-review-reject="' + q.id + '">No, a different person</button>' +
      '</div></div>';
  }
  function answerReview(id, confirm, btn) {
    document.querySelectorAll('[data-review-id="' + id + '"] button').forEach(function (b) { b.disabled = true; });
    api("/api/identity/review", { body: { review_id: id, confirm: !!confirm } }).then(function (out) {
      if (!out.ok) {
        console.warn("Identity review failed:", out.j.error);
        document.querySelectorAll('[data-review-id="' + id + '"] button').forEach(function (b) { b.disabled = false; });
      }
      App.sigs.reviews = null;    // rebuild on the next push so it clears at once
    });
  }

  // ── Seat plan ────────────────────────────────────────────────
  const PEN_RING = '<svg class="pen-ring" viewBox="0 0 120 70" preserveAspectRatio="none" aria-hidden="true">' +
    '<path pathLength="1" d="M14 44 C6 26 30 9 62 8 C94 7 116 20 113 38 C110 56 80 65 52 62 C27 59 8 50 12 34 C14 26 24 18 38 14" /></svg>';
  function buildSeat(label) {
    const card = document.createElement("article");
    card.className = "seat";
    card.dataset.seat = label;
    card.tabIndex = 0;
    card.setAttribute("role", "button");
    card.innerHTML =
      '<header class="seat-head">' +
        '<span class="seat-swatch" data-ref="swatch"></span>' +
        '<span class="seat-id" data-ref="sid">' + label + '</span>' +
        '<span class="seat-name" data-ref="name">' + label + '</span>' +
        '<span class="stamp" data-ref="stamp" data-v="OK">—</span>' +
      '</header>' +
      '<div class="seat-body">' +
        '<div>' +
          '<div class="seat-scoreline"><div class="seat-score">' + PEN_RING + '<span class="v" data-ref="score">—</span><span class="u">%</span></div>' +
          '<span class="peak" data-ref="peak"></span></div>' +
          '<div class="seat-now" data-ref="now"><span data-ref="nowText">—</span><span class="when" data-ref="nowWhen"></span></div>' +
          // Said plainly on the card, because a seat the engine cannot
          // read is the one case where the numbers above it mean
          // nothing at all.
          '<div class="seat-blind" data-ref="blind" hidden></div>' +
        '</div>' +
        '<div class="gaze is-idle" data-ref="gaze" aria-hidden="true">' +
          '<div class="gaze-dial"><div class="gaze-fan" data-ref="fan"></div></div>' +
          '<div class="gaze-needle" data-ref="needle"></div>' +
          // A second, shorter needle for where the EYES point. The long
          // needle is the head; this one is what the head cannot tell
          // you. Hidden entirely when eye tracking is off.
          '<div class="gaze-eye" data-ref="eyeNeedle" hidden></div>' +
          '<div class="gaze-head"></div>' +
          '<span class="gaze-down">DOWN</span>' +
        '</div>' +
      '</div>' +
      '<svg class="spark" viewBox="0 0 100 34" preserveAspectRatio="none" aria-hidden="true">' +
        '<line class="spark-thr" data-ref="thr" x1="0" x2="100" y1="17" y2="17" />' +
        '<path class="spark-fill" data-ref="sparkFill" d="" /><path class="spark-line" data-ref="spark" d="" />' +
      '</svg>' +
      '<div class="pattern is-empty" data-ref="pattern">No pattern yet</div>' +
      '<footer class="seat-foot">' +
        '<span class="facts" data-ref="facts">—</span>' +
        // A human already knows when a student has permission to leave;
        // stating it is more reliable than any timeout the system could
        // infer. The seat is held and monitoring pauses meanwhile.
        '<button type="button" class="seat-break" data-ref="brk" data-break-for="' + label + '">Break</button>' +
      '</footer>';
    const r = refsOf(card);
    r.card = card;
    return r;
  }
  function renderSeats(state) {
    const grid = $("seats");
    if (!App.built.seats) {
      App.refs.seats = {};
      SEATS.forEach(function (l) {
        const r = buildSeat(l);
        r.card.hidden = true;
        grid.appendChild(r.card);
        App.refs.seats[l] = r;
      });
      const empty = document.createElement("div");
      empty.className = "seats-empty";
      empty.id = "seats-empty";
      empty.textContent = "No one is seated yet. Seats appear as the camera finds each student.";
      grid.appendChild(empty);
      grid.addEventListener("click", function (e) {
        const brk = e.target.closest("[data-break-for]");
        if (brk) { e.stopPropagation(); toggleBreak(brk.dataset.breakFor, brk); return; }
        const card = e.target.closest("[data-seat]");
        if (card) openInspector(card.dataset.seat, true);
      });
      grid.addEventListener("keydown", function (e) {
        if ((e.key === "Enter" || e.key === " ") && e.target.matches("[data-seat]")) {
          e.preventDefault();
          openInspector(e.target.dataset.seat, true);
        }
      });
      App.built.seats = true;
    }
    const order = seatOrder(state);
    let shown = 0;
    SEATS.forEach(function (l) {
      const r = App.refs.seats[l], p = students(state)[l];
      const on = !!p && seatOnPlan(state, l);
      show(r.card, on);
      if (!on) return;
      shown++;
      setStyle(r.card, "order", String(order.indexOf(l)));
      updateSeat(r, p, slotInfo(state, l), state);
    });
    show($("seats-empty"), shown === 0 && !!App.firstTickAt);
  }
  function updateSeat(r, p, info, state) {
    const tier = tierOf(p, info);
    const cal = isCalibrated(p);
    setData(r.card, "tier", tier);
    setData(r.card, "level", levelKeyOf(p));
    toggle(r.card, "is-selected", App.inspecting === p.label);
    setAttr(r.card, "aria-label", nameOf(p) + ", seat " + p.label + ", " + levelWordOf(p) + ". Open details.");
    setStyle(r.swatch, "background", seatInk(p));
    setText(r.name, nameOf(p));
    show(r.sid, nameOf(p) !== p.label);     // no "S2 S2" for an unregistered seat

    const v = tier === "break" ? "BREAK" : tier === "away" ? "AWAY" : tier === "calib" ? "CALIBRATING" : (verdictOf(p) || levelWordOf(p));
    setData(r.stamp, "v", v);
    setText(r.stamp, v === "CALIBRATING" ? "Calibrating" : v === "AWAY" ? "Not in view" : v === "BREAK" ? "On break" : v);

    if (cal && p.suspicion) {
      setText(r.score, fmtNum(p.suspicion.score, 0));
      setText(r.peak, "peak " + fmtNum(p.suspicion.peak, 0));
    } else {
      setText(r.score, "—");
      setText(r.peak, "");
    }

    // Now-line
    let now = "", when = "", away = false;
    if (tier === "break") {
      now = "On an authorised break";
      when = info && isNum(info.break_seconds) ? fmtDur(info.break_seconds) : "";
    } else if (tier === "away") {
      now = "Not in view, seat held";
      when = info && isNum(info.seconds_since_seen) ? fmtDur(info.seconds_since_seen) : "";
    } else if (tier === "calib") {
      now = "Learning their normal posture";
      when = fmtPct((p.calibration.progress || 0) * 100);
    } else {
      now = directionText(p);
      away = now !== "Facing forward";
      when = away && p.temporal && p.temporal.current_away_duration > 0 ? fmtSecs(p.temporal.current_away_duration) : "";
    }
    setText(r.nowText, now);
    setText(r.nowWhen, when);
    toggle(r.now, "is-away", away);

    // Visibility (presence.py). Stated, never scored: the score and the
    // verdict on this card were computed from whatever the engine could
    // see, and right now that is nothing.
    const pres = p.presence;
    const blind = !!(pres && pres.reportable);
    show(r.blind, blind);
    toggle(r.card, "is-blind", blind);
    if (blind) {
      setText(r.blind, "No readable face for " + fmtDur(pres.current_sec) +
        (pres.body_seen === true ? " · still seated"
          : pres.body_seen === false ? " · seat looks empty" : ""));
    }

    // Gaze dial
    const g = p.visible ? gazeOf(p) : { idle: true };
    setCls(r.gaze, "gaze" + (g.idle ? " is-idle" : "") + (g.away ? " is-away" : "") + (g.down ? " is-down" : ""));
    if (!g.idle) {
      setStyle(r.needle, "transform", "rotate(" + g.yawDeg.toFixed(1) + "deg)");
      // Eyes. The offset is a percent of eye width, drawn one degree per
      // percent, which puts a hard-over iris near the edge of the dial —
      // about how far an eye can actually travel.
      const hasEye = typeof g.eye === "number";
      show(r.eyeNeedle, hasEye);
      if (hasEye) {
        const ed = Math.max(-45, Math.min(45, g.eye));
        setStyle(r.eyeNeedle, "transform", "rotate(" + ed.toFixed(1) + "deg)");
        toggle(r.eyeNeedle, "is-aside", !!g.eyeAside);
      }
      const fan = g.fan ? "conic-gradient(from " + g.fan[0].toFixed(1) + "deg, var(--blue-soft) 0 " +
        (g.fan[1] - g.fan[0]).toFixed(1) + "deg, transparent 0)" : "none";
      setStyle(r.fan, "background", fan);
    }

    // Sparkline (2 Hz)
    if (performance.now() - (r._sparkAt || 0) > CHART_EVERY_MS) {
      r._sparkAt = performance.now();
      drawSpark(r, p, state);
    }

    // Pattern
    const hp = p.headline_pattern;
    const psig = hp ? hp.key + hp.label + Math.round(hp.confidence * 100) + hp.severity : "none" + cal;
    if (r._psig !== psig) {
      r._psig = psig;
      if (hp) {
        setCls(r.pattern, "pattern is-" + (hp.severity || "neutral"));
        r.pattern.innerHTML = "<b>" + esc(hp.label) + '</b><span class="conf">' + Math.round((hp.confidence || 0) * 100) + "%</span>";
        r.pattern.title = hp.description || "";
      } else {
        setCls(r.pattern, "pattern is-empty");
        r.pattern.textContent = cal ? "No pattern so far" : "Patterns appear after calibration";
        r.pattern.title = "";
      }
    }

    // Facts + break button
    if (cal && p.temporal) {
      const n = p.temporal.total_events || 0;
      setText(r.facts, n + " look" + (n === 1 ? "" : "s") + " · " + fmtPct((p.temporal.away_ratio_raw || 0) * 100) + " away");
    } else {
      setText(r.facts, p.calibration ? fmtInt(p.calibration.samples) + " / " + fmtInt(p.calibration.required) + " samples" : "—");
    }
    const onBreak = !!(info && info.on_break);
    const demo = !!(state.system && state.system.mock);
    setText(r.brk, onBreak ? "End break" : "Break");
    toggle(r.brk, "is-on", onBreak);
    r.brk.disabled = demo || !state.identity;
    r.brk.title = demo ? "Not available with demo data"
      : onBreak ? "They are back: resume monitoring" : "Hold this seat and pause monitoring while they are away with permission";
  }
  function drawSpark(r, p, state) {
    const hist = p.score_history || [];
    const win = (state.config && state.config.risk_timeline && state.config.risk_timeline.window_sec) || 300;
    const L = levelsCfg(state);
    const y = function (s) { return 34 - (Math.max(0, Math.min(100, s)) / 100) * 32 - 1; };
    r.thr.setAttribute("y1", y(L.moderate).toFixed(1));
    r.thr.setAttribute("y2", y(L.moderate).toFixed(1));
    if (hist.length < 2 || !isCalibrated(p)) {
      r.spark.setAttribute("d", "");
      r.sparkFill.setAttribute("d", "");
      return;
    }
    const pts = hist.map(function (pt) {
      return [(1 - Math.min(pt.ago, win) / win) * 100, y(pt.score)];
    });
    const d = pts.map(function (q, i) { return (i ? "L" : "M") + q[0].toFixed(2) + " " + q[1].toFixed(2); }).join(" ");
    r.spark.setAttribute("d", d);
    r.sparkFill.setAttribute("d", d + " L" + pts[pts.length - 1][0].toFixed(2) + " 34 L" + pts[0][0].toFixed(2) + " 34 Z");
  }
  function toggleBreak(label, btn) {
    const slot = SEATS.indexOf(label);
    if (slot < 0 || btn.disabled) return;
    btn.disabled = true;
    // No optimistic update: the button shows what the engine confirms on
    // the next push, never what we hoped.
    api("/api/break", { body: { slot: slot, on_break: !btn.classList.contains("is-on") } })
      .then(function (out) { if (!out.ok) btn.title = out.j.error || "Could not change the break"; });
  }

  // ── Camera ───────────────────────────────────────────────────
  // The MJPEG stream runs only while the Live page is open: nobody is
  // looking at it anywhere else, and it is the heaviest thing on the page.
  function setCameraStream(on) {
    const img = $("video-feed");
    if (!img) return;
    if (!App.built.camera) {
      img.addEventListener("error", function () {
        if (img.getAttribute("src")) { img.hidden = true; show($("camera-empty"), true); }
      });
      img.addEventListener("load", function () { img.hidden = false; show($("camera-empty"), false); });
      App.built.camera = true;
    }
    if (on && !img.getAttribute("src")) {
      img.hidden = false;
      show($("camera-empty"), false);
      img.setAttribute("src", "/video_feed?t=" + Date.now());
    } else if (!on && img.getAttribute("src")) {
      img.removeAttribute("src");
      img.hidden = true;
      show($("camera-empty"), true);
    }
  }
  function renderCamera(state) {
    const sys = state.system || {};
    setText($("camera-face-count"), String(sys.visible_students || 0));
    const tag = $("camera-tag-status");
    let st = "off", txt = "Camera offline";
    if (sys.mock) { st = "demo"; txt = "Demo · no camera"; }
    else if (sys.replay) { st = "replay"; txt = "Recorded clip"; }
    else if (sys.engine_connected && sys.fps > 0) { st = "live"; txt = "Live"; }
    setData(tag, "status", st);
    setText($("camera-status-text"), txt);
    setText($("cam-fps"), fmtNum(sys.fps, 0));
    setText($("cam-cal"), fmtInt(sys.calibrated_students) + " / " + fmtInt(sys.visible_students));
    const cap = state.roster_cap;
    setText($("cam-filtered"), cap ? String((cap.small_faces_dropped || 0) + (cap.over_cap_dropped || 0) + (cap.duplicates_dropped || 0)) : "—");
  }

  // ── Suspicion chart ──────────────────────────────────────────
  // Every student on one axis, labelled at the end of their own line
  // rather than in a legend. Drawn at the element's real pixel size, so
  // text is never stretched. No chart library: the dashboard has no
  // build step, and one CDN dependency for one chart would be the only
  // thing on the page that breaks when the network does.
  function renderRiskChart(state) {
    const svg = $("risk-chart"), wrap = $("risk-chart-wrap");
    const W = Math.max(wrap.clientWidth, 240), H = wrap.clientHeight || 230;
    const pad = { l: 30, r: 120, t: 10, b: 22 };
    const pw = W - pad.l - pad.r, ph = H - pad.t - pad.b;
    const win = (state.config && state.config.risk_timeline && state.config.risk_timeline.window_sec) || 300;
    const x = function (ago) { return pad.l + (1 - Math.min(ago, win) / win) * pw; };
    const y = function (s) { return pad.t + (1 - Math.max(0, Math.min(s, 100)) / 100) * ph; };
    const L = levelsCfg(state);
    const out = [];
    [["high", L.high, 100], ["moderate", L.moderate, L.high], ["mild", L.mild, L.moderate]].forEach(function (b) {
      out.push('<rect class="chart-band ' + b[0] + '" x="' + pad.l + '" y="' + y(b[2]).toFixed(1) + '" width="' + pw +
        '" height="' + (y(b[1]) - y(b[2])).toFixed(1) + '"/>');
      out.push('<text class="chart-band-label" x="' + (pad.l + 6) + '" y="' + (y(b[2]) + 11).toFixed(1) + '">' +
        LEVEL_WORDS[b[0]] + '</text>');
    });
    [0, 25, 50, 75, 100].forEach(function (v) {
      out.push('<line class="chart-grid" x1="' + pad.l + '" x2="' + (pad.l + pw) + '" y1="' + y(v).toFixed(1) + '" y2="' + y(v).toFixed(1) + '"/>');
      out.push('<text class="chart-axis" x="' + (pad.l - 7) + '" y="' + (y(v) + 3.5).toFixed(1) + '" text-anchor="end">' + v + '</text>');
    });
    out.push('<text class="chart-axis" x="' + pad.l + '" y="' + (H - 4) + '">−' + Math.round(win / 60) + ' min</text>');
    out.push('<text class="chart-axis" x="' + (pad.l + pw) + '" y="' + (H - 4) + '" text-anchor="end">now</text>');

    // The room, drawn first so student lines sit on top of it. Dashed
    // and in plain ink: it is context, not a student and not an alarm.
    if (App.roomHist.length > 1) {
      const now = performance.now();
      const rd = App.roomHist.map(function (pt, i) {
        return (i ? "L" : "M") + x((now - pt.at) / 1000).toFixed(1) + " " + y(pt.pct).toFixed(1);
      }).join(" ");
      out.push('<path class="chart-room" d="' + rd + '"/>');
      // Label inside the plot at top-left, not in the right-hand margin:
      // that margin already stacks one label per student and the room
      // line would collide with whichever student happened to be near it.
      const lastR = App.roomHist[App.roomHist.length - 1];
      out.push('<text class="chart-room-label" x="' + (pad.l + 6) + '" y="' + (pad.t + 10) +
        '">- - the room ' + Math.round(lastR.pct) + '% looking away</text>');
    }

    const ends = [];
    seatOrder(state).forEach(function (l) {
      const p = students(state)[l];
      const hist = (p && p.score_history) || [];
      if (hist.length < 2) return;
      const col = seatInk(p);
      const d = hist.map(function (pt, i) { return (i ? "L" : "M") + x(pt.ago).toFixed(1) + " " + y(pt.score).toFixed(1); }).join(" ");
      out.push('<path class="chart-line" d="' + d + '" stroke="' + col + '"/>');
      const last = hist[hist.length - 1];
      out.push('<circle cx="' + x(last.ago).toFixed(1) + '" cy="' + y(last.score).toFixed(1) + '" r="3.5" fill="' + col + '"/>');
      ends.push({ y: y(last.score), x: x(last.ago), name: nameOf(p), v: last.score, col: col });
    });
    // End labels: spread apart so two students at the same score stay legible.
    ends.sort(function (a, b) { return a.y - b.y; });
    for (let i = 1; i < ends.length; i++) if (ends[i].y - ends[i - 1].y < 15) ends[i].y = ends[i - 1].y + 15;
    for (let i = ends.length - 2; i >= 0; i--) if (ends[i].y > ends[i + 1].y - 15) ends[i].y = ends[i + 1].y - 15;
    ends.forEach(function (e) {
      out.push('<text class="chart-end" x="' + (pad.l + pw + 10) + '" y="' + (e.y + 4).toFixed(1) + '" fill="' + e.col + '">' +
        esc(e.name.length > 11 ? e.name.slice(0, 10) + "…" : e.name) + ' <tspan class="v">' + fmtNum(e.v, 0) + '%</tspan></text>');
    });
    if (!ends.length) {
      out.push('<text class="chart-empty" x="' + (pad.l + pw / 2) + '" y="' + (pad.t + ph / 2) + '" text-anchor="middle">Lines appear once students are calibrated.</text>');
    }
    svg.setAttribute("viewBox", "0 0 " + W + " " + H);
    svg.innerHTML = out.join("");
  }

  // ── Looks-away lanes (last 60 s) ─────────────────────────────
  function renderLanes(state) {
    const box = $("lanes");
    const order = seatOrder(state).filter(function (l) { return seatOnPlan(state, l); });
    const sig = order.map(function (l) { return l + nameForLabel(l); }).join("|") + document.documentElement.dataset.theme;
    if (sig !== App.sigs.lanes) {
      App.sigs.lanes = sig;
      box.innerHTML = order.map(function (l) {
        const p = students(state)[l];
        return '<div class="lane"><span class="lane-name"><span class="seat-swatch" style="background:' + seatInk(p) + '"></span>' +
          esc(nameOf(p)) + '</span><div class="lane-track" data-lane="' + l + '"></div></div>';
      }).join("") || '<div class="empty">No one in view.</div>';
    }
    const now = Date.now(), win = 60000;
    order.forEach(function (l) {
      const track = box.querySelector('[data-lane="' + l + '"]');
      if (!track) return;
      const html = App.looks.filter(function (e) { return e.label === l && now - e.at < win; }).map(function (e) {
        // The event is reported when it ENDS; draw it ending there.
        const endPct = 100 - ((now - e.at) / win) * 100;
        const wPct = Math.max(0.8, Math.min(40, ((e.duration || 0) * 1000 / win) * 100));
        return '<span class="lane-mark" data-level="' + e.level + '" style="left:' + Math.max(0, endPct - wPct).toFixed(2) +
          '%;width:' + wPct.toFixed(2) + '%" title="' + esc(e.direction + " · " + fmtSecs(e.duration)) + '"></span>';
      }).join("");
      setHTML(track, html);
    });
  }

  // ── Log ──────────────────────────────────────────────────────
  function eventKind(e) {
    const t = e.event || "";
    if (/^Verdict: /.test(t)) return "verdict";
    if (/^EVIDENCE SAVED/i.test(t)) return "evidence";
    if (/Calibration complete/.test(t)) return "calibration";
    if (e.student === "SYSTEM") return "system";
    return "identity";
  }
  function logItems() {
    const items = App.serverEvents.map(function (e) {
      return { kind: eventKind(e), hms: e.time || "", seq: e.seq, e: e };
    }).concat(App.looks.map(function (l) {
      return { kind: "look", hms: l.time || "", seq: l.seq, l: l };
    }));
    items.sort(function (a, b) { return a.hms < b.hms ? 1 : a.hms > b.hms ? -1 : b.seq - a.seq; });
    const f = App.logFilter;
    return items.filter(function (it) {
      if (f === "all") return true;
      if (f === "system") return it.kind === "system" || it.kind === "calibration" || it.kind === "identity";
      return it.kind === f;
    }).slice(0, 80);
  }
  function logText(it) {
    if (it.kind === "look") {
      const l = it.l;
      return "<b>" + esc(nameForLabel(l.label)) + "</b> looked " + esc(String(l.direction || "away").toLowerCase()) +
        " for " + fmtSecs(l.duration);
    }
    const e = it.e, who = e.student === "SYSTEM" ? "" : "<b>" + esc(nameForLabel(e.student)) + "</b> ";
    const t = e.event || "";
    if (it.kind === "verdict") {
      return who + "moved to " + esc(t.replace("Verdict: ", "")) + (e.score ? ' <span class="sc">' + fmtNum(e.score, 0) + "%</span>" : "");
    }
    if (it.kind === "evidence") {
      const m = /^EVIDENCE SAVED for \S+: (.*)$/i.exec(t);
      return "Snapshot saved for " + who + (m ? "· " + esc(m[1]) : "");
    }
    if (it.kind === "calibration") return who + "calibrated: normal posture learned";
    return who + esc(t);
  }
  function renderLog(state) {
    const list = $("log");
    const items = logItems();
    const sig = App.logFilter + ":" + items.length + ":" + (items[0] ? items[0].seq : 0) + ":" +
      SEATS.map(nameForLabel).join(",");
    if (sig === App.sigs.log) return;
    App.sigs.log = sig;
    if (!items.length) {
      list.innerHTML = '<li class="empty">' + (App.logFilter === "all" ? "Nothing has happened yet." : "Nothing of this kind yet.") + "</li>";
      return;
    }
    list.innerHTML = items.map(function (it) {
      const v = it.kind === "verdict" ? it.e.event.replace("Verdict: ", "") : "";
      const lvl = it.kind === "look" ? it.l.level : "";
      return '<li class="log-item"><span class="log-time">' + esc(it.hms) + '</span>' +
        '<span class="log-mark" data-k="' + it.kind + '" data-v="' + esc(v) + '" data-level="' + lvl + '"></span>' +
        '<span class="log-text">' + logText(it) + "</span></li>";
    }).join("");
  }

  // ═══════════════════════════════════════════════════════════════
  // SEAT INSPECTOR — the reasons behind one verdict, without leaving Live
  // ═══════════════════════════════════════════════════════════════
  function openInspector(label, fromSeat) {
    if (!App.state || !students(App.state)[label]) return;
    App.inspecting = label;
    App.selected = label;
    const box = $("inspector");
    box.innerHTML =
      '<header class="insp-head">' +
        '<div class="insp-title"><p class="eyebrow" data-ref="eyebrow">Seat</p><h2 class="insp-name" data-ref="name">—</h2></div>' +
        '<span class="stamp" data-ref="stamp">—</span>' +
        '<button type="button" class="icon-btn" data-insp-close aria-label="Close"><svg viewBox="0 0 24 24"><path d="M6 6l12 12M18 6L6 18"/></svg></button>' +
      '</header>' +
      '<div class="insp-body">' +
        '<div class="insp-score"><span class="big" data-ref="score">—</span>' +
          '<div class="meta"><span data-ref="level">—</span><span class="muted" data-ref="peak"></span><span data-ref="now"></span></div></div>' +
          // Visibility sits directly under the score on purpose: it is
          // the one thing that can make every figure below it stale.
          '<p class="insp-blind" data-ref="blind" hidden></p>' +
          // Eyes, stated separately from the head, because the whole
          // point of the signal is that the two can disagree.
          '<p class="insp-eye" data-ref="eye" hidden></p>' +
          // Context from the rest of the room, directly under the eye
          // line: both answer "compared with what?".
          '<p class="insp-room" data-ref="room" hidden></p>' +
        '<section class="insp-section"><h3 class="eyebrow">Why this verdict</h3><ul class="checks" data-ref="checks"></ul>' +
          '<p class="fine" style="margin-top:8px">A verdict counts how many of these four checks are met: none is OK, one WATCH, two ALERT, three or more FLAG. It is what triggers a saved snapshot.</p></section>' +
        '<section class="insp-section"><h3 class="eyebrow">Patterns</h3><div class="patterns-list" data-ref="patterns"></div></section>' +
        '<section class="insp-section"><h3 class="eyebrow">Recent looks away</h3><ul class="mini-events" data-ref="events"></ul></section>' +
      '</div>' +
      '<footer class="insp-actions">' +
        '<button type="button" class="btn btn-primary btn-sm" data-insp-full>Full analysis</button>' +
        '<button type="button" class="btn btn-sm" data-insp-room>Show in 3D room</button>' +
        '<button type="button" class="btn btn-sm" data-ref="brk" data-break-for="' + label + '">Break</button>' +
      '</footer>';
    App.refs.insp = refsOf(box);
    App.refs.insp.sigs = {};
    show(box, true);
    show($("scrim"), true);
    if (window.DigitalTwin && window.DigitalTwin.select) window.DigitalTwin.select(label);
    renderInspector(App.state);
    if (fromSeat) { const c = box.querySelector("[data-insp-close]"); if (c) c.focus({ preventScroll: true }); }
  }
  function closeInspector() {
    if (!App.inspecting) return;
    const was = App.inspecting;
    App.inspecting = null;
    show($("inspector"), false);
    show($("scrim"), false);
    $("inspector").innerHTML = "";
    const card = document.querySelector('.seat[data-seat="' + was + '"]');
    if (card) { card.classList.remove("is-selected"); if (App.page === "live") card.focus({ preventScroll: true }); }
  }
  function renderInspector(state) {
    const label = App.inspecting, r = App.refs.insp;
    const p = students(state)[label];
    if (!p || !r) return;
    const info = slotInfo(state, label), tier = tierOf(p, info), cal = isCalibrated(p);
    setText(r.eyebrow, "Seat " + label + (p.visible ? "" : " · not in view"));
    setText(r.name, nameOf(p));
    const v = tier === "calib" ? "CALIBRATING" : tier === "break" ? "BREAK" : (verdictOf(p) || levelWordOf(p));
    setData(r.stamp, "v", v);
    setText(r.stamp, v === "BREAK" ? "On break" : v === "CALIBRATING" ? "Calibrating" : v);
    setText(r.score, cal && p.suspicion ? fmtNum(p.suspicion.score, 0) + "%" : "—");
    setCls(r.score, "big lvl-" + (tier === "look" ? "high" : tier === "watch" ? "mild" : "normal"));
    setText(r.level, cal && p.suspicion ? p.suspicion.level + " suspicion score" : "Not scored until calibrated");
    setText(r.peak, cal && p.suspicion ? "Peak " + fmtNum(p.suspicion.peak, 0) + "% this session" : "");
    setText(r.now, p.visible ? directionText(p) : "Not in view");
    // Said here above everything else: when the engine cannot read this
    // face, every number in this drawer is from the last moment it
    // could, and the checks below have had nothing to work with since.
    const ipres = p.presence;
    const iblind = !!(ipres && ipres.reportable);
    show(r.blind, iblind || !!(ipres && ipres.total_unmeasured_sec));
    if (ipres) {
      // What the hands were doing, when the body check is running and
      // saw them. Stated as description, never as a finding: the
      // measurements behind it are explicitly not a detector (see
      // body_presence.py).
      const hs = ipres.hands;
      let hnote = "";
      if (hs) {
        if (hs.visibility < 0.10) hnote = " Their hands were out of view.";
        else if (hs.near_face < 1.5) hnote = " Their hands were up near their face.";
        else if (hs.height > 1.3) hnote = " Their hands were low, around desk height.";
        else hnote = " Their hands were in front of them.";
      }
      setText(r.blind, iblind
        ? ("No readable face for " + fmtDur(ipres.current_sec) +
           (ipres.body_seen === true ? " — someone is still sitting there."
            : ipres.body_seen === false ? " — no one seen in that seat." : ".") +
           hnote +
           " The figures below are from before that.")
        : ("Unreadable for " + fmtDur(ipres.total_unmeasured_sec) + " in total this session" +
           (ipres.longest_unmeasured_sec ? ", longest " + fmtDur(ipres.longest_unmeasured_sec) : "") + "."));
    }
    const brk = r.brk, onBreak = !!(info && info.on_break);
    setText(brk, onBreak ? "End break" : "Mark on break");
    toggle(brk, "is-on", onBreak);
    brk.disabled = !!(state.system && state.system.mock) || !state.identity;

    // Eyes (eye_gaze.py). Hidden entirely when eye tracking is off, so
    // the drawer never implies a measurement that was not taken.
    const eye = p.eye;
    show(r.eye, !!eye);
    if (eye) {
      let txt;
      if (!eye.calibrated) {
        txt = "Eyes: still learning where this student's eyes normally rest.";
      } else {
        const off = (eye.offset || 0) - (eye.baseline || 0);
        const voff = (eye.down_offset || 0) - (eye.down_baseline || 0);
        // Whichever axis moved further names the look, the same rule the
        // engine uses to label the event.
        const vertical = Math.abs(voff) > Math.abs(off);
        const amount = vertical ? Math.abs(voff) : Math.abs(off);
        const side = vertical ? (voff > 0 ? "below" : "above") : (off < 0 ? "left" : "right");
        txt = amount < 4
          ? "Eyes resting where they normally do."
          : "Eyes " + Math.round(amount) + "% of an eye-width " + side +
            " where they normally rest.";
        if (eye.aside) {
          txt += vertical
            ? " The head is level — this is a look down that head angle alone would not show."
            : " The head is forward — this is a look the head alone would not show.";
        }
      }
      setText(r.eye, txt);
      toggle(r.eye, "is-aside", !!eye.aside);
    }

    // Room context (room_context.py). The one thing a per-student score
    // cannot say on its own: was everyone else doing this too?
    const rm = p.room;
    show(r.room, !!rm);
    if (rm) {
      const pct = Math.round(rm.away_fraction * 100);
      setText(r.room, rm.shared
        ? ("Most of the room was looking away at the same moment (" + pct +
           "% of " + rm.others + " others). This counted for less than the same " +
           "glance made alone.")
        : (pct === 0
            ? "Nobody else in the room was looking away at the same moment."
            : pct + "% of the other " + rm.others + " students were also looking away."));
      toggle(r.room, "is-shared", !!rm.shared);
    }

    // Checks — rebuilt only when their outcome or values change.
    const checks = (p.verdict && p.verdict.checks) || [];
    const csig = checks.map(function (c) { return c.key + c.met + c.value; }).join("|") + cal;
    if (r.sigs.checks !== csig) {
      r.sigs.checks = csig;
      r.checks.innerHTML = checks.length ? checks.map(checkHTML).join("")
        : '<li class="checks-empty">' + (cal ? "This engine version does not report its verdict checks." : "Checks start once calibration is complete.") + "</li>";
    }
    const pats = p.patterns || [];
    const psig = pats.map(function (q) { return q.key + Math.round(q.confidence * 100); }).join("|");
    if (r.sigs.pats !== psig) {
      r.sigs.pats = psig;
      r.patterns.innerHTML = pats.length ? pats.map(function (q) {
        return '<div class="pattern-card is-' + esc(q.severity || "neutral") + '"><div class="row"><b>' + esc(q.label) +
          '</b><span class="num muted">' + Math.round((q.confidence || 0) * 100) + '%</span></div><p>' + esc(q.description || "") + "</p></div>";
      }).join("") : '<p class="fine">No named pattern. ' + (cal ? "Patterns appear when looking away forms a recognisable shape: repeated, long, one-directional, downward, or restless but undirected." : "") + "</p>";
    }
    const evs = (p.temporal && p.temporal.recent_events) || [];
    const esig = evs.length + (evs[0] ? evs[0].time + evs[0].duration : "");
    if (r.sigs.events !== esig) {
      r.sigs.events = esig;
      r.events.innerHTML = evs.length ? evs.slice(0, 8).map(function (e) {
        return '<li><span class="t">' + esc(e.time) + "</span><span>Looked " + esc(String(e.direction).toLowerCase()) +
          '</span><span class="d">' + fmtSecs(e.duration) + "</span></li>";
      }).join("") : '<li><span></span><span class="muted">None recorded yet.</span><span></span></li>';
    }
  }
  function checkHTML(c) {
    let val = "";
    if (c.key === "repeated") val = c.value + " in " + (c.window_sec || 20) + " s · needs " + c.limit;
    else if (c.key === "away_ratio") val = fmtPct(c.value * 100) + " · over " + fmtPct(c.limit * 100);
    else if (c.key === "confirmed") val = fmtSecs(c.value) + " · over " + c.limit + " s";
    else if (c.key === "peak") val = "peak " + fmtNum(c.value, 0) + "% · needs " + c.limit + "%";
    else val = String(c.value);
    return '<li class="check' + (c.met ? " is-met" : "") + '"><span class="check-box"></span>' +
      '<span class="check-label">' + esc(c.label) + '</span><span class="check-value">' + esc(val) + "</span></li>";
  }

  // ═══════════════════════════════════════════════════════════════
  // SETUP
  // ═══════════════════════════════════════════════════════════════
  function renderSetup(state) {
    renderRegister(state);
    renderHeadcount(state);
    renderChecklist(state);
  }
  // Rebuilt only when the roster itself changes, so a state push never
  // wipes a field someone is typing in.
  function renderRegister(state) {
    const r = state.roster || { enabled: false };
    const demo = !!(state.system && state.system.mock);
    const off = !r.enabled || demo;
    show($("register-off"), off);
    const locked = !!r.locked;
    const max = r.max_students || (state.system && state.system.max_students) || 4;
    const sig = JSON.stringify(r.students || {}) + "|" + locked + "|" + off + "|" + max;
    if (sig !== App.sigs.register) {
      App.sigs.register = sig;
      let html = "";
      for (let i = 0; i < max; i++) {
        const rec = (r.students || {})[i] || (r.students || {})[String(i)];
        const dis = locked || off ? " disabled" : "";
        html += '<div class="register-row' + (rec ? " is-registered" : "") + '">' +
          '<span class="register-seat"><span class="dot"></span>S' + (i + 1) + "</span>" +
          '<input class="field roster-name" data-seat="' + i + '" type="text" autocomplete="off" placeholder="Student name" value="' + esc(rec ? rec.name : "") + '"' + dis + " />" +
          '<input class="field roster-roll roll" data-seat="' + i + '" type="text" autocomplete="off" placeholder="Roll no." value="' + esc(rec && rec.roll_no ? rec.roll_no : "") + '"' + dis + " />" +
          (rec ? '<button type="button" class="btn btn-quiet btn-sm" data-roster-remove="' + i + '"' + dis + ">Remove</button>"
               : '<button type="button" class="btn btn-sm" data-roster-save="' + i + '"' + dis + ">Save</button>") +
        "</div>";
      }
      $("roster-rows").innerHTML = html;
    }
    const st = $("roster-panel-status");
    setText(st, off ? "" : locked ? "Locked · " + (r.registered || 0) + " registered" : (r.registered || 0) + " of " + max + " registered");
    toggle(st, "is-locked", locked);
    const lock = $("roster-lock-btn");
    setText(lock, locked ? "Unlock register" : "Lock register");
    toggle(lock, "is-locked", locked);
    lock.disabled = off || (!locked && !(r.registered > 0));
  }
  function postRoster(body) {
    return api("/api/roster/students", { body: body }).then(function (out) {
      if (!out.ok) setText($("roster-panel-status"), out.j.error || "Change refused");
      App.sigs.register = null;
    });
  }
  function saveRosterRow(seat) {
    const name = document.querySelector('.roster-name[data-seat="' + seat + '"]');
    const roll = document.querySelector('.roster-roll[data-seat="' + seat + '"]');
    postRoster({ action: "register", slot: seat, name: name ? name.value : "", roll_no: roll ? roll.value : "" });
  }
  function renderHeadcount(state) {
    const cap = state.roster_cap;
    const demo = !!(state.system && state.system.mock);
    const cur = cap ? cap.expected_students : undefined;
    $("roster-btns").querySelectorAll("[data-roster]").forEach(function (b) {
      const v = b.dataset.roster;
      toggle(b, "is-active", !!cap && (v === "auto" ? cur == null : parseInt(v, 10) === cur));
      b.disabled = demo || !cap;
    });
    let note;
    if (!cap) note = demo ? "Not available with demo data." : "Waiting for the engine.";
    else {
      const filtered = (cap.small_faces_dropped || 0) + (cap.over_cap_dropped || 0) + (cap.duplicates_dropped || 0);
      note = (cur == null ? "Automatic: tracking up to " + cap.max_students + " faces." : "Tracking at most " + cur + (cur === 1 ? " face." : " faces.")) +
        (filtered ? " " + filtered + " false detection" + (filtered === 1 ? "" : "s") + " filtered so far." : "");
    }
    setText($("roster-note"), note);
  }
  function renderChecklist(state) {
    const sys = state.system || {}, r = state.roster || {}, cap = state.roster_cap;
    const demo = !!sys.mock;
    const items = [
      { done: !!(sys.engine_connected && sys.fps > 0) || demo, title: "Camera running",
        note: demo ? "Demo data: no camera needed." : sys.fps > 0 ? "The engine is analysing " + fmtNum(sys.fps, 0) + " frames a second." : "Start the engine and point the camera at the desks." },
      { done: (sys.visible_students || 0) > 0, title: "Students in view",
        note: (sys.visible_students || 0) + " of " + (sys.max_students || 4) + " seats have a face in view." },
      { done: (r.registered || 0) > 0, title: "Students registered",
        note: r.enabled && !demo ? "Names make every report name people, not seats. Optional." : "Needs the real engine." },
      { done: !!(cap && cap.expected_students != null), title: "Head count set",
        note: cap ? "Stops background objects being tracked as students." : "Needs the real engine." },
      { done: !!r.locked, title: "Register locked",
        note: "Caps tracking to the registered students for the whole exam." },
      { done: (sys.visible_students || 0) > 0 && sys.calibrated_students >= sys.visible_students, title: "Everyone calibrated",
        note: "Each student looks at their paper normally for about five seconds while the system learns their posture." },
    ];
    const sig = items.map(function (i) { return i.done + i.note; }).join("|");
    if (sig === App.sigs.checklist) return;
    App.sigs.checklist = sig;
    $("setup-checks").innerHTML = items.map(function (it, k) {
      return '<li class="' + (it.done ? "is-done" : "") + '"><span class="todo-box">' + (k + 1) + "</span><div><b>" + esc(it.title) +
        '</b><span class="fine">' + esc(it.note) + "</span></div></li>";
    }).join("");
  }

  // ═══════════════════════════════════════════════════════════════
  // REVIEW
  // ═══════════════════════════════════════════════════════════════
  function renderReview(state) {
    const items = SEATS.map(function (l) { return students(state)[l] && students(state)[l].latest_evidence; }).filter(Boolean);
    const sig = items.map(function (e) { return e.image; }).join("|") + SEATS.map(nameForLabel).join();
    if (sig === App.sigs.evidence) return;
    App.sigs.evidence = sig;
    const grid = $("evidence-grid");
    if (!items.length) {
      grid.innerHTML = '<div class="empty" style="grid-column:1/-1">No snapshots yet. One is saved whenever a student reaches ALERT or FLAG, or their score crosses the capture threshold.</div>';
      return;
    }
    grid.innerHTML = items.map(function (ev) {
      const src = "/evidence/" + ev.image;
      return '<figure class="exhibit" style="margin:0"><a href="' + esc(src) + '" target="_blank" rel="noopener">' +
        '<img src="' + esc(src) + '" alt="Snapshot of ' + esc(nameForLabel(ev.student)) + '" loading="lazy" /></a>' +
        '<figcaption class="exhibit-body"><div class="exhibit-tag"><span class="eyebrow">Exhibit · ' + esc(ev.student) + "</span>" +
        '<span class="num muted">' + esc(ev.time || "") + "</span></div>" +
        '<span class="exhibit-name">' + esc(nameForLabel(ev.student)) + ' <span class="num lvl-high">' + fmtNum(ev.score, 0) + "%</span></span>" +
        '<span class="exhibit-reason">' + esc(ev.reason || "") + "</span></figcaption></figure>";
    }).join("");
  }

  // ═══════════════════════════════════════════════════════════════
  // SHARED: student tabs
  // ═══════════════════════════════════════════════════════════════
  function renderTabs(id, state) {
    const wrap = $(id);
    if (!App.built[id]) {
      wrap.innerHTML = SEATS.map(function (l) {
        return '<button type="button" class="tab" role="tab" data-student="' + l + '"><span class="status-dot status-dot--off"></span>' +
          '<span class="tab-id">' + l + '</span><span class="tab-name">' + l + "</span></button>";
      }).join("");
      wrap.addEventListener("click", function (e) {
        const t = e.target.closest("[data-student]");
        if (!t) return;
        App.selected = t.dataset.student;
        renderAll();
      });
      App.built[id] = true;
    }
    wrap.querySelectorAll("[data-student]").forEach(function (t) {
      const l = t.dataset.student, p = students(state)[l];
      show(t, !!p);
      if (!p) return;
      const on = App.selected === l;
      toggle(t, "is-active", on);
      t.setAttribute("aria-selected", on ? "true" : "false");
      toggle(t, "is-absent", !p.visible);
      setText(t.querySelector(".tab-name"), nameOf(p));
      show(t.querySelector(".tab-id"), nameOf(p) !== p.label);
      setCls(t.querySelector(".status-dot"), "status-dot status-dot--" + levelKeyOf(p));
    });
  }

  // ═══════════════════════════════════════════════════════════════
  // STUDENTS
  // ═══════════════════════════════════════════════════════════════
  function renderStudentsPage(state) {
    renderTabs("student-tabs", state);
    const box = $("student-detail");
    const p = students(state)[App.selected];
    const shape = p ? App.selected + "|" + isCalibrated(p) : "none";
    if (App.shapes.students !== shape) {
      App.shapes.students = shape;
      App.refs.students = p ? buildStudent(box, p) : null;
      if (!p) box.innerHTML = '<div class="state-note"><h3>No students yet</h3><p>Students appear once the engine reports them.</p></div>';
    }
    if (p && App.refs.students) updateStudent(App.refs.students, p, state);
  }
  function heroHTML() {
    return '<section class="hero" data-ref="hero">' +
      '<div><p class="eyebrow" data-ref="heroSeat">Seat</p><h2 class="hero-name" data-ref="heroName">—</h2>' +
        '<div class="hero-tags"><span class="stamp" data-ref="heroStamp">—</span><span class="tag" data-ref="heroVis">—</span><span class="tag" data-ref="heroDir">—</span></div></div>' +
      '<div class="dial"><svg viewBox="0 0 120 120" aria-hidden="true"><circle class="dial-track" cx="60" cy="60" r="50"/>' +
        '<circle class="dial-peak" data-ref="dialPeak" cx="60" cy="60" r="50" stroke-dasharray="0 314.2"/>' +
        '<circle class="dial-fill" data-ref="dialFill" cx="60" cy="60" r="50" stroke-dasharray="314.2" stroke-dashoffset="314.2"/></svg>' +
        '<div class="dial-centre"><span class="dial-v" data-ref="dialV">—</span><span class="dial-u">SUSPICION %</span></div></div>' +
      '<div class="hero-facts">' +
        '<span>Looks away</span><b data-ref="fEvents">—</b><span>Time away</span><b data-ref="fAway">—</b>' +
        '<span>Share of session</span><b data-ref="fRatio">—</b><span>Session memory</span><b data-ref="fMem">—</b>' +
        '<span>Time with a high score</span><b data-ref="fConf">—</b><span>Peak</span><b data-ref="fPeak">—</b>' +
      "</div></section>";
  }
  function buildStudent(box, p) {
    const cal = isCalibrated(p);
    let html = heroHTML();
    if (!cal) {
      html += '<div class="state-note"><h3>Learning ' + esc(nameOf(p)) + "'s normal posture</h3>" +
        "<p>Every later measurement is compared with how this student naturally sits, so nothing is scored until this finishes.</p>" +
        '<div class="progress"><div data-ref="calProg"></div></div><p class="fine" data-ref="calText" style="margin-top:10px"></p></div>';
    } else {
      html +=
        '<p class="notice" data-ref="awayNote" hidden>Not in view right now. The figures below are from when they were last seen.</p>' +
        '<div class="grid-2">' +
          '<section class="pane"><header class="pane-head"><div><p class="eyebrow">Verdict</p><h2 class="pane-title">Why this verdict</h2></div></header>' +
            '<ul class="checks" data-ref="checks"></ul>' +
            '<p class="fine" style="margin-top:10px">None met is OK, one WATCH, two ALERT, three or more FLAG. The verdict is what triggers a saved snapshot.</p></section>' +
          '<section class="pane"><header class="pane-head"><div><p class="eyebrow">Patterns</p><h2 class="pane-title">What the looking away resembles</h2></div></header>' +
            '<div class="patterns-list" data-ref="patterns"></div></section>' +
        "</div>" +
        '<section class="pane" style="margin-top:18px"><header class="pane-head"><div><p class="eyebrow">In plain words</p><h2 class="pane-title">What the numbers say</h2></div></header>' +
          '<p class="narrative" data-ref="narrative"></p></section>' +
        '<div class="grid-2" style="margin-top:18px">' +
          '<section class="pane"><header class="pane-head"><div><p class="eyebrow">Score</p><h2 class="pane-title">What makes up the score</h2></div></header>' +
            '<div class="bars" data-ref="bars"></div></section>' +
          '<section class="pane"><header class="pane-head"><div><p class="eyebrow">Head movement</p><h2 class="pane-title">Left and right, last 20 seconds</h2></div></header>' +
            graphHTML() + "</section>" +
        "</div>" +
        '<div class="grid-3" style="margin-top:18px">' +
          '<section class="pane"><header class="pane-head"><div><p class="eyebrow">History</p><h2 class="pane-title">Recent looks away</h2></div></header>' +
            '<table class="table"><thead><tr><th>Time</th><th>Direction</th><th>Length</th></tr></thead><tbody data-ref="events"></tbody></table></section>' +
          kvPane("Head pose", "Where the head points", "head", [["raw", "Raw yaw"], ["filt", "Smoothed yaw"], ["base", "Baseline"], ["diff", "From baseline"], ["z", "Z-score"], ["dir", "Direction"], ["prof", "Profile face"]]) +
          kvPane("Kinematics", "How it moves", "kin", [["vel", "Turning speed"], ["maxv", "Fastest this session"], ["acc", "Acceleration"]]) +
        "</div>";
    }
    box.innerHTML = html;
    const r = refsOf(box);
    if (r.bars) r.barRows = buildBars(r.bars);
    if (r.graphCanvas) bindGraph(r);
    return r;
  }
  function kvPane(eyebrow, title, prefix, rows) {
    return '<section class="pane"><header class="pane-head"><div><p class="eyebrow">' + esc(eyebrow) + '</p><h2 class="pane-title">' + esc(title) + "</h2></div></header>" +
      '<div class="kv">' + rows.map(function (r) {
        return '<div class="kv-row"><span>' + esc(r[1]) + '</span><b data-ref="' + prefix + "_" + r[0] + '">—</b></div>';
      }).join("") + "</div></section>";
  }
  function updateHero(r, p, state) {
    const info = slotInfo(state, p.label), tier = tierOf(p, info), cal = isCalibrated(p);
    setData(r.hero, "tier", tier);
    setText(r.heroSeat, "Seat " + p.label);
    setText(r.heroName, nameOf(p));
    const v = tier === "calib" ? "CALIBRATING" : tier === "break" ? "BREAK" : (verdictOf(p) || levelWordOf(p));
    setData(r.heroStamp, "v", v);
    setText(r.heroStamp, v === "CALIBRATING" ? "Calibrating" : v === "BREAK" ? "On break" : v);
    setText(r.heroVis, p.visible ? "In view" : "Not in view");
    setText(r.heroDir, p.visible ? directionText(p) : "—");
    toggle(r.heroDir, "is-away", p.visible && directionText(p) !== "Facing forward");
    const circ = 2 * Math.PI * 50;
    if (cal && p.suspicion) {
      const s = p.suspicion.score, pk = p.suspicion.peak;
      setStyle(r.dialFill, "strokeDashoffset", String(circ * (1 - s / 100)));
      setStyle(r.dialFill, "stroke", tier === "look" ? "var(--pen)" : tier === "watch" ? "var(--c-mild)" : "var(--ink)");
      r.dialPeak.setAttribute("stroke-dasharray", (circ * pk / 100).toFixed(1) + " " + circ.toFixed(1));
      setText(r.dialV, fmtNum(s, 0));
    } else {
      setStyle(r.dialFill, "strokeDashoffset", String(circ));
      setText(r.dialV, "—");
    }
    const t = p.temporal || {};
    setText(r.fEvents, cal ? fmtInt(t.total_events) : "—");
    setText(r.fAway, cal ? fmtSecs(t.total_away_time) : "—");
    setText(r.fRatio, cal ? fmtPct((t.away_ratio_raw || 0) * 100) : "—");
    setText(r.fMem, cal ? fmtPct((t.session_score || 0) * 100) : "—");
    setText(r.fConf, cal && p.suspicion ? fmtSecs(p.suspicion.confirmed_secs) : "—");
    setText(r.fPeak, cal && p.suspicion ? fmtPct(p.suspicion.peak) : "—");
  }
  function updateStudent(r, p, state) {
    updateHero(r, p, state);
    if (!isCalibrated(p)) {
      const c = p.calibration || {};
      setStyle(r.calProg, "width", ((c.progress || 0) * 100).toFixed(1) + "%");
      setText(r.calText, fmtInt(c.samples) + " of " + fmtInt(c.required) + " samples" + (p.visible ? "" : " · not in view, paused"));
      return;
    }
    show(r.awayNote, !p.visible);
    const checks = (p.verdict && p.verdict.checks) || [];
    const csig = checks.map(function (c) { return c.key + c.met + c.value; }).join("|");
    if (r._csig !== csig) {
      r._csig = csig;
      r.checks.innerHTML = checks.length ? checks.map(checkHTML).join("") : '<li class="checks-empty">This engine version does not report its verdict checks.</li>';
    }
    const pats = p.patterns || [];
    const psig = pats.map(function (q) { return q.key + Math.round(q.confidence * 100); }).join("|");
    if (r._psig !== psig) {
      r._psig = psig;
      r.patterns.innerHTML = pats.length ? pats.map(function (q) {
        return '<div class="pattern-card is-' + esc(q.severity || "neutral") + '"><div class="row"><b>' + esc(q.label) +
          '</b><span class="num muted">' + Math.round((q.confidence || 0) * 100) + "%</span></div><p>" + esc(q.description || "") + "</p></div>";
      }).join("") : '<p class="fine">No named pattern so far. That is the usual state for a student who is simply working.</p>';
    }
    setHTML(r.narrative, narrative(p));
    updateBars(r.barRows, p);
    drawYawGraph(r.graphCanvas, p);
    drawEyePanel(r, p);
    const evs = (p.temporal && p.temporal.recent_events) || [];
    const esig = evs.length + (evs[0] ? evs[0].time + evs[0].duration : "");
    if (r._esig !== esig) {
      r._esig = esig;
      r.events.innerHTML = evs.length ? evs.map(function (e) {
        return '<tr><td class="num">' + esc(e.time) + "</td><td>" + esc(e.direction) + '</td><td class="num">' + fmtSecs(e.duration) + "</td></tr>";
      }).join("") : '<tr><td colspan="3" class="muted">None recorded yet.</td></tr>';
    }
    const hp = p.head_pose || {}, c = p.calibration || {}, k = p.kinematics || {};
    setText(r.head_raw, fmtDeg(hp.raw_yaw));
    setText(r.head_filt, fmtDeg(hp.filtered_yaw));
    setText(r.head_base, fmtDeg(c.baseline));
    setText(r.head_diff, isNum(hp.filtered_yaw) && isNum(c.baseline) ? fmtDeg(hp.filtered_yaw - c.baseline) : "—");
    setText(r.head_z, fmtNum(hp.z_score, 2));
    setText(r.head_dir, hp.direction || "—");
    setText(r.head_prof, hp.is_profile ? "Yes" : "No");
    setText(r.kin_vel, fmtNum(k.velocity, 1, "°/s"));
    setText(r.kin_maxv, fmtNum(k.max_velocity, 1, "°/s"));
    setText(r.kin_acc, fmtNum(k.acceleration, 1));
  }
  // Plain-English reading of the numbers, written to UNDER-CLAIM: what was
  // observed and what it is consistent with, never what the student did.
  function narrative(p) {
    const s = p.suspicion, t = p.temporal || {}, name = "<b>" + esc(nameOf(p)) + "</b>";
    if (!s) return "";
    const n = function (v) { return '<span class="n">' + v + "</span>"; };
    const out = [];
    const desc = {
      normal: "Their head movement is within their own normal range.",
      low: "There are small departures from how they normally sit.",
      mild: "They are looking away somewhat more than their own normal.",
      moderate: "They are looking away noticeably more than their own normal.",
      high: "Their looking away is well beyond their own normal. That is a reason to look, not proof of anything.",
    }[s.level_key] || "";
    const v = p.verdict && p.verdict.label && p.verdict.label !== "CALIBRATING"
      ? ", verdict " + esc(p.verdict.label) + " (" + p.verdict.evidence + " of 4 checks)" : "";
    out.push(name + " has a suspicion score of " + n(fmtNum(s.score, 0) + "%") + ", " + esc(s.level) + v + ". " + desc);
    if (t.currently_away) out.push("Right now they are " + esc(directionText(p).toLowerCase()) + " (" + n(fmtSecs(t.current_away_duration)) + " so far).");
    const ev = t.total_events || 0;
    out.push(ev === 0 ? "No looks away have been recorded this session."
      : n(ev) + " look" + (ev === 1 ? "" : "s") + " away recorded, " + n(t.events_window || 0) + " in the last " + (t.window_sec || 20) + " seconds.");
    const ar = (t.away_ratio_raw || 0) * 100;
    out.push(ar < 2 ? "Time spent looking away is negligible."
      : ar < 15 ? "They have spent " + n(fmtPct(ar)) + " of the session looking away, within the usual range."
      : "They have spent " + n(fmtPct(ar)) + " of the session looking away, which weighs heavily in the score.");
    const mem = (t.session_score || 0) * 100, half = Math.round((t.session_halflife_sec || 600) / 60);
    if (mem > 50) out.push("Session memory is " + n(fmtPct(mem)) + ": a pattern repeated over time, weighted towards recent minutes (older events fade with a " + half + "-minute half-life).");
    else if ((t.session_lifetime_raw || 0) > 1 && mem <= 20) out.push("Earlier looks away have faded from the score, though they stay in the full record.");
    if (s.peak >= levelsCfg(App.state).high) out.push("The score reached HIGH RISK at least once (peak " + n(fmtNum(s.peak, 0) + "%") + ").");
    return out.join(" ");
  }
  function buildBars(root, tech) {
    const rows = {};
    const list = tech ? TECH_SIGNALS : SIGNALS;
    root.innerHTML = list.map(function (s) {
      const head = tech
        ? '<code class="sig-var">' + esc(s.v) + '</code><span class="sig-note">' + esc(s.note) + "</span>"
        : esc(s.label);
      return '<div class="bar-row' + (tech ? " is-tech" : "") + '" data-bar="' + s.key + '">' +
        '<div class="bar-top"><span>' + head + '<span class="w"></span></span><span class="v">—</span></div>' +
        '<div class="bar-track"><div class="bar-fill"></div></div>' +
        (tech ? '<div class="sig-foot"><span class="sig-const">' + esc(s.c) + '</span><span class="sig-contrib"></span></div>' : "") +
        "</div>";
    }).join("");
    list.forEach(function (s) {
      const row = root.querySelector('[data-bar="' + s.key + '"]');
      rows[s.key] = { w: row.querySelector(".w"), v: row.querySelector(".v"),
                      fill: row.querySelector(".bar-fill"),
                      contrib: row.querySelector(".sig-contrib") };
    });
    rows._tech = !!tech;
    return rows;
  }
  function updateBars(rows, p) {
    const w = (window.CONFIG && window.CONFIG.weights) || {};
    const tech = !!rows._tech;
    (tech ? TECH_SIGNALS : SIGNALS).forEach(function (s) {
      const r = rows[s.key], v = p.signals ? p.signals[s.key] : 0;
      setText(r.w, isNum(w[s.key])
        ? (tech ? "= " + w[s.key].toFixed(2) : "weight " + Math.round(w[s.key] * 100) + "%")
        : "");
      setText(r.v, fmtPct(v));
      setStyle(r.fill, "width", Math.max(0, Math.min(100, v || 0)) + "%");
      const k = riskKey(v || 0);
      setStyle(r.fill, "background", k === "normal" ? "var(--ink-3)" : "var(--c-" + k + ")");
      // What this signal actually puts into the final number, so the
      // five bars visibly add up to the score instead of being five
      // unrelated percentages.
      if (tech && r.contrib) {
        setText(r.contrib, isNum(w[s.key])
          ? "contributes " + fmtNum((w[s.key] * (v || 0)), 1) + " of " + fmtNum(p.suspicion ? p.suspicion.score : 0, 1)
          : "");
      }
    });
  }

  // ── Yaw graph (canvas) ───────────────────────────────────────
  function graphHTML() {
    return '<div class="graph-wrap"><canvas class="graph-canvas" data-ref="graphCanvas"></canvas><div class="graph-tip" data-ref="graphTip"></div></div>' +
      '<p class="fine" style="margin-top:8px">Blue is their calibrated straight-ahead; the shaded edges are where the engine counts a look as "away".</p>' +
      // The eyes, drawn apart from the head because the whole point of
      // tracking them is that the two can disagree. Hidden entirely when
      // eye tracking is off, so nobody is shown a graph of nothing.
      '<div class="graph-eye" data-ref="eyeWrap" hidden>' +
        '<p class="eyebrow graph-eye-title">Eyes, left and right</p>' +
        '<div class="graph-wrap graph-wrap--eye"><canvas class="graph-canvas" data-ref="graphEye"></canvas></div>' +
        '<p class="fine" data-ref="eyeNote"></p>' +
      '</div>';
  }
  function bindGraph(r) {
    const c = r.graphCanvas, tip = r.graphTip;
    c.addEventListener("mousemove", function (e) {
      const rect = c.getBoundingClientRect(), x = e.clientX - rect.left;
      const pt = c._find && c._find(x);
      if (!pt) { tip.classList.remove("show"); return; }
      tip.classList.add("show");
      tip.style.left = Math.min(rect.width - 120, x + 12) + "px";
      tip.textContent = "yaw " + fmtNum(pt.yaw, 1) + "°";
    });
    c.addEventListener("mouseleave", function () { tip.classList.remove("show"); });
  }
  function drawYawGraph(canvas, p) {
    const rect = canvas.getBoundingClientRect();
    if (!rect.width) return;
    const dpr = window.devicePixelRatio || 1, w = rect.width, h = rect.height || 200;
    if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(h * dpr)) {
      canvas.width = Math.round(w * dpr);
      canvas.height = Math.round(h * dpr);
    }
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    const C = { rule: cssVar("--rule"), ink: cssVar("--ink"), ink3: cssVar("--ink-3"), blue: cssVar("--blue"), pen: cssVar("--pen"), penSoft: cssVar("--pen-soft"), mono: cssVar("--font-mono") };
    const graph = p.graph || [];
    if (graph.length < 2) {
      ctx.fillStyle = C.ink3;
      ctx.font = "italic 15px " + cssVar("--font-display");
      ctx.textAlign = "center";
      ctx.fillText("Collecting head movement…", w / 2, h / 2);
      canvas._find = null;
      return;
    }
    const cal = p.calibration || {}, base = cal.baseline || 0;
    const up = cal.upper_threshold, lo = cal.lower_threshold;
    const ys = graph.map(function (q) { return q.yaw; });
    let minY = Math.min(base, isNum(lo) ? lo : base, Math.min.apply(null, ys));
    let maxY = Math.max(base, isNum(up) ? up : base, Math.max.apply(null, ys));
    const padY = Math.max((maxY - minY) * 0.18, 2);
    minY -= padY; maxY += padY;
    const t0 = graph[0].t, t1 = graph[graph.length - 1].t, span = Math.max(t1 - t0, 0.001);
    const X = function (t) { return ((t - t0) / span) * (w - 48); };
    const Y = function (v) { return h - ((v - minY) / (maxY - minY)) * h; };
    ctx.strokeStyle = C.rule; ctx.lineWidth = 1;
    for (let i = 1; i < 4; i++) { const gy = (h / 4) * i; ctx.beginPath(); ctx.moveTo(0, gy); ctx.lineTo(w, gy); ctx.stroke(); }
    if (isNum(up) && isNum(lo)) {
      ctx.fillStyle = C.penSoft;
      ctx.fillRect(0, 0, w, Y(up));
      ctx.fillRect(0, Y(lo), w, h - Y(lo));
      ctx.strokeStyle = C.pen; ctx.globalAlpha = 0.5; ctx.setLineDash([4, 4]);
      [up, lo].forEach(function (v) { ctx.beginPath(); ctx.moveTo(0, Y(v)); ctx.lineTo(w, Y(v)); ctx.stroke(); });
      ctx.setLineDash([]); ctx.globalAlpha = 1;
    }
    ctx.strokeStyle = C.blue; ctx.lineWidth = 1.5; ctx.setLineDash([2, 3]);
    ctx.beginPath(); ctx.moveTo(0, Y(base)); ctx.lineTo(w, Y(base)); ctx.stroke(); ctx.setLineDash([]);
    ctx.strokeStyle = C.ink; ctx.lineWidth = 1.8; ctx.lineJoin = "round";
    ctx.beginPath();
    graph.forEach(function (q, i) { const px = X(q.t), py = Y(q.yaw); if (i) ctx.lineTo(px, py); else ctx.moveTo(px, py); });
    ctx.stroke();
    const last = graph[graph.length - 1];
    ctx.fillStyle = C.ink;
    ctx.beginPath(); ctx.arc(X(last.t), Y(last.yaw), 3.5, 0, Math.PI * 2); ctx.fill();
    ctx.fillStyle = C.ink3; ctx.font = "500 10px " + C.mono; ctx.textAlign = "right";
    ctx.fillText(maxY.toFixed(1) + "°", w - 4, 12);
    ctx.fillText(minY.toFixed(1) + "°", w - 4, h - 4);
    canvas._find = function (px) {
      const t = t0 + (px / (w - 48)) * span;
      let best = null, bd = Infinity;
      graph.forEach(function (q) { const d = Math.abs(q.t - t); if (d < bd) { bd = d; best = q; } });
      return best;
    };
  }

  // The eye strip. Units are PERCENT OF EYE WIDTH from this student's own
  // resting eye position, which is not degrees, so it gets its own axis
  // rather than being squeezed onto the head graph's. Dashed pen lines
  // are the engine's absolute floor (Config.EYE_MIN_OFFSET): the eyes
  // must move at least that far, and well past their own wobble, to
  // count. Teal is the same eye colour the 3D room uses; a stretch the
  // engine actually counted as "eyes aside" turns pen red.
  function drawEyeGraph(canvas, hist, floor) {
    const rect = canvas.getBoundingClientRect();
    if (!rect.width) return;
    const dpr = window.devicePixelRatio || 1, w = rect.width, h = rect.height || 96;
    if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(h * dpr)) {
      canvas.width = Math.round(w * dpr);
      canvas.height = Math.round(h * dpr);
    }
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    const C = { rule: cssVar("--rule"), ink3: cssVar("--ink-3"), blue: cssVar("--blue"),
                pen: cssVar("--pen"), penSoft: cssVar("--pen-soft"),
                eye: cssVar("--room-eye") || cssVar("--blue"), mono: cssVar("--font-mono") };
    const vals = hist.filter(function (q) { return q.v !== null; }).map(function (q) { return Math.abs(q.v); });
    const R = Math.max(floor * 2.2, (vals.length ? Math.max.apply(null, vals) : 0) * 1.15);
    const now = performance.now() / 1000, span = 20, pw = w - 48;
    const X = function (t) { return Math.max(0, ((t - (now - span)) / span) * pw); };
    const Y = function (v) { return h / 2 - (v / R) * (h / 2); };
    ctx.fillStyle = C.penSoft;
    ctx.fillRect(0, 0, w, Y(floor));
    ctx.fillRect(0, Y(-floor), w, h - Y(-floor));
    ctx.strokeStyle = C.pen; ctx.globalAlpha = 0.5; ctx.setLineDash([4, 4]); ctx.lineWidth = 1;
    [floor, -floor].forEach(function (v) { ctx.beginPath(); ctx.moveTo(0, Y(v)); ctx.lineTo(w, Y(v)); ctx.stroke(); });
    ctx.setLineDash([]); ctx.globalAlpha = 1;
    ctx.strokeStyle = C.blue; ctx.lineWidth = 1.5; ctx.setLineDash([2, 3]);
    ctx.beginPath(); ctx.moveTo(0, Y(0)); ctx.lineTo(w, Y(0)); ctx.stroke(); ctx.setLineDash([]);
    ctx.lineWidth = 1.8; ctx.lineJoin = "round";
    for (let i = 1; i < hist.length; i++) {
      const a = hist[i - 1], b = hist[i];
      if (a.v === null || b.v === null) continue;     // a gap, not a zero
      ctx.strokeStyle = b.aside ? C.pen : C.eye;
      ctx.beginPath(); ctx.moveTo(X(a.t), Y(a.v)); ctx.lineTo(X(b.t), Y(b.v)); ctx.stroke();
    }
    const last = hist[hist.length - 1];
    if (last && last.v !== null) {
      ctx.fillStyle = last.aside ? C.pen : C.eye;
      ctx.beginPath(); ctx.arc(X(last.t), Y(last.v), 3.5, 0, Math.PI * 2); ctx.fill();
    }
    ctx.fillStyle = C.ink3; ctx.font = "500 10px " + C.mono; ctx.textAlign = "right";
    ctx.fillText("+" + Math.round(R) + "%", w - 4, 12);
    ctx.fillText("\u2212" + Math.round(R) + "%", w - 4, h - 4);
  }

  // Shows or hides the eye strip and says why when there is nothing in it.
  function drawEyePanel(r, p) {
    if (!r.eyeWrap) return;
    const cfgE = (App.state && App.state.config && App.state.config.eye) || null;
    const on = !!(cfgE && cfgE.enabled);
    show(r.eyeWrap, on);
    if (!on) return;
    const hist = App.eyeHist[p.label] || [];
    const has = hist.some(function (q) { return q.v !== null; });
    show(r.graphEye.parentNode, has);
    if (has) drawEyeGraph(r.graphEye, hist, isNum(cfgE.min_offset) ? cfgE.min_offset : 12);
    setText(r.eyeNote, has
      ? "Teal is where the eyes point, measured from their normal resting position in percent of eye width. " +
        "Red means the engine counted it as eyes aside while the head was forward. " +
        "Gaps mean the iris could not be read (head down, a blink, or the face too small)."
      : "No readable iris in the last 20 seconds. Eyes cannot be read while the head is down, " +
        "for example when writing, so nothing is drawn rather than guessing.");
  }

  // ═══════════════════════════════════════════════════════════════
  // CALIBRATION
  // ═══════════════════════════════════════════════════════════════
  function renderCalibrationPage(state) {
    const row = $("calib-status-row");
    if (!App.built.calibRow) {
      row.innerHTML = SEATS.map(function (l) {
        return '<div class="calib-card" data-cal="' + l + '"><div class="calib-card-head"><span class="who"><span class="seat-swatch"></span><span class="nm">' + l + "</span></span>" +
          '<span class="stamp">—</span></div><div class="calib-card-body"><div class="ring"><svg viewBox="0 0 64 64"><circle class="track" cx="32" cy="32" r="27"/>' +
          '<circle class="fill" cx="32" cy="32" r="27" stroke-dasharray="169.6" stroke-dashoffset="169.6"/></svg><span class="ring-v">—</span></div>' +
          '<div class="kv"><div class="kv-row"><span>Baseline</span><b class="cb">—</b></div><div class="kv-row"><span>Spread</span><b class="cs">—</b></div>' +
          '<div class="kv-row"><span>Away beyond</span><b class="ct">—</b></div></div></div></div>';
      }).join("");
      App.built.calibRow = true;
    }
    SEATS.forEach(function (l) {
      const card = row.querySelector('[data-cal="' + l + '"]'), p = students(state)[l];
      show(card, !!p && seatOnPlan(state, l));
      if (!p) return;
      const c = p.calibration || {}, done = !!c.complete;
      card.querySelector(".seat-swatch").style.background = seatInk(p);
      setText(card.querySelector(".nm"), nameOf(p));
      const st = card.querySelector(".stamp");
      const v = !p.visible && !done ? "AWAY" : done ? "OK" : "CALIBRATING";
      setData(st, "v", v);
      setText(st, v === "OK" ? "Calibrated" : v === "AWAY" ? "Not in view" : "Calibrating");
      const circ = 2 * Math.PI * 27;
      const ring = card.querySelector(".ring");
      toggle(ring, "is-done", done);
      setStyle(ring.querySelector(".fill"), "strokeDashoffset", String(circ * (1 - (c.progress || 0))));
      setText(ring.querySelector(".ring-v"), fmtPct((c.progress || 0) * 100));
      setText(card.querySelector(".cb"), fmtDeg(done ? c.baseline : c.running_mean));
      setText(card.querySelector(".cs"), fmtDeg(c.std));
      setText(card.querySelector(".ct"), done ? "±" + fmtDeg(c.threshold) : "—");
    });
    renderTabs("calib-tabs", state);
    const box = $("calib-detail"), p = students(state)[App.selected];
    const shape = p ? App.selected + "|" + isCalibrated(p) : "none";
    if (App.shapes.calib !== shape) {
      App.shapes.calib = shape;
      if (!p) { box.innerHTML = ""; App.refs.calib = null; }
      else if (!isCalibrated(p)) {
        box.innerHTML = '<div class="grid-2">' +
          kvPane("In progress", "Calibration", "cp", [["samples", "Samples collected"], ["required", "Samples needed"], ["progress", "Progress"]]) +
          kvPane("Live readings", "While calibrating", "cl", [["raw", "Raw yaw"], ["filt", "Smoothed yaw"], ["mean", "Running mean"], ["std", "Running spread"]]) +
          '</div><section class="pane" style="margin-top:18px"><header class="pane-head"><div><p class="eyebrow">How it works</p><h2 class="pane-title">What calibration is</h2></div></header>' +
          '<p class="narrative" style="font-size:16px">The system averages the first ' + fmtInt(p.calibration.required) + ' frames of each student (Welford\'s online algorithm) to learn their natural posture and how much they normally move. Afterwards, a head turn counts as a look away only beyond K_SIGMA × their own spread. Ask students to look at their papers as usual; there is no need to sit unnaturally still.</p></section>';
        App.refs.calib = refsOf(box);
      } else {
        box.innerHTML = '<div class="grid-2">' +
          kvPane("Result", "Calibration result", "cr", [["base", "Baseline (straight ahead)"], ["std", "Spread (std dev)"], ["k", "K_SIGMA"], ["thr", "Away beyond"], ["lo", "Lower bound"], ["up", "Upper bound"], ["n", "Samples used"]]) +
          '<section class="pane"><header class="pane-head"><div><p class="eyebrow">Right now</p><h2 class="pane-title">Inside their forward zone?</h2></div></header>' +
            '<div class="range"><div class="range-track" data-ref="track"><div class="range-zone" data-ref="zone"></div><div class="range-base" data-ref="base"></div><div class="range-now" data-ref="now"></div></div>' +
            '<div class="range-labels"><span data-ref="lo">—</span><span class="c" data-ref="mid">—</span><span data-ref="up">—</span></div>' +
            '<div class="range-explain"><div><b>Lower bound</b>baseline − K_SIGMA × spread</div><div><b>Baseline</b>their calibrated straight-ahead</div><div><b>Upper bound</b>baseline + K_SIGMA × spread</div></div></div></section>' +
          "</div>";
        App.refs.calib = refsOf(box);
      }
    }
    const r = App.refs.calib;
    if (!p || !r) return;
    const c = p.calibration || {}, hp = p.head_pose || {};
    if (!isCalibrated(p)) {
      setText(r.cp_samples, fmtInt(c.samples));
      setText(r.cp_required, fmtInt(c.required));
      setText(r.cp_progress, fmtPct((c.progress || 0) * 100));
      setText(r.cl_raw, fmtDeg(hp.raw_yaw));
      setText(r.cl_filt, fmtDeg(hp.filtered_yaw));
      setText(r.cl_mean, fmtDeg(c.running_mean));
      setText(r.cl_std, fmtDeg(c.std));
      return;
    }
    setText(r.cr_base, fmtDeg(c.baseline));
    setText(r.cr_std, fmtDeg(c.std));
    setText(r.cr_k, fmtNum(c.k_sigma, 2));
    setText(r.cr_thr, "±" + fmtDeg(c.threshold));
    setText(r.cr_lo, fmtDeg(c.lower_threshold));
    setText(r.cr_up, fmtDeg(c.upper_threshold));
    setText(r.cr_n, fmtInt(c.samples) + " / " + fmtInt(c.required));
    // The track shows twice the forward zone either side, so movement shows.
    const span = Math.max((c.upper_threshold - c.lower_threshold) || 1, 0.1);
    const lo = c.baseline - span, hi = c.baseline + span;
    const pos = function (v) { return Math.max(0, Math.min(100, ((v - lo) / (hi - lo)) * 100)); };
    setStyle(r.zone, "left", pos(c.lower_threshold) + "%");
    setStyle(r.zone, "width", (pos(c.upper_threshold) - pos(c.lower_threshold)) + "%");
    setStyle(r.base, "left", pos(c.baseline) + "%");
    setStyle(r.now, "left", pos(hp.filtered_yaw) + "%");
    toggle(r.now, "is-away", hp.filtered_yaw < c.lower_threshold || hp.filtered_yaw > c.upper_threshold);
    setText(r.lo, fmtDeg(c.lower_threshold));
    setText(r.mid, fmtDeg(c.baseline));
    setText(r.up, fmtDeg(c.upper_threshold));
  }

  // ═══════════════════════════════════════════════════════════════
  // DEVELOPER
  // ═══════════════════════════════════════════════════════════════
  function renderDeveloperPage(state) {
    renderTabs("dev-tabs", state);
    const box = $("dev-detail"), p = students(state)[App.selected];
    const shape = p ? App.selected + "|" + isCalibrated(p) : "none";
    if (App.shapes.dev !== shape) {
      App.shapes.dev = shape;
      if (!p) { box.innerHTML = ""; App.refs.dev = null; return; }
      box.innerHTML =
        '<section class="pane"><header class="pane-head"><div><p class="eyebrow">Pipeline</p><h2 class="pane-title">Every stage, this student, now</h2></div></header>' +
          '<div class="pipeline">' + PIPELINE.map(function (s, i) {
            return '<div class="pl-node" data-pl="' + i + '"><span class="k">' + esc(s) + '</span><span class="v">—</span></div>';
          }).join("") + "</div></section>" +
        '<section class="pane"><header class="pane-head"><div><p class="eyebrow">System</p><h2 class="pane-title">Engine</h2></div></header><div class="metrics">' +
          [["fps", "Engine FPS"], ["vis", "In view"], ["cal", "Calibrated"], ["time", "Session"], ["ev", "Looks away"], ["log", "Updates logged"]].map(function (m) {
            return '<div class="metric"><span class="eyebrow">' + m[1] + '</span><span class="num" data-ref="m_' + m[0] + '">—</span></div>';
          }).join("") + "</div></section>" +
        (isCalibrated(p) ?
          '<div class="grid-3" style="margin-top:18px">' +
            kvPane("Kinematics", "Motion", "dk", [["vel", "Velocity"], ["maxv", "Max velocity"], ["acc", "Acceleration"], ["vn", "Velocity norm"], ["an", "Accel norm"]]) +
            kvPane("Temporal", "Event engine", "dt", [["state", "State"], ["dur", "Current away"], ["win", "Events in window"], ["ws", "Window"], ["tot", "Total events"], ["away", "Total away"], ["ratio", "Away ratio (raw)"], ["fs", "Frequency score"], ["ds", "Duration score"], ["ss", "Session score"]]) +
            kvPane("Fusion", "Suspicion", "df", [["score", "Score"], ["peak", "Peak"], ["conf", "Confirmed time"], ["alpha", "EMA alpha"], ["cthr", "Confirmed threshold"], ["level", "Level"], ["verdict", "Verdict"]]) +
          "</div>" +
          '<div class="grid-2" style="margin-top:18px"><section class="pane"><header class="pane-head"><div><p class="eyebrow">Signals</p><h2 class="pane-title">Fusion inputs</h2></div></header><div class="bars" data-ref="bars"></div>' +
            '<p class="formula">probability = W_POSITION&middot;pos_score + W_VELOCITY&middot;vel_score + W_FREQUENCY&middot;freq_score + W_DURATION&middot;dur_score + W_SESSION&middot;sess_score, then smoothed by SUSPICION_EMA_ALPHA (suspicion.py).</p>' +
            // Where the away events came from. The score deliberately
            // mixes the three sources, so this is the only place that
            // answers "how much of it was the eyes?".
            '<div class="srcs" data-ref="srcs"></div></section>' +
          '<section class="pane"><header class="pane-head"><div><p class="eyebrow">Yaw</p><h2 class="pane-title">Kalman-smoothed, last 20 s</h2></div></header>' + graphHTML() + "</section></div>"
          : '<div class="state-note" style="margin-top:18px"><h3>Awaiting calibration</h3><p>Stage values fill in once ' + esc(nameOf(p)) + " is calibrated.</p></div>") +
        '<section class="pane" style="margin-top:18px"><details class="fold" data-ref="jsonFold"><summary><span><span class="eyebrow">Payload</span><span class="pane-title" style="display:block">Raw JSON for this student</span></span><span class="num muted" data-ref="jsonSize">—</span></summary>' +
          '<pre class="json" data-ref="json"></pre></details></section>' +
        '<section class="pane"><header class="pane-head"><div><p class="eyebrow">Health</p><h2 class="pane-title">Errors the engine swallowed</h2></div></header><div data-ref="faults"></div></section>' +
        '<section class="pane"><header class="pane-head"><div><p class="eyebrow">Stream</p><h2 class="pane-title">Last updates received</h2></div></header><ul class="devlog" data-ref="devlog"></ul></section>';
      App.refs.dev = refsOf(box);
      if (App.refs.dev.bars) App.refs.dev.barRows = buildBars(App.refs.dev.bars, true);
      if (App.refs.dev.graphCanvas) bindGraph(App.refs.dev);
    }
    const r = App.refs.dev;
    if (!r) return;
    const sys = state.system || {}, cfg = state.config || {}, cal = isCalibrated(p);
    const hp = p.head_pose || {}, t = p.temporal || {}, s = p.suspicion || {};
    const vals = [
      p.visible ? "Active" : "Idle", fmtDeg(hp.raw_yaw), p.visible ? "Filtered" : "—", fmtDeg(hp.filtered_yaw),
      cal ? "z " + fmtNum(hp.z_score, 2) : "—", cal ? hp.state : "Calibrating",
      cal && t.currently_away ? "Away " + fmtSecs(t.current_away_duration) : cal ? "None" : "—",
      cal ? fmtInt(t.events_window) + " in window" : "—", cal ? fmtPct((t.session_score || 0) * 100) : "—",
      cal ? fmtNum(s.score, 1) : "—", cal ? s.level : "—", verdictOf(p) || "—",
    ];
    box.querySelectorAll("[data-pl]").forEach(function (n) {
      const v = String(vals[+n.dataset.pl]);
      setText(n.querySelector(".v"), v);
      toggle(n, "is-on", v !== "—" && v !== "None" && v !== "Idle");
    });
    setText(r.m_fps, fmtNum(sys.fps, 1));
    setText(r.m_vis, fmtInt(sys.visible_students) + " / " + fmtInt(sys.max_students));
    setText(r.m_cal, fmtInt(sys.calibrated_students));
    setText(r.m_time, fmtClock(sys.session_time));
    setText(r.m_ev, cal ? fmtInt(t.total_events) : "—");
    setText(r.m_log, String(App.devLog.length));

    // Errors a per-frame except caught and carried on from. Signature-
    // checked before rebuilding, like every other panel here.
    if (r.faults) {
      const flt = sys.faults;
      const fsig = flt ? [flt.failed, flt.subsystem, flt.distinct].join("-") : "off";
      if (r._fsig !== fsig) {
        r._fsig = fsig;
        if (!flt) {
          r.faults.innerHTML = '<p class="fine">Not tracked in this mode.</p>';
        } else if (!flt.failed && !flt.subsystem) {
          r.faults.innerHTML = '<p class="fine ok-note">No frame raised an error this session. ' +
            fmtInt(flt.frames) + ' frames processed.</p>';
        } else {
          r.faults.innerHTML =
            '<p class="' + (flt.degraded ? "fault-head is-bad" : "fault-head") + '">' +
              (flt.degraded ? "Run degraded — " : "") +
              fmtInt(flt.failed) + ' of ' + fmtInt(flt.frames) + ' frames lost (' +
              Math.round(flt.fraction * 1000) / 10 + '%)' +
              (flt.subsystem ? ', ' + fmtInt(flt.subsystem) + ' subsystem errors' : '') +
            '</p>' +
            (flt.degraded ? '<p class="fine">Enough frames failed that the figures on this ' +
              'dashboard may be missing most of the session.</p>' : '') +
            '<ul class="fault-list">' + (flt.faults || []).map(function (q) {
              return '<li><span class="fault-n">' + fmtInt(q.count) + '&times;</span>' +
                '<code>' + esc(q.signature) + '</code>' +
                '<span class="fine">frames ' + q.first_frame + '–' + q.last_frame + '</span></li>';
            }).join("") + '</ul>';
        }
      }
    }

    // Where the away events came from. Rebuilt only when the counts
    // change — this panel updates on the ~8 Hz stream and rebuilding it
    // every tick is the flicker bug this project has hit before.
    if (r.srcs) {
      const src = (t && t.event_sources) || null;
      const ssig = src ? [src.head, src.down, src.eyes].join("-") : "none";
      if (r._srcSig !== ssig) {
        r._srcSig = ssig;
        if (!src || !src.total) {
          r.srcs.innerHTML = '<p class="fine">No away events yet. When there are, this says which signal raised each one.</p>';
        } else {
          const parts = [
            ["head", "Head turned", src.head, "yaw past K_SIGMA x std"],
            ["down", "Looked down", src.down, "pitch past PITCH_K_SIGMA and PITCH_MIN_DOWN_DEG"],
            ["eyes", "Eyes aside", src.eyes, "eye offset past EYE_K_SIGMA, head still forward"],
          ];
          r.srcs.innerHTML = '<p class="eyebrow">Where the ' + plural(src.total, "away event") + ' came from</p>' +
            parts.map(function (q) {
              const pct = src.total ? Math.round(100 * q[2] / src.total) : 0;
              return '<div class="src-row' + (q[2] ? "" : " is-zero") + '"><span class="src-k">' + esc(q[1]) +
                '</span><span class="src-n">' + q[2] + '</span>' +
                '<div class="src-bar"><i style="width:' + pct + '%"></i></div>' +
                '<span class="src-why">' + esc(q[3]) + "</span></div>";
            }).join("") +
            '<p class="fine">An eye glance is routed through the same five signals above, exactly as a head turn is — it has no separate weight. This is the only place that separates them.</p>';
        }
      }
    }
    if (cal) {
      const k = p.kinematics || {};
      setText(r.dk_vel, fmtNum(k.velocity, 1, "°/s")); setText(r.dk_maxv, fmtNum(k.max_velocity, 1, "°/s"));
      setText(r.dk_acc, fmtNum(k.acceleration, 1)); setText(r.dk_vn, fmtNum(k.velocity_norm, 3)); setText(r.dk_an, fmtNum(k.accel_norm, 3));
      setText(r.dt_state, t.current_state || "—"); setText(r.dt_dur, fmtSecs(t.current_away_duration));
      setText(r.dt_win, fmtInt(t.events_window)); setText(r.dt_ws, fmtInt(t.window_sec, " s")); setText(r.dt_tot, fmtInt(t.total_events));
      setText(r.dt_away, fmtSecs(t.total_away_time)); setText(r.dt_ratio, fmtPct((t.away_ratio_raw || 0) * 100));
      setText(r.dt_fs, fmtPct((t.frequency_score || 0) * 100)); setText(r.dt_ds, fmtPct((t.duration_score || 0) * 100));
      setText(r.dt_ss, fmtPct((t.session_score || 0) * 100));
      setText(r.df_score, fmtNum(s.score, 1)); setText(r.df_peak, fmtNum(s.peak, 1)); setText(r.df_conf, fmtSecs(s.confirmed_secs));
      setText(r.df_alpha, fmtNum(cfg.ema_alpha, 3)); setText(r.df_cthr, fmtNum(cfg.confirmed_threshold, 0)); setText(r.df_level, s.level || "—");
      setText(r.df_verdict, p.verdict ? p.verdict.label + " (" + p.verdict.evidence + "/4)" : "—");
      updateBars(r.barRows, p);
      drawYawGraph(r.graphCanvas, p);
      drawEyePanel(r, p);
    }
    const json = JSON.stringify(p, null, 2);
    setText(r.jsonSize, (json.length / 1024).toFixed(1) + " KB");
    if (r.jsonFold.open) setHTML(r.json, highlightJSON(json));
    if (performance.now() - (r._logAt || 0) > 500) {
      r._logAt = performance.now();
      r.devlog.innerHTML = App.devLog.slice(-30).reverse().map(function (e) {
        return '<li><span class="t">' + esc(e.t) + '</span><span class="f">' + fmtNum(e.fps, 0) + ' fps</span><span>' + esc(e.msg) + "</span></li>";
      }).join("");
    }
  }
  function highlightJSON(json) {
    return esc(json).replace(/(&quot;(?:\\u[a-fA-F0-9]{4}|\\[^u]|(?!&quot;)[^\\])*&quot;)(\s*:)?|\b(true|false|null)\b|-?\d+(?:\.\d*)?(?:[eE][+\-]?\d+)?/g, function (m, str, colon, lit) {
      const cls = str ? (colon ? "k" : "s") : lit ? (lit === "null" ? "null" : "b") : "n";
      return '<span class="' + cls + '">' + m + "</span>";
    });
  }

  // ═══════════════════════════════════════════════════════════════
  // VALIDATION — clips of known behaviour, replayed and compared with
  // their labels (validation.py). Not part of the live stream: loaded
  // when the page opens, polled only while a check is running.
  // ═══════════════════════════════════════════════════════════════
  const VERDICT_ORDER = ["CALIBRATING", "OK", "WATCH", "ALERT", "FLAG"];
  function fmtWhen(iso) {
    if (!iso) return "—";
    const d = new Date(iso);
    return isNaN(d) ? iso : d.toLocaleString("en-GB", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
  }
  function plural(n, word) { return n + " " + word + (n === 1 ? "" : "s"); }
  function loadValidation() {
    api("/api/validation").then(function (out) {
      if (!out.ok) { setText($("val-line"), out.j.error || "Could not load validation results."); return; }
      renderValidation(out.j);
    });
  }
  function scheduleValidationPoll(running) {
    clearTimeout(App.valPoll);
    if (running && App.page === "validation") App.valPoll = setTimeout(loadValidation, 1500);
  }
  function renderValidation(d) {
    const wasRunning = App.val && App.val.job && App.val.job.state === "running";
    App.val = d;
    const latest = d.latest, job = d.job || {}, running = job.state === "running";
    scheduleValidationPoll(running);
    if (wasRunning && !running && job.state === "done") App.wf = null;   // new batch: old what-if is stale

    // Headline
    let line, meta = "";
    if (!d.clips.length) {
      line = "No clips recorded yet.";
      meta = "Record the suggested clips below, about two minutes each, then run the check here.";
    } else if (!latest) {
      line = cap(countWord(d.clips.length)) + " clip" + (d.clips.length === 1 ? "" : "s") + " recorded. The check has not been run yet.";
      meta = "It replays every clip through the engine; allow about as long as the clips themselves.";
    } else {
      const c = latest.counts, sus = c.caught + c.missed, inn = c.false_alarm + c.cleared;
      const parts = [];
      if (sus) parts.push("it flagged " + c.caught + " of the " + sus + " recorded as cheating");
      if (inn) parts.push(c.false_alarm + " of the " + inn + " recorded as normal");
      line = "On " + plural(sus + inn, "labelled clip") + ", " + (parts.join(", and ") || "nothing was labelled") + ".";
      const s = latest.settings || {};
      meta = "Checked " + fmtWhen(latest.created) + (isNum(s.k_sigma) ? " · K_SIGMA " + s.k_sigma : "") +
        (c.errors ? " · " + plural(c.errors, "clip") + " could not be analysed" : "") +
        " · Staged clips of one person: this shows behaviour on this footage, not general accuracy.";
    }
    setText($("val-line"), line);
    setText($("val-meta"), meta);
    setText($("val-eyebrow"), latest ? "Latest check · " + latest.id.replace("batch_", "") : "Latest check");

    // Run button + progress
    const btn = $("val-run-btn");
    btn.disabled = running || !d.clips.length;
    setText(btn, running ? "Checking…" : latest ? "Run the check again" : "Run the check");
    if (!App.valNote) setText($("val-run-note"), d.clips.length ? plural(d.clips.length, "clip") + " in the clips folder" : "");
    $("val-run-note").title = d.clips_dir || "";
    show($("val-progress"), running);
    if (running) {
      const done = Math.max(0, (job.i || 1) - 1), n = job.n || 1;
      setStyle($("val-progress-bar"), "width", Math.max(3, (done / n) * 100) + "%");
      setText($("val-progress-text"), "Replaying clip " + (job.i || 1) + " of " + n + (job.clip ? " · " + job.clip : "") +
        " · " + fmtDur(job.elapsed_sec || 0) + " so far");
    }
    if (job.state === "failed") setText($("val-run-note"), "The last check failed: " + String(job.error || "").split("\n").filter(Boolean).slice(-1)[0]);

    renderMarking(latest);
    const stale = [];
    if (latest && latest.clips_now_not_in_batch.length) stale.push(plural(latest.clips_now_not_in_batch.length, "clip") + " recorded since this check (" + latest.clips_now_not_in_batch.join(", ") + "). Run it again to include them.");
    if (latest && latest.rows.some(function (r) { return "flagged" in r && !r.has_inputs; })) stale.push("This check was run before scoring inputs were saved, so What if cannot use it. Run the check again.");
    setText($("val-stale"), stale.join(" "));
    show($("val-stale"), stale.length > 0);
    renderValTable(latest);
    renderWhatifForm(d);
    renderPlan(d);
    renderValHistory(d);
  }
  function renderMarking(latest) {
    const grid = $("val-grid");
    if (!latest) { grid.innerHTML = ""; return; }
    const by = { caught: [], missed: [], false_alarm: [], cleared: [] };
    latest.rows.forEach(function (r) {
      if (!("flagged" in r)) return;
      const oc = r.label === "suspicious" ? (r.flagged ? "caught" : "missed") : r.label === "innocent" ? (r.flagged ? "false_alarm" : "cleared") : null;
      if (oc) by[oc].push(r.clip.replace(/\.[^.]+$/, ""));
    });
    const cell = function (key, word, cls) {
      const list = by[key];
      return '<div class="mk-cell ' + cls + (list.length ? "" : " is-empty") + '"><div class="mk-n"><span class="num">' + list.length +
        '</span><span class="w">' + word + '</span></div><div class="mk-clips">' +
        list.map(function (c) { return '<span class="mk-clip">' + esc(c) + "</span>"; }).join("") + "</div></div>";
    };
    grid.innerHTML =
      '<div class="mk-h"></div><div class="mk-h"><span class="eyebrow">Flagged for review</span></div><div class="mk-h"><span class="eyebrow">Not flagged</span></div>' +
      '<div class="mk-row"><b>Cheating</b><span class="fine">recorded as</span></div>' +
      cell("caught", "caught", "") + cell("missed", "missed", "is-wrong") +
      '<div class="mk-row"><b>Normal</b><span class="fine">recorded as</span></div>' +
      cell("false_alarm", "wrongly flagged", "is-wrong") + cell("cleared", "correctly left alone", "is-cleared");
  }
  function verdictBar(spv) {
    const total = VERDICT_ORDER.reduce(function (a, v) { return a + (spv[v] || 0); }, 0);
    if (!total) return '<div class="vbar"></div>';
    return '<div class="vbar">' + VERDICT_ORDER.filter(function (v) { return spv[v]; }).map(function (v) {
      return '<span class="vseg" data-v="' + v + '" style="width:' + ((spv[v] / total) * 100).toFixed(2) + '%" title="' +
        v + ": " + fmtSecs(spv[v]) + '"></span>';
    }).join("") + "</div>";
  }
  function renderValTable(latest) {
    const box = $("val-table");
    if (!latest) { box.innerHTML = '<div class="empty">Results appear here after the first check.</div>'; return; }
    box.innerHTML = '<div class="vrow is-head"><span class="eyebrow">Clip</span><span class="eyebrow">Recorded as</span>' +
      '<span class="eyebrow">Time at each verdict</span><span class="eyebrow">Result</span></div>' +
      latest.rows.map(function (r) {
        const err = !("flagged" in r);
        const match = r.outcome === "matches label";
        const v = err ? "ERROR" : r.label ? (match ? "MATCH" : "DIFFERS") : "OK";
        const facts = err ? "The engine could not analyse this clip." :
          "highest " + r.highest_verdict + " · peak " + fmtNum(r.peak_score, 0) + "% · " + plural(r.events, "look") + " away" +
          (r.face_seen_pct < 90 ? " · face seen " + fmtNum(r.face_seen_pct, 0) + "%" : "") +
          (r.patterns && r.patterns.length ? " · " + r.patterns.slice(0, 2).join(", ") : "");
        // How much of the clip had no readable face. Shown on its own
        // line, not folded into the facts above, because "not flagged"
        // on a clip the engine mostly could not read means something
        // completely different from "not flagged" on one it could.
        const unm = r.unmeasured_sec || 0;
        const blind = unm > 0 ? '<span class="vblind">Could not read a face for ' +
          fmtNum(unm, 0) + "s of " + fmtNum(r.footage_sec || 0, 0) + "s" +
          (r.longest_unmeasured_sec ? " · longest " + fmtNum(r.longest_unmeasured_sec, 0) + "s" : "") +
          "</span>" : "";
        return '<div class="vrow"><div class="vclip"><b title="' + esc(r.clip) + '">' + esc(r.clip.replace(/\.[^.]+$/, "")) + "</b>" +
          (r.note ? "<span>" + esc(r.note) + "</span>" : "") + "</div>" +
          '<span class="vlabel' + (r.label === "suspicious" ? " is-suspicious" : "") + '">' + esc(r.label === "suspicious" ? "Cheating" : r.label === "innocent" ? "Normal" : "No label") + "</span>" +
          '<div class="vbar-wrap">' + verdictBar(r.seconds_per_verdict || {}) + '<span class="vfacts">' + esc(facts) + "</span>" + blind + "</div>" +
          '<div class="vout"><span class="stamp" data-v="' + v + '">' + (v === "MATCH" ? "As recorded" : v === "DIFFERS" ? "Differs" : v === "ERROR" ? "Error" : "—") + "</span>" +
          '<span class="fine">' + (err ? "" : r.flagged ? "flagged for review" : "not flagged") + "</span></div></div>";
      }).join("");
  }
  // What-if form: built once, so typing is never interrupted by a reload.
  function renderWhatifForm(d) {
    const sel = $("wf-setting");
    if (!App.built.whatif) {
      sel.innerHTML = d.tunable.map(function (t) {
        return '<option value="' + esc(t.name) + '">' + esc(t.label) + "</option>";
      }).join("");
      sel.addEventListener("change", function () { fillWhatif(true); });
      $("wf-run").addEventListener("click", runWhatif);
      App.built.whatif = true;
      fillWhatif(true);
    }
    const hasInputs = d.latest && d.latest.rows.some(function (r) { return r.has_inputs; });
    $("wf-run").disabled = !hasInputs || !!App.wfBusy;
    if (!hasInputs && !App.wf) {
      $("wf-result").innerHTML = '<div class="empty">' + (d.latest ? "Run the check again to save the numbers this needs." : "Available after the first check.") + "</div>";
    }
  }
  function fillWhatif(resetValues) {
    const t = (App.val.tunable || []).find(function (x) { return x.name === $("wf-setting").value; });
    if (!t) return;
    if (resetValues) $("wf-values").value = t.suggest.join(", ");
    setText($("wf-help"), t.help + " Today: " + t.current + " (" + t.name + ").");
  }
  function runWhatif() {
    const d = App.val;
    if (!d || !d.latest) return;
    const setting = $("wf-setting").value;
    const values = $("wf-values").value.split(/[\s,;]+/).filter(Boolean);
    App.wfBusy = true;
    $("wf-run").disabled = true;
    setText($("wf-run"), "Working…");
    api("/api/validation/whatif", { body: { batch: d.latest.id, setting: setting, values: values } }).then(function (out) {
      App.wfBusy = false;
      $("wf-run").disabled = false;
      setText($("wf-run"), "Try it");
      if (!out.ok) { $("wf-result").innerHTML = '<p class="auth-error" style="margin-top:14px">' + esc(out.j.error || "The what-if failed.") + "</p>"; return; }
      App.wf = out.j;
      renderWhatifResult(out.j);
    });
  }
  function renderWhatifResult(r) {
    const s = r.sweep;
    const labels = {};
    (App.val.latest ? App.val.latest.rows : []).forEach(function (row) { labels[row.clip] = row.label; });
    const cols = [{ head: "Today (" + s.current_value + ")", ev: r.current, today: true }].concat(s.points.map(function (p) {
      return { head: String(p.value), ev: p, today: String(p.value) === String(s.current_value) };
    }));
    const sus = r.current.counts.caught + r.current.counts.missed, inn = r.current.counts.false_alarm + r.current.counts.cleared;
    const fid = r.fidelity_ok
      ? '<p class="wf-fidelity"><span class="num">✓</span><span>Re-scoring with the recorded settings reproduces the engine\'s own result on all ' + r.clips_with_inputs + " clips, so the columns below are the engine's answer at each value.</span></p>"
      : '<p class="wf-fidelity is-bad"><span class="num">!</span><span>Re-scoring did not reproduce the engine on: ' + esc(r.fidelity.filter(function (f) { return !f.reproduced; }).map(function (f) { return f.clip; }).join(", ")) + ". Treat these columns as approximate.</span></p>";
    let html = fid + '<table class="wf-map"><thead><tr><th>' + esc(s.setting) + "</th>" +
      cols.map(function (c) { return '<th class="' + (c.today ? "is-today" : "") + '">' + esc(c.head) + "</th>"; }).join("") + "</tr></thead><tbody>";
    html += '<tr class="wf-sum"><td>Caught (of ' + sus + " cheating)</td>" + cols.map(function (c) {
      return '<td class="' + (c.today ? "is-today" : "") + '">' + c.ev.counts.caught + "</td>"; }).join("") + "</tr>";
    html += '<tr class="wf-sum"><td>Wrongly flagged (of ' + inn + " normal)</td>" + cols.map(function (c) {
      return '<td class="' + (c.today ? "is-today" : "") + '">' + c.ev.counts.false_alarm + "</td>"; }).join("") + "</tr>";
    r.current.rows.forEach(function (row, k) {
      html += '<tr><td title="' + esc(row.clip) + '">' + esc(row.clip.replace(/\.[^.]+$/, "")) + "</td>" + cols.map(function (c) {
        const x = c.ev.rows[k], lab = labels[row.clip];
        const cls = x.flagged ? "pip is-flag" + (lab === "innocent" ? " is-wrong" : "") : "pip" + (lab === "suspicious" ? " is-miss" : "");
        return '<td class="' + (c.today ? "is-today" : "") + '"><span class="' + cls + '" title="' + (x.flagged ? "flagged, highest " + x.highest_verdict : "not flagged") + '"></span></td>';
      }).join("") + "</tr>";
    });
    html += "</tbody></table>" + '<p class="fine" style="margin-top:10px">Filled dot: flagged for review (red when the clip was recorded as normal). Red ring: a clip recorded as cheating that was not flagged.</p>';
    $("wf-result").innerHTML = html;
  }
  function renderPlan(d) {
    const done = d.plan.filter(function (p) { return p.recorded; }).length;
    setText($("plan-status"), done + " of " + d.plan.length + " recorded");
    const extra = d.clips.filter(function (c) {
      return !d.plan.some(function (p) { return p.name + ".mp4" === c.file; });
    });
    $("val-plan").innerHTML = d.plan.map(function (p, k) {
      return '<li class="' + (p.recorded ? "is-done" : "") + '"><span class="todo-box">' + (k + 1) + "</span><div><b>" + esc(p.name) +
        '<span class="lab">' + (p.label === "suspicious" ? "cheating" : "normal") + "</span></b>" +
        '<span class="fine">' + esc(p.what) + "</span>" +
        (p.recorded ? "" : '<span class="cmd"><code>' + esc(p.command) + '</code><button type="button" class="btn btn-quiet btn-sm" data-copy="' + esc(p.command) + '">Copy</button></span>') +
        "</div></li>";
    }).join("") + (extra.length ? '<li><span class="todo-box">+</span><div><b>Also recorded</b><span class="fine">' +
      esc(extra.map(function (c) { return c.file; }).join(", ")) + "</span></div></li>" : "");
  }
  function renderValHistory(d) {
    const latest = d.latest;
    $("val-changes").innerHTML = latest ? latest.changes.map(function (c) { return "<li>" + esc(c) + "</li>"; }).join("")
      : '<li class="muted">Nothing yet.</li>';
    if (!d.batches.length) { $("val-history").innerHTML = '<p class="fine">No checks yet.</p>'; return; }
    $("val-history").innerHTML = '<table class="hist"><thead><tr><th>When</th><th>Caught</th><th>Missed</th><th>Wrongly flagged</th><th>Left alone</th><th>K_SIGMA</th></tr></thead><tbody>' +
      d.batches.map(function (b) {
        const c = b.counts;
        return "<tr><td>" + esc(fmtWhen(b.created)) + "</td><td>" + c.caught + '</td><td class="' + (c.missed ? "bad" : "") + '">' + c.missed +
          '</td><td class="' + (c.false_alarm ? "bad" : "") + '">' + c.false_alarm + "</td><td>" + c.cleared + "</td><td>" +
          esc(String((b.settings || {}).k_sigma != null ? b.settings.k_sigma : "—")) + "</td></tr>";
      }).join("") + "</tbody></table>";
  }
  function runValidation() {
    const btn = $("val-run-btn");
    btn.disabled = true;
    App.valNote = true;
    setText($("val-run-note"), "Starting…");
    api("/api/validation/run", { method: "POST" }).then(function (out) {
      App.valNote = !out.ok;
      setText($("val-run-note"), out.ok ? "" : (out.j.error || "Could not start the check."));
      if (!out.ok) btn.disabled = false;
      loadValidation();
    });
  }

  // ═══════════════════════════════════════════════════════════════
  // EVENTS & KEYBOARD
  // ═══════════════════════════════════════════════════════════════
  function bindUi() {
    window.addEventListener("hashchange", function () { applyPage(pageFromHash()); });
    $("theme-btn").addEventListener("click", toggleTheme);
    $("account-btn").addEventListener("click", function (e) { e.stopPropagation(); setMenu($("account-menu").hidden); });
    document.addEventListener("click", function (e) { if (!e.target.closest(".account")) setMenu(false); });
    $("mock-toggle").addEventListener("click", function () { setMenu(false); toggleMode(); });
    $("signout-btn").addEventListener("click", signOut);
    $("shortcuts-btn").addEventListener("click", function () { setMenu(false); setShortcuts(true); });
    $("shortcuts-scrim").addEventListener("click", function () { setShortcuts(false); });
    $("scrim").addEventListener("click", closeInspector);
    $("val-run-btn").addEventListener("click", runValidation);

    document.addEventListener("click", function (e) {
      if (e.target.closest("[data-identity-alert-dismiss]")) { show($("identity-alert"), false); return; }
      const yes = e.target.closest("[data-review-confirm]");
      if (yes) { answerReview(parseInt(yes.dataset.reviewConfirm, 10), true, yes); return; }
      const no = e.target.closest("[data-review-reject]");
      if (no) { answerReview(parseInt(no.dataset.reviewReject, 10), false, no); return; }
      if (e.target.closest("[data-insp-close]")) { closeInspector(); return; }
      if (e.target.closest("[data-insp-full]")) { const l = App.inspecting; closeInspector(); App.selected = l; goToPage("students"); return; }
      if (e.target.closest("[data-insp-room]")) {
        const l = App.inspecting;
        closeInspector();
        if (window.DigitalTwin && window.DigitalTwin.select) window.DigitalTwin.select(l);
        const room = document.querySelector('[data-twin-root="live"]');
        if (room) room.scrollIntoView({ behavior: "smooth", block: "center" });
        return;
      }
      const ib = e.target.closest("#inspector [data-break-for]");
      if (ib) { toggleBreak(ib.dataset.breakFor, ib); return; }
      const save = e.target.closest("[data-roster-save]");
      if (save && !save.disabled) { saveRosterRow(parseInt(save.dataset.rosterSave, 10)); return; }
      const rem = e.target.closest("[data-roster-remove]");
      if (rem && !rem.disabled) { postRoster({ action: "unregister", slot: parseInt(rem.dataset.rosterRemove, 10) }); return; }
      const lock = e.target.closest("#roster-lock-btn");
      if (lock && !lock.disabled) { postRoster({ action: lock.classList.contains("is-locked") ? "unlock" : "lock" }); return; }
      const cap = e.target.closest("[data-roster]");
      if (cap && !cap.disabled) {
        const v = cap.dataset.roster;
        api("/api/roster", { body: { count: v === "auto" ? null : parseInt(v, 10) } });
        return;
      }
      const cp = e.target.closest("[data-copy]");
      if (cp) {
        const txt = cp.dataset.copy;
        const done = function () { setText(cp, "Copied"); setTimeout(function () { setText(cp, "Copy"); }, 1500); };
        if (navigator.clipboard) navigator.clipboard.writeText(txt).then(done, function () {});
        return;
      }
      const lf = e.target.closest("[data-log]");
      if (lf) {
        App.logFilter = lf.dataset.log;
        $("log-filter").querySelectorAll("[data-log]").forEach(function (b) { toggle(b, "is-active", b === lf); });
        App.sigs.log = null;
        if (App.state) renderLog(App.state);
      }
    });
    document.addEventListener("keydown", function (e) {
      if (e.key === "Enter" && e.target.matches(".roster-name, .roster-roll") && !e.target.disabled) {
        saveRosterRow(parseInt(e.target.dataset.seat, 10));
        return;
      }
      if (e.target.closest("input, textarea, select, [contenteditable]") || e.ctrlKey || e.metaKey || e.altKey) return;
      if (e.key === "Escape") { closeInspector(); setShortcuts(false); setMenu(false); return; }
      if (e.key === "?") { setShortcuts($("shortcuts").hidden); return; }
      if (e.key === "t" || e.key === "T") { toggleTheme(); return; }
      if (/^[1-4]$/.test(e.key) && App.state) {
        const order = seatOrder(App.state).filter(function (l) { return seatOnPlan(App.state, l); });
        // On Live, keys follow the seats as they appear left to right; on
        // the per-student pages they pick the seat number.
        const label = App.page === "live" ? order[+e.key - 1] : "S" + e.key;
        if (!label || !students(App.state)[label]) return;
        if (App.page === "live") openInspector(label, true);
        else if (App.page === "students" || App.page === "calibration" || App.page === "developer") { App.selected = label; renderAll(); }
      }
    });
    // Selecting a head in the 3D room highlights that seat's card.
    document.addEventListener("twin:select", function (e) {
      if (!e.detail || e.detail.instance !== "live") return;
      document.querySelectorAll(".seat").forEach(function (c) { toggle(c, "is-selected", c.dataset.seat === e.detail.label); });
    });
    let rt;
    window.addEventListener("resize", function () {
      clearTimeout(rt);
      rt = setTimeout(function () { App.lastChartAt = 0; if (App.state) renderAll(); }, 150);
    });
  }
  function setShortcuts(on) {
    show($("shortcuts"), on);
    show($("shortcuts-scrim"), on);
  }

  // ── Start ──────────────────────────────────────────────────────
  document.addEventListener("DOMContentLoaded", function () {
    bindUi();
    applyPage(pageFromHash());
    loadAccount();
    refreshMode();
    setInterval(refreshMode, 10000);
    setInterval(function () { if (App.state) renderMast(App.state); }, 1000);
    connectStream();
  });
})();

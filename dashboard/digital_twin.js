/**
 * digital_twin.js — 3D EXAM ROOM ("digital twin").
 *
 * WHY THIS EXISTS
 * ───────────────────────────────────────────────────────────────────
 * "Yaw -7.4, baseline -0.6" means nothing at a glance; a room of heads
 * that visibly turn does. Every student in view sits at their own desk
 * and their head turns to the yaw and pitch the engine measured, relative
 * to THEIR OWN calibration-time straight-ahead. The selected student's
 * engine thresholds are drawn as shaded fans, so a head leaving a fan and
 * the engine saying AWAY are the same event. Links the correlation layer
 * finds BETWEEN students (mutual glancing, one-way copying) are drawn as a
 * beam between their heads — always with its "correlation is not proof"
 * caveat beside it, never on its own.
 *
 * HISTORY
 *  - Sep 2026: single-avatar stage, so one person could test it alone.
 *  - 17 Sep 2026: a real-camera log showed "straight" drifting and the
 *    drawing far too sensitive. Now drawn against the calibration-time
 *    baseline with a smaller gain, a dead zone and slower smoothing.
 *  - Sep 2026: the whole room (up to 4 students), correlation beams, and
 *    INSTANCES: the Live Monitor owns one, the Reports page's session
 *    replay another (session_replay.js). Same code drives both.
 *  - Sep 2026 redesign ("Paper & Ink", see styles.css): every colour now
 *    comes from the --room-* tokens, so the room is a light clay model in
 *    the Paper theme and a dark one at Night, and switches live
 *    (DigitalTwin.retheme). gazeView() exports the exact head mapping, so
 *    the gaze dials on the seat cards draw what this room draws.
 *
 * RULES
 * ───────────────────────────────────────────────────────────────────
 *  - ZERO INTELLIGENCE. Every angle, baseline, threshold, state and link
 *    comes verbatim from the payload. Nothing here decides "away".
 *  - NO SECOND CONNECTION. dashboard.js holds the one SSE stream and calls
 *    DigitalTwin.update(); a replay instance is fed by session_replay.js.
 *  - RENDER ONLY WHEN SEEN. Each instance sleeps while its page is hidden,
 *    it is scrolled away or the tab is hidden, and stops once nothing is
 *    moving — an idle room costs nothing.
 *  - FAILURE IS CONTAINED. No WebGL or no Three.js: the panel says so and
 *    the numeric readout still works; nothing else on the page is touched.
 *  - BUILD ONCE, UPDATE IN PLACE. Buttons and labels are rebuilt only when
 *    what they list changes, never on the ~8 Hz stream tick — rebuilding
 *    on every tick is the bug that once made dashboard buttons unclickable.
 *
 * SIGN CONVENTIONS (checked, not assumed)
 *  - Yaw: CalibrationEngine.is_away() calls positive (yaw - baseline)
 *    "Right"; a synthetic probe of extract_head_pose() on the mirrored
 *    frame agrees (nose toward image-right gives positive yaw).
 *  - Pitch: which sign is "down" is Config.PITCH_DOWN_SIGN, sent as
 *    pitch.down_sign, so flipping that flag flips the avatars too.
 *
 * SEAT ORDER
 *  Desks run left to right in the order students appear in the mirrored
 *  camera feed (identity[slot].cx), so the Camera view matches the video.
 *  Only the ORDER is real: the engine measures no distances, so desk
 *  spacing is illustrative.
 */
(function () {
  "use strict";

  // ── Display constants (presentation only) ─────────────────────────
  // Local copy first so the view works offline in an exam hall; public
  // CDN second. r149: last release with a classic build/three.min.js.
  const THREE_SOURCES = [
    "vendor/three.min.js",
    "https://cdn.jsdelivr.net/npm/three@0.149.0/build/three.min.js",
  ];

  // Engine head-pose angles are NOT true degrees (head_pose.py scales
  // RQDecomp3x3's output, and the scale depends on MediaPipe's depth
  // estimate), so they are drawn enlarged. First 6, from a synthetic
  // probe; a real 22-minute session (17 Sep 2026) showed that was far too
  // sensitive — the engine rated the student OK on every frame yet the
  // avatar sat >15° off-centre a third of the time. 3 puts the default
  // away threshold (2.3 × 3.5 ≈ 8 units) near 24°. Still provisional until
  // real head angles are read against the "From straight" numbers. Fans go
  // through the same mapping (drawDeg), so the head still crosses a fan
  // edge exactly when the engine's threshold is crossed.
  const ANGLE_GAIN = 3.0;
  // Offsets this small (engine units) are squeezed toward zero so heads
  // hold still through ordinary sitting wobble (~0.3 units peak-to-peak
  // per second in that real log).
  const DEAD_ZONE = 0.35;
  // Clamps stay above threshold × gain so a head can always visibly cross
  // its own fan edge.
  const YAW_CLAMP_DEG = 85;
  const PITCH_CLAMP_DEG = 80;
  const PITCH_FAN_UP_DEG = 22;   // how far above level the pitch fan reaches
  const GAUGE_HALF_RANGE = 12;   // engine units, centre to gauge edge
  const HEAD_SMOOTHING = 5;      // ~200 ms to follow the 8 Hz stream
  const SEATS = ["S1", "S2", "S3", "S4"];
  const DESK_SPACING = 1.7;      // metres between desks (illustrative)
  const EYE_Y = 1.32, EYE_Z = 0.02;  // eye point relative to a seat's root
  const LABEL_Y = 1.72;              // name label anchor above each head
  const FAN_R = 0.62;
  const BEAM_LEN = 0.7;

  const VIEWS = {
    // From the webcam's position, whole room, mirrored like the live feed.
    camera: { az: 0.0, el: 0.2, minDist: 3.2, mirror: true, room: true },
    // From behind and above the room: unmirrored, still left-is-left.
    room:   { az: Math.PI + 0.55, el: 0.6, minDist: 4.2, mirror: false, room: true },
    // Close-up on the selected student, from the webcam side.
    focus:  { az: 0.0, el: 0.24, dist: 2.45, mirror: true, room: false },
  };

  const deg = function (d) { return (d * Math.PI) / 180; };
  const clamp = function (v, lo, hi) { return Math.max(lo, Math.min(hi, v)); };
  const num = function (v) { return typeof v === "number" && Number.isFinite(v); };

  function cssVar(name, fallback) {
    const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return /^#[0-9a-f]{3,8}$/i.test(v) ? v : fallback;
  }
  // Colours come from the dashboard's design tokens, never invented here.
  function readPalette() {
    const mode = getComputedStyle(document.documentElement).getPropertyValue("--room-mode").trim();
    return {
      dark:     mode !== "light",
      bg:       cssVar("--room-bg", "#151412"),
      floor:    cssVar("--room-floor", "#1a1916"),
      grid:     cssVar("--room-grid", "#26241f"),
      grid2:    cssVar("--room-grid-2", "#36332c"),
      desk:     cssVar("--room-desk", "#37342d"),
      metal:    cssVar("--room-metal", "#2b2924"),
      skin:     cssVar("--room-skin", "#d1c9b8"),
      cloth:    cssVar("--room-cloth", "#4a463d"),
      visor:    cssVar("--room-visor", "#0b0a09"),
      paper:    cssVar("--room-paper", "#e9e3d5"),
      forward:  cssVar("--room-forward", "#93a6ff"),
      away:     cssVar("--room-away", "#ff7a5e"),
      fan:      cssVar("--room-fan", "#93a6ff"),
      link:     cssVar("--room-link", "#93a6ff"),
      accent:   cssVar("--blue", "#93a6ff"),
      normal:   cssVar("--c-normal", "#7ea487"),
      low:      cssVar("--c-low", "#d3aa48"),
      mild:     cssVar("--c-mild", "#e98f41"),
      moderate: cssVar("--c-moderate", "#f26e45"),
      high:     cssVar("--c-high", "#ff5b50"),
      off:      cssVar("--c-off", "#5d584e"),
      calib:    cssVar("--c-calib", "#93a6ff"),
      pen:      cssVar("--pen", "#ff7a5e"),
    };
  }

  // One shared load for every instance; a failure is shared too, so both
  // panels explain the same problem the same way.
  let threeLoad = null;
  function loadThree() {
    if (window.THREE) return Promise.resolve(window.THREE);
    if (threeLoad) return threeLoad;
    threeLoad = THREE_SOURCES.reduce(function (chain, src) {
      return chain.catch(function () {
        return new Promise(function (resolve, reject) {
          const s = document.createElement("script");
          s.src = src;
          s.async = true;
          s.onload = function () { window.THREE ? resolve(window.THREE) : reject(new Error("no THREE")); };
          s.onerror = function () { s.remove(); reject(new Error("failed: " + src)); };
          document.head.appendChild(s);
        });
      });
    }, Promise.reject(new Error("start")));
    return threeLoad;
  }

  // ── Pure payload readers (shared by every instance) ───────────────
  function levelKey(p) {
    if (!p || !p.visible) return "off";
    if (!p.calibration || !p.calibration.complete) return "calib";
    return (p.suspicion && p.suspicion.level_key) || "off";
  }
  function levelText(p) {
    const k = levelKey(p);
    if (k === "off") return p && !p.visible ? "Not visible" : "No data";
    if (k === "calib") return "Calibrating";
    return (p.suspicion && p.suspicion.level) || k;
  }
  function pitchOn(p) { return !!(p.pitch && p.pitch.enabled !== false && num(p.pitch.pitch)); }
  function downSign(p) { return p.pitch && num(p.pitch.down_sign) ? p.pitch.down_sign : -1; }
  // Engine units -> drawn degrees. Offsets under DEAD_ZONE are squeezed
  // toward zero with x - dz·tanh(x/dz): smooth, and it never changes which
  // of two offsets is larger, so fans drawn through it still meet the head
  // exactly at the engine's threshold.
  function drawDeg(units, clampDeg) {
    const soft = units - DEAD_ZONE * Math.tanh(units / DEAD_ZONE);
    return clamp(soft * ANGLE_GAIN, -clampDeg, clampDeg);
  }
  // The engine's own verdict on where a student is looking.
  function gazeState(p) {
    if (!p || !p.visible) return { key: "hidden", text: "Not visible" };
    const cal = p.calibration || {};
    // A replay knows THAT a student was calibrating but not how far along.
    if (!cal.complete) return { key: "calib", text: "Calibrating" + (num(cal.progress) ? " " + Math.round(cal.progress * 100) + "%" : "") };
    const hp = p.head_pose || {};
    if (hp.state === "AWAY") return { key: "away", text: "Looking " + String(hp.direction || "away").toLowerCase() };
    if (p.pitch && p.pitch.looking_down) return { key: "away", text: "Looking down" };
    return { key: "forward", text: "Facing forward" };
  }
  function fmt(v) { return num(v) ? v.toFixed(1) : "—"; }
  // "2.8 right", "1.1 down", "0.0" — the number to read off when checking
  // how engine units compare with real head angles.
  function offText(v, pos, neg) {
    if (!num(v)) return "—";
    if (Math.abs(v) < 0.05) return "0.0";
    return Math.abs(v).toFixed(1) + " " + (v > 0 ? pos : neg);
  }
  // The head as this room would draw it, for the flat gaze dials on the
  // dashboard's seat cards: same straight-ahead (calibration-time), same
  // gain and dead zone, same fan edges. Pure — no instance, no Re-centre.
  // yawDeg: + = the student's right. fan: [left edge, right edge] of the
  // engine's forward zone, drawn degrees.
  function gazeView(p) {
    const cal = (p && p.calibration) || {}, hp = (p && p.head_pose) || {};
    if (!p || !p.visible || !cal.complete || !num(hp.filtered_yaw)) return { idle: true };
    const centre = num(cal.initial_baseline) ? cal.initial_baseline : num(cal.baseline) ? cal.baseline : 0;
    const out = {
      idle: false,
      yawDeg: drawDeg(hp.filtered_yaw - centre, YAW_CLAMP_DEG),
      away: hp.state === "AWAY" || !!(p.pitch && p.pitch.looking_down),
      down: !!(p.pitch && p.pitch.looking_down),
      fan: null,
    };
    if (num(cal.threshold) && num(cal.baseline)) {
      const yb = cal.baseline - centre;
      out.fan = [drawDeg(yb - cal.threshold, YAW_CLAMP_DEG), drawDeg(yb + cal.threshold, YAW_CLAMP_DEG)];
    }
    return out;
  }

  // ── Panel markup ──────────────────────────────────────────────────
  // Built by JS so the live and replay panels are guaranteed identical.
  // Elements are found by data-role within the panel, never by id, so any
  // number of instances can share a page.
  function panelMarkup(opts) {
    const esc = function (s) {
      return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/"/g, "&quot;");
    };
    const kv = function (label, role) {
      return '<div class="twin-kv"><span>' + label + '</span><b data-role="' + role + '">—</b></div>';
    };
    const axis = function (title, prefix, beyond) {
      return '<div class="twin-axis">' +
        '<div class="twin-axis-head"><span class="eyebrow">' + title + '</span><span class="twin-axis-state" data-role="' + prefix + '-state">—</span></div>' +
        '<div class="twin-gauge"><div class="twin-gauge-band" data-role="' + prefix + '-band"></div><div class="twin-gauge-mark" data-role="' + prefix + '-mark"></div></div>' +
        kv("Measured", prefix + "-val") + kv("From straight", prefix + "-off") + kv("Straight ahead", prefix + "-ref") +
        kv("Engine baseline", prefix + "-base") + kv(beyond, prefix + "-thr") + '</div>';
    };
    return '' +
      '<header class="pane-head">' +
        '<div><p class="eyebrow">' + esc(opts.eyebrow) + '</p><h2 class="pane-title">' + esc(opts.title) + '</h2></div>' +
        '<div class="twin-controls">' +
          '<div class="seg" data-role="seats" role="group" aria-label="Student shown"></div>' +
          '<div class="seg" role="group" aria-label="Viewpoint">' +
            '<button type="button" class="seg-btn is-active" data-twin-view="camera" title="Whole room from the webcam, mirrored like the live feed">Camera</button>' +
            '<button type="button" class="seg-btn" data-twin-view="room" title="Whole room from behind and above">Room</button>' +
            '<button type="button" class="seg-btn" data-twin-view="focus" title="Close-up on the selected student">Focus</button>' +
          '</div>' +
          // "Straight ahead" defaults to where the student sat during
          // calibration; Re-centre swaps in their current head position, in
          // this browser only — it never changes anything the engine measures.
          '<div class="seg" role="group" aria-label="Straight ahead">' +
            '<button type="button" class="seg-btn" data-role="recenter" title="Treat the selected student\'s current head position as straight ahead (this view only)">Re-centre</button>' +
            '<button type="button" class="seg-btn" data-role="recenter-reset" title="Go back to the position measured during calibration" hidden>Use calibration</button>' +
          '</div>' +
        '</div>' +
      '</header>' +
      '<p class="pane-sub">' + esc(opts.subtitle) + '</p>' +
      '<div class="twin-body">' +
        '<div class="twin-viewport" data-role="viewport">' +
          '<div class="twin-labels" data-role="labels"></div>' +
          '<div class="twin-message" data-role="message">Loading the 3D room…</div>' +
          '<div class="twin-tag" data-role="tag"></div>' +
        '</div>' +
        '<div class="twin-readout">' +
          '<div class="twin-who"><span class="status-dot status-dot--off" data-role="dot"></span>' +
            '<span class="twin-name" data-role="name">—</span><span class="badge lvl-off" data-role="level">—</span></div>' +
          '<div class="twin-axes">' +
            axis("Yaw · left / right", "yaw", "Away beyond") +
            axis("Pitch · up / down", "pitch", "Down beyond") +
          '</div>' +
          '<div class="twin-links" data-role="links" hidden></div>' +
          '<details class="twin-note"><summary>How to read this view</summary><p>Click a head or name to select a student. Straight ahead is where each student sat during calibration, or where you last re-centred; if the engine\'s own baseline drifts, the shaded fan moves, not the head. Angles are engine units drawn about 3× larger. Thresholds and links always come from the engine. Only the left-to-right order of desks is real; the spacing is illustrative.</p></details>' +
        '</div>' +
      '</div>' +
      '<div data-role="footer"></div>';
  }

  // ── Scene building blocks (no instance state) ─────────────────────
  // `paint(material, token, prop)` colours a material from the palette AND
  // remembers it, so a theme switch can repaint the whole room in place.
  function shadowed(m) { m.castShadow = true; m.receiveShadow = true; return m; }
  function box(THREE, w, h, d, mat, x, y, z) {
    const m = shadowed(new THREE.Mesh(new THREE.BoxGeometry(w, h, d), mat));
    m.position.set(x, y, z);
    return m;
  }
  // Glows: added light on a dark room, plain translucent ink on a light
  // one (added light is invisible against a pale background).
  function glowBlending(THREE, pal) { return pal.dark ? THREE.AdditiveBlending : THREE.NormalBlending; }

  function buildGrid(THREE, pal) {
    const grid = new THREE.GridHelper(24, 48, new THREE.Color(pal.grid2), new THREE.Color(pal.grid));
    grid.position.y = 0.002;
    grid.material.transparent = true;
    grid.material.opacity = pal.dark ? 0.55 : 0.8;
    return grid;
  }

  // Floor, grid and the webcam at the front of the room. Students face +Z,
  // toward the webcam, which is where the Camera view looks from.
  function buildStage(THREE, scene, paint, pal) {
    const floor = new THREE.Mesh(new THREE.PlaneGeometry(40, 40),
      paint(new THREE.MeshStandardMaterial({ roughness: 1, metalness: 0 }), "floor"));
    floor.rotation.x = -Math.PI / 2;
    floor.receiveShadow = true;
    scene.add(floor);
    const grid = buildGrid(THREE, pal);
    scene.add(grid);

    const metal = paint(new THREE.MeshStandardMaterial({ roughness: 0.5, metalness: 0.3 }), "metal");
    const cam = new THREE.Group();
    cam.add(box(THREE, 0.16, 0.09, 0.06, metal, 0, 0, 0));
    const lensMat = paint(new THREE.MeshStandardMaterial({ roughness: 0.2, emissiveIntensity: 0.35 }), "visor");
    paint(lensMat, "accent", "emissive");
    const lens = new THREE.Mesh(new THREE.CylinderGeometry(0.028, 0.028, 0.02, 20), lensMat);
    lens.rotation.x = Math.PI / 2;
    lens.position.z = -0.035;
    cam.add(lens);
    cam.add(box(THREE, 0.02, 1.25, 0.02, metal, 0, -0.67, 0));
    cam.position.set(0, 1.30, 3.4);
    scene.add(cam);
    return { cameraMarker: cam, grid: grid };
  }

  // One seat: desk, chair and a seated, deliberately stylised figure —
  // readable, not a likeness. Everything hangs off `root`, so a whole seat
  // can slide to its place in the row. Only the head group turns; it pivots
  // at the top of the neck, yaw applied before pitch like a real neck.
  function buildSeat(THREE, paint, pal) {
    const root = new THREE.Group();
    const pickables = [];
    const wood = paint(new THREE.MeshStandardMaterial({ roughness: 0.8 }), "desk");
    const metal = paint(new THREE.MeshStandardMaterial({ roughness: 0.5, metalness: 0.3 }), "metal");
    root.add(box(THREE, 1.10, 0.04, 0.60, wood, 0, 0.74, 0.46));
    [[-0.5, 0.2], [0.5, 0.2], [-0.5, 0.72], [0.5, 0.72]].forEach(function (p) {
      root.add(box(THREE, 0.04, 0.72, 0.04, metal, p[0], 0.36, p[1]));
    });
    // The exam paper. Looking down at it is ordinary, which is why pitch
    // has its own, higher bar than yaw in the engine.
    const paper = new THREE.Mesh(new THREE.PlaneGeometry(0.21, 0.297),
      paint(new THREE.MeshStandardMaterial({ roughness: 0.9 }), "paper"));
    paper.rotation.x = -Math.PI / 2;
    paper.rotation.z = 0.08;
    paper.position.set(0.05, 0.762, 0.36);
    paper.receiveShadow = true;
    root.add(paper);
    root.add(box(THREE, 0.46, 0.05, 0.44, wood, 0, 0.46, -0.16));
    root.add(box(THREE, 0.46, 0.46, 0.04, wood, 0, 0.74, -0.37));
    [[-0.2, 0.02], [0.2, 0.02], [-0.2, -0.34], [0.2, -0.34]].forEach(function (p) {
      root.add(box(THREE, 0.035, 0.46, 0.035, metal, p[0], 0.23, p[1]));
    });

    const skin = paint(new THREE.MeshStandardMaterial({ roughness: 0.6 }), "skin");
    const cloth = paint(new THREE.MeshStandardMaterial({ roughness: 0.8 }), "cloth");
    const visor = paint(new THREE.MeshStandardMaterial({ roughness: 0.15, metalness: 0.6, emissiveIntensity: 0.18 }), "visor");
    paint(visor, "accent", "emissive");
    const collar = new THREE.MeshStandardMaterial({ color: pal.accent, roughness: 0.4 });
    const add = function (parent, geo, mat, x, y, z) {
      const m = shadowed(new THREE.Mesh(geo, mat));
      m.position.set(x, y, z);
      parent.add(m);
      pickables.push(m);
      return m;
    };
    add(root, new THREE.BoxGeometry(0.36, 0.12, 0.34), cloth, 0, 0.54, -0.12);
    [-0.1, 0.1].forEach(function (x) {
      add(root, new THREE.CapsuleGeometry(0.065, 0.3, 4, 12), cloth, x, 0.55, 0.09).rotation.x = Math.PI / 2;
      add(root, new THREE.CapsuleGeometry(0.055, 0.36, 4, 12), cloth, x, 0.30, 0.26);
    });
    add(root, new THREE.CapsuleGeometry(0.17, 0.3, 6, 20), cloth, 0, 0.86, -0.13).scale.set(1.2, 1, 0.72);
    [-1, 1].forEach(function (s) {
      const upper = add(root, new THREE.CapsuleGeometry(0.05, 0.22, 4, 12), cloth, s * 0.25, 0.9, -0.07);
      upper.rotation.x = -0.5;
      upper.rotation.z = s * 0.1;
      add(root, new THREE.CapsuleGeometry(0.045, 0.26, 4, 12), cloth, s * 0.22, 0.80, 0.14).rotation.x = Math.PI / 2 - 0.1;
    });
    add(root, new THREE.TorusGeometry(0.075, 0.018, 10, 32), collar, 0, 1.1, -0.1).rotation.x = Math.PI / 2;
    add(root, new THREE.CylinderGeometry(0.05, 0.058, 0.12, 16), skin, 0, 1.14, -0.1);

    const head = new THREE.Group();
    head.rotation.order = "YXZ";
    head.position.set(0, 1.2, -0.1);
    root.add(head);
    add(head, new THREE.SphereGeometry(0.125, 40, 30), skin, 0, 0.12, 0).scale.set(0.92, 1.1, 1.0);
    // Visor band across the eyes — a clear "front" from any angle.
    add(head, new THREE.SphereGeometry(0.129, 40, 8, Math.PI / 2 - 0.95, 1.9, Math.PI / 2 - 0.34, 0.3),
      visor, 0, 0.12, 0).scale.set(0.92, 1.1, 1.0);
    add(head, new THREE.ConeGeometry(0.02, 0.055, 16), skin, 0, 0.085, 0.125).rotation.x = Math.PI / 2;
    [-1, 1].forEach(function (s) {
      add(head, new THREE.SphereGeometry(0.026, 16, 12), skin, s * 0.114, 0.11, -0.005).scale.set(0.5, 1, 0.8);
    });

    // Gaze beam from between the eyes. Short, so a small head turn does not
    // sweep it across the screen, and drawn on top of everything (no depth
    // test) so a downward look into the lap stays visible from any angle.
    const beam = new THREE.Group();
    beam.position.set(0, 0.12, 0.12);
    head.add(beam);
    const beamMat = new THREE.MeshBasicMaterial({ color: pal.forward, transparent: true, opacity: 0.24,
      blending: glowBlending(THREE, pal), depthWrite: false, depthTest: false });
    const cone = new THREE.Mesh(new THREE.CylinderGeometry(0.04, 0.005, BEAM_LEN, 24, 1, true), beamMat);
    cone.rotation.x = Math.PI / 2;
    cone.position.z = BEAM_LEN / 2;
    const lineMat = new THREE.LineBasicMaterial({ color: pal.forward, transparent: true, opacity: 0.8, depthTest: false });
    const line = new THREE.Line(new THREE.BufferGeometry().setFromPoints(
      [new THREE.Vector3(0, 0, 0), new THREE.Vector3(0, 0, BEAM_LEN)]), lineMat);
    const tipMat = new THREE.MeshBasicMaterial({ color: pal.forward, depthTest: false });
    const tip = new THREE.Mesh(new THREE.SphereGeometry(0.018, 16, 12), tipMat);
    tip.position.z = BEAM_LEN;
    [cone, line, tip].forEach(function (m) { m.renderOrder = 10; beam.add(m); });

    // Floor ring in the student's current risk-level colour.
    const haloMat = new THREE.MeshBasicMaterial({ color: pal.off, transparent: true, opacity: 0.6, depthWrite: false });
    const haloFillMat = new THREE.MeshBasicMaterial({ color: pal.off, transparent: true, opacity: 0.07, depthWrite: false });
    [new THREE.Mesh(new THREE.RingGeometry(0.62, 0.7, 64), haloMat),
     new THREE.Mesh(new THREE.CircleGeometry(0.62, 64), haloFillMat)].forEach(function (m) {
      m.rotation.x = -Math.PI / 2;
      m.position.set(0, 0.004, 0.1);
      root.add(m);
    });

    return {
      root: root, head: head, beam: beam, pickables: pickables,
      beamMat: beamMat, lineMat: lineMat, tipMat: tipMat,
      haloMat: haloMat, haloFillMat: haloFillMat, collarMat: collar,
      ghostMats: [skin, cloth, visor, collar],
      yaw: 0, pitch: 0, targetYaw: 0, targetPitch: 0, ghost: 1, targetGhost: 1,
      x: 0, targetX: 0, labelEl: null, labelText: null, labelDot: null,
    };
  }

  // Threshold fans for the selected student. Anchored at eye height on the
  // seat, NOT on the head, so they stay put while the head turns through.
  // Both are drawn in CircleGeometry's XY plane and rotated into place:
  //   yaw   -> horizontal; angle -90° points straight ahead (+Z), and a
  //            turn right by r sits at -90° - r
  //   pitch -> vertical; 0° is ahead, looking down by d sits at -d
  function buildFans(THREE, paint, pal) {
    const group = new THREE.Group();
    group.position.set(0, EYE_Y, EYE_Z);
    const yaw = new THREE.Group();
    yaw.rotation.x = -Math.PI / 2;
    const pitch = new THREE.Group();
    pitch.rotation.y = -Math.PI / 2;
    group.add(yaw, pitch);
    return {
      group: group, yaw: yaw, pitch: pitch, yawKey: null, pitchKey: null,
      fillMat: paint(new THREE.MeshBasicMaterial({ transparent: true, opacity: pal.dark ? 0.07 : 0.1,
        side: THREE.DoubleSide, depthWrite: false }), "fan"),
      edgeMat: paint(new THREE.LineBasicMaterial({ transparent: true, opacity: pal.dark ? 0.5 : 0.6 }), "fan"),
    };
  }
  function setSector(THREE, holder, start, length, fillMat, edgeMat, edges) {
    while (holder.children.length) {
      const c = holder.children[holder.children.length - 1];
      holder.remove(c);
      c.geometry.dispose();
    }
    const at = function (a) { return new THREE.Vector3(FAN_R * Math.cos(a), FAN_R * Math.sin(a), 0); };
    holder.add(new THREE.Mesh(new THREE.CircleGeometry(FAN_R, 48, start, length), fillMat));
    [start, start + length].forEach(function (a, i) {
      if (!edges[i]) return;
      holder.add(new THREE.Line(new THREE.BufferGeometry().setFromPoints([new THREE.Vector3(), at(a)]), edgeMat));
    });
    const arc = [];
    for (let i = 0; i <= 32; i++) arc.push(at(start + (length * i) / 32));
    holder.add(new THREE.Line(new THREE.BufferGeometry().setFromPoints(arc), edgeMat));
  }

  // A link the correlation layer found between two students: a thin glowing
  // ARC from head to head, drawn on top so desks never hide it. Arcs rather
  // than straight lines because with several links in a row, straight beams
  // at head height ran through the heads in between and merged into one
  // bar; a wider pair arcs higher, so each link stays distinguishable.
  function buildLink(THREE, color, pal) {
    const core = new THREE.MeshBasicMaterial({ color: color, transparent: true, opacity: pal.dark ? 0.75 : 0.85,
      blending: glowBlending(THREE, pal), depthWrite: false, depthTest: false });
    const haze = new THREE.MeshBasicMaterial({ color: color, transparent: true, opacity: pal.dark ? 0.14 : 0.12,
      blending: glowBlending(THREE, pal), depthWrite: false, depthTest: false });
    const group = new THREE.Group();
    const endA = new THREE.Mesh(new THREE.SphereGeometry(0.03, 16, 12), core);
    const endB = new THREE.Mesh(new THREE.SphereGeometry(0.03, 16, 12), core);
    [endA, endB].forEach(function (m) { m.renderOrder = 11; group.add(m); });
    const link = {
      group: group, core: core, haze: haze, endA: endA, endB: endB,
      tube: null, glow: null, shapeKey: null, apex: new THREE.Vector3(),
      dispose: function () {
        [link.tube, link.glow, endA, endB].forEach(function (m) { if (m) m.geometry.dispose(); });
        core.dispose();
        haze.dispose();
      },
    };
    return link;
  }
  function placeLink(THREE, link, a, b) {
    // Rebuild the arc only when an end has actually moved (seats slide when
    // a student arrives or leaves); otherwise this is free every frame.
    const shapeKey = [a.x, a.y, b.x, b.y].map(function (v) { return v.toFixed(2); }).join(",");
    if (shapeKey === link.shapeKey) return;
    link.shapeKey = shapeKey;
    const span = Math.abs(b.x - a.x);
    const rise = 0.28 + 0.2 * span;
    // Quadratic Bézier: the curve's midpoint rises half as far as the control
    // point, so the control point sits at twice the wanted height.
    const control = a.clone().add(b).multiplyScalar(0.5).add(new THREE.Vector3(0, 2 * rise, 0));
    const curve = new THREE.QuadraticBezierCurve3(a.clone(), control, b.clone());
    [link.tube, link.glow].forEach(function (m) {
      if (m) { link.group.remove(m); m.geometry.dispose(); }
    });
    link.tube = new THREE.Mesh(new THREE.TubeGeometry(curve, 48, 0.012, 8, false), link.core);
    link.glow = new THREE.Mesh(new THREE.TubeGeometry(curve, 48, 0.04, 10, false), link.haze);
    [link.tube, link.glow].forEach(function (m) { m.renderOrder = 11; link.group.add(m); });
    link.endA.position.copy(a);
    link.endB.position.copy(b);
    link.apex.copy(curve.getPoint(0.5));
  }

  // ── One 3D room panel ─────────────────────────────────────────────
  // root: the card element to fill. Returns { root, update(state), debug() }.
  function createTwin(root, options) {
    const opts = Object.assign({
      instance: "live",
      eyebrow: "3D room",
      title: "Where every head is turned",
      subtitle: "Each student's head as measured, against their own straight-ahead. Drag to orbit; click the view, then scroll to zoom.",
    }, options || {});
    root.innerHTML = panelMarkup(opts);
    const refs = {};
    root.querySelectorAll("[data-role]").forEach(function (el) { refs[el.dataset.role] = el; });

    const T = {
      status: "idle",      // idle | loading | ready | failed
      THREE: null, pal: null, renderer: null, scene: null, camera: null,
      canvas: null, stage: null, fans: null, raycaster: null,
      seats: {},           // label -> seat unit, built the first time it is needed
      order: [],           // labels of seats on show, screen left -> right
      links: {}, linkSig: null,
      view: "camera", userMoved: false,
      orbit: { az: VIEWS.camera.az, el: VIEWS.camera.el, dist: 6, tx: 0, ty: 1.0, tz: 0.1 },
      orbitAnim: null, dragging: null, engaged: false,
      raf: 0, lastFrame: 0, lastState: null,
      paints: [], lights: null,
    };
    const Sel = { label: null, manual: false, sig: null, chips: {} };

    function setText(el, t) { if (el && el.textContent !== t) el.textContent = t; }
    function setCls(el, c) { if (el && el.className !== c) el.className = c; }
    function msg(text) { setText(refs.message, text); }

    // Colour a material from the palette and remember it for retheme().
    function paint(mat, key, prop) {
      prop = prop || "color";
      T.paints.push({ mat: mat, key: key, prop: prop });
      mat[prop].set(T.pal[key]);
      return mat;
    }
    // A clay model wants soft, even light; a dark room a cooler key and a
    // coloured rim. Same lights, different settings.
    function applyLights() {
      const L = T.lights, pal = T.pal;
      L.hemi.color.set(pal.dark ? 0xdce6ff : 0xfffaf0);
      L.hemi.groundColor.set(pal.floor);
      L.hemi.intensity = pal.dark ? 0.85 : 0.95;
      L.key.intensity = pal.dark ? 0.95 : 0.85;
      L.rim.color.set(pal.accent);
      L.rim.intensity = pal.dark ? 0.55 : 0.12;
      T.renderer.setClearColor(new T.THREE.Color(pal.bg), 1);
      T.scene.fog.color.set(pal.bg);
    }

    function initScene(THREE) {
      const pal = readPalette();
      let renderer;
      try {
        renderer = new THREE.WebGLRenderer({ antialias: true, powerPreference: "low-power" });
      } catch (e) {
        throw new Error("webgl");
      }
      renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 1.75));
      renderer.shadowMap.enabled = true;
      renderer.shadowMap.type = THREE.PCFSoftShadowMap;
      refs.viewport.insertBefore(renderer.domElement, refs.viewport.firstChild);

      const scene = new THREE.Scene();
      scene.fog = new THREE.Fog(new THREE.Color(pal.bg), 9, 24);
      const hemi = new THREE.HemisphereLight(0xffffff, 0xffffff, 0.85);
      scene.add(hemi);
      const key = new THREE.DirectionalLight(0xffffff, 0.95);
      key.position.set(3, 6, 5);
      key.castShadow = true;
      key.shadow.mapSize.set(2048, 2048);
      Object.assign(key.shadow.camera, { left: -5, right: 5, top: 4, bottom: -2, near: 0.5, far: 20 });
      scene.add(key);
      const rim = new THREE.DirectionalLight(0xffffff, 0.55);
      rim.position.set(-3, 3, -4);
      scene.add(rim);

      Object.assign(T, {
        THREE: THREE, pal: pal, renderer: renderer, scene: scene,
        camera: new THREE.PerspectiveCamera(38, 1, 0.05, 60),
        canvas: renderer.domElement, raycaster: new THREE.Raycaster(),
        lights: { hemi: hemi, key: key, rim: rim },
      });
      applyLights();
      T.stage = buildStage(THREE, scene, paint, pal);
      T.fans = buildFans(THREE, paint, pal);
      applyMirror();
      bindPointer();
      new ResizeObserver(resize).observe(refs.viewport);
      document.addEventListener("visibilitychange", function () { if (!document.hidden) wake(); });
    }

    // Theme switch: repaint every remembered material, swap the grid and
    // light settings, and rebuild links and glows with the new blending.
    function retheme() {
      if (T.status !== "ready") return;
      const THREE = T.THREE;
      T.pal = readPalette();
      T.paints.forEach(function (p) { p.mat[p.prop].set(T.pal[p.key]); });
      applyLights();
      T.scene.remove(T.stage.grid);
      T.stage.grid.geometry.dispose();
      T.stage.grid.material.dispose();
      T.stage.grid = buildGrid(THREE, T.pal);
      T.scene.add(T.stage.grid);
      const blend = glowBlending(THREE, T.pal);
      Object.keys(T.seats).forEach(function (l) {
        const m = T.seats[l].beamMat;
        if (m.blending !== blend) { m.blending = blend; m.needsUpdate = true; }
      });
      T.fans.fillMat.opacity = T.pal.dark ? 0.07 : 0.1;
      T.fans.edgeMat.opacity = T.pal.dark ? 0.5 : 0.6;
      T.linkSig = null;
      if (T.lastState) applyState(T.lastState);
      wake();
    }

    // ── Camera ───────────────────────────────────────────────────────
    // Room views back off far enough to fit every desk on show.
    function roomFitDist(minDist) {
      const n = Math.max(T.order.length, 1);
      const halfWidth = ((n - 1) / 2) * DESK_SPACING + 1.1;
      const aspect = T.camera && T.camera.aspect ? T.camera.aspect : 1.6;
      const halfFov = Math.atan(Math.tan(deg(19)) * aspect);
      return Math.max(minDist, (halfWidth / Math.tan(halfFov)) * 1.05);
    }
    function viewPose(name) {
      const v = VIEWS[name];
      if (v.room) return { az: v.az, el: v.el, dist: roomFitDist(v.minDist), tx: 0, ty: 1.0, tz: 0.1 };
      const s = Sel.label && T.seats[Sel.label];
      return { az: v.az, el: v.el, dist: v.dist, tx: s ? s.targetX : 0, ty: 1.12, tz: 0.1 };
    }
    function applyCamera() {
      const o = T.orbit;
      T.camera.position.set(
        o.tx + o.dist * Math.sin(o.az) * Math.cos(o.el),
        o.ty + o.dist * Math.sin(o.el),
        o.tz + o.dist * Math.cos(o.az) * Math.cos(o.el));
      T.camera.lookAt(o.tx, o.ty, o.tz);
    }
    function applyMirror() {
      const mirror = VIEWS[T.view].mirror;
      if (T.canvas) T.canvas.classList.toggle("is-mirrored", mirror);
      // The webcam model would sit in front of the Camera view's own lens.
      if (T.stage) T.stage.cameraMarker.visible = !mirror;
    }
    function setView(name, instant) {
      if (!VIEWS[name]) return;
      T.view = name;
      T.userMoved = false;
      root.querySelectorAll("[data-twin-view]").forEach(function (b) {
        b.classList.toggle("is-active", b.dataset.twinView === name);
      });
      const to = viewPose(name);
      if (T.status !== "ready" || instant) {
        Object.assign(T.orbit, to);
        applyMirror();
        wake();
        return;
      }
      const from = Object.assign({}, T.orbit);
      to.az = from.az + Math.atan2(Math.sin(to.az - from.az), Math.cos(to.az - from.az));
      T.orbitAnim = { from: from, to: to, t0: performance.now(), dur: 650 };
      wake();
    }
    function stepOrbit(now) {
      const a = T.orbitAnim;
      if (!a) return false;
      const p = clamp((now - a.t0) / a.dur, 0, 1);
      const e = p < 0.5 ? 2 * p * p : 1 - Math.pow(-2 * p + 2, 2) / 2;
      ["az", "el", "dist", "tx", "ty", "tz"].forEach(function (k) {
        T.orbit[k] = a.from[k] + (a.to[k] - a.from[k]) * e;
      });
      // Mirroring flips at the halfway point, when the flip is least visible.
      if (p >= 0.5) applyMirror();
      if (p >= 1) { T.orbitAnim = null; return false; }
      return true;
    }

    // ── Pointer: drag to orbit, click to select ─────────────────────
    function bindPointer() {
      const vp = refs.viewport;
      vp.addEventListener("pointerdown", function (e) {
        if (e.button !== 0) return;
        if (T.orbitAnim) { T.orbitAnim = null; applyMirror(); }
        T.dragging = { x: e.clientX, y: e.clientY, x0: e.clientX, y0: e.clientY, id: e.pointerId, target: e.target };
        T.engaged = true;
        vp.setPointerCapture(e.pointerId);
        vp.classList.add("is-dragging");
      });
      vp.addEventListener("pointermove", function (e) {
        const d = T.dragging;
        if (!d || d.id !== e.pointerId) return;
        const flip = VIEWS[T.view].mirror ? -1 : 1;
        T.orbit.az -= (e.clientX - d.x) * 0.008 * flip;
        T.orbit.el = clamp(T.orbit.el + (e.clientY - d.y) * 0.006, -0.05, 1.35);
        if (Math.abs(e.clientX - d.x0) + Math.abs(e.clientY - d.y0) > 4) T.userMoved = true;
        d.x = e.clientX;
        d.y = e.clientY;
        wake();
      });
      const end = function (e) {
        const d = T.dragging;
        if (!d || d.id !== e.pointerId) return;
        T.dragging = null;
        vp.classList.remove("is-dragging");
        if (e.type === "pointerup" && Math.abs(e.clientX - d.x0) + Math.abs(e.clientY - d.y0) <= 4) pick(e, d.target);
      };
      vp.addEventListener("pointerup", end);
      vp.addEventListener("pointercancel", end);
      vp.addEventListener("pointerleave", function () { T.engaged = false; });
      // Wheel zooms only after the viewer has clicked into the view, so
      // scrolling the dashboard past this panel is never hijacked.
      vp.addEventListener("wheel", function (e) {
        if (!T.engaged) return;
        e.preventDefault();
        T.orbitAnim = null;
        T.userMoved = true;
        T.orbit.dist = clamp(T.orbit.dist * Math.exp(e.deltaY * 0.001), 1.2, 16);
        wake();
      }, { passive: false });
    }
    function pick(e, target) {
      const label = target && target.closest ? target.closest("[data-seat]") : null;
      if (label) { select(label.dataset.seat); return; }
      if (T.status !== "ready") return;
      const r = T.canvas.getBoundingClientRect();
      let x = ((e.clientX - r.left) / r.width) * 2 - 1;
      if (VIEWS[T.view].mirror) x = -x;   // the canvas is mirrored by CSS; rays are not
      const y = -((e.clientY - r.top) / r.height) * 2 + 1;
      T.raycaster.setFromCamera({ x: x, y: y }, T.camera);
      const candidates = [];
      T.order.forEach(function (l) {
        const s = T.seats[l];
        if (s && s.root.visible) candidates.push.apply(candidates, s.pickables);
      });
      const hit = T.raycaster.intersectObjects(candidates, false)[0];
      if (hit) select(hit.object.userData.seat);
    }
    function select(label) {
      if (!label) return;
      Sel.label = label;
      Sel.manual = true;
      if (T.lastState) applyState(T.lastState);
      if (T.view === "focus") setView("focus");
      // Lets the page around the room follow along (dashboard.js marks
      // that student's seat card).
      root.dispatchEvent(new CustomEvent("twin:select", { bubbles: true,
        detail: { label: label, instance: opts.instance } }));
    }

    // ── Render loop: runs only while something is actually changing ───
    // Visibility is measured on demand: an observer's last answer can go
    // stale while a window is throttled and leave the view asleep.
    function isShown() {
      if (document.hidden) return false;
      const page = refs.viewport.closest(".page");
      if (page && !page.classList.contains("is-active")) return false;
      const r = refs.viewport.getBoundingClientRect();
      return r.width > 0 && r.bottom > 0 && r.top < window.innerHeight;
    }
    function wake() {
      if (!T.raf && T.status === "ready" && isShown()) {
        T.lastFrame = performance.now();
        T.raf = requestAnimationFrame(frame);
      }
    }
    function fitCanvas() {
      const w = refs.viewport.clientWidth, h = refs.viewport.clientHeight;
      if (!w || !h) return false;
      T.renderer.setSize(w, h, false);
      T.camera.aspect = w / h;
      T.camera.updateProjectionMatrix();
      return true;
    }
    function resize() {
      if (!fitCanvas()) return;
      if (!T.userMoved && !T.orbitAnim && VIEWS[T.view].room) Object.assign(T.orbit, viewPose(T.view));
      wake();
    }
    function stepSeats(dt) {
      const k = 1 - Math.exp(-dt * HEAD_SMOOTHING);
      const kx = 1 - Math.exp(-dt * 4);
      let busy = false;
      Object.keys(T.seats).forEach(function (label) {
        const s = T.seats[label];
        if (!s.root.visible) return;
        s.yaw += (s.targetYaw - s.yaw) * k;
        s.pitch += (s.targetPitch - s.pitch) * k;
        s.ghost += (s.targetGhost - s.ghost) * k;
        s.x += (s.targetX - s.x) * kx;
        s.root.position.x = s.x;
        // Facing +Z, a student's own right is -X: turning right is a
        // negative rotation about Y. Nodding down is positive about X.
        s.head.rotation.y = -s.yaw;
        s.head.rotation.x = s.pitch;
        const see = s.ghost < 0.99;
        s.ghostMats.forEach(function (m) {
          m.opacity = s.ghost;
          if (m.transparent !== see) { m.transparent = see; m.needsUpdate = true; }
        });
        busy = busy || Math.abs(s.targetYaw - s.yaw) > 1e-4 || Math.abs(s.targetPitch - s.pitch) > 1e-4 ||
          Math.abs(s.targetGhost - s.ghost) > 1e-3 || Math.abs(s.targetX - s.x) > 1e-3;
      });
      return busy;
    }
    function placeLinks() {
      const V = T.THREE.Vector3;
      Object.keys(T.links).forEach(function (key) {
        const L = T.links[key], a = T.seats[L.from], b = T.seats[L.to];
        const ok = !!(a && b && a.root.visible && b.root.visible);
        L.group.visible = ok;
        if (ok) placeLink(T.THREE, L, new V(a.x, EYE_Y + 0.14, EYE_Z), new V(b.x, EYE_Y + 0.14, EYE_Z));
      });
    }
    // HTML labels, pinned over the 3D scene after each render. Kept
    // un-mirrored so they stay readable when the canvas itself is mirrored.
    function placeLabels() {
      const w = refs.viewport.clientWidth, h = refs.viewport.clientHeight;
      const mirror = VIEWS[T.view].mirror;
      const v = new T.THREE.Vector3();
      const put = function (el, x, y, z) {
        v.set(x, y, z).project(T.camera);
        const off = v.z < -1 || v.z > 1;
        if (el.hidden !== off) el.hidden = off;
        if (off) return;
        let sx = (v.x * 0.5 + 0.5) * w;
        if (mirror) sx = w - sx;
        const sy = (-v.y * 0.5 + 0.5) * h;
        el.style.transform = "translate(" + sx.toFixed(1) + "px," + sy.toFixed(1) + "px) translate(-50%,-100%)";
      };
      Object.keys(T.seats).forEach(function (label) {
        const s = T.seats[label];
        if (!s.labelEl) return;
        if (!s.root.visible) { s.labelEl.hidden = true; return; }
        put(s.labelEl, s.x, LABEL_Y, 0);
      });
      // Link labels sit at the top of their arcs. Where two would still
      // overlap on screen, the later one is lifted clear, so every
      // finding's words stay readable.
      const placed = [];
      Object.keys(T.links).forEach(function (key) {
        const L = T.links[key];
        if (!L.group.visible || !L.shapeKey) { L.labelEl.hidden = true; return; }
        v.set(L.apex.x, L.apex.y + 0.06, L.apex.z).project(T.camera);
        const off = v.z < -1 || v.z > 1;
        if (L.labelEl.hidden !== off) L.labelEl.hidden = off;
        if (off) return;
        let sx = (v.x * 0.5 + 0.5) * w;
        if (mirror) sx = w - sx;
        let sy = (-v.y * 0.5 + 0.5) * h;   // label's bottom edge
        const bw = L.labelEl.offsetWidth, bh = L.labelEl.offsetHeight;
        for (let guard = 0, moved = true; moved && guard < 8; guard++) {
          moved = false;
          placed.forEach(function (o) {
            if (Math.abs(o.sx - sx) < (o.w + bw) / 2 + 4 && sy > o.sy - o.h - 2 && sy - bh < o.sy + 2) {
              sy = o.sy - o.h - 3;
              moved = true;
            }
          });
        }
        placed.push({ sx: sx, sy: sy, w: bw, h: bh });
        L.labelEl.style.transform = "translate(" + sx.toFixed(1) + "px," + sy.toFixed(1) + "px) translate(-50%,-100%)";
      });
    }
    function frame(now) {
      T.raf = 0;
      if (T.status !== "ready" || !isShown()) return;   // sleeps until woken
      const dt = Math.min((now - T.lastFrame) / 1000, 0.1);
      T.lastFrame = now;
      // Size check on every drawn frame, not just ResizeObserver: a page
      // started in a background tab gets no observer callback until it is
      // painted, and would render squashed into the canvas's default size.
      const pr = T.renderer.getPixelRatio();
      if (T.canvas.width !== Math.floor(refs.viewport.clientWidth * pr) ||
          T.canvas.height !== Math.floor(refs.viewport.clientHeight * pr)) {
        // Re-frame the room too: a panel that started at zero size fitted
        // its camera distance to the wrong shape and drew everyone tiny.
        if (fitCanvas() && !T.userMoved && !T.orbitAnim && VIEWS[T.view].room) {
          Object.assign(T.orbit, viewPose(T.view));
        }
      }
      let busy = stepSeats(dt);
      busy = stepOrbit(now) || busy;
      if (T.view === "focus" && !T.orbitAnim) {
        const s = Sel.label && T.seats[Sel.label];
        if (s && s.root.visible && Math.abs(s.x - T.orbit.tx) > 1e-3) {
          T.orbit.tx += (s.x - T.orbit.tx) * (1 - Math.exp(-dt * 4));
          busy = true;
        }
      }
      placeLinks();
      applyCamera();
      T.renderer.render(T.scene, T.camera);
      placeLabels();
      if (busy) T.raf = requestAnimationFrame(frame);
    }

    // ── "Straight ahead" per student ─────────────────────────────────
    // Drawn against a FIXED straight-ahead, not the engine's live baseline:
    // a real 22-minute log (17 Sep 2026) showed the live baseline wandering
    // toward wherever the student had been looking, which drew a student
    // facing the camera as turned 17° away. Default is the calibration-time
    // baseline; Re-centre swaps in the current head position, for this
    // browser only.
    const Centre = {};   // label -> { key, manual: {yaw, pitch} | null }
    function centreState(p) {
      const cal = p.calibration || {};
      // A new occupant or a recalibration changes the key, dropping any
      // re-centre made for the previous person.
      const key = String(!!cal.complete) + "|" + cal.initial_baseline;
      let c = Centre[p.label];
      if (!c || c.key !== key) c = Centre[p.label] = { key: key, manual: null };
      return c;
    }
    function yawCentre(p) {
      const hp = p.head_pose || {}, cal = p.calibration || {}, m = centreState(p).manual;
      if (m && num(m.yaw)) return m.yaw;
      if (cal.complete) return num(cal.initial_baseline) ? cal.initial_baseline : cal.baseline;
      if (cal.samples > 0 && num(cal.running_mean)) return cal.running_mean;
      return num(hp.filtered_yaw) ? hp.filtered_yaw : 0;
    }
    function pitchCentre(p) {
      const pt = p.pitch || {}, m = centreState(p).manual;
      if (m && num(m.pitch)) return m.pitch;
      if (pt.calibrated) return num(pt.initial_baseline) ? pt.initial_baseline : pt.baseline;
      if (num(pt.running_mean)) return pt.running_mean;
      return num(pt.pitch) ? pt.pitch : 0;
    }
    // Offsets from straight ahead, engine units. Yaw + = the student's
    // right; pitch + = down (Config.PITCH_DOWN_SIGN).
    function relYaw(p) {
      const hp = p.head_pose || {};
      return num(hp.filtered_yaw) ? hp.filtered_yaw - yawCentre(p) : 0;
    }
    function relDown(p) {
      return pitchOn(p) ? (p.pitch.pitch - pitchCentre(p)) * downSign(p) : 0;
    }
    // Where the engine's LIVE baseline sits relative to straight ahead. The
    // engine measures "away" from it, so fans and gauge bands centre there.
    function yawBaseOffset(p) {
      const cal = p.calibration || {};
      return cal.complete && num(cal.baseline) ? cal.baseline - yawCentre(p) : 0;
    }
    function downBaseOffset(p) {
      const pt = p.pitch || {};
      return pitchOn(p) && pt.calibrated && num(pt.baseline)
        ? (pt.baseline - pitchCentre(p)) * downSign(p) : 0;
    }

    // ── Which seats are on show, and in what order ───────────────────
    const Seen = {};   // label -> has this seat's student ever been visible
    function seatVisibility(state, label, p) {
      if (p.visible) { Seen[label] = true; return "present"; }
      if (!Seen[label]) return "hidden";
      const slot = state.identity ? state.identity[SEATS.indexOf(label)] : null;
      if (slot && (slot.state === "empty" || slot.state === "expired")) return "hidden";
      return "away";   // seat held, on a break, or no identity data: a ghost
    }
    function orderSeats(state, labels) {
      const ident = state.identity || {};
      const items = labels.map(function (l) {
        const i = SEATS.indexOf(l), slot = ident[i];
        return { l: l, i: i, cx: slot && num(slot.cx) ? slot.cx : null };
      });
      const known = items.filter(function (o) { return o.cx !== null; }).sort(function (a, b) { return a.cx - b.cx; });
      const unknown = items.filter(function (o) { return o.cx === null; }).sort(function (a, b) { return a.i - b.i; });
      return known.concat(unknown).map(function (o) { return o.l; });
    }
    function ensureSeat(label) {
      let s = T.seats[label];
      if (s) return s;
      s = T.seats[label] = buildSeat(T.THREE, paint, T.pal);
      s.pickables.forEach(function (m) { m.userData.seat = label; });
      s.root.visible = false;
      T.scene.add(s.root);
      const el = document.createElement("div");
      el.className = "twin-label";
      el.dataset.seat = label;
      el.hidden = true;
      s.labelDot = document.createElement("span");
      s.labelDot.className = "status-dot status-dot--off";
      s.labelText = document.createElement("span");
      el.append(s.labelDot, s.labelText);
      refs.labels.appendChild(el);
      s.labelEl = el;
      return s;
    }
    function applySeat(s, p, vis, selected) {
      const pal = T.pal, present = vis === "present";
      const g = gazeState(p);
      s.targetYaw = present ? deg(drawDeg(relYaw(p), YAW_CLAMP_DEG)) : 0;
      s.targetPitch = present ? deg(drawDeg(relDown(p), PITCH_CLAMP_DEG)) : 0;
      s.targetGhost = present ? 1 : 0.22;
      s.beam.visible = present;
      const beamCol = g.key === "away" ? pal.away : g.key === "calib" ? pal.calib : pal.forward;
      s.beamMat.color.set(beamCol);
      s.lineMat.color.set(beamCol);
      s.tipMat.color.set(beamCol);
      s.beamMat.opacity = g.key === "away" ? (pal.dark ? 0.42 : 0.5) : (pal.dark ? 0.24 : 0.3);
      const lvl = pal[levelKey(p)] || pal.off;
      s.haloMat.color.set(lvl);
      s.haloFillMat.color.set(lvl);
      s.collarMat.color.set(p.color || pal.accent);
      const name = p.display_name || p.label;
      setText(s.labelText, name === p.label ? p.label : p.label + " · " + name);
      setCls(s.labelDot, "status-dot status-dot--" + (present ? levelKey(p) : "off"));
      setCls(s.labelEl, "twin-label" + (selected ? " is-selected" : "") +
        (present ? "" : " is-ghost") + (g.key === "away" ? " is-away" : ""));
    }

    // ── Fans and links ───────────────────────────────────────────────
    // Fan edges are the engine's own limits around ITS live baseline, drawn
    // through the same drawDeg() as the head: when that baseline drifts away
    // from straight ahead, the fan slides and the head does not.
    function updateFans(p) {
      const f = T.fans, s = p && T.seats[p.label];
      if (!s || !s.root.visible || !p.visible) { f.group.visible = false; return; }
      if (f.group.parent !== s.root) s.root.add(f.group);
      f.group.visible = true;
      const cal = p.calibration || {}, pt = p.pitch || {};
      f.yaw.visible = !!cal.complete && num(cal.threshold);
      if (f.yaw.visible) {
        const yb = yawBaseOffset(p);
        const right = deg(drawDeg(yb + cal.threshold, YAW_CLAMP_DEG));
        const left = deg(drawDeg(yb - cal.threshold, YAW_CLAMP_DEG));
        const key = right.toFixed(2) + "|" + left.toFixed(2);
        if (key !== f.yawKey) {
          f.yawKey = key;
          setSector(T.THREE, f.yaw, -Math.PI / 2 - right, right - left, f.fillMat, f.edgeMat, [true, true]);
        }
      }
      f.pitch.visible = pitchOn(p) && !!pt.calibrated && num(pt.down_threshold_deg);
      if (f.pitch.visible) {
        const db = downBaseOffset(p);
        const bar = deg(drawDeg(db + pt.down_threshold_deg, PITCH_CLAMP_DEG));
        const top = -deg(drawDeg(db, PITCH_CLAMP_DEG)) + deg(PITCH_FAN_UP_DEG);
        const key = bar.toFixed(2) + "|" + top.toFixed(2);
        if (key !== f.pitchKey) {
          f.pitchKey = key;
          // Only the lower edge is a threshold; the upper one just ends the fan.
          setSector(T.THREE, f.pitch, -bar, top + bar, f.fillMat, f.edgeMat, [true, false]);
        }
      }
    }
    function findingsOf(state) {
      return (state.correlations || []).filter(function (c) {
        return Array.isArray(c.slots) && c.slots.length === 2 && SEATS[c.slots[0]] && SEATS[c.slots[1]];
      });
    }
    function linkKey(c) { return c.key + ":" + c.slots.join("-"); }
    function updateLinks(state) {
      const found = findingsOf(state);
      const sig = found.map(function (c) { return linkKey(c) + ":" + c.severity; }).join("|");
      if (sig !== T.linkSig) {
        T.linkSig = sig;
        Object.keys(T.links).forEach(function (k) {
          const L = T.links[k];
          T.scene.remove(L.group);
          L.dispose();
          L.labelEl.remove();
        });
        T.links = {};
        found.forEach(function (c) {
          const L = buildLink(T.THREE, c.severity === "suspicious" ? T.pal.pen : T.pal.link, T.pal);
          L.from = SEATS[c.slots[0]];
          L.to = SEATS[c.slots[1]];
          L.labelEl = document.createElement("div");
          L.labelEl.className = "twin-link-label" + (c.severity === "suspicious" ? " is-suspicious" : "");
          L.labelEl.hidden = true;
          refs.labels.appendChild(L.labelEl);
          T.scene.add(L.group);
          T.links[linkKey(c)] = L;
        });
      }
      found.forEach(function (c) {
        const L = T.links[linkKey(c)];
        if (L) setText(L.labelEl, c.label + " · " + Math.round((c.confidence || 0) * 100) + "%");
      });
    }
    // Plain-text list beside the room, so every beam has its words and its
    // caveat next to it. Rebuilt only when the findings change.
    let listSig = null;
    function renderLinkList(state) {
      const found = findingsOf(state);
      const sig = found.map(function (c) { return linkKey(c) + Math.round((c.confidence || 0) * 100); }).join("|");
      if (sig === listSig) return;
      listSig = sig;
      refs.links.hidden = !found.length;
      refs.links.textContent = "";
      if (!found.length) return;
      const head = document.createElement("div");
      head.className = "twin-links-head";
      head.textContent = "Between students";
      refs.links.appendChild(head);
      found.forEach(function (c) {
        const row = document.createElement("div");
        row.className = "twin-link";
        const dot = document.createElement("span");
        dot.className = "twin-link-dot" + (c.severity === "suspicious" ? " is-suspicious" : "");
        const text = document.createElement("span");
        text.textContent = c.label + " · " + (c.pair || []).join(" & ") + " · " + Math.round((c.confidence || 0) * 100) + "%";
        row.append(dot, text);
        refs.links.appendChild(row);
      });
      const caveat = document.createElement("div");
      caveat.className = "twin-link-caveat";
      caveat.textContent = found[0].caveat || "Correlation is not proof of collaboration.";
      refs.links.appendChild(caveat);
    }

    // ── Seat buttons (rebuilt only when the list of names changes) ───
    function buildChips(state) {
      const st = state.students || {};
      const labels = SEATS.filter(function (l) { return st[l]; });
      const sig = labels.map(function (l) { return l + ":" + (st[l].display_name || l); }).join("|");
      if (sig === Sel.sig) return;
      Sel.sig = sig;
      Sel.chips = {};
      refs.seats.textContent = "";
      labels.forEach(function (l) {
        const name = st[l].display_name || l;
        const b = document.createElement("button");
        b.type = "button";
        b.className = "seg-btn twin-seat";
        b.dataset.twinSeat = l;
        b.title = "Show " + name + (name !== l ? " (" + l + ")" : "");
        const dot = document.createElement("span");
        dot.className = "status-dot status-dot--off";
        const text = document.createElement("span");
        text.textContent = name;
        b.append(dot, text);
        refs.seats.appendChild(b);
        Sel.chips[l] = { btn: b, dot: dot };
      });
    }
    function chooseSeat(st, shown) {
      if (Sel.manual && st[Sel.label]) return Sel.label;
      if (Sel.label && shown[Sel.label] === "present") return Sel.label;
      const present = T.order.find(function (l) { return shown[l] === "present"; });
      if (present) return present;
      if (Sel.label && shown[Sel.label]) return Sel.label;
      return T.order[0] || null;
    }
    function bindUi() {
      refs.seats.addEventListener("click", function (e) {
        const b = e.target.closest("[data-twin-seat]");
        if (b) select(b.dataset.twinSeat);
      });
      root.querySelectorAll("[data-twin-view]").forEach(function (b) {
        b.addEventListener("click", function () { setView(b.dataset.twinView); });
      });
      const selected = function () {
        const st = T.lastState && T.lastState.students;
        return st && Sel.label ? st[Sel.label] : null;
      };
      refs.recenter.addEventListener("click", function () {
        const p = selected();
        if (!p || !p.visible || !(p.calibration && p.calibration.complete)) return;
        centreState(p).manual = {
          yaw: p.head_pose && num(p.head_pose.filtered_yaw) ? p.head_pose.filtered_yaw : null,
          pitch: pitchOn(p) ? p.pitch.pitch : null,
        };
        applyState(T.lastState);
      });
      refs["recenter-reset"].addEventListener("click", function () {
        const p = selected();
        if (p) centreState(p).manual = null;
        if (T.lastState) applyState(T.lastState);
      });
    }

    // ── Readout for the selected student ─────────────────────────────
    function gauge(prefix, lo, hi, value, markCls) {
      const pos = function (v) { return clamp(50 + (v / GAUGE_HALF_RANGE) * 50, 0, 100); };
      const band = refs[prefix + "-band"], mark = refs[prefix + "-mark"];
      band.hidden = lo == null;
      if (lo != null) {
        band.style.left = pos(lo) + "%";
        band.style.width = (pos(hi) - pos(lo)) + "%";
      }
      mark.style.left = pos(value) + "%";
      setCls(mark, markCls);
    }
    function renderReadout(p, g) {
      const name = p.display_name || p.label;
      setText(refs.name, name + (name !== p.label ? " · " + p.label : ""));
      const lk = levelKey(p);
      setCls(refs.dot, "status-dot status-dot--" + lk);
      setCls(refs.level, "badge lvl-" + lk);
      setText(refs.level, levelText(p));
      setText(refs.tag, p.label + " · " + g.text);
      refs.tag.classList.toggle("is-away", g.key === "away");

      const hp = p.head_pose || {}, cal = p.calibration || {};
      const manual = centreState(p).manual;
      const yawAway = p.visible && cal.complete && hp.state === "AWAY";
      setText(refs["yaw-state"], !p.visible ? "—" : !cal.complete ? "Calibrating"
        : yawAway ? "Away · " + (hp.direction || "") : "Forward");
      setCls(refs["yaw-state"], "twin-axis-state" + (yawAway ? " is-away" : ""));
      setText(refs["yaw-val"], fmt(hp.filtered_yaw));
      setText(refs["yaw-off"], cal.complete ? offText(relYaw(p), "right", "left") : "—");
      setText(refs["yaw-ref"], cal.complete
        ? fmt(yawCentre(p)) + (manual && num(manual.yaw) ? " · re-centred" : " · calibration")
        : "calibrating " + (cal.samples || 0) + "/" + (cal.required || "—"));
      setText(refs["yaw-base"], cal.complete ? fmt(cal.baseline) : "—");
      setText(refs["yaw-thr"], cal.complete && num(cal.threshold) ? "± " + fmt(cal.threshold) : "—");
      // Gauge centre is straight ahead; the band is the engine's forward
      // zone around ITS live baseline, so drift shows as a sliding band.
      const yb = yawBaseOffset(p);
      gauge("yaw", cal.complete ? yb - cal.threshold : null, cal.complete ? yb + cal.threshold : null, relYaw(p),
        "twin-gauge-mark" + (!p.visible || !cal.complete ? " is-idle" : yawAway ? " is-away" : ""));

      const pt = p.pitch || {};
      const pOn = pitchOn(p);
      const pReady = pOn && pt.calibrated && num(pt.down_threshold_deg);
      const down = p.visible && !!pt.looking_down;
      setText(refs["pitch-state"], pt.recorded === false ? "Not recorded" : !pOn ? "Off" : !p.visible ? "—"
        : !pt.calibrated ? "Calibrating" : down ? "Looking down" : "Level");
      setCls(refs["pitch-state"], "twin-axis-state" + (down ? " is-away" : ""));
      setText(refs["pitch-val"], pOn ? fmt(pt.pitch) : "—");
      setText(refs["pitch-off"], pOn && pt.calibrated ? offText(relDown(p), "down", "up") : "—");
      setText(refs["pitch-ref"], pOn && pt.calibrated
        ? fmt(pitchCentre(p)) + (manual && num(manual.pitch) ? " · re-centred" : " · calibration")
        : pOn ? "calibrating" : "—");
      setText(refs["pitch-base"], pOn && pt.calibrated ? fmt(pt.baseline) : "—");
      setText(refs["pitch-thr"], pReady ? fmt(pt.down_threshold_deg) + " down" : "—");
      // Left is up, right is down; the band runs up to the engine's bar.
      gauge("pitch", pReady ? -GAUGE_HALF_RANGE : null,
        pReady ? downBaseOffset(p) + pt.down_threshold_deg : null, relDown(p),
        "twin-gauge-mark" + (!p.visible || !pReady ? " is-idle" : down ? " is-away" : ""));

      refs.recenter.disabled = !(p.visible && cal.complete);
      if (refs["recenter-reset"].hidden !== !manual) refs["recenter-reset"].hidden = !manual;
    }

    // ── One update ───────────────────────────────────────────────────
    function applyState(state) {
      const st = state.students || {};
      const shown = {};
      SEATS.forEach(function (label) {
        const p = st[label];
        if (!p) return;
        const vis = seatVisibility(state, label, p);
        if (vis !== "hidden") shown[label] = vis;
      });
      const order = orderSeats(state, Object.keys(shown));
      const orderChanged = order.join() !== T.order.join();
      T.order = order;

      buildChips(state);
      const label = chooseSeat(st, shown);
      Sel.label = label;
      Object.keys(Sel.chips).forEach(function (l) {
        const c = Sel.chips[l], sp = st[l];
        setCls(c.btn, "seg-btn twin-seat" + (l === label ? " is-active" : "") + (sp && sp.visible ? "" : " is-absent"));
        setCls(c.dot, "status-dot status-dot--" + (sp && sp.visible ? levelKey(sp) : "off"));
      });

      const p = label ? st[label] : null;
      if (p) renderReadout(p, gazeState(p));
      else setText(refs.tag, "");
      renderLinkList(state);
      if (T.status !== "ready") return;
      msg(order.length ? "" : (opts.emptyMessage || "Waiting for the engine to report a student…"));

      SEATS.forEach(function (l) {
        const vis = shown[l];
        if (!vis) {
          if (T.seats[l]) T.seats[l].root.visible = false;
          return;
        }
        const s = ensureSeat(l);
        s.targetX = ((order.length - 1) / 2 - order.indexOf(l)) * DESK_SPACING;
        if (!s.root.visible) {
          s.root.visible = true;
          s.x = s.targetX;
          s.root.position.x = s.x;
        }
        applySeat(s, st[l], vis, l === label);
      });
      updateFans(p && shown[label] ? p : null);
      updateLinks(state);
      if (orderChanged && !T.userMoved && VIEWS[T.view].room) setView(T.view);
      wake();
    }

    function fail(text) {
      T.status = "failed";
      msg(text);
      console.warn("[digital twin] " + text);
    }
    function start() {
      T.status = "loading";
      bindUi();
      loadThree().then(function (THREE) {
        try {
          initScene(THREE);
        } catch (e) {
          fail(e.message === "webgl"
            ? "3D view unavailable: this browser could not start WebGL. The readout on the right still works."
            : "3D view could not start (" + e.message + "). The readout on the right still works.");
          return;
        }
        T.status = "ready";
        msg("");
        fitCanvas();
        if (T.lastState) applyState(T.lastState);
        setView(T.view, true);
      }).catch(function () {
        fail("3D view unavailable: Three.js could not be loaded. It comes from a public CDN when online; " +
             "to run offline, save three.min.js (r149) as dashboard/vendor/three.min.js.");
      });
    }

    return {
      root: root,
      refs: refs,
      update: function (state) {
        if (!state) return;
        T.lastState = state;
        if (T.status === "idle") start();
        applyState(state);
      },
      select: select,
      retheme: retheme,
      // Inspection only (browser console / tests). Changes nothing.
      debug: function () {
        const seats = {};
        Object.keys(T.seats).forEach(function (l) {
          const s = T.seats[l];
          seats[l] = {
            shown: s.root.visible, desk: T.order.indexOf(l), x: +s.x.toFixed(2), targetX: +s.targetX.toFixed(2),
            yawRightDeg: +(s.yaw * 180 / Math.PI).toFixed(2), pitchDownDeg: +(s.pitch * 180 / Math.PI).toFixed(2),
            targetYawRightDeg: +(s.targetYaw * 180 / Math.PI).toFixed(2),
            targetPitchDownDeg: +(s.targetPitch * 180 / Math.PI).toFixed(2), ghost: +s.ghost.toFixed(2),
          };
        });
        return {
          status: T.status, view: T.view, selected: Sel.label, order: T.order.slice(), seats: seats,
          links: Object.keys(T.links), fansOn: !!(T.fans && T.fans.group.visible && T.fans.group.parent),
          fansOnSeat: T.fans && T.fans.group.parent ? (Object.keys(T.seats).find(function (l) { return T.seats[l].root === T.fans.group.parent; }) || null) : null,
          rendering: !!T.raf, userMoved: T.userMoved, gain: ANGLE_GAIN,
          orbit: { dist: +T.orbit.dist.toFixed(2), tx: +T.orbit.tx.toFixed(2) },
          mirrored: !!(T.canvas && T.canvas.classList.contains("is-mirrored")),
        };
      },
    };
  }

  // ── Public interface ──────────────────────────────────────────────
  let liveTwin = null;
  const instances = [];
  window.DigitalTwin = {
    // A new, independent room panel inside `rootEl` (session_replay.js).
    create: function (rootEl, options) {
      const t = createTwin(rootEl, options);
      instances.push(t);
      return t;
    },
    // The Live Monitor's panel, created on first use; called by dashboard.js.
    update: function (state) {
      if (!liveTwin) {
        const el = document.querySelector('[data-twin-root="live"]');
        if (!el) return;
        liveTwin = createTwin(el, { instance: "live" });
        instances.push(liveTwin);
      }
      liveTwin.update(state);
    },
    // Select a student in the Live room (the seat cards' "Show in 3D room").
    select: function (label) { if (liveTwin) liveTwin.select(label); },
    // Re-read the theme's colours in every room on the page.
    retheme: function () { instances.forEach(function (t) { t.retheme(); }); },
    // Pure: how a student's head is drawn (for the flat gaze dials).
    gazeView: gazeView,
    debug: function () { return liveTwin ? liveTwin.debug() : { status: "not created" }; },
  };
})();

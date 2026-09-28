/**
 * login.js — the invigilator sign-in page (see dashboard_auth.py).
 *
 * Three cases, decided by the server (/api/auth/state), never guessed here:
 *  - accounts exist            -> sign in
 *  - no account, this computer -> create the first account
 *  - no account, another device -> explain where to create it; the first
 *    account can only be made on the computer running the monitor, so
 *    nobody else on the network can claim the dashboard first.
 */
(function () {
  "use strict";

  const $ = function (id) { return document.getElementById(id); };
  const HEADERS = { "Content-Type": "application/json", "X-Requested-With": "exam-monitor" };
  let mode = "signin";

  // Only ever return to a page on this site.
  function nextUrl() {
    const n = new URLSearchParams(location.search).get("next") || "/";
    return /^\/(?![\/\\])/.test(n) ? n : "/";
  }
  function setError(msg) {
    $("auth-error").textContent = msg || "";
    $("auth-error").hidden = !msg;
  }
  function setMode(m, st) {
    mode = m;
    const form = $("auth-form");
    if (m === "remote-setup") {
      $("auth-eyebrow").textContent = "First run";
      $("auth-title").textContent = "No account yet";
      $("auth-lede").textContent = "Before anyone can sign in, an invigilator account has to be created. For safety that can only be done on the computer running the exam monitor.";
      $("auth-help").innerHTML = "On that computer, open <code>http://127.0.0.1:5000</code>, or run <code>python dashboard_auth.py add NAME</code> in the project folder.";
      form.hidden = true;
      return;
    }
    form.hidden = false;
    if (m === "setup") {
      $("auth-eyebrow").textContent = "First run";
      $("auth-title").textContent = "Create the invigilator account";
      $("auth-lede").textContent = "This is the first time the dashboard has been opened. Choose a name and a password; you will use them to sign in from now on.";
      $("auth-pass").autocomplete = "new-password";
      $("auth-pass2-row").hidden = false;
      $("auth-submit").textContent = "Create account and sign in";
      $("auth-help").innerHTML = "More accounts, or a forgotten password: <code>python dashboard_auth.py add NAME</code> / <code>passwd NAME</code>.";
    } else {
      $("auth-help").innerHTML = "Forgot the password? On the computer running the monitor: <code>python dashboard_auth.py passwd NAME</code>.";
    }
    $("auth-user").focus();
  }

  function submit(e) {
    e.preventDefault();
    setError("");
    const user = $("auth-user").value.trim(), pass = $("auth-pass").value;
    if (!user || !pass) { setError("Enter a name and a password."); return; }
    if (mode === "setup") {
      if (pass.length < 8) { setError("Use a password of at least 8 characters."); return; }
      if (pass !== $("auth-pass2").value) { setError("The two passwords don't match."); return; }
    }
    const btn = $("auth-submit");
    btn.disabled = true;
    fetch(mode === "setup" ? "/api/auth/setup" : "/api/auth/login", {
      method: "POST", headers: HEADERS, credentials: "same-origin",
      body: JSON.stringify({ username: user, password: pass }),
    })
      .then(function (res) { return res.json().catch(function () { return {}; }).then(function (j) { return { ok: res.ok, j: j }; }); })
      .then(function (out) {
        if (out.ok) { location.replace(nextUrl()); return; }
        setError(out.j.error || "Sign-in failed.");
        $("auth-pass").value = "";
        $("auth-pass").focus();
        btn.disabled = false;
      })
      .catch(function () {
        setError("Could not reach the exam monitor. Is it still running?");
        btn.disabled = false;
      });
  }

  $("auth-form").addEventListener("submit", submit);
  fetch("/api/auth/state", { credentials: "same-origin" })
    .then(function (r) { return r.json(); })
    .then(function (st) {
      if (!st.enabled || st.user) { location.replace(nextUrl()); return; }
      if (st.setup_needed) setMode(st.setup_allowed ? "setup" : "remote-setup", st);
      else setMode("signin", st);
    })
    .catch(function () {
      setMode("signin");
      setError("Could not reach the exam monitor. Is it still running?");
    });
})();

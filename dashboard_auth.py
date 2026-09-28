"""
dashboard_auth.py — invigilator sign-in for the web dashboard.

WHY THIS EXISTS
───────────────────────────────────────────────────────────────────────
Until Sep 2026 the dashboard had no login at all. Anyone who could reach
the port could watch live camera frames of students, read every score,
answer identity questions, lock the roster, and flip the whole page to
demo data mid-exam (POST /api/mode). Harmless while testing alone on
127.0.0.1; not acceptable in a classroom, or in an evaluation demo on a
shared network. This module is the sign-in the dashboard now requires.

WHAT IT STORES — access/users.json (next to this file, never served)
  - a random secret key that signs the sign-in cookie, so a sign-in
    survives a restart and cannot be forged
  - accounts: {name: {"hash": ..., "created": ...}}. Passwords are kept
    ONLY as salted hashes (werkzeug, which Flask already installs); the
    password itself is never written anywhere.

FIRST RUN
  With no account yet, the sign-in page offers to create one — but only
  to a browser on the SAME computer as the server. Otherwise whoever
  reached the page first over the network could claim the dashboard.

COMMAND LINE (manage accounts without a browser)
  python dashboard_auth.py add NAME       create an account
  python dashboard_auth.py passwd NAME    change a password
  python dashboard_auth.py remove NAME    delete an account
  python dashboard_auth.py list           show account names
Changing a password or removing an account signs that account out of
every browser at once (each sign-in cookie carries a fingerprint of the
password hash it was issued against).

ACCESS LOG — access/access_log.jsonl
  Append-only: every sign-in, failed sign-in, lockout and sign-out, with
  time and address. Same principle as the rest of the project: nothing
  about who looked at student data is silently lost.

LIMITS (stated, not hidden)
  - Plain HTTP. On a network you don't trust, a password can be read in
    transit. Keep Config.DASHBOARD_HOST on 127.0.0.1 unless you need
    another device to connect.
  - One kind of account: every signed-in user can do everything.
"""

import getpass
import hashlib
import json
import os
import re
import secrets
import sys
import threading
import time
from datetime import datetime

from werkzeug.security import check_password_hash, generate_password_hash

_HERE = os.path.dirname(os.path.abspath(__file__))
ACCESS_DIR = os.path.join(_HERE, "access")
USERS_PATH = os.path.join(ACCESS_DIR, "users.json")
LOG_PATH = os.path.join(ACCESS_DIR, "access_log.jsonl")

MIN_PASSWORD_LEN = 8
_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,32}$")

# Failed sign-ins from one address before it has to wait, and how long.
# Each further lockout doubles the wait, up to the cap: a person who
# mistypes twice is never slowed down; a script guessing is.
_FREE_ATTEMPTS = 5
_FIRST_LOCK_SEC = 30.0
_MAX_LOCK_SEC = 15 * 60.0

_LOOPBACK = {"127.0.0.1", "::1", "::ffff:127.0.0.1"}


def is_loopback(addr) -> bool:
    return (addr or "") in _LOOPBACK


def validate_name(name):
    if not _NAME_RE.match(name or ""):
        return "Use 1-32 letters, digits, dots, dashes or underscores for the name."
    return None


def validate_password(pw):
    if len(pw or "") < MIN_PASSWORD_LEN:
        return f"Use a password of at least {MIN_PASSWORD_LEN} characters."
    return None


class DashboardAuth:
    """Accounts, sign-in checks, lockouts and the access log."""

    def __init__(self, enabled=True, users_path=USERS_PATH, log_path=LOG_PATH):
        self.enabled = bool(enabled)
        self._users_path = users_path
        self._log_path = log_path
        self._lock = threading.Lock()
        self._fails = {}   # addr -> {"n": failures, "locks": lockouts so far, "until": t}
        self._data = self._load()
        # Checked when the name is unknown, so a wrong name takes as long
        # to reject as a wrong password and names can't be probed.
        self._dummy_hash = generate_password_hash(secrets.token_hex(8))

    # ── storage ──────────────────────────────────────────────────────
    def _load(self):
        data = {}
        if os.path.exists(self._users_path):
            with open(self._users_path, encoding="utf-8") as f:
                data = json.load(f)
        changed = False
        if not isinstance(data.get("secret_key"), str) or len(data["secret_key"]) < 32:
            data["secret_key"] = secrets.token_hex(32)
            changed = True
        if not isinstance(data.get("accounts"), dict):
            data["accounts"] = {}
            changed = True
        if changed:
            self._save(data)
        return data

    def _save(self, data):
        os.makedirs(os.path.dirname(self._users_path), exist_ok=True)
        tmp = self._users_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, self._users_path)

    @property
    def secret_key(self):
        return self._data["secret_key"]

    def has_accounts(self):
        with self._lock:
            return bool(self._data["accounts"])

    def account_names(self):
        with self._lock:
            return sorted(self._data["accounts"])

    def add_account(self, name, password, replace=False):
        """-> None on success, else a message saying what is wrong."""
        err = validate_name(name) or validate_password(password)
        if err:
            return err
        with self._lock:
            if name in self._data["accounts"] and not replace:
                return f"An account called '{name}' already exists."
            if replace and name not in self._data["accounts"]:
                return f"There is no account called '{name}'."
            self._data["accounts"][name] = {
                "hash": generate_password_hash(password),
                "created": datetime.now().isoformat(timespec="seconds"),
            }
            self._save(self._data)
        return None

    def remove_account(self, name):
        with self._lock:
            if name not in self._data["accounts"]:
                return f"There is no account called '{name}'."
            del self._data["accounts"][name]
            self._save(self._data)
        return None

    # ── sign-in ──────────────────────────────────────────────────────
    def _fingerprint(self, name):
        acct = self._data["accounts"].get(name)
        if not acct:
            return None
        return hashlib.sha256(acct["hash"].encode()).hexdigest()[:16]

    def wait_seconds(self, addr):
        """Seconds this address must still wait before trying again."""
        with self._lock:
            f = self._fails.get(addr)
            return max(0.0, f["until"] - time.time()) if f else 0.0

    def check_password(self, name, password, addr):
        """-> (ok, fingerprint_or_message). Counts failures per address."""
        with self._lock:
            acct = self._data["accounts"].get(name or "")
            stored = acct["hash"] if acct else self._dummy_hash
        ok = check_password_hash(stored, password or "") and acct is not None
        with self._lock:
            if ok:
                self._fails.pop(addr, None)
                return True, self._fingerprint(name)
            f = self._fails.setdefault(addr, {"n": 0, "locks": 0, "until": 0.0})
            f["n"] += 1
            if f["n"] >= _FREE_ATTEMPTS:
                wait = min(_FIRST_LOCK_SEC * (2 ** f["locks"]), _MAX_LOCK_SEC)
                f["until"] = time.time() + wait
                f["locks"] += 1
                f["n"] = 0
                self.log("lockout", name, addr, f"{wait:.0f}s")
        return False, "That name and password don't match."

    def session_user(self, session):
        """The signed-in account for this cookie, or None. A password
        change or account removal invalidates older cookies."""
        name = session.get("user")
        if not name:
            return None
        with self._lock:
            fp = self._fingerprint(name)
        if fp is None or session.get("fp") != fp:
            return None
        return name

    # ── access log ───────────────────────────────────────────────────
    def log(self, event, name, addr, detail=""):
        rec = {"time": datetime.now().isoformat(timespec="seconds"),
               "event": event, "user": name or "", "addr": addr or "",
               "detail": detail}
        try:
            os.makedirs(os.path.dirname(self._log_path), exist_ok=True)
            with open(self._log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec) + "\n")
        except OSError as e:
            print(f"[WARN] access log not written: {e}")
        print(f"[AUTH] {event} user={name or '-'} from {addr or '-'} {detail}".rstrip())


def safe_host(host, login_enabled):
    """The address to serve on. Refuses to expose an unprotected
    dashboard to the network: with login off, only this computer."""
    if not login_enabled and host not in ("127.0.0.1", "localhost", "::1"):
        print("[WARN] Dashboard login is off, so the dashboard is served on "
              "127.0.0.1 only (Config.DASHBOARD_HOST ignored).")
        return "127.0.0.1"
    return host


def lan_address():
    """This computer's address on the local network, for the start-up hint
    when serving beyond localhost. Sends nothing (UDP connect only picks a
    route)."""
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        addr = s.getsockname()[0]
        s.close()
        return addr
    except OSError:
        return None


def print_urls(host, port):
    print(f"[INFO] Dashboard available at: http://127.0.0.1:{port}")
    if host not in ("127.0.0.1", "localhost", "::1"):
        lan = lan_address()
        if lan:
            print(f"[INFO] Other devices on this network: http://{lan}:{port}")


# ── command line ─────────────────────────────────────────────────────
def _ask_password():
    while True:
        pw = getpass.getpass("New password: ")
        err = validate_password(pw)
        if err:
            print(err)
            continue
        if getpass.getpass("Repeat it: ") != pw:
            print("Those didn't match. Try again.")
            continue
        return pw


def _main(argv):
    usage = ("usage: python dashboard_auth.py add NAME | passwd NAME | "
             "remove NAME | list")
    if not argv or argv[0] not in ("add", "passwd", "remove", "list"):
        print(usage)
        return 2
    auth = DashboardAuth()
    cmd = argv[0]
    if cmd == "list":
        names = auth.account_names()
        print("\n".join(names) if names else "No accounts yet.")
        return 0
    if len(argv) != 2:
        print(usage)
        return 2
    name = argv[1]
    if cmd == "remove":
        err = auth.remove_account(name)
        if not err:
            auth.log("account removed", name, "command line")
    else:
        err = validate_name(name)
        if not err:
            err = auth.add_account(name, _ask_password(), replace=(cmd == "passwd"))
        if not err:
            auth.log("account created" if cmd == "add" else "password changed",
                     name, "command line")
    print(err or "Done.")
    return 1 if err else 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))

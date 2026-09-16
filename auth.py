"""Accounts: argon2id passwords, signed-cookie sessions, CSRF on writes.

Reads stay open. Looking at tonight's targets is not sensitive, and requiring
a password to *view* would mean observers fumbling credentials on a dome
screen. Only routes that change state are protected -- but now that the board
answers on the public internet, "protected" has to mean something better than
one shared password pasted into a file.

Three pieces:

  passwords   argon2id via argon2-cffi, with the library's own parameters.
              Nothing here is reversible and nothing is ever logged.

  sessions    Flask's signed cookie. The cookie carries a user id and a CSRF
              token, both signed with a key that lives in data/ and is
              generated once. It is not encrypted -- a signed cookie is
              tamper-evident, not secret -- so it holds an id and nothing
              worth reading.

  CSRF        every state-changing form carries a token that must match the
              one in the session. Without it any page on the internet could
              POST /mark on a logged-in observer's behalf, and the browser
              would attach the cookie for them.

The old shared basic-auth credential still works, deliberately. Luka's team is
mid-season; locking them out the moment this deploys, on the strength of a
schema change nobody has used yet, would be the wrong trade. Delete data/auth
when everyone has an account.
"""

import hmac
import os
import re
import secrets
import time
from functools import wraps

from flask import (Response, g, redirect, request, session, url_for)

import config
import db

try:
    from argon2 import PasswordHasher
    from argon2.exceptions import (InvalidHashError, VerificationError,
                                   VerifyMismatchError)
    _HASHER = PasswordHasher()
except ImportError:                                  # pragma: no cover
    # Soft, on purpose. A missing dependency must not take the board down:
    # reads are the thing observers need at 3 a.m., and they need no hashing.
    # Sign-in says so plainly instead of serving a traceback -- see
    # HASHING_AVAILABLE, which the login and register routes check.
    _HASHER = None

HASHING_AVAILABLE = _HASHER is not None
NO_HASHING = ("Password hashing is unavailable on this host: argon2-cffi is "
              "not installed. Run `pip install -r requirements.txt` and "
              "restart the site. Reading the board is unaffected.")


# --- secret key -------------------------------------------------------------

SECRET_PATH = os.path.join(config.DATA_DIR, "secret_key")


def secret_key():
    """The session-signing key, generated once and kept in data/.

    It must survive a restart: regenerating it invalidates every session, so
    a routine deploy would log the whole observatory out mid-night. It must
    also never reach the repository, which is why it is a file in data/ and
    not a constant in config.py.
    """
    env = os.environ.get("WHICHNEO_SECRET_KEY")
    if env:
        return env.encode()
    try:
        with open(SECRET_PATH, "rb") as f:
            key = f.read().strip()
        if key:
            return key
    except OSError:
        pass
    key = secrets.token_hex(32).encode()
    os.makedirs(config.DATA_DIR, exist_ok=True)
    # Written 0600 from the start rather than chmod'ed afterwards -- between
    # the two there is a moment where the key is world-readable.
    fd = os.open(SECRET_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(key)
    return key


# --- passwords --------------------------------------------------------------

MIN_PASSWORD = 10
USERNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{1,31}$")


def hash_password(password):
    return _HASHER.hash(password)


def verify_password(stored_hash, password):
    """Returns (ok, new_hash or None).

    The second element is an upgraded hash when argon2's parameters have moved
    on since this password was set. The caller is expected to store it; that is
    the only way an old account's hash ever gets stronger.
    """
    try:
        _HASHER.verify(stored_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False, None
    try:
        if _HASHER.check_needs_rehash(stored_hash):
            return True, _HASHER.hash(password)
    except Exception:
        pass
    return True, None


def username_error(name):
    if not name:
        return "Pick a username."
    if not USERNAME_RE.match(name):
        return ("Usernames are 2-32 characters: letters, digits, dot, dash or "
                "underscore, starting with a letter or digit.")
    return None


def password_error(password, confirm=None):
    if not password or len(password) < MIN_PASSWORD:
        return f"Passwords need at least {MIN_PASSWORD} characters."
    if confirm is not None and password != confirm:
        return "The two passwords do not match."
    return None


# --- login throttle ---------------------------------------------------------
#
# A public login form with no throttle is an open invitation to guess. argon2
# already makes each attempt cost real CPU, which is a defence against offline
# cracking but also a denial-of-service surface online: enough parallel wrong
# guesses and the board stops rendering. This caps both.

FAIL_LIMIT = 8
FAIL_WINDOW_S = 900
_fails = {}


def _throttle_key():
    return request.headers.get("X-Forwarded-For", request.remote_addr or "?")


def throttled():
    hits = [t for t in _fails.get(_throttle_key(), [])
            if t > time.time() - FAIL_WINDOW_S]
    return len(hits) >= FAIL_LIMIT


def note_failure():
    key = _throttle_key()
    cutoff = time.time() - FAIL_WINDOW_S
    hits = [t for t in _fails.get(key, []) if t > cutoff] + [time.time()]
    _fails[key] = hits
    # Opportunistic sweep. Without it every address that ever mistyped a
    # password stays in the dict for the life of the process.
    if len(_fails) > 512:
        for k in [k for k, v in _fails.items() if not any(t > cutoff for t in v)]:
            _fails.pop(k, None)


def clear_failures():
    _fails.pop(_throttle_key(), None)


# --- sessions ---------------------------------------------------------------

def start_session(user):
    session.clear()
    session["uid"] = user["id"]
    session["csrf"] = secrets.token_urlsafe(32)
    session.permanent = True


def end_session():
    session.clear()


def current_user():
    """The logged-in account, or None. Cached per request.

    Looked up fresh from the database rather than trusted from the cookie
    beyond the id: an account deleted or demoted between requests should stop
    working on the next one, not when the cookie happens to expire.
    """
    if "whichneo_user" in g:
        return g.whichneo_user
    uid = session.get("uid")
    user = None
    if uid:
        conn = db.connect()
        try:
            user = db.user_by_id(conn, uid)
        finally:
            conn.close()
        if user is None:
            session.clear()
    g.whichneo_user = user
    return user


def display_name():
    """Who the writes are attributed to: an account name, or the shared
    credential's username when someone is still on basic auth."""
    user = current_user()
    if user:
        return user["username"]
    if _basic_valid(request.authorization):
        return BASIC_CREDENTIALS[0]
    return None


# --- CSRF -------------------------------------------------------------------

def csrf_token():
    """The session's token, minted on first use so a logged-out visitor's
    page still renders a form that will work once they log in."""
    token = session.get("csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf"] = token
    return token


def csrf_ok():
    sent = request.form.get("csrf") or request.headers.get("X-CSRF-Token") or ""
    held = session.get("csrf") or ""
    if not held or not sent:
        # A request authenticated by basic auth carries no session and so no
        # token. That is the transitional path -- a script with the shared
        # password, not a browser that can be tricked into posting -- and CSRF
        # does not apply to it.
        return _basic_valid(request.authorization)
    return hmac.compare_digest(sent, held)


# --- transitional basic auth ------------------------------------------------

def _load_basic():
    raw = os.environ.get("WHICHNEO_AUTH")
    if not raw:
        try:
            with open(os.path.join(config.DATA_DIR, "auth")) as f:
                raw = f.read().strip()
        except OSError:
            return None
    if not raw or ":" not in raw:
        return None
    user, _, password = raw.partition(":")
    return user.strip(), password.strip()


BASIC_CREDENTIALS = _load_basic()
BASIC_ENABLED = BASIC_CREDENTIALS is not None

# Writes are protected unconditionally now. The old flag said "a password file
# exists"; it now says what the status JSON actually meant by it, which is
# whether an anonymous visitor can change state. They cannot.
ENABLED = True


def _basic_valid(authorization):
    if not authorization or BASIC_CREDENTIALS is None:
        return False
    user, password = BASIC_CREDENTIALS
    # compare_digest on both halves: equality on the username alone would
    # leak its length through timing.
    return (hmac.compare_digest(authorization.username or "", user)
            and hmac.compare_digest(authorization.password or "", password))


# --- the decorator ----------------------------------------------------------

def _wants_html():
    return "text/html" in (request.headers.get("Accept") or "")


def _deny():
    """A browser gets sent to the login form; anything else gets a 401.

    Redirecting a curl script to an HTML page would turn a clear failure into
    a confusing 200, and redirecting a browser to a basic-auth prompt would
    ask for a credential we are trying to retire.
    """
    if request.authorization is not None or not _wants_html():
        return Response(
            "Authentication required to change target state.\n", 401,
            {"WWW-Authenticate": 'Basic realm="WhichNEO", charset="UTF-8"'})
    return redirect(url_for("login", next=request.referrer or url_for("index")))


def required(view):
    """Protect a state-changing route: a session or the shared credential,
    plus a matching CSRF token."""
    @wraps(view)
    def wrapper(*a, **kw):
        if current_user() is None and not _basic_valid(request.authorization):
            return _deny()
        if not csrf_ok():
            return Response(
                "Stale or missing form token. Reload the page and try again.\n",
                400)
        return view(*a, **kw)
    return wrapper

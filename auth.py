"""Optional HTTP basic auth for the state-changing routes.

Off by default, because on the observatory LAN an observer should not have to
log in mid-session to mark a target done. It matters when the board is
reachable from the public internet: without it, anyone who finds the URL can
mark targets observed, hide them or reorder the queue.

Credentials come from the environment or a file, never from the repository:

    export WHICHNEO_AUTH='user:password'
    # or
    echo 'user:password' > data/auth   # chmod 600

Reads are left open on purpose. Looking at tonight's targets is not sensitive,
and requiring a password to *view* would mean observers fumbling credentials
on a dome screen. Only routes that change state are protected.
"""

import hmac
import os
from functools import wraps

from flask import Response, request

import config


def _load():
    """Returns (user, password) or None if no credentials are configured."""
    raw = os.environ.get("WHICHNEO_AUTH")
    if not raw:
        path = os.path.join(config.DATA_DIR, "auth")
        try:
            with open(path) as f:
                raw = f.read().strip()
        except OSError:
            return None
    if not raw or ":" not in raw:
        return None
    user, _, password = raw.partition(":")
    return user.strip(), password.strip()


CREDENTIALS = _load()
ENABLED = CREDENTIALS is not None


def _valid(auth):
    if not auth or CREDENTIALS is None:
        return False
    user, password = CREDENTIALS
    # compare_digest on both halves: equality on the username alone would
    # leak its length through timing.
    return (hmac.compare_digest(auth.username or "", user)
            and hmac.compare_digest(auth.password or "", password))


def required(view):
    """Protect a route. A no-op when no credentials are configured."""
    @wraps(view)
    def wrapper(*a, **kw):
        if not ENABLED or _valid(request.authorization):
            return view(*a, **kw)
        return Response(
            "Authentication required to change target state.\n", 401,
            {"WWW-Authenticate": 'Basic realm="WhichNEO", charset="UTF-8"'})
    return wrapper

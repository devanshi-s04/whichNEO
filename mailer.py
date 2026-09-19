"""Outgoing mail over Mario's relay at mail.infra.juriclab.org.

The board sends very little: password resets, and nothing else. That shapes
every decision here.

**Sends happen on a background thread.** Not for throughput -- we send maybe
one message a month -- but because the alternative leaks. A /forgot form that
answers quickly for an unknown address and slowly for a real one tells a
stranger which accounts exist, which is the first half of a password guess.
Handing the send to a thread makes both answers instantly identical.

**A failed send is logged, never surfaced.** The observer asking for a reset
must not learn from an error whether their address is on file. The log is
where a failure gets noticed; `manage.py mailtest` is how it gets diagnosed.

Credentials never reach the repository:

    echo 'the-password' > data/smtp_password   # chmod 600
    # or
    export WHICHNEO_SMTP_PASSWORD='...'
"""

import logging
import os
import smtplib
import ssl
import threading
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

import config

log = logging.getLogger(__name__)


def password():
    """The relay password, from the environment or data/smtp_password."""
    env = os.environ.get("WHICHNEO_SMTP_PASSWORD")
    if env:
        return env
    try:
        with open(os.path.join(config.DATA_DIR, "smtp_password")) as f:
            return f.read().strip() or None
    except OSError:
        return None


def available():
    """Whether mail can be sent at all. The site has to work without it --
    epyc had no relay for the whole first season, and a board that refuses to
    render because it cannot send email is a worse failure than no email."""
    return bool(config.SMTP_HOST and config.SMTP_USER and password())


def build(to, subject, body, reply_to=None):
    msg = EmailMessage()
    msg["From"] = config.SMTP_FROM
    msg["To"] = to
    msg["Subject"] = subject
    # So replying to a feedback message reaches whoever wrote it. Set only
    # from an address the caller has already validated -- a header taking a
    # newline is how a form becomes a way to send mail to strangers.
    if reply_to and "\n" not in reply_to and "\r" not in reply_to:
        msg["Reply-To"] = reply_to
    msg["Date"] = formatdate(localtime=True)
    # An explicit Message-ID with our own domain rather than the relay's
    # guess: it is what makes a bounce traceable back to this host.
    msg["Message-ID"] = make_msgid(domain=config.SMTP_FROM.split("@")[-1])
    msg["Auto-Submitted"] = "auto-generated"   # keeps vacation responders off
    msg.set_content(body)
    return msg


def send_now(to, subject, body, reply_to=None):
    """Synchronous send. Raises on failure. Used by manage.py mailtest, where
    an exception is exactly what the operator wants to see."""
    if not available():
        raise RuntimeError(
            "No SMTP credentials: set WHICHNEO_SMTP_PASSWORD or write "
            f"{os.path.join(config.DATA_DIR, 'smtp_password')}")
    msg = build(to, subject, body, reply_to=reply_to)
    with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT,
                      timeout=config.SMTP_TIMEOUT_S) as s:
        s.ehlo()
        if config.SMTP_STARTTLS:
            # Verified against the system trust store. Without a context
            # STARTTLS still encrypts, but against anyone who answers -- which
            # is no protection at all from something already on the path.
            s.starttls(context=ssl.create_default_context())
            s.ehlo()
        s.login(config.SMTP_USER, password())
        s.send_message(msg)
    return msg["Message-ID"]


def send(to, subject, body):
    """Fire and forget. Returns immediately; see the module docstring."""
    if not available():
        log.warning("mail not configured, dropping message to %s (%s)",
                    to, subject)
        return False

    def worker():
        try:
            mid = send_now(to, subject, body)
            log.info("sent %r to %s (%s)", subject, to, mid)
        except Exception as e:
            # Deliberately swallowed. Nothing upstream may branch on this --
            # see the module docstring on why the caller must not find out.
            log.error("mail to %s failed: %s: %s", to, type(e).__name__, e)

    threading.Thread(target=worker, name="mailer", daemon=True).start()
    return True


def send_reporting(to, subject, body, reply_to=None, on_done=None):
    """Like send(), but says afterwards whether it worked.

    send() swallows failures on purpose: an error reaching the caller of a
    password reset tells a stranger whether an address is on file. Feedback
    has no such secret to keep, and something has to record that a message
    never went out -- otherwise storing it gains nothing over mailing it.

    `on_done(error)` is called on the sending thread with None on success, so
    it must do its own database connection rather than borrow the request's.
    """
    def worker():
        error = None
        try:
            if not available():
                raise RuntimeError("mail not configured")
            mid = send_now(to, subject, body, reply_to=reply_to)
            log.info("sent %r to %s (%s)", subject, to, mid)
        except Exception as e:
            error = f"{type(e).__name__}: {e}"
            log.error("mail to %s failed: %s", to, error)
        if on_done is not None:
            try:
                on_done(error)
            except Exception:
                log.exception("feedback delivery callback failed")

    threading.Thread(target=worker, name="mailer-reporting",
                     daemon=True).start()

"""Account administration from the shell.

The website has a reset-by-email link, but it only reaches accounts that have
an email address on file, and it stays silent when the relay is down --
deliberately, so that the form cannot be used to find out which accounts
exist. So the shell stays the fallback for both: `passwd` for an account with
no email, and `mailtest` for finding out why nothing arrived.

Passwords are read from a prompt, never from argv -- an argument is visible in
`ps` to every user on the machine and lands in shell history besides.

`adduser` needs a terminal to type a password into. `invite` does not -- it
creates the account with an unusable password and mails its owner a link to
choose their own, so nothing secret is typed, printed, or left in a shell
history. Prefer it.

    python3 manage.py list
    python3 manage.py invite <username> --email a@b [--admin] [--resend]
    python3 manage.py adduser <username> [--email a@b] [--admin]
    python3 manage.py passwd <username>
    python3 manage.py admin <username> [--off]
    python3 manage.py deluser <username>
    python3 manage.py mailtest <address>
"""

import argparse
import getpass
import os
import sqlite3
import sys

import auth
import config
import db


def _prompt_password():
    """Read a password twice from the terminal.

    Returns None on any refusal, having already said why. The no-terminal case
    is called out explicitly because getpass raises EOFError there, which as a
    traceback tells you nothing about what to do instead -- and it is not an
    exotic situation: it is what happens whenever this is run through a
    wrapper that does not allocate a tty, which is most of them.
    """
    if not sys.stdin.isatty():
        print("No terminal to type a password into.\n"
              "  Run this from a shell with a tty, or use:\n"
              "      manage.py invite <username> --email <address>\n"
              "  which mails the person a link to set their own password.",
              file=sys.stderr)
        return None
    try:
        first = getpass.getpass("Password: ")
        second = getpass.getpass("Repeat: ")
    except (EOFError, OSError) as e:
        print(f"Could not read a password from this terminal ({e}). "
              "Try `manage.py invite` instead.", file=sys.stderr)
        return None
    err = auth.password_error(first, second)
    if err:
        print(err, file=sys.stderr)
        return None
    return first


def cmd_list(conn, args):
    rows = conn.execute(
        "SELECT username, email, created_utc, last_login_utc, is_admin "
        "FROM users ORDER BY id").fetchall()
    if not rows:
        print("no accounts yet")
        return 0
    print(f"{'username':<20} {'email':<26} {'created':<20} "
          f"{'last login':<20} admin")
    for r in rows:
        print(f"{r['username']:<20} {(r['email'] or '-'):<26} "
              f"{r['created_utc']:<20} {(r['last_login_utc'] or '-'):<20} "
              f"{'yes' if r['is_admin'] else ''}")
    return 0


def cmd_adduser(conn, args):
    err = auth.username_error(args.username)
    if err:
        print(err, file=sys.stderr)
        return 1
    password = _prompt_password()
    if password is None:
        return 1
    try:
        db.create_user(conn, args.username, auth.hash_password(password),
                       args.email, 1 if args.admin else 0)
    except sqlite3.IntegrityError:
        print("that username or email is already taken", file=sys.stderr)
        return 1
    print(f"created {args.username}" + (" (admin)" if args.admin else ""))
    return 0


def cmd_invite(conn, args):
    """Create an account nobody can sign into yet, and mail its owner a link.

    This is the better way to onboard someone, and the only way that works
    without a terminal. The account gets a hash of 32 random bytes that are
    then thrown away -- an unusable password, not an empty one, so the account
    cannot be signed into by anyone including us, and the only way in is the
    emailed link.

    The link is an ordinary reset token: signed, single-use, one hour. Nothing
    secret is printed, typed, or left in a shell history.
    """
    import secrets

    import mailer

    err = auth.username_error(args.username)
    if err:
        print(err, file=sys.stderr)
        return 1
    if not mailer.available():
        print("no SMTP credentials configured, so there is nothing to send "
              "the invitation with. See `manage.py mailtest`.", file=sys.stderr)
        return 1

    existing = db.user_by_name(conn, args.username)
    if existing and not args.resend:
        print(f"{existing['username']} already exists. Use --resend to send "
              "them a fresh link instead.", file=sys.stderr)
        return 1

    if existing:
        user = existing
        if args.email and (user["email"] or "").lower() != args.email.lower():
            print(f"account's email is {user['email'] or '(none)'}, not "
                  f"{args.email}. Not changing it; sending to the address on "
                  "file.", file=sys.stderr)
        if not user["email"]:
            print("that account has no email address on file, so there is "
                  "nowhere to send a link.", file=sys.stderr)
            return 1
    else:
        try:
            uid = db.create_user(conn, args.username,
                                 auth.hash_password(secrets.token_urlsafe(32)),
                                 args.email, 1 if args.admin else 0)
        except sqlite3.IntegrityError:
            print("that username or email is already taken", file=sys.stderr)
            return 1
        user = db.user_by_id(conn, uid)
        print(f"created {user['username']}"
              + (" (admin)" if args.admin else ""))

    link = (config.SITE_URL.rstrip("/")
            + "/reset/" + auth.reset_token(user, kind="invite"))
    window = auth.lifetime_phrase("invite")
    body = (f"You have a WhichNEO account on the L01 target board at "
            f"{config.SITE_URL}.\n\n"
            f'Username: {user["username"]}\n\n'
            f"Choose a password here, within the next {window}:\n\n"
            f"    {link}\n\n"
            "The link works once. If it expires, ask for another.\n\n"
            "-- WhichNEO, L01 Tican Station, Visnjan Observatory\n")
    try:
        mid = mailer.send_now(user["email"], "Your WhichNEO account", body)
    except Exception as e:
        # The account exists at this point and cannot be signed into. Say so,
        # rather than leaving someone to discover it later and wonder.
        print(f"account is ready but the invitation FAILED to send: "
              f"{type(e).__name__}: {e}\n"
              f"  Fix the relay and run: manage.py invite "
              f"{user['username']} --resend", file=sys.stderr)
        return 1
    print(f"invitation sent to {user['email']} ({mid})")
    print(f"good for {window}, once.")
    return 0


def cmd_passwd(conn, args):
    user = db.user_by_name(conn, args.username)
    if not user:
        print(f"no such account: {args.username}", file=sys.stderr)
        return 1
    password = _prompt_password()
    if password is None:
        return 1
    db.update_password(conn, user["id"], auth.hash_password(password))
    # Existing sessions survive this, because they are signed with the
    # server key rather than derived from the password. That is the right
    # trade for a forgotten password and the wrong one for a stolen account:
    # if the account was compromised, restart the site to cycle nothing and
    # instead delete and recreate it, which changes the user id every session
    # cookie is keyed on.
    print(f"password changed for {user['username']}")
    return 0


def cmd_admin(conn, args):
    user = db.user_by_name(conn, args.username)
    if not user:
        print(f"no such account: {args.username}", file=sys.stderr)
        return 1
    with conn:
        conn.execute("UPDATE users SET is_admin = ? WHERE id = ?",
                     (0 if args.off else 1, user["id"]))
    print(f"{user['username']} is {'no longer' if args.off else 'now'} an admin")
    return 0


def cmd_deluser(conn, args):
    user = db.user_by_name(conn, args.username)
    if not user:
        print(f"no such account: {args.username}", file=sys.stderr)
        return 1
    if input(f"delete {user['username']}? type the username to confirm: "
             ).strip() != user["username"]:
        print("not deleted")
        return 1
    with conn:
        conn.execute("DELETE FROM users WHERE id = ?", (user["id"],))
    print(f"deleted {user['username']}")
    return 0


def cmd_ds42import(conn, args):
    """Import ds42 scores from the banked run archive into ds42_scores.

    Nights were scored by hand before the updater did it itself, and those
    objects have since left NEOCP -- so their scores exist only as TSV on
    disk while their outcomes are still listed in MPC's archive. Importing
    them turns a pile of files into the labelled set the research note needs,
    and it is the one direction the data cannot be recovered from: the
    astrometry behind those scores is already unfetchable.

    INSERT OR IGNORE, so a score the updater has since produced itself always
    wins over a hand-run one.
    """
    import csv
    import glob
    import json

    runs = sorted(glob.glob(os.path.join(args.runs, "*", "scores.tsv")))
    if not runs:
        print(f"no scores.tsv under {args.runs}", file=sys.stderr)
        return 1
    total = 0
    for path in runs:
        night = os.path.basename(os.path.dirname(path))
        prov_path = os.path.join(os.path.dirname(path), "provenance.json")
        try:
            with open(prov_path) as f:
                p = json.load(f)
        except OSError:
            print(f"  {night}: no provenance.json, skipped", file=sys.stderr)
            continue
        prov = {"ds42_rev": p.get("ds42_git_head"),
                "ds42_dirty": p.get("ds42_dirty"),
                "model_sha256": p.get("model_sha256"),
                "config": p.get("config") or {}}
        scores = {}
        with open(path) as f:
            for r in csv.DictReader(f, delimiter="\t"):
                scores[r["object_id"]] = {
                    "p_neo": _num(r.get("p_neo")),
                    "log_lr": _num(r.get("log_lr")),
                    "status": r.get("status") or "",
                    "n_obs": int(_num(r.get("n_obs")) or 0),
                    "arc_h": _num(r.get("arc_h")),
                    "obscode": r.get("obscode") or "",
                    "vmag": _num(r.get("V")),
                }
        n = db.save_ds42_scores(conn, scores, prov)
        total += n
        print(f"  {night}: {len(scores)} in file, {n} new")
    print(f"imported {total} score(s); ds42_scores now holds "
          f"{db.count_ds42_scores(conn)}")
    return 0


def _num(s):
    """Same nan-to-None conversion ds42score uses at its parse boundary: a
    stored nan compares false against itself and can never be matched again."""
    if s in (None, ""):
        return None
    try:
        v = float(s)
    except ValueError:
        return None
    return None if v != v else v


def cmd_mailtest(conn, args):
    """Send one message and let the exception through.

    The website deliberately swallows send failures -- an observer asking for
    a reset must not learn from an error whether their address is on file.
    That makes a broken relay invisible from the browser, so this is where it
    becomes visible.
    """
    import mailer

    if not mailer.available():
        print("no SMTP credentials configured.\n"
              "  export WHICHNEO_SMTP_PASSWORD='...'\n"
              "  or: printf '%s' '...' > data/smtp_password && "
              "chmod 600 data/smtp_password", file=sys.stderr)
        return 1
    print(f"relay    {config.SMTP_HOST}:{config.SMTP_PORT} "
          f"({'STARTTLS' if config.SMTP_STARTTLS else 'plain'})")
    print(f"as       {config.SMTP_USER}")
    print(f"from     {config.SMTP_FROM}")
    print(f"to       {args.address}")
    try:
        mid = mailer.send_now(
            args.address, "WhichNEO relay test",
            "This is a test message from the WhichNEO board at "
            f"{config.SITE_URL}.\n\nIf you are reading it, outgoing mail "
            "works and password resets will reach people.\n")
    except Exception as e:
        print(f"FAILED: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    print(f"sent     {mid}")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list")

    a = sub.add_parser("adduser")
    a.add_argument("username")
    a.add_argument("--email")
    a.add_argument("--admin", action="store_true")

    a = sub.add_parser("invite")
    a.add_argument("username")
    a.add_argument("--email", required=True)
    a.add_argument("--admin", action="store_true")
    a.add_argument("--resend", action="store_true",
                   help="send a fresh link to an account that already exists")

    a = sub.add_parser("passwd")
    a.add_argument("username")

    a = sub.add_parser("admin")
    a.add_argument("username")
    a.add_argument("--off", action="store_true")

    a = sub.add_parser("deluser")
    a.add_argument("username")

    a = sub.add_parser("mailtest")
    a.add_argument("address")

    a = sub.add_parser("ds42import")
    a.add_argument("--runs", default=os.path.join(config.DS42_ROOT, "runs"),
                   help="the ds42 run archive (default: %(default)s)")

    args = p.parse_args(argv)
    conn = db.connect()
    db.init(conn)
    try:
        return globals()["cmd_" + args.cmd](conn, args)
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())

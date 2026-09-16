"""Account administration from the shell.

The website has a reset-by-email link, but it only reaches accounts that have
an email address on file, and it stays silent when the relay is down --
deliberately, so that the form cannot be used to find out which accounts
exist. So the shell stays the fallback for both: `passwd` for an account with
no email, and `mailtest` for finding out why nothing arrived.

Passwords are read from a prompt, never from argv -- an argument is visible in
`ps` to every user on the machine and lands in shell history besides.

    python3 manage.py list
    python3 manage.py adduser <username> [--email a@b] [--admin]
    python3 manage.py passwd <username>
    python3 manage.py admin <username> [--off]
    python3 manage.py deluser <username>
    python3 manage.py mailtest <address>
"""

import argparse
import getpass
import sqlite3
import sys

import auth
import config
import db


def _prompt_password():
    first = getpass.getpass("Password: ")
    err = auth.password_error(first, getpass.getpass("Repeat: "))
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

    a = sub.add_parser("passwd")
    a.add_argument("username")

    a = sub.add_parser("admin")
    a.add_argument("username")
    a.add_argument("--off", action="store_true")

    a = sub.add_parser("deluser")
    a.add_argument("username")

    a = sub.add_parser("mailtest")
    a.add_argument("address")

    args = p.parse_args(argv)
    conn = db.connect()
    db.init(conn)
    try:
        return globals()["cmd_" + args.cmd](conn, args)
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())

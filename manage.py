"""Account administration from the shell.

There is no mail relay on this host, so there is no password-reset email and
no "forgot password" link. That is a deliberate limit, not an oversight: the
board serves one observatory and a handful of people, and the recovery path
for a forgotten password is an admin running

    python3 manage.py passwd <username>

on epyc. The same goes for creating accounts when self-service sign-up is
closed, and for handing out admin.

Passwords are read from a prompt, never from argv -- an argument is visible in
`ps` to every user on the machine and lands in shell history besides.

    python3 manage.py list
    python3 manage.py adduser <username> [--email a@b] [--admin]
    python3 manage.py passwd <username>
    python3 manage.py admin <username> [--off]
    python3 manage.py deluser <username>
"""

import argparse
import getpass
import sqlite3
import sys

import auth
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

    args = p.parse_args(argv)
    conn = db.connect()
    db.init(conn)
    try:
        return globals()["cmd_" + args.cmd](conn, args)
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())

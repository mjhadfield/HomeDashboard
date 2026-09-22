"""Host-side admin: `python -m app.cli <command>` (or the `hub` wrapper installed by deploy/install.sh)."""
from __future__ import annotations

import argparse
import datetime as dt
import sys

from . import auth, config, db, seed


def _qr(url: str) -> None:
    try:
        import qrcode
        qr = qrcode.QRCode(border=1)
        qr.add_data(url)
        qr.print_ascii(invert=True)
    except Exception:
        pass  # the URL is printed as well; a QR is a convenience


def cmd_enrol(args: argparse.Namespace) -> None:
    token = auth.create_enrol_token(args.role, args.name)
    url = f"{config.public_url()}/enrol/{token}"
    print(f"\nEnrol a device as {args.role!r} ({args.name}). Open this on the device — valid for 15 minutes, one use:\n\n  {url}\n")
    _qr(url)


def cmd_devices(_: argparse.Namespace) -> None:
    rows = auth.list_devices()
    if not rows:
        print("No devices enrolled yet. Run: hub enrol")
    for d in rows:
        seen = dt.datetime.fromtimestamp(d["last_seen"]).strftime("%Y-%m-%d %H:%M") if d["last_seen"] else "never"
        print(f"{d['id']:<18} {d['role']:<6} {d['name']:<24} last seen {seen}{'  [revoked]' if d['revoked'] else ''}")


def cmd_revoke(args: argparse.Namespace) -> None:
    print("Revoked." if auth.revoke_device(args.id) else "No such (active) device.")


def cmd_seed(_: argparse.Namespace) -> None:
    print("Added:", seed.seed(force=True))


def cmd_status(_: argparse.Namespace) -> None:
    with db.db() as con:
        for table in ("devices", "projects", "links", "widget_settings"):
            print(f"{table:<16} {con.execute(f'SELECT COUNT(*) c FROM {table}').fetchone()['c']}")
    print(f"database         {config.DB_PATH}")
    print(f"url              {config.public_url()}")


def main(argv: list[str] | None = None) -> None:
    db.migrate()
    p = argparse.ArgumentParser(prog="hub", description="Citadel admin")
    sub = p.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("enrol", help="print a one-time link (and QR) to enrol a device")
    e.add_argument("--role", choices=auth.ROLES, default="owner")
    e.add_argument("--name", default="New device")
    e.set_defaults(fn=cmd_enrol)
    sub.add_parser("devices", help="list enrolled devices").set_defaults(fn=cmd_devices)
    r = sub.add_parser("revoke", help="revoke a device")
    r.add_argument("id")
    r.set_defaults(fn=cmd_revoke)
    sub.add_parser("seed", help="fill any empty projects/links/settings from .env and the built-in defaults").set_defaults(fn=cmd_seed)
    sub.add_parser("status", help="show counts and where things live").set_defaults(fn=cmd_status)
    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    sys.exit(main())

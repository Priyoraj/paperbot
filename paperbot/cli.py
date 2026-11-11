"""Command line: `paperbot <command>`.  Everything here is also in the desktop app
(`paperbot-app` or the PaperBot shortcut); the commands remain for scripting and the
scheduled task."""
from __future__ import annotations

import argparse
import getpass
import logging
import sys
import time
from pathlib import Path

from . import config as config_mod
from .app import (Busy, LockLost, SyncError, build_bot, check_sharing, fetch_one, make_vpn,
                  manual_session_factory, set_secret, setup_checks, setup_logging,
                  single_instance, start_sharing, stop_sharing, together)
from .config import ConfigError

log = logging.getLogger("paperbot")
WAIT_FOR_OTHER_S = 60    # an automatic check waits this long while another computer works


def cmd_init(args) -> None:
    path = config_mod.init_config()
    print(f"Config file: {path}\nFill in [owner] and [mail], then run: paperbot set-password mail")


def cmd_set_password(args) -> None:
    cfg = config_mod.load()
    if args.which == "mail":
        if not cfg.mail_username:
            raise ConfigError("set [mail] username in config.toml first")
        print(f"App password for {cfg.mail_username}.")
        print("Gmail: create one at https://myaccount.google.com/apppasswords "
              "(needs 2-Step Verification). It is stored in Windows Credential Manager.")
        pw = getpass.getpass("App password (input hidden): ").replace(" ", "")
        if not pw:
            raise SystemExit("nothing entered")
        set_secret(f"mail:{cfg.mail_username}", pw)
    else:
        print(f"Only needed if Windows has no saved password for {cfg.vpn_name}.")
        user = input("VPN username: ").strip()
        pw = getpass.getpass("VPN password (input hidden): ")
        if not user or not pw:
            raise SystemExit("nothing entered")
        set_secret(f"vpn-user:{cfg.vpn_name}", user)
        set_secret(f"vpn:{cfg.vpn_name}", pw)
    print("Saved.")


def cmd_check(args) -> None:
    cfg = config_mod.load()
    print("Checking PaperBot setup...")
    results = setup_checks(cfg)
    for name, ok, detail in results:
        print(f"  {'OK  ' if ok else 'FAIL'}  {name}{' - ' + detail if detail else ''}")
    print("All good." if all(ok for _, ok, _ in results)
          else "Fix the FAIL lines above, then run `paperbot check` again.")


def check_once(cfg) -> None:
    """One automatic check. Not an error: PaperBot is already at work - here or on another
    computer that shares these records - or this computer's turn ran out under it."""
    try:
        with single_instance(cfg):
            try:
                build_bot(cfg).run_once(wait_s=WAIT_FOR_OTHER_S)
            except Busy as exc:          # "PaperBot on LAPTOP is working right now"
                log.info("skipped this check: %s", str(exc).split(" - ")[0])
            except LockLost as exc:
                log.warning("stopped this check: %s", exc)
    except Busy:
        log.info("skipped this check: another PaperBot run is in progress on this computer")


def cmd_run(args) -> None:
    cfg = config_mod.load()
    cfg.check_ready()
    check_once(cfg)


def cmd_watch(args) -> None:
    cfg = config_mod.load()
    cfg.check_ready()
    print(f"Checking mail every {cfg.poll_minutes} min. Ctrl+C to stop.")
    while True:
        try:
            check_once(cfg)
        except Exception:  # noqa: BLE001
            log.exception("run failed")
        time.sleep(cfg.poll_minutes * 60)


def cmd_status(args) -> None:
    cfg = config_mod.load()
    from .db import DB
    db = DB(cfg.data_dir / "paperbot.db")
    print(f"First-time requesters need approval: {'ON' if cfg.require_approval else 'OFF'}")
    here = together(cfg, db)
    if here.joined or not here.allowed:
        print(f"More than one computer: {here.summary}")
    if cfg.auto_approve_domains:
        print(f"Always trusted domains: {', '.join(cfg.auto_approve_domains)}")
    print(f"Institute downloads today: {db.downloads_today()} of {cfg.daily_ceiling} "
          f"({cfg.own_reserve} kept for you; {'recommended' if cfg.use_recommended_limits else 'custom'}"
          f" limits, at most {cfg.hourly_cap} an hour)")
    people = db.people()
    for status in ("pending", "approved", "denied"):
        group = [p for p in people if p["status"] == status]
        if group:
            print(f"\n{status.capitalize()} ({len(group)}):")
            for p in group:
                print(f"  {p['email']}" + (f"  ({p['name']})" if p["name"] else ""))
    manual = db.items_with_status("manual")
    if manual:
        print("\nWaiting for you (`paperbot manual`):")
        for r in manual:
            print(f"  #{r['id']}  {r['title'] or r['raw']}  (for {r['sender']})")


def cmd_decide(args) -> None:
    cfg = config_mod.load()
    approve = args.command == "approve"
    with single_instance(cfg, wait_s=120):
        bot = build_bot(cfg)
        if args.command == "forget":
            bot.forget(args.email)
            print(f"Forgot {args.email}; their next request is treated as first-time.")
            return
        n = bot.decide(args.email, approve=approve)
    print(f"{args.email} {'approved' if approve else 'denied'}.")
    if approve and n:
        print(f"{n} waiting request(s) will be answered on the next run (or now: paperbot run).")


def cmd_approval(args) -> None:
    config_mod.set_value("access", "require_approval", args.state == "on")
    print("First-time requesters now " + ("need your approval." if args.state == "on"
                                         else "get papers without asking you."))


def cmd_sync(args) -> None:
    """PaperBot on more than one computer: status | on | off | check."""
    cfg = config_mod.load()
    if args.action == "status":
        from .db import DB
        here = together(cfg, DB(cfg.data_dir / "paperbot.db"))
        print(here.summary)
        if here.synced_at:
            print(f"Records last exchanged with the mailbox: {here.synced_at:%d %b %H:%M} "
                  f"(folder {cfg.sync_folder}).")
        if not here.joined and here.allowed:
            print("To let PaperBot on another computer work together with this one:  "
                  "paperbot sync on")
    elif args.action == "check":
        print("Trying out the mailbox (saves, reads and deletes one test message)...")
        print(check_sharing(cfg))
    elif args.action == "on":
        with single_instance(cfg, wait_s=120):
            print(start_sharing(cfg))
    else:
        with single_instance(cfg, wait_s=120):
            print(stop_sharing(cfg))


def cmd_fetch(args) -> None:
    cfg = config_mod.load()
    mode = "vpn" if args.vpn_only else ("free+vpn" if args.vpn else "free")
    out = Path(args.out) if args.out else None
    if mode == "free":
        fetch_one(cfg, args.what, mode, out)
    else:                       # the VPN part must not overlap the automatic check's
        with single_instance(cfg, wait_s=120):
            fetch_one(cfg, args.what, mode, out)


def cmd_manual(args) -> None:
    cfg = config_mod.load()
    with single_instance(cfg, wait_s=120):
        got = build_bot(cfg).run_manual(manual_session_factory(cfg))
    print(f"\nDone: {got} paper(s) fetched and emailed.")


def cmd_attach(args) -> None:
    cfg = config_mod.load()
    from .sources import is_pdf
    data = Path(args.file).read_bytes()
    if not is_pdf(data):
        raise SystemExit(f"{args.file} is not a PDF")
    with single_instance(cfg, wait_s=120):
        build_bot(cfg).attach(int(args.id.lstrip("#")), data)
    print(f"Sent #{args.id.lstrip('#')} to the requester.")


def cmd_giveup(args) -> None:
    cfg = config_mod.load()
    ids = None if args.ids == ["all"] else [int(i.lstrip("#")) for i in args.ids]
    with single_instance(cfg, wait_s=120):
        n = build_bot(cfg).give_up(ids)
    print(f"Gave up on {n} paper(s); the requesters have been told.")


def cmd_vpn(args) -> None:
    cfg = config_mod.load()
    vpn = make_vpn(cfg)
    if args.action == "up":
        vpn.connect()
    elif args.action == "down":
        vpn.disconnect()
    print(f"{cfg.vpn_name}: {'connected' if vpn.is_connected() else 'not connected'}")


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="paperbot", description="Answers paper requests by email.")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="create config.toml").set_defaults(fn=cmd_init)
    p = sub.add_parser("set-password", help="store the mail (or VPN) password securely")
    p.add_argument("which", choices=["mail", "vpn"])
    p.set_defaults(fn=cmd_set_password)
    sub.add_parser("check", help="test mail login, VPN profile and browser").set_defaults(
        fn=cmd_check)
    sub.add_parser("run", help="process the inbox once (used by the scheduled task)").set_defaults(
        fn=cmd_run)
    sub.add_parser("watch", help="keep running, checking mail every few minutes").set_defaults(
        fn=cmd_watch)
    sub.add_parser("status", help="people, queue and papers waiting for you").set_defaults(
        fn=cmd_status)
    for name, text in (("approve", "approve a requester"), ("deny", "block a requester"),
                       ("forget", "forget a requester (asks again next time)")):
        p = sub.add_parser(name, help=text)
        p.add_argument("email")
        p.set_defaults(fn=cmd_decide)
    p = sub.add_parser("approval", help="turn first-time approval on/off")
    p.add_argument("state", choices=["on", "off"])
    p.set_defaults(fn=cmd_approval)
    p = sub.add_parser("fetch", help="test: fetch one paper (DOI/link/title) without email")
    p.add_argument("what")
    p.add_argument("--vpn", action="store_true", help="also try the publisher through the VPN")
    p.add_argument("--vpn-only", action="store_true", help="skip open access; publisher only")
    p.add_argument("--out", help="folder for the PDF (default data/test)")
    p.set_defaults(fn=cmd_fetch)
    sub.add_parser("manual", help="fetch CAPTCHA-blocked papers in a visible browser"
                   ).set_defaults(fn=cmd_manual)
    p = sub.add_parser("attach", help="send a waiting paper you downloaded yourself")
    p.add_argument("id")
    p.add_argument("file")
    p.set_defaults(fn=cmd_attach)
    p = sub.add_parser("giveup", help="tell requesters a paper can't be had (ids or 'all')")
    p.add_argument("ids", nargs="+")
    p.set_defaults(fn=cmd_giveup)
    p = sub.add_parser("vpn", help="VPN control for testing")
    p.add_argument("action", choices=["up", "down", "status"])
    p.set_defaults(fn=cmd_vpn)
    p = sub.add_parser("sync", help="PaperBot on more than one computer, working together "
                                    "through the mailbox")
    p.add_argument("action", choices=["status", "on", "off", "check"],
                   help="status: where this computer stands; on: start (or join); off: this "
                        "computer keeps its own records again; check: try out the mailbox")
    p.set_defaults(fn=cmd_sync)
    args = ap.parse_args(argv)

    try:
        cfg = config_mod.load() if args.command != "init" else None
    except ConfigError:
        cfg = None
    setup_logging(cfg, args.verbose or args.command in ("fetch", "manual", "vpn", "check",
                                                        "sync"))
    try:
        args.fn(args)
    except ConfigError as exc:
        raise SystemExit(f"Config problem: {exc}")
    except (Busy, LockLost) as exc:
        raise SystemExit(str(exc))
    except SyncError as exc:
        log.error("%s", exc)
        raise SystemExit(f"Working together: {exc}")
    except KeyboardInterrupt:
        raise SystemExit("Stopped.")
    except Exception as exc:  # noqa: BLE001 - log it for the scheduled task, show it to people
        log.exception("%s failed", args.command)
        raise SystemExit(f"{args.command} failed: {exc}")


if __name__ == "__main__":
    sys.exit(main())

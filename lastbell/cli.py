"""Command-line entrypoint: ``lastbell <command>``."""
from __future__ import annotations

import argparse
import sys

from . import __version__
from . import config as cfg
from . import secrets as secretstore


def _cmd_setup(args: argparse.Namespace) -> int:
    from . import setup_wizard

    return setup_wizard.main()


def _cmd_set_password(args: argparse.Namespace) -> int:
    conf = cfg.load()
    pw = secretstore.prompt_password(f"Password for {conf.username} (hidden): ")
    secretstore.set_password(conf.username, pw)
    print(f"Stored password for {conf.username} in the OS keyring.")
    return 0


def _cmd_preflight(args: argparse.Namespace) -> int:
    from . import preflight

    argv = []
    if args.district:
        argv += ["--district", args.district]
    if args.username:
        argv += ["--username", args.username]
    for flag in ("anonymous", "report", "json", "show_values", "dump"):
        if getattr(args, flag):
            argv.append("--" + flag.replace("_", "-"))
    return preflight.main(argv)


def _cmd_discover(args: argparse.Namespace) -> int:
    from .client import ParentVueClient

    conf = cfg.load()
    pw = secretstore.get_password(conf.username, conf.secret_backend)
    client = ParentVueClient(conf.base_url, conf.username, pw)
    for c in client.get_children():
        print(f"  {c.agu:>4}  {c.name}  ({c.school})")
    return 0


def _cmd_init_db(args: argparse.Namespace) -> int:
    from . import store

    conf = cfg.load()
    store.init_db(conf.db_path)
    print(f"Initialized database at {conf.db_path}")
    return 0


def _cmd_collect(args: argparse.Namespace) -> int:
    """Pull normalized gradebook data for every student and class, as JSON.

    Read-only debug view of exactly what a ``run`` pass would persist.
    """
    import dataclasses
    import json

    from .client import ParentVueClient
    from .collector import collect_student

    conf = cfg.load()
    pw = secretstore.get_password(conf.username, conf.secret_backend)
    client = ParentVueClient(conf.base_url, conf.username, pw)

    out = []
    for child in client.get_children():
        col = collect_student(client, child)
        out.append({
            "student": {"agu": child.agu, "name": child.name, "school": child.school},
            "term": col.school_classes.current_term,
            "mark_periods": [dataclasses.asdict(p) for p in col.school_classes.mark_periods],
            "classes": [
                {
                    "course": dataclasses.asdict(c.course),
                    "missing": c.details.missing_text,
                    "assignments": [dataclasses.asdict(a) for a in c.details.assignments],
                }
                for c in col.classes
            ],
            "errors": col.errors,
        })

    print(json.dumps(out, indent=2, default=str))
    return 0


def _run_once(client, conn, notifier, conf) -> int:
    """One poll: collect -> derive -> diff -> persist -> notify. Returns the event count."""
    import os
    from datetime import datetime

    from . import differ, outbox, router, store, watchers
    from .collector import collect_student

    total = 0
    for child in client.get_children():
        col = collect_student(client, child)
        for err in col.errors:
            print(f"warning [{col.student.initials}]: {err} — that class was "
                  f"skipped this poll; the others were still checked",
                  file=sys.stderr)

        snapshot = differ.apply_time_rules(
            col.snapshot,
            grace_days=conf.ungraded_grace_days,
            lookahead_days=conf.lookahead_days,
        )
        previous = store.load_snapshot(conn, child.agu)
        events = differ.diff(previous, snapshot,
                             grade_drop_points=conf.grade_drop_points)
        store.persist_snapshot(conn, col.student, snapshot)

        n_assign = len(snapshot.assignments)
        if previous is None:
            print(f"baseline established for {col.student.initials}: "
                  f"{len(col.classes)} classes, {n_assign} assignments (no alerts on first run)")
            continue
        print(f"checked {col.student.initials}: {len(col.classes)} classes, "
              f"{n_assign} assignments, {len(events)} event(s)")
        if events:
            # Phase 3: fan out per subscription; the global channel remains the
            # fallback so a bare install with no watchers still alerts.
            # Phase 4: a delivery with a digest time or inside quiet hours is
            # queued to the outbox instead of sent now.
            deliveries, warnings = router.plan(conn, child.agu, events)
            if router.has_subscriptions(conn, child.agu):
                now = datetime.now()
                immediate, queued = [], 0
                for d in deliveries:
                    send_after = outbox.compute_send_after(now, d.send_at, d.quiet_hours)
                    if send_after is None:
                        immediate.append(d)
                    else:
                        queued += outbox.enqueue(conn, d, send_after)
                sent, send_warnings = router.dispatch(immediate, col.student.initials)
                warnings += send_warnings
                print(f"  delivered to {sent} watcher channel(s)"
                      + (f", queued {queued} item(s) for later" if queued else ""))
            else:
                notifier.send(router.subject(col.student.initials, events),
                              "\n".join(f"• {e.detail}" for e in events))
            for w in warnings:
                print(f"warning: {w}", file=sys.stderr)
            for e in events:
                store.record_alert(conn, child.agu, e)
            total += len(events)
    # After students are persisted: an install with zero watchers gets one
    # for the credential holder, subscribed to everyone (UX decision 3).
    w = watchers.ensure_default_watcher(
        conn, conf.username, os.environ.get("LASTBELL_SMTP_TO"))
    if w is not None:
        print(f"created default watcher {w.name!r} (guardian, via "
              f"{', '.join(w.channels)}), subscribed to all students — "
              f"adjust with `lastbell watcher` / `subscribe`")
    return total


def _tick(conn, conf) -> None:
    """The between-polls housekeeping: flush due outbox items (digests,
    quiet-hours holdbacks) and any daily summaries whose time has come."""
    from . import outbox, summary

    sent, warnings = outbox.flush_due(conn)
    if sent:
        print(f"flushed {sent} deferred message(s)")
    s_sent, s_warnings = summary.send_due(conn, lookahead_days=conf.lookahead_days)
    if s_sent:
        print(f"sent {s_sent} daily summar{'ies' if s_sent != 1 else 'y'}")
    for w in warnings + s_warnings:
        print(f"warning: {w}", file=sys.stderr)


_TICK_SECONDS = 60


def _cmd_run(args: argparse.Namespace) -> int:
    """The watch loop — snapshot, diff against last run, alert.

    Collection hits the portal every POLL_MINUTES; in --loop mode the outbox
    and summaries are checked every minute in between, so a 17:00 digest goes
    out at 17:00, not at the next three-hour poll.
    """
    import time

    from . import notify, store
    from .client import ParentVueClient

    conf = cfg.load()
    pw = secretstore.get_password(conf.username, conf.secret_backend)
    notifier = notify.get(conf.notify_channel)

    conn = store.connect(conf.db_path)
    store.ensure_schema(conn)
    next_poll = 0.0
    try:
        while True:
            if time.time() >= next_poll:
                # Fresh client per pass: portal sessions expire well inside a
                # poll interval, and re-login is one request.
                client = ParentVueClient(conf.base_url, conf.username, pw)
                try:
                    _run_once(client, conn, notifier, conf)
                except Exception as e:
                    if not args.loop:
                        raise
                    import requests as _rq
                    from datetime import datetime, timedelta

                    retry_at = (datetime.now()
                                + timedelta(minutes=conf.poll_minutes)).strftime("%H:%M")
                    reason = ("couldn't reach the portal"
                              if isinstance(e, _rq.RequestException)
                              else "couldn't finish this poll")
                    print(f"{reason} ({e or e.__class__.__name__}) — "
                          f"retrying at {retry_at}", file=sys.stderr)
                next_poll = time.time() + conf.poll_minutes * 60
                if args.loop:
                    print(f"next portal poll in {conf.poll_minutes} min "
                          f"(outbox/summaries checked every minute)")
            _tick(conn, conf)
            if not args.loop:
                return 0
            time.sleep(_TICK_SECONDS)
    finally:
        conn.close()


# ── Phase 3: watchers, subscriptions, dashboard ───────────────────────


def _db(conf):
    from . import store

    conn = store.connect(conf.db_path)
    store.ensure_schema(conn)
    return conn


def _parse_channel_args(pairs: list[str]) -> dict:
    """``email=kid@example.com`` -> ``{"email": {"to": "kid@example.com"}}``."""
    from . import notify

    channels: dict = {}
    for pair in pairs:
        name, sep, value = pair.partition("=")
        if name not in notify.ADDRESS_KEY:
            raise SystemExit(f"error: unknown channel {name!r} "
                             f"(valid: {', '.join(notify.CHANNEL_NAMES)})")
        key = notify.ADDRESS_KEY[name]
        if key is None:            # console: no address needed
            channels[name] = {}
        elif not sep or not value:
            channels[name] = None  # sentinel: remove this channel
        else:
            try:
                channels[name] = {key: notify.validate_address(name, value)}
            except ValueError as e:
                raise SystemExit(f"error: {e}")
    return channels


def _cmd_watcher_add(args: argparse.Namespace) -> int:
    from . import watchers
    from .models import WatcherKind

    conn = _db(cfg.load())
    try:
        w = watchers.add_watcher(conn, args.name, WatcherKind(args.kind),
                                 _parse_channel_args(args.channel))
        chans = ", ".join(w.channels) or "none — add with `watcher set-channel`"
        print(f"added {w.kind.value} watcher {w.name!r} (channels: {chans})")
        return 0
    finally:
        conn.close()


def _cmd_watcher_list(args: argparse.Namespace) -> int:
    from . import watchers

    conn = _db(cfg.load())
    try:
        rows = watchers.list_watchers(conn)
        if not rows:
            print("no watchers yet — add one with `lastbell watcher add`")
            return 0
        for w in rows:
            chans = ", ".join(f"{k}={list(v.values())[0] if v else '·'}"
                              for k, v in w.channels.items()) or "no channels"
            print(f"  {w.name}  ({w.kind.value})  {chans}")
        return 0
    finally:
        conn.close()


def _cmd_watcher_remove(args: argparse.Namespace) -> int:
    from . import watchers

    conn = _db(cfg.load())
    try:
        watchers.remove_watcher(conn, args.name)
        print(f"removed watcher {args.name!r} (and their subscriptions)")
        return 0
    finally:
        conn.close()


def _cmd_watcher_set_channel(args: argparse.Namespace) -> int:
    from . import watchers

    conn = _db(cfg.load())
    try:
        w = watchers.set_channels(conn, args.name, _parse_channel_args(args.channel))
        print(f"{w.name}: channels now {', '.join(w.channels) or 'none'}")
        return 0
    finally:
        conn.close()


def _cmd_subscribe(args: argparse.Namespace) -> int:
    from . import watchers

    conn = _db(cfg.load())
    try:
        w = watchers.require_watcher(conn, args.watcher)
        student = watchers.resolve_student(conn, args.student)
        types = None if args.types in (None, "all") else args.types.split(",")
        chans = None if args.channels in (None, "all") else args.channels.split(",")
        added = watchers.subscribe(conn, w, student["id"], types, chans,
                                   send_at=args.at, urgent_now=args.urgent)
        scope = args.types or "all alerts"
        via = args.channels or "all configured channels"
        when = f" daily at {args.at}" if args.at else ""
        print(f"{w.name} ⇒ {student['name']}: {scope} via {via}{when} "
              f"({len(added)} new subscription row(s))")
        return 0
    finally:
        conn.close()


def _cmd_quiet_hours(args: argparse.Namespace) -> int:
    from . import watchers

    conn = _db(cfg.load())
    try:
        if args.clear:
            w = watchers.set_quiet_hours(conn, args.name, None, None)
            print(f"{w.name}: quiet hours cleared")
            return 0
        if not args.window:
            raise SystemExit("error: give a window like 21:00-07:00, or --clear")
        start, sep, end = args.window.partition("-")
        if not sep:
            raise SystemExit("error: window format is START-END, e.g. 21:00-07:00")
        w = watchers.set_quiet_hours(conn, args.name, start, end)
        print(f"{w.name}: quiet from {w.quiet_hours['start']} to {w.quiet_hours['end']} "
              f"(alerts are held and delivered when the window ends)")
        return 0
    finally:
        conn.close()


def _cmd_alerts(args: argparse.Namespace) -> int:
    from . import store

    conn = _db(cfg.load())
    try:
        rows = store.list_alerts(conn, limit=args.limit)
        if not rows:
            print("no alerts yet")
            return 0
        import json

        for r in rows:
            try:
                detail = json.loads(r["body"]).get("detail", "")
            except Exception:
                detail = r["body"]
            print(f"  {r['id'][:8]}  {r['created_at']}  {r['initials'] or r['student_name']:8}"
                  f"  {detail}")
        return 0
    finally:
        conn.close()


def _cmd_flush(args: argparse.Namespace) -> int:
    from . import outbox, summary

    conf = cfg.load()
    conn = _db(conf)
    try:
        sent, warnings = outbox.flush_due(conn)
        s_sent, s_warnings = summary.send_due(conn, lookahead_days=conf.lookahead_days)
        for w in warnings + s_warnings:
            print(f"warning: {w}", file=sys.stderr)
        remaining = outbox.pending(conn)
        print(f"sent {sent} deferred message(s) and {s_sent} summar{'ies' if s_sent != 1 else 'y'}; "
              f"{len(remaining)} item(s) still queued")
        for r in remaining[:10]:
            print(f"  → {r['watcher_name']} via {r['channel']} after {r['send_after']}: "
                  f"{r['detail']}")
        return 0
    finally:
        conn.close()


def _cmd_unsubscribe(args: argparse.Namespace) -> int:
    from . import watchers

    conn = _db(cfg.load())
    try:
        w = watchers.require_watcher(conn, args.watcher)
        student_id = None
        label = "all students"
        if args.student:
            row = watchers.resolve_student(conn, args.student)
            student_id, label = row["id"], row["name"]
        n = watchers.unsubscribe(conn, w, student_id)
        print(f"removed {n} subscription row(s) for {w.name} ({label})")
        return 0
    finally:
        conn.close()


def _cmd_subscriptions(args: argparse.Namespace) -> int:
    from . import watchers

    conn = _db(cfg.load())
    try:
        subs = watchers.list_subscriptions(conn)
        if not subs:
            print("no subscriptions yet — create one with `lastbell subscribe`")
            return 0
        for s in subs:
            alert = "all alerts" if s.alert_type == "*" else s.alert_type
            via = "all configured channels" if s.channel == "*" else s.channel
            when = f" daily at {s.send_at}" if s.send_at else ""
            print(f"  {s.watcher_name} ⇒ {s.student_name}: {alert} via {via}{when}")
        return 0
    finally:
        conn.close()


def _cmd_dashboard(args: argparse.Namespace) -> int:
    from pathlib import Path

    from . import dashboard

    conf = cfg.load()
    dashboard.serve(Path(args.db) if args.db else conf.db_path,
                    args.host or conf.dashboard_host,
                    args.port or conf.dashboard_port)
    return 0


def _cmd_seed_demo(args: argparse.Namespace) -> int:
    from pathlib import Path

    from . import paths, seed, store

    target = Path(args.db) if args.db else paths.data_dir() / "demo.db"
    conf = cfg.load()
    if target.resolve() == conf.db_path.resolve():
        print(f"refusing to seed demo data into the configured live database "
              f"({conf.db_path}) — pass a different --db", file=sys.stderr)
        return 2
    if target.exists():
        if not args.force:
            print(f"{target} already exists — pass --force to overwrite it",
                  file=sys.stderr)
            return 2
        target.unlink()
    conn = store.connect(target)
    try:
        store.ensure_schema(conn)
        stats = seed.seed_demo(conn, seed=args.seed)
    finally:
        conn.close()
    print(f"seeded {target}: {stats['students']} students, "
          f"{stats['assignments']} assignments across {' + '.join(stats['terms'])}, "
          f"{stats['alerts']} alerts, {stats['course_history']} course-history "
          f"rows over {stats['span_days']} days")
    print(f"view it:  lastbell dashboard --db {target}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="lastbell", description="Self-hosted ParentVUE grade monitor")
    parser.add_argument("--version", action="version", version=f"lastbell {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("setup", help="interactive first-run wizard: district, "
                   "credentials, notifications, first collection"
                   ).set_defaults(func=_cmd_setup)

    sub.add_parser("set-password", help="store a credential's password in the OS keyring").set_defaults(func=_cmd_set_password)

    p_pre = sub.add_parser("preflight",
                           help="district go/no-go check (redacted, shareable)")
    p_pre.add_argument("--district", "-d", help="portal hostname (default: env)")
    p_pre.add_argument("--username", "-u",
                       help="login for the full check (omit for anonymous mode)")
    p_pre.add_argument("--anonymous", action="store_true",
                       help="probe public endpoints only; send no credentials")
    p_pre.add_argument("--report", action="store_true",
                       help="emit a paste-ready Markdown district report")
    p_pre.add_argument("--json", action="store_true", help="machine-readable output")
    p_pre.add_argument("--show-values", action="store_true",
                       help="reveal names/grades locally (never exported)")
    p_pre.add_argument("--dump", action="store_true",
                       help="save raw portal pages to the data dir's debug/ (local only)")
    p_pre.set_defaults(func=_cmd_preflight)

    sub.add_parser("discover", help="list students on the configured credential").set_defaults(func=_cmd_discover)
    sub.add_parser("init-db", help="create the local database").set_defaults(func=_cmd_init_db)
    sub.add_parser("collect", help="pull all gradebook data as JSON (read-only)").set_defaults(func=_cmd_collect)

    p_run = sub.add_parser("run", help="collect, diff against the last run, and alert")
    p_run.add_argument("--loop", action="store_true",
                       help="keep polling every LASTBELL_POLL_MINUTES")
    p_run.set_defaults(func=_cmd_run)

    # Phase 3: watcher accounts + subscriptions + dashboard
    p_w = sub.add_parser("watcher", help="manage watcher accounts (guardians & students)")
    w_sub = p_w.add_subparsers(dest="watcher_command", required=True)

    p_wa = w_sub.add_parser("add", help="add a watcher")
    p_wa.add_argument("name", help="unique name, e.g. Mom or Jasper")
    p_wa.add_argument("--kind", choices=["guardian", "student"], default="guardian")
    p_wa.add_argument("--channel", action="append", default=[], metavar="CH=ADDR",
                      help="e.g. email=kid@example.com, ntfy=my-topic, "
                           "telegram=123456789, pushover=uKEY (repeatable)")
    p_wa.set_defaults(func=_cmd_watcher_add)

    w_sub.add_parser("list", help="list watchers").set_defaults(func=_cmd_watcher_list)

    p_wr = w_sub.add_parser("remove", help="remove a watcher and their subscriptions")
    p_wr.add_argument("name")
    p_wr.set_defaults(func=_cmd_watcher_remove)

    p_wc = w_sub.add_parser("set-channel", help="add/update/remove a watcher's channels")
    p_wc.add_argument("name")
    p_wc.add_argument("channel", nargs="+", metavar="CH=ADDR",
                      help="CH=ADDR to set; bare CH= to remove")
    p_wc.set_defaults(func=_cmd_watcher_set_channel)

    p_wq = w_sub.add_parser("quiet-hours",
                            help="hold this watcher's alerts during a daily window")
    p_wq.add_argument("name")
    p_wq.add_argument("window", nargs="?", metavar="START-END",
                      help="e.g. 21:00-07:00 (may cross midnight)")
    p_wq.add_argument("--clear", action="store_true", help="remove the window")
    p_wq.set_defaults(func=_cmd_quiet_hours)

    p_sub = sub.add_parser("subscribe", help="route a student's alerts to a watcher")
    p_sub.add_argument("watcher", help="watcher name")
    p_sub.add_argument("student", help="student AGU, or a name/initials prefix")
    p_sub.add_argument("--types", metavar="T1,T2",
                       help="comma-separated alert types (default: all)")
    p_sub.add_argument("--channels", metavar="C1,C2",
                       help="comma-separated channels (default: all configured)")
    p_sub.add_argument("--urgent", action="store_true",
                       help="send urgent alert types (missing, due soon, grade drop) "
                            "immediately even when --at batches the rest")
    p_sub.add_argument("--at", metavar="HH:MM",
                       help="deliver daily at this time instead of immediately: "
                            "event types batch into a digest; daily_summary "
                            "generates the standing report (default 07:00)")
    p_sub.set_defaults(func=_cmd_subscribe)

    p_uns = sub.add_parser("unsubscribe", help="remove a watcher's subscriptions")
    p_uns.add_argument("watcher")
    p_uns.add_argument("student", nargs="?", help="omit to drop all of them")
    p_uns.set_defaults(func=_cmd_unsubscribe)

    sub.add_parser("subscriptions", help="list who gets what").set_defaults(func=_cmd_subscriptions)

    # Alert log, and the deferred-delivery outbox
    p_al = sub.add_parser("alerts", help="list recent alerts")
    p_al.add_argument("--limit", type=int, default=50)
    p_al.set_defaults(func=_cmd_alerts)

    sub.add_parser("flush", help="send due digests/summaries now; list what's still queued"
                   ).set_defaults(func=_cmd_flush)

    p_dash = sub.add_parser("dashboard", help="serve the web dashboard")
    p_dash.add_argument("--host", help="bind address (default: 127.0.0.1)")
    p_dash.add_argument("--port", type=int, help="port (default: 8321)")
    p_dash.add_argument("--db", help="serve a different database "
                        "(e.g. the seed-demo output; default: env)")
    p_dash.set_defaults(func=_cmd_dashboard)

    p_seed = sub.add_parser(
        "seed-demo",
        help="fabricate a demo database: a fake family at quarter-end volume "
             "(screenshots, docs, design work — no real student data)")
    p_seed.add_argument("--db",
                        help="where to write it (default: demo.db in the data dir)")
    p_seed.add_argument("--seed", type=int, default=2026,
                        help="RNG seed; same seed, same database")
    p_seed.add_argument("--force", action="store_true",
                        help="overwrite an existing file at --db")
    p_seed.set_defaults(func=_cmd_seed_demo)

    args = parser.parse_args()
    try:
        # Every user-facing failure exits as one plain-language line with the
        # next step — a traceback is a bug report, not an error message.
        import requests

        from .client import LoginError, ParentVueError
        from .gradebook import ParseError
        from .watchers import WatcherError

        raise SystemExit(args.func(args))
    except (cfg.ConfigError, secretstore.SecretError, WatcherError, LoginError) as e:
        print(f"error: {e}", file=sys.stderr)
        raise SystemExit(2)
    except ParseError as e:
        print(f"error: the portal's pages have changed shape ({e}). Run "
              f"`lastbell preflight --report` and file a district report — "
              f"that's the fastest path to a parser fix.", file=sys.stderr)
        raise SystemExit(2)
    except ParentVueError as e:
        print(f"error: the portal answered in an unexpected way ({e}). Nothing "
              f"was collected — `lastbell preflight` shows whether the data "
              f"path still works.", file=sys.stderr)
        raise SystemExit(2)
    except requests.RequestException as e:
        print(f"error: couldn't reach the portal "
              f"({e.__class__.__name__}). Check the network and "
              f"LASTBELL_DISTRICT; nothing was collected.", file=sys.stderr)
        raise SystemExit(2)
    except KeyboardInterrupt:
        print("\nstopped.", file=sys.stderr)
        raise SystemExit(130)


if __name__ == "__main__":
    main()

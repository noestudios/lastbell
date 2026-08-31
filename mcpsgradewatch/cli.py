"""Command-line entrypoint: ``mcpsgradewatch <command>``."""
from __future__ import annotations

import argparse
import sys

from . import __version__
from . import config as cfg
from . import secrets as secretstore


def _cmd_set_password(args: argparse.Namespace) -> int:
    conf = cfg.load()
    pw = secretstore.prompt_password(f"Password for {conf.username} (hidden): ")
    secretstore.set_password(conf.username, pw)
    print(f"Stored password for {conf.username} in the OS keyring.")
    return 0


def _cmd_preflight(args: argparse.Namespace) -> int:
    from . import preflight

    argv = ["mcpsgradewatch preflight"]
    if args.show_values:
        argv.append("--show-values")
    if args.dump:
        argv.append("--dump")
    sys.argv = argv
    preflight.main()
    return 0


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
    from . import differ, router, store
    from .collector import collect_student

    total = 0
    for child in client.get_children():
        col = collect_student(client, child)
        for err in col.errors:
            print(f"warning [{col.student.initials}]: {err}", file=sys.stderr)

        snapshot = differ.apply_time_rules(
            col.snapshot,
            grace_days=conf.ungraded_grace_days,
            lookahead_days=conf.lookahead_days,
        )
        previous = store.load_snapshot(conn, child.agu)
        events = differ.diff(previous, snapshot)
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
            deliveries, warnings = router.plan(conn, child.agu, events)
            if router.has_subscriptions(conn, child.agu):
                sent, send_warnings = router.dispatch(deliveries, col.student.initials)
                warnings += send_warnings
                print(f"  delivered to {sent} watcher channel(s)")
            else:
                notifier.send(router.subject(col.student.initials, events),
                              "\n".join(f"• {e.detail}" for e in events))
            for w in warnings:
                print(f"warning: {w}", file=sys.stderr)
            for e in events:
                store.record_alert(conn, child.agu, e)
            total += len(events)
    return total


def _cmd_run(args: argparse.Namespace) -> int:
    """Phase 1: the watch loop — snapshot, diff against last run, alert."""
    import time

    from . import notify, store
    from .client import ParentVueClient

    conf = cfg.load()
    pw = secretstore.get_password(conf.username, conf.secret_backend)
    notifier = notify.get(conf.notify_channel)

    conn = store.connect(conf.db_path)
    store.ensure_schema(conn)
    try:
        while True:
            # Fresh client per pass: portal sessions expire well inside a poll
            # interval, and re-login is one request.
            client = ParentVueClient(conf.base_url, conf.username, pw)
            try:
                _run_once(client, conn, notifier, conf)
            except Exception as e:
                if not args.loop:
                    raise
                print(f"poll failed (will retry next cycle): {e}", file=sys.stderr)
            if not args.loop:
                return 0
            print(f"sleeping {conf.poll_minutes} min …")
            time.sleep(conf.poll_minutes * 60)
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
            channels[name] = {key: value}
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
            print("no watchers yet — add one with `mcpsgradewatch watcher add`")
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
        added = watchers.subscribe(conn, w, student["id"], types, chans)
        scope = args.types or "all alerts"
        via = args.channels or "all configured channels"
        print(f"{w.name} ⇒ {student['name']}: {scope} via {via} "
              f"({added} new subscription row(s))")
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
            print("no subscriptions yet — create one with `mcpsgradewatch subscribe`")
            return 0
        for s in subs:
            alert = "all alerts" if s.alert_type == "*" else s.alert_type
            via = "all configured channels" if s.channel == "*" else s.channel
            print(f"  {s.watcher_name} ⇒ {s.student_name}: {alert} via {via}")
        return 0
    finally:
        conn.close()


def _cmd_dashboard(args: argparse.Namespace) -> int:
    from . import dashboard

    conf = cfg.load()
    dashboard.serve(conf.db_path,
                    args.host or conf.dashboard_host,
                    args.port or conf.dashboard_port)
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="mcpsgradewatch", description="Self-hosted ParentVUE grade monitor")
    parser.add_argument("--version", action="version", version=f"mcpsgradewatch {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("set-password", help="store a credential's password in the OS keyring").set_defaults(func=_cmd_set_password)

    p_pre = sub.add_parser("preflight", help="district go/no-go check")
    p_pre.add_argument("--show-values", action="store_true")
    p_pre.add_argument("--dump", action="store_true",
                       help="save raw portal pages to data/debug/ (local only)")
    p_pre.set_defaults(func=_cmd_preflight)

    sub.add_parser("discover", help="list students on the configured credential").set_defaults(func=_cmd_discover)
    sub.add_parser("init-db", help="create the local database").set_defaults(func=_cmd_init_db)
    sub.add_parser("collect", help="pull all gradebook data as JSON (read-only)").set_defaults(func=_cmd_collect)

    p_run = sub.add_parser("run", help="collect, diff against the last run, and alert")
    p_run.add_argument("--loop", action="store_true",
                       help="keep polling every MCPSGRADEWATCH_POLL_MINUTES")
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

    p_sub = sub.add_parser("subscribe", help="route a student's alerts to a watcher")
    p_sub.add_argument("watcher", help="watcher name")
    p_sub.add_argument("student", help="student AGU, or a name/initials prefix")
    p_sub.add_argument("--types", metavar="T1,T2",
                       help="comma-separated alert types (default: all)")
    p_sub.add_argument("--channels", metavar="C1,C2",
                       help="comma-separated channels (default: all configured)")
    p_sub.set_defaults(func=_cmd_subscribe)

    p_uns = sub.add_parser("unsubscribe", help="remove a watcher's subscriptions")
    p_uns.add_argument("watcher")
    p_uns.add_argument("student", nargs="?", help="omit to drop all of them")
    p_uns.set_defaults(func=_cmd_unsubscribe)

    sub.add_parser("subscriptions", help="list who gets what").set_defaults(func=_cmd_subscriptions)

    p_dash = sub.add_parser("dashboard", help="serve the read-only web dashboard")
    p_dash.add_argument("--host", help="bind address (default: 127.0.0.1)")
    p_dash.add_argument("--port", type=int, help="port (default: 8321)")
    p_dash.set_defaults(func=_cmd_dashboard)

    args = parser.parse_args()
    try:
        from .watchers import WatcherError

        raise SystemExit(args.func(args))
    except (cfg.ConfigError, secretstore.SecretError, WatcherError) as e:
        print(f"error: {e}", file=sys.stderr)
        raise SystemExit(2)


if __name__ == "__main__":
    main()

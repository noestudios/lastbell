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
    from . import differ, store
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
            subject = f"[MCPSGradeWatch] {len(events)} update(s) for {col.student.initials}"
            body = "\n".join(f"• {e.detail}" for e in events)
            notifier.send(subject, body)
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

    args = parser.parse_args()
    try:
        raise SystemExit(args.func(args))
    except (cfg.ConfigError, secretstore.SecretError) as e:
        print(f"error: {e}", file=sys.stderr)
        raise SystemExit(2)


if __name__ == "__main__":
    main()

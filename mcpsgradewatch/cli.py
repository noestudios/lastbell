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
    """Pull normalized gradebook data for every student and print it as JSON.

    Phase 0 scope: per student, the term list + subject rows and the default
    class's grade/assignments. Phase 1 iterates every subject and term, and
    persists snapshots for diffing.
    """
    import dataclasses
    import json

    from .client import ParentVueClient
    from .gradebook import parse_class_details, parse_school_classes

    conf = cfg.load()
    pw = secretstore.get_password(conf.username, conf.secret_backend)
    client = ParentVueClient(conf.base_url, conf.username, pw)

    out = []
    for child in client.get_children():
        focus = client.get_focus_args(child.agu)
        sc = parse_school_classes(
            client.load_control("Gradebook_SchoolClasses", focus.as_parameters(),
                                agu_header=focus.agu_header)
        )
        cd = parse_class_details(
            client.load_control("Gradebook_ClassDetails", focus.as_parameters(),
                                agu_header=focus.agu_header),
            course_gu=str(focus.args.get("classID", "")),
        )
        out.append({
            "student": {"agu": child.agu, "name": child.name, "school": child.school},
            "term": sc.current_term,
            "mark_periods": [dataclasses.asdict(p) for p in sc.mark_periods],
            "subjects": [dataclasses.asdict(r) for r in sc.rows],
            "default_class": {
                "mark": cd.mark,
                "percent": cd.percent,
                "missing": cd.missing_text,
                "assignments": [dataclasses.asdict(a) for a in cd.assignments],
            },
        })

    print(json.dumps(out, indent=2, default=str))
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
    sub.add_parser("collect", help="(Phase 0 gate) pull gradebook data").set_defaults(func=_cmd_collect)

    args = parser.parse_args()
    try:
        raise SystemExit(args.func(args))
    except (cfg.ConfigError, secretstore.SecretError) as e:
        print(f"error: {e}", file=sys.stderr)
        raise SystemExit(2)


if __name__ == "__main__":
    main()

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
    print(
        "collect: blocked on the Phase 0 gate — gradebook.py parsers are stubs "
        "until a real LoadControl fragment is captured. Run `mcpsgradewatch preflight` "
        "to check the gate."
    )
    return 1


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

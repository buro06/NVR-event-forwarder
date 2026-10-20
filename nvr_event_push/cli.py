"""Command-line entry point."""

from __future__ import annotations

import argparse
import logging
import signal
import sys

from . import __version__, runner
from .config import ConfigError, load, resolve_path
from .client import AuthError, NvrError, make_client


def setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # requests/urllib3 debug output is noise unless something is really wrong.
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def _load_or_exit(path, need_nvr: bool = True):
    try:
        config = load(path)
    except FileNotFoundError:
        print(
            f"No config found at {resolve_path(path)}. "
            "Run `nvr-event-push` (no arguments) to set one up.",
            file=sys.stderr,
        )
    except ConfigError as exc:
        print(f"Config error in {resolve_path(path)}: {exc}", file=sys.stderr)
    else:
        if config.nvrs or not need_nvr:
            return config
        print(
            f"No NVR configured in {resolve_path(path)}. "
            "Run `nvr-event-push` (no arguments) to add one.",
            file=sys.stderr,
        )
    sys.exit(2)


def cmd_run(args) -> int:
    config = _load_or_exit(args.config)
    # Let `systemctl stop` / `docker stop` shut down as cleanly as Ctrl+C.
    signal.signal(signal.SIGTERM, signal.default_int_handler)
    try:
        runner.run_forwarder(config)
    except AuthError as exc:
        logging.error("%s", exc)
        return 2
    except KeyboardInterrupt:
        logging.info("Stopped")
    return 0


def cmd_watch(args) -> int:
    config = _load_or_exit(args.config)
    print("Watching events (Ctrl+C to stop)...", flush=True)
    try:
        runner.watch(config, show_all=args.all, raw=args.raw)
    except AuthError as exc:
        logging.error("%s", exc)
        return 2
    except KeyboardInterrupt:
        pass
    return 0


def cmd_cameras(args) -> int:
    config = _load_or_exit(args.config)
    status = 0
    for nvr in config.nvrs:
        if len(config.nvrs) > 1:
            print(f"{nvr.name} ({nvr.host}):")
        try:
            cameras = make_client(nvr).list_channels()
        except NvrError as exc:
            print(exc, file=sys.stderr)
            status = 1
            continue
        if not cameras:
            print("The NVR did not report any channels.")
            status = 1
        for cid, name in cameras.items():
            print(f"{cid:>3}  {name}")
    return status


def cmd_test_url(args) -> int:
    config = _load_or_exit(args.config, need_nvr=False)
    rule = next((r for r in config.rules if r.name == args.rule), None)
    if rule is None:
        names = ", ".join(repr(r.name) for r in config.rules) or "none"
        print(f"No rule named {args.rule!r}. Rules: {names}", file=sys.stderr)
        return 1
    if args.off and not rule.off_url:
        print(f"Rule {args.rule!r} has no off URL", file=sys.stderr)
        return 1
    resp = runner.send_test(config, rule, off=args.off)
    return 0 if resp is not None and resp.ok else 1


def cmd_menu(args) -> int:
    from .interactive import run_menu

    return run_menu(args.config)


def _common_options(suppress_defaults: bool) -> argparse.ArgumentParser:
    # Options are accepted both before and after the subcommand. Subparsers
    # must not apply their own defaults, or they'd overwrite `-c x run`.
    kw = {"default": argparse.SUPPRESS} if suppress_defaults else {}
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "-c",
        "--config",
        help="config file (default: $NVR_EVENT_PUSH_CONFIG or ./config.yaml)",
        **kw,
    )
    common.add_argument("-v", "--verbose", action="store_true", help="debug logging", **kw)
    return common


def build_parser() -> argparse.ArgumentParser:
    common = _common_options(suppress_defaults=True)
    parser = argparse.ArgumentParser(
        prog="nvr-event-push",
        description="Forward Hikvision/Annke or Dahua/Amcrest NVR motion events to HTTP GET "
        "requests. "
        "Run without a command for the interactive menu.",
        parents=[_common_options(suppress_defaults=False)],
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("menu", parents=[common], help="interactive setup menu (default)").set_defaults(
        func=cmd_menu
    )
    sub.add_parser("run", parents=[common], help="listen and forward (headless)").set_defaults(
        func=cmd_run
    )
    p = sub.add_parser("watch", parents=[common], help="print live events without forwarding")
    p.add_argument("--all", action="store_true", help="include idle heartbeat messages")
    p.add_argument("--raw", action="store_true", help="also print the raw event XML/JSON")
    p.set_defaults(func=cmd_watch)
    sub.add_parser("cameras", parents=[common], help="list cameras on each NVR").set_defaults(
        func=cmd_cameras
    )
    p = sub.add_parser("test-url", parents=[common], help="send one test request for a rule")
    p.add_argument("rule", help="rule name")
    p.add_argument("--off", action="store_true", help="send the rule's off request instead")
    p.set_defaults(func=cmd_test_url)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    setup_logging(args.verbose)
    func = getattr(args, "func", cmd_menu)
    return func(args)

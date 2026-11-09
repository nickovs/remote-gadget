"""Command line interface: ``remote-gadget <command> ...``."""

from __future__ import annotations

import argparse
import sys
import warnings
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from . import __version__
from .api import Gadget
from .config import Config
from .errors import RemoteGadgetError, TypingTimeoutError, UsageError
from .output import Reporter

EXIT_REBOOT = 4
EXIT_INTERRUPTED = 130

_STATUS_LEVEL = {"ok": "success", "warn": "warn", "fail": "error", "unknown": "info"}


def _global_options() -> argparse.ArgumentParser:
    # SUPPRESS defaults let these appear before or after the subcommand without clobbering.
    p = argparse.ArgumentParser(add_help=False, argument_default=argparse.SUPPRESS)
    g = p.add_argument_group("global options")
    g.add_argument("-p", "--profile", help="profile name in the config file")
    g.add_argument("--config", type=Path, help="use only this config file")
    g.add_argument("--host", help="gadget host name or address")
    g.add_argument("-u", "--username", help="ssh user name")
    g.add_argument("-i", "--key", help="ssh private key file")
    g.add_argument("--sudo", dest="nosudo", action="store_false", help="use sudo (default)")
    g.add_argument("--nosudo", dest="nosudo", action="store_true", help="never use sudo")
    g.add_argument("--path", help="state directory on the gadget host")
    g.add_argument("--size", type=int, help="storage size in MiB (used by init)")
    g.add_argument("--keymap", help="keyboard layout, e.g. us, gb, de:nodeadkeys")
    g.add_argument("--vendor-id", type=lambda s: int(s, 0), help="USB vendor ID")
    g.add_argument("--product-id", type=lambda s: int(s, 0), help="USB product ID")
    g.add_argument("-q", "--quiet", action="store_true", help="only print warnings and errors")
    g.add_argument("--color", choices=["auto", "always", "never"], help="colour output")
    return p


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser (exposed for tests and documentation)."""
    common = _global_options()
    p = argparse.ArgumentParser(
        prog="remote-gadget",
        description="Control a Raspberry Pi USB gadget (keyboard + storage) over ssh.",
        parents=[common],
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = p.add_subparsers(dest="command", required=True, metavar="command")

    def add(name: str, help: str) -> argparse.ArgumentParser:
        return sub.add_parser(name, help=help, description=help, parents=[common])

    sp = add("init", "set up the gadget host (idempotent)")
    sp.add_argument("--reboot", action="store_true", help="reboot if needed")
    sp.add_argument("--install-packages", action="store_true", help="apt-get install tools")
    sp.add_argument("--reformat", action="store_true", help="recreate the image (erases it)")
    sp.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation")
    add("state", "show whether the gadget is plugged in")
    add("doctor", "diagnose problems").add_argument(
        "--fix", action="store_true", help="fix what can be fixed"
    )
    sp = add("write", "copy a local file into the gadget's storage")
    sp.add_argument("local")
    sp.add_argument("remote", nargs="?")
    sp = add("read", "copy a file out of the gadget's storage")
    sp.add_argument("remote")
    sp.add_argument("local", nargs="?")
    sp.add_argument("-f", "--force", action="store_true", help="overwrite an existing file")
    add("ls", "list the gadget's storage").add_argument("directory", nargs="?", default="/")
    sp = add("rm", "delete a file or directory from the gadget's storage")
    sp.add_argument("remote")
    sp.add_argument("-r", "--recursive", action="store_true", help="delete a directory tree")
    sp = add("plug", "connect the gadget to the target")
    mode = sp.add_mutually_exclusive_group()
    mode.add_argument("--ro", dest="ro", action="store_true", default=None, help="read-only")
    mode.add_argument("--rw", dest="ro", action="store_false", help="read-write")
    add("unplug", "disconnect the gadget from the target")
    sp = add("type", "type text on the target (from TEXT or stdin)")
    sp.add_argument("text", nargs="?", help="text to type; '-' or omitted reads stdin")
    sp.add_argument("--enter", action="store_true", help="press Enter after the text")
    sp.add_argument("--timeout", type=float, default=5.0, help="seconds without progress")
    sp.add_argument("--delay", type=float, default=0.0, help="milliseconds between keys")
    add("reset", "send Ctrl-Alt-Del to the target")
    return p


_OVERRIDES = ("host", "username", "key", "nosudo", "path", "size", "keymap", "vendor_id",
              "product_id")  # fmt: skip


def load_config(args: argparse.Namespace, out: Reporter) -> Config:
    """Build the Config from files and command-line overrides, reporting warnings."""
    overrides = {k: getattr(args, k, None) for k in _OVERRIDES}
    config_file = getattr(args, "config", None)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        cfg = Config.load(
            profile=getattr(args, "profile", None),
            overrides=overrides,
            files=[config_file] if config_file else None,
        )
    for w in caught:
        out.warn(str(w.message))
    return cfg


def _print_checks(out: Reporter, checks: list[dict[str, Any]]) -> None:
    for c in checks:
        level = _STATUS_LEVEL.get(c["status"], "info")
        tag = "fixed" if c.get("fixed") else c["status"]
        line = f"[{tag:>7}] {c['name']}: {c['message']}"
        if level == "error":
            out.error(line, c.get("hint"))
        elif level == "warn":
            out.warn(line + (f" ({c['hint']})" if c.get("hint") else ""))
        elif level == "success":
            out.success(line)
        else:
            out.info(line)


def _confirm(question: str) -> bool:
    if not sys.stdin.isatty():
        raise UsageError(f"{question} (needs --yes when not interactive)")
    return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")


def run(args: argparse.Namespace, out: Reporter) -> int:
    """Execute a parsed command; return the exit status."""
    cfg = load_config(args, out)
    g = Gadget(cfg, on_warning=out.warn)
    name = cfg.profile or cfg.host
    cmd = args.command

    if cmd == "init":
        if args.reformat and not args.yes and not _confirm("Erase the gadget's storage image?"):
            raise UsageError("aborted")
        res = g.init(reboot=args.reboot, install_packages=args.install_packages,
                     reformat=args.reformat)  # fmt: skip
        _print_checks(out, res["checks"])
        if res.get("reboot_required"):
            if res.get("rebooting"):
                out.warn(f"{name}: rebooting to enable the USB device controller")
            else:
                out.warn(f"{name}: reboot required (re-run with --reboot, or reboot it yourself)")
            return EXIT_REBOOT
        out.success(f"{name}: initialised")
    elif cmd == "doctor":
        res = g.doctor(fix=args.fix)
        _print_checks(out, res["checks"])
        unknown = sum(c["status"] == "unknown" for c in res["checks"])
        if unknown:
            out.warn(f"{unknown} checks could not be verified on this platform")
        if any(c["status"] == "fail" for c in res["checks"]):
            return 1
        if res.get("reboot_required"):
            out.warn(f"{name}: reboot required")
            return EXIT_REBOOT
        out.success(f"{name}: no problems found")
    elif cmd == "state":
        st = g.state()
        line = f"{name}: {st.state}"
        if st.state == "plugged":
            line += f" {'read-only' if st.ro else 'read-write'}"
            line += f" (udc {st.udc}, target: {st.udc_state})"
        out.info(line)
        if st.size_mb is not None:
            free = "" if st.free_bytes is None else f", {st.free_bytes / 2**20:.1f} MiB free"
            out.info(f"storage: {st.size_mb} MiB{free} · keymap: {cfg.keymap}")
    elif cmd == "write":
        res = g.write(args.local, args.remote)
        out.success(f"wrote {res['bytes']} bytes to {res['dst']}")
    elif cmd == "read":
        res = g.read(args.remote, args.local, force=args.force)
        out.success(f"read {res['bytes']} bytes to {res['local']}")
    elif cmd == "ls":
        out.info(g.ls(args.directory).rstrip())
    elif cmd == "rm":
        res = g.rm(args.remote, recursive=args.recursive)
        out.success(f"deleted {res['kind']} {res['removed']}")
    elif cmd == "plug":
        res = g.plug(ro=args.ro)
        mode = "read-only" if res["ro"] else "read-write"
        out.success(f"{name}: plugged {mode}" + ("" if res["changed"] else " (already)"))
    elif cmd == "unplug":
        res = g.unplug()
        out.success(f"{name}: unplugged" + ("" if res["changed"] else " (already)"))
    elif cmd == "type":
        text = sys.stdin.read() if args.text in (None, "-") else args.text
        if args.enter:
            text += "\n"
        try:
            res = g.type(text, timeout=args.timeout, delay_ms=args.delay)
        except TypingTimeoutError as e:
            out.error(e.message, e.hint)
            out.error(f"not typed: {e.unsent!r}")
            return 1
        out.success(f"typed {res['sent']} characters")
    elif cmd == "reset":
        g.reset()
        out.success(f"{name}: sent Ctrl-Alt-Del")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for the ``remote-gadget`` script."""
    args = build_parser().parse_args(argv)
    out = Reporter(quiet=getattr(args, "quiet", False), colour=getattr(args, "color", "auto"))
    try:
        return run(args, out)
    except RemoteGadgetError as e:
        out.error(e.message, e.hint)
        return e.exit_code
    except KeyboardInterrupt:
        out.error("interrupted")
        return EXIT_INTERRUPTED


if __name__ == "__main__":
    sys.exit(main())

"""Terse, optionally coloured, console output."""

from __future__ import annotations

import os
import sys
from typing import TextIO

_COLOURS = {"success": "32", "warn": "33", "error": "31"}


def use_colour(stream: TextIO, mode: str) -> bool:
    """Decide whether to colour ``stream`` for ``--color`` ``mode`` (auto/always/never)."""
    if mode != "auto":
        return mode == "always"
    return stream.isatty() and "NO_COLOR" not in os.environ and os.environ.get("TERM") != "dumb"


class Reporter:
    """Prints info/success to stdout and warn/error to stderr.  ``quiet`` hides stdout."""

    def __init__(
        self,
        quiet: bool = False,
        colour: str = "auto",
        out: TextIO | None = None,
        err: TextIO | None = None,
    ):
        self.quiet = quiet
        self.out = out or sys.stdout
        self.err = err or sys.stderr
        self._colour = {s: use_colour(s, colour) for s in (self.out, self.err)}

    def _emit(self, stream: TextIO, level: str, msg: str) -> None:
        if level in _COLOURS and self._colour[stream]:
            msg = f"\033[{_COLOURS[level]}m{msg}\033[0m"
        print(msg, file=stream, flush=True)  # keep stdout/stderr ordered when piped

    def info(self, msg: str) -> None:
        if not self.quiet:
            self._emit(self.out, "info", msg)

    def success(self, msg: str) -> None:
        if not self.quiet:
            self._emit(self.out, "success", msg)

    def warn(self, msg: str) -> None:
        self._emit(self.err, "warn", "warning: " + msg)

    def error(self, msg: str, hint: str | None = None) -> None:
        self._emit(self.err, "error", "error: " + msg)
        if hint:
            self._emit(self.err, "info", "  hint: " + hint)

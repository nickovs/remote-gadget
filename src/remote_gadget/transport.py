"""Running commands on, and copying files to and from, the gadget host."""

from __future__ import annotations

import os
import shlex
import subprocess
from typing import Protocol

from .config import Config
from .errors import SshConnectionError

SSH_CONNECT_FAILED = 255


class Transport(Protocol):
    """What :class:`~remote_gadget.api.Gadget` needs from a connection."""

    def run(
        self, argv: list[str], stdin: bytes | None = None, timeout: float | None = None
    ) -> subprocess.CompletedProcess[bytes]:
        """Run ``argv`` on the gadget host; return its exit status and output."""
        ...

    def upload(self, local: str, remote: str) -> None:
        """Copy local file ``local`` to ``remote`` on the gadget host."""
        ...

    def download(self, remote: str, local: str) -> None:
        """Copy ``remote`` on the gadget host to local file ``local``."""
        ...


class SshTransport:
    """:class:`Transport` using the system ``ssh`` and ``scp`` commands."""

    def __init__(self, config: Config, connect_timeout: int = 10):
        self.config = config
        self.options = ["-o", "BatchMode=yes", "-o", f"ConnectTimeout={connect_timeout}"]
        if config.key:
            self.options += ["-i", config.key]
        self.target = f"{config.username}@{config.host}" if config.username else config.host

    def _exec(
        self, cmd: list[str], stdin: bytes | None, timeout: float | None
    ) -> subprocess.CompletedProcess[bytes]:
        try:
            return subprocess.run(cmd, input=stdin, capture_output=True, timeout=timeout)
        except FileNotFoundError:
            raise SshConnectionError(f"{cmd[0]} is not installed") from None
        except subprocess.TimeoutExpired:
            raise SshConnectionError(f"{cmd[0]} to {self.config.host} timed out") from None

    def run(
        self, argv: list[str], stdin: bytes | None = None, timeout: float | None = None
    ) -> subprocess.CompletedProcess[bytes]:
        """Run ``argv`` remotely.  Each argument is quoted, so none is shell-interpreted."""
        remote_cmd = " ".join(shlex.quote(a) for a in argv)
        cmd = ["ssh", *self.options, "--", self.target, remote_cmd]
        return self._exec(cmd, stdin if stdin is not None else b"", timeout)

    def _scp(self, src: str, dst: str) -> None:
        r = self._exec(["scp", "-q", *self.options, "--", src, dst], None, None)
        if r.returncode != 0:
            msg = r.stderr.decode(errors="replace").strip() or f"exit status {r.returncode}"
            raise SshConnectionError(f"scp failed: {msg}")

    def upload(self, local: str, remote: str) -> None:
        """Copy ``local`` to ``remote``.  ``remote`` must have no shell metacharacters."""
        self._scp(_local(local), f"{self.target}:{remote}")

    def download(self, remote: str, local: str) -> None:
        """Copy ``remote`` to ``local``."""
        self._scp(f"{self.target}:{remote}", _local(local))


def _local(path: str) -> str:
    # An absolute path stops scp reading "a:b" as host "a".
    return os.path.abspath(path)

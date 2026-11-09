"""High-level API: one :class:`Gadget` per gadget host.

Methods return data and raise :class:`~remote_gadget.errors.RemoteGadgetError`
subclasses; they never print.  Warnings from the gadget host go to ``on_warning``.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import socket
import subprocess
import uuid
from collections.abc import Callable
from importlib import resources
from typing import Any

from .config import Config
from .errors import (
    REMOTE_ERRORS,
    LocalFileError,
    NotInitialisedError,
    RemoteError,
    SshConnectionError,
)
from .transport import SSH_CONNECT_FAILED, SshTransport, Transport

HELPER_EXIT_VERSION = 10
SHELL_NOT_FOUND = 127


def helper_source() -> bytes:
    """The bytes of the remote helper shipped with this package."""
    return resources.files("remote_gadget.remote").joinpath("helper.py").read_bytes()


def helper_version() -> str:
    """Content hash the helper compares against its own file (see ``--expect-version``)."""
    return hashlib.sha256(helper_source()).hexdigest()[:16]


@dataclasses.dataclass(frozen=True)
class GadgetState:
    """Result of :meth:`Gadget.state`."""

    state: str  # uninitialised | stopped | unplugged | plugged
    udc: str | None = None
    udc_state: str | None = None  # e.g. "configured"; only known while plugged
    ro: bool | None = None
    size_mb: int | None = None
    free_bytes: int | None = None


class Gadget:
    """Control one gadget host described by ``config``."""

    def __init__(
        self,
        config: Config,
        transport: Transport | None = None,
        on_warning: Callable[[str], None] | None = None,
    ):
        self.config = config
        self.transport = transport or SshTransport(config)
        self.on_warning = on_warning or (lambda msg: None)
        self.helper_path = f"{config.path}/bin/rgadget-helper"

    # ------------------------------------------------------------------ helper plumbing

    def _helper(
        self,
        op: str,
        *args: str,
        root: bool = False,
        stdin: bytes | None = None,
        bootstrap: bool = False,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Run a helper operation and return its ``result``.

        ``bootstrap`` runs the packaged helper from stdin, for when the installed
        copy may be missing (``init``, ``doctor``).
        """
        prefix = ["sudo", "-n"] if root and not self.config.nosudo else []
        if bootstrap:
            argv = [*prefix, "python3", "-", "--path", self.config.path, op, *args]
            stdin = helper_source()
        else:
            argv = [*prefix, "python3", self.helper_path, "--expect-version", helper_version(),
                    "--path", self.config.path, op, *args]  # fmt: skip
        r = self.transport.run(argv, stdin=stdin, timeout=timeout)
        if r.returncode == HELPER_EXIT_VERSION and not bootstrap:
            self.install_helper()
            r = self.transport.run(argv, stdin=stdin, timeout=timeout)
        return self._parse(r)

    def _parse(self, r: subprocess.CompletedProcess[bytes]) -> dict[str, Any]:
        stderr = r.stderr.decode(errors="replace").strip()
        if r.returncode == SSH_CONNECT_FAILED:
            raise SshConnectionError(
                f"cannot connect to {self.config.host}: {stderr}", "run `remote-gadget doctor`"
            )
        try:
            payload = json.loads(r.stdout.decode(errors="replace").strip().splitlines()[-1])
        except (ValueError, IndexError):
            payload = None
        if not isinstance(payload, dict):
            if r.returncode in (2, SHELL_NOT_FOUND) and "No such file" in stderr:
                raise NotInitialisedError(
                    "not_initialised", "helper not installed", "run `remote-gadget init`"
                )
            if "password is required" in stderr:
                raise RemoteError(
                    "sudo", "sudo needs a password",
                    "allow passwordless sudo for this user, or set `nosudo`",
                )  # fmt: skip
            raise RemoteError("helper_failed", f"helper failed: {stderr[-500:] or r.returncode}")
        for warning in payload.get("warnings", []):
            self.on_warning(str(warning))
        if payload.get("ok"):
            return dict(payload.get("result") or {})
        err = dict(payload.get("error") or {})
        code = str(err.pop("code", "unknown"))
        cls = REMOTE_ERRORS.get(code, RemoteError)
        raise cls(code, str(err.pop("message", code)), err.pop("hint", None), **err)

    def install_helper(self) -> None:
        """Upload the packaged helper to the gadget host."""
        with resources.as_file(resources.files("remote_gadget.remote") / "helper.py") as src:
            self.transport.upload(str(src), self.helper_path)

    def _id_args(self) -> list[str]:
        return ["--vendor-id", hex(self.config.vendor_id), "--product-id",
                hex(self.config.product_id)]  # fmt: skip

    # ------------------------------------------------------------------ commands

    def init(
        self, reboot: bool = False, install_packages: bool = False, reformat: bool = False
    ) -> dict[str, Any]:
        """Set up the gadget host (idempotent).  Returns the setup checks.

        If ``reboot_required`` is set in the result and ``reboot`` is true, the host
        is rebooted.
        """
        args = ["--size", str(self.config.size)]
        if install_packages:
            args.append("--install-packages")
        if reformat:
            args.append("--reformat")
        result = self._helper("setup", *args, root=True, bootstrap=True)
        self.install_helper()
        if result.get("reboot_required") and reboot:
            prefix = [] if self.config.nosudo else ["sudo", "-n"]
            # ssh may drop with 255 as the host goes down; that is expected.
            self.transport.run([*prefix, "systemctl", "reboot"], timeout=30)
            result["rebooting"] = True
        return result

    def state(self) -> GadgetState:
        """Report the gadget's lifecycle state (never raises for "uninitialised")."""
        try:
            res = self._helper("state")
        except NotInitialisedError:
            return GadgetState(state="uninitialised")
        fields = {f.name for f in dataclasses.fields(GadgetState)}
        return GadgetState(**{k: v for k, v in res.items() if k in fields})

    def plug(self, ro: bool | None = None) -> dict[str, Any]:
        """Connect the gadget to the target.  ``ro`` defaults to the profile's ``readonly``."""
        ro = self.config.readonly if ro is None else ro
        return self._helper("plug", *self._id_args(), *(["--ro"] if ro else []), root=True)

    def unplug(self) -> dict[str, Any]:
        """Disconnect the gadget so the target sees no device."""
        return self._helper("unplug", *self._id_args(), root=True)

    def write(self, local: str, remote: str | None = None) -> dict[str, Any]:
        """Copy local file ``local`` into the image (default: its basename in the root)."""
        if not os.path.isfile(local):
            raise LocalFileError(f"{local}: not a regular file")
        if not os.access(local, os.R_OK):
            raise LocalFileError(f"{local}: not readable")
        name = os.path.basename(local)
        if remote is None:
            remote = "/" + name
        elif remote.endswith("/"):
            remote += name
        staged = uuid.uuid4().hex
        self.transport.upload(local, f"{self.config.path}/staging/{staged}")
        return self._helper("img-put", staged, remote)  # the helper removes the staged copy

    def read(self, remote: str, local: str | None = None, force: bool = False) -> dict[str, Any]:
        """Copy ``remote`` from the image to ``local`` (default: ./basename)."""
        name = remote.rstrip("/").rsplit("/", 1)[-1]
        if not name:
            raise LocalFileError("no file name in remote path")
        local = local or name
        if os.path.isdir(local):
            local = os.path.join(local, name)
        if os.path.exists(local) and not force:
            raise LocalFileError(f"{local} exists", "use -f to overwrite")
        staged = uuid.uuid4().hex
        try:
            result = self._helper("img-get", remote, staged)
            part = local + ".part"
            try:
                self.transport.download(f"{self.config.path}/staging/{staged}", part)
                os.replace(part, local)
            except BaseException:
                if os.path.exists(part):
                    os.unlink(part)
                raise
        finally:
            self._helper("staging-clean", staged)
        result["local"] = local
        return result

    def ls(self, directory: str = "/") -> str:
        """Return the image's directory listing (mtools ``mdir`` format)."""
        return str(self._helper("img-ls", directory)["listing"])

    def rm(self, remote: str, recursive: bool = False) -> dict[str, Any]:
        """Delete ``remote`` from the image; directories need ``recursive`` unless empty."""
        return self._helper("img-rm", remote, *(["--recursive"] if recursive else []))

    def type(self, text: str, timeout: float = 5.0, delay_ms: float = 0) -> dict[str, Any]:
        """Type ``text`` on the target.  Raises TypingTimeoutError with the unsent text."""
        return self._helper(
            "type", "--keymap", self.config.keymap, "--timeout", str(timeout),
            "--delay", str(delay_ms), root=True, stdin=text.encode("utf-8"),
        )  # fmt: skip

    def reset(self, timeout: float = 5.0) -> dict[str, Any]:
        """Send Ctrl-Alt-Del to the target."""
        return self._helper("reset", "--timeout", str(timeout), root=True)

    def doctor(self, fix: bool = False) -> dict[str, Any]:
        """Diagnose (and with ``fix``, repair) the connection and the gadget host."""
        checks = self._local_checks()
        if any(c["status"] == "fail" for c in checks):
            return {"checks": checks, "reboot_required": False, "debian": None}
        try:
            remote = self._helper("doctor", *(["--fix"] if fix else []), root=fix, bootstrap=True)
        except RemoteError as e:
            checks.append(_check("remote-checks", "fail", e.message, e.hint))
            return {"checks": checks, "reboot_required": False, "debian": None}
        checks += remote["checks"]
        checks.append(self._helper_check())
        remote["checks"] = checks
        return remote

    def _local_checks(self) -> list[dict[str, Any]]:
        checks = []
        host, port = _resolve_ssh_target(self.config.host)
        try:
            socket.create_connection((host, port), timeout=5).close()
            checks.append(_check("network", "ok", f"{host}:{port} reachable"))
        except OSError as e:
            checks.append(_check("network", "fail", f"cannot reach {host}:{port}: {e}"))
            return checks
        r = self.transport.run(["true"], timeout=30)
        if r.returncode != 0:
            msg = r.stderr.decode(errors="replace").strip()
            checks.append(_check("ssh", "fail", f"ssh login failed: {msg}",
                                 "set up key-based login (ssh-copy-id)"))  # fmt: skip
            return checks
        checks.append(_check("ssh", "ok", "ssh login works"))
        if not self.config.nosudo:
            r = self.transport.run(["sudo", "-n", "true"], timeout=30)
            if r.returncode != 0:
                checks.append(_check("sudo", "fail", "passwordless sudo not available",
                                     "add a NOPASSWD sudoers rule or set `nosudo`"))  # fmt: skip
                return checks
            checks.append(_check("sudo", "ok", "passwordless sudo works"))
        return checks

    def _helper_check(self) -> dict[str, Any]:
        # Called directly (not via _helper) so an old helper is reported, not replaced.
        argv = ["python3", self.helper_path, "--expect-version", helper_version(), "ping"]
        r = self.transport.run(argv, timeout=30)
        if r.returncode == 0:
            return _check("helper", "ok", "installed helper is current")
        if r.returncode == HELPER_EXIT_VERSION:
            return _check("helper", "warn", "installed helper is out of date",
                          "it is updated automatically on next use")  # fmt: skip
        return _check("helper", "warn", "helper not installed", "run `remote-gadget init`")


def _check(name: str, status: str, message: str, hint: str | None = None) -> dict[str, Any]:
    return {"name": name, "status": status, "message": message, "hint": hint, "fixed": False}


def _resolve_ssh_target(host: str) -> tuple[str, int]:
    """Apply the user's ssh config (HostName, Port) to ``host``."""
    try:
        r = subprocess.run(["ssh", "-G", "--", host], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return host, 22
    opts = dict(line.split(" ", 1) for line in r.stdout.splitlines() if " " in line)
    try:
        return opts.get("hostname", host), int(opts.get("port", "22"))
    except ValueError:
        return host, 22

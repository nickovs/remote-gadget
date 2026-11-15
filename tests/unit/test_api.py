"""Gadget API against a fake transport."""

from __future__ import annotations

import json
import subprocess

import pytest

from remote_gadget.api import Gadget, helper_version
from remote_gadget.config import Config
from remote_gadget.errors import (
    LocalFileError,
    NotInitialisedError,
    RemoteError,
    SshConnectionError,
    TypingTimeoutError,
    UsageError,
    WrongStateError,
)


def ok(result=None, warnings=()):
    return (0, {"ok": True, "result": result or {}, "warnings": list(warnings)})


def fail(code, message="m", rc=1, **data):
    return (rc, {"ok": False, "error": {"code": code, "message": message, "hint": None, **data}})


class FakeTransport:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.runs: list[tuple[list[str], bytes | None]] = []
        self.uploads: list[tuple[str, str]] = []
        self.downloads: list[tuple[str, str]] = []

    def run(self, argv, stdin=None, timeout=None):
        self.runs.append((argv, stdin))
        rc, payload = self.replies.pop(0)
        out = json.dumps(payload).encode() if isinstance(payload, dict) else payload
        err = b"" if isinstance(payload, dict) else payload
        return subprocess.CompletedProcess(argv, rc, out, err)

    def upload(self, local, remote):
        self.uploads.append((local, remote))

    def download(self, remote, local):
        self.downloads.append((remote, local))
        with open(local, "wb") as f:
            f.write(b"data")


def gadget(*replies, **cfg):
    t = FakeTransport(*replies)
    warnings: list[str] = []
    g = Gadget(Config(host="pi", **cfg), transport=t, on_warning=warnings.append)
    return g, t, warnings


def test_root_ops_use_sudo_and_expect_version():
    g, t, _ = gadget(ok({"state": "started", "changed": True}))
    g.start()
    argv = t.runs[0][0]
    assert argv[:4] == ["sudo", "-n", "python3", "/var/lib/remote-gadget/bin/rgadget-helper"]
    assert argv[argv.index("--expect-version") + 1] == helper_version()
    assert argv[-5:] == ["start", "--vendor-id", "0x1d6b", "--product-id", "0x104"]


def test_nosudo_and_readonly_default():
    g, t, _ = gadget(ok(), ok(), ok(), nosudo=True, readonly=True)
    g.mount()
    g.insert(ro=False)
    assert t.runs[0][0][0] == "python3"
    assert t.runs[0][0][-2:] == ["mount", "--ro"]
    assert t.runs[1][0][-1] == "mount"


@pytest.mark.parametrize("force, tail", [(False, ["eject"]), (True, ["eject", "--force"])])
def test_stop_and_eject_args(force, tail):
    g, t, _ = gadget(ok(), ok())
    g.stop()
    g.eject(force=force)
    assert t.runs[0][0][-1] == "stop"
    assert t.runs[1][0][-len(tail) :] == tail


def test_state_does_not_use_sudo():
    g, t, _ = gadget(ok({"state": "stopped", "inserted": True, "ro": False, "size_mb": 64, "x": 1}))
    st = g.state()
    assert t.runs[0][0][0] == "python3"
    assert (st.state, st.inserted, st.ro, st.size_mb) == ("stopped", True, False, 64)


def test_state_uninitialised():
    g, _, _ = gadget(fail("not_initialised"))
    assert g.state().state == "uninitialised"


def test_version_mismatch_reuploads_and_retries():
    g, t, _ = gadget(fail("version_mismatch", rc=10), ok())
    g.stop()
    assert len(t.runs) == 2
    assert t.uploads[0][1] == "/var/lib/remote-gadget/bin/rgadget-helper"


def test_connection_failure():
    g, _, _ = gadget((255, b"ssh: connect to host pi port 22: Connection refused"))
    with pytest.raises(SshConnectionError, match="Connection refused"):
        g.stop()


def test_missing_helper_means_not_initialised():
    g, _, _ = gadget((2, b"python3: can't open file '/x': [Errno 2] No such file or directory"))
    with pytest.raises(NotInitialisedError):
        g.stop()


def test_sudo_password_needed():
    g, _, _ = gadget((1, b"sudo: a password is required"))
    with pytest.raises(RemoteError, match="password"):
        g.stop()


def test_error_mapping_and_warnings():
    rc, payload = fail("wrong_state", "cannot write while the target can see the media")
    payload["warnings"] = ["careful"]
    g, _, warnings = gadget((rc, payload))
    with pytest.raises(WrongStateError, match="can see the media"):
        g.stop()
    assert warnings == ["careful"]


def test_type_sends_text_on_stdin_and_reports_unsent():
    g, t, _ = gadget(fail("typing_timeout", sent=2, unsent="llo"), keymap="gb")
    with pytest.raises(TypingTimeoutError) as e:
        g.type("hello", timeout=3)
    assert e.value.unsent == "llo"
    argv, stdin = t.runs[0]
    assert stdin == b"hello"
    assert argv[argv.index("--keymap") + 1] == "gb"


def test_init_bootstraps_from_stdin_then_installs():
    g, t, _ = gadget(ok({"checks": [], "reboot_required": True}), ok())
    res = g.init(reboot=True, install_packages=True)
    argv, stdin = t.runs[0]
    assert argv[:4] == ["sudo", "-n", "python3", "-"]
    assert argv[-4:] == ["setup", "--size", "64", "--install-packages"]
    assert stdin and stdin.startswith(b"#!/usr/bin/env python3")
    assert t.uploads and res["rebooting"] is True
    assert t.runs[1][0][-2:] == ["systemctl", "reboot"]


def test_write_defaults_remote_name(tmp_path):
    f = tmp_path / "fw.bin"
    f.write_bytes(b"x")
    g, t, _ = gadget(ok({"dst": "/fw.bin", "bytes": 1}), ok())
    g.write(str(f))
    g.write(str(f), "/dir/")
    staged = t.uploads[0][1].rsplit("/", 1)[1]
    assert t.uploads[0][1].startswith("/var/lib/remote-gadget/staging/")
    assert t.runs[0][0][-3:] == ["img-put", staged, "/fw.bin"]
    assert t.runs[1][0][-1] == "/dir/fw.bin"


def test_write_missing_local(tmp_path):
    g, _, _ = gadget()
    with pytest.raises(LocalFileError):
        g.write(str(tmp_path / "nope"))


def test_read_refuses_overwrite(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "x.txt").write_text("keep")
    g, _, _ = gadget()
    with pytest.raises(LocalFileError, match="exists"):
        g.read("/x.txt")


def test_read_downloads_and_cleans_staging(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    g, t, _ = gadget(ok({"bytes": 4}), ok())
    res = g.read("/a/x.txt")
    assert (tmp_path / "x.txt").read_bytes() == b"data"
    assert res["local"] == "x.txt"
    assert t.runs[1][0][-2] == "staging-clean"
    assert not list(tmp_path.glob("*.part"))


def test_read_cleans_staging_on_failure(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    g, t, _ = gadget(fail("mtools"), ok())
    with pytest.raises(RemoteError):
        g.read("/x.txt")
    assert t.runs[1][0][-2] == "staging-clean"


def test_rm():
    g, t, _ = gadget(ok({"removed": "/d", "kind": "directory"}), ok())
    assert g.rm("/d", recursive=True)["kind"] == "directory"
    g.rm("/f")
    assert t.runs[0][0][-3:] == ["img-rm", "/d", "--recursive"]
    assert t.runs[1][0][-2:] == ["img-rm", "/f"]
    assert t.runs[0][0][0] == "python3"  # no sudo needed


def test_reset_passes_fswitch():
    g, t, _ = gadget(ok(), ok())
    g.reset()
    g.reset(fswitch=3)
    assert "--fswitch" not in t.runs[0][0]
    argv = t.runs[1][0]
    assert argv[argv.index("--fswitch") + 1] == "3"


@pytest.mark.parametrize("n", [0, 13])
def test_reset_rejects_bad_fswitch(n):
    g, t, _ = gadget()
    with pytest.raises(UsageError):
        g.reset(fswitch=n)
    assert t.runs == []

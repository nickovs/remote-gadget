"""CLI parsing, output and exit codes."""

from __future__ import annotations

import io
from typing import ClassVar

import pytest

from remote_gadget import cli
from remote_gadget.api import GadgetState
from remote_gadget.errors import TypingTimeoutError, WrongStateError
from remote_gadget.output import Reporter, use_colour


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "home").mkdir()
    monkeypatch.delenv("RGADGET_PROFILE", raising=False)
    monkeypatch.setattr(
        "remote_gadget.config.search_path",
        lambda: [tmp_path / "etc-rgadget", tmp_path / "home/.rgadget", tmp_path / ".rgadget"],
    )


class FakeGadget:
    calls: ClassVar[list] = []
    raise_on: ClassVar[dict] = {}

    def __init__(self, cfg, on_warning=None):
        self.cfg = cfg

    def __getattr__(self, name):
        def method(*args, **kwargs):
            FakeGadget.calls.append((name, args, kwargs))
            if name in FakeGadget.raise_on:
                raise FakeGadget.raise_on[name]
            return {
                "state": GadgetState("plugged", "udc0", "configured", True, 64, 2**20),
                "plug": {"ro": True, "changed": True},
                "unplug": {"changed": True},
                "rm": {"removed": "/d", "kind": "directory"},
                "type": {"sent": 3},
            }.get(name, {})

        return method


@pytest.fixture
def fake(monkeypatch):
    FakeGadget.calls = []
    FakeGadget.raise_on = {}
    monkeypatch.setattr(cli, "Gadget", FakeGadget)
    return FakeGadget


def test_no_host_is_config_error(capsys):
    assert cli.main(["state"]) == 2
    assert "no host" in capsys.readouterr().err


def test_global_options_before_or_after_command(fake):
    assert cli.main(["--host", "pi", "plug", "--ro"]) == 0
    assert cli.main(["plug", "--host", "pi", "--rw"]) == 0
    assert [c[2] for c in fake.calls] == [{"ro": True}, {"ro": False}]


def test_state_output(fake, capsys):
    assert cli.main(["--host", "pi", "state"]) == 0
    out = capsys.readouterr().out
    assert "pi: plugged read-only (udc udc0, target: configured)" in out
    assert "1.0 MiB free" in out


def test_quiet(fake, capsys):
    cli.main(["-q", "--host", "pi", "unplug"])
    assert capsys.readouterr().out == ""


def test_remote_error_exit_code(fake, capsys):
    fake.raise_on["write"] = WrongStateError("wrong_state", "cannot write while plugged", "unplug")
    assert cli.main(["--host", "pi", "write", "f"]) == 1
    err = capsys.readouterr().err
    assert "cannot write while plugged" in err and "hint: unplug" in err


def test_type_from_stdin_with_enter(fake, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO("abc"))
    assert cli.main(["--host", "pi", "type", "--enter"]) == 0
    assert fake.calls[0][1] == ("abc\n",)


def test_type_timeout_prints_unsent(fake, capsys):
    fake.raise_on["type"] = TypingTimeoutError("typing_timeout", "stopped", unsent="lo")
    assert cli.main(["--host", "pi", "type", "hello"]) == 1
    assert "not typed: 'lo'" in capsys.readouterr().err


def test_init_reboot_exit_code(fake, monkeypatch):
    monkeypatch.setattr(
        FakeGadget, "init", lambda self, **kw: {"checks": [], "reboot_required": True},
        raising=False,
    )  # fmt: skip
    assert cli.main(["--host", "pi", "init"]) == cli.EXIT_REBOOT


def test_reformat_needs_yes_when_not_interactive(fake, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    assert cli.main(["--host", "pi", "init", "--reformat"]) == 2
    assert "--yes" in capsys.readouterr().err


def test_doctor_fail_exit_and_unknown_note(fake, monkeypatch, capsys):
    checks = [
        {"name": "a", "status": "fail", "message": "bad", "hint": "fix it"},
        {"name": "b", "status": "unknown", "message": "?", "hint": None},
    ]
    monkeypatch.setattr(FakeGadget, "doctor", lambda self, fix: {"checks": checks}, raising=False)
    assert cli.main(["--host", "pi", "doctor"]) == 1
    assert "1 checks could not be verified" in capsys.readouterr().err


def test_colour_detection(monkeypatch):
    tty = io.StringIO()
    tty.isatty = lambda: True  # type: ignore[method-assign]
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm")
    assert use_colour(tty, "auto")
    monkeypatch.setenv("NO_COLOR", "1")
    assert not use_colour(tty, "auto")
    assert use_colour(io.StringIO(), "always")
    assert not use_colour(tty, "never")


def test_reporter_quiet_keeps_warnings():
    out, err = io.StringIO(), io.StringIO()
    r = Reporter(quiet=True, colour="never", out=out, err=err)
    r.info("i")
    r.success("s")
    r.warn("w")
    assert out.getvalue() == "" and err.getvalue() == "warning: w\n"


def test_rm(fake, capsys):
    assert cli.main(["--host", "pi", "rm", "-r", "/d"]) == 0
    assert fake.calls[0] == ("rm", ("/d",), {"recursive": True})
    assert "deleted directory /d" in capsys.readouterr().out


def test_config_command_all_options_after_command(tmp_path, capsys):
    argv = ["config", "-p", "lab", "--host", "pi.local", "--keymap", "gb", "--ro",
            "--nosudo", "--vendor-id", "0x1234"]  # fmt: skip
    assert cli.main(argv) == 0
    assert "saved profile [lab]" in capsys.readouterr().out
    assert (tmp_path / ".rgadget").read_text() == (
        "[lab]\nhost = pi.local\nnosudo = true\nkeymap = gb\nreadonly = true\nvendor_id = 0x1234\n"
    )
    assert cli.main(["config", "-p", "lab", "--host", "x"]) == 2
    assert "--update or --overwrite" in capsys.readouterr().err
    assert cli.main(["config", "-p", "lab", "--update", "--rw", "--unset", "keymap"]) == 0
    assert "readonly = false" in (tmp_path / ".rgadget").read_text()
    assert "keymap" not in (tmp_path / ".rgadget").read_text()


def test_config_needs_profile(capsys):
    assert cli.main(["config", "--host", "x"]) == 2
    assert "-p/--profile" in capsys.readouterr().err


def test_config_update_overwrite_exclusive(capsys):
    with pytest.raises(SystemExit):
        cli.main(["config", "-p", "a", "--update", "--overwrite"])

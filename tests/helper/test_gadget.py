"""Gadget lifecycle, state, image operations and the JSON protocol."""

from __future__ import annotations

import errno
import io
import os
import shutil
import sys

import pytest
from conftest import UDC, FifoReader, completed


def test_not_initialised(run):
    code, out = run("state")
    assert code == 1
    assert out["error"]["code"] == "not_initialised"


def test_version_mismatch(helper, host, capsys):
    code = helper.main(["--expect-version", "nope", *host.args("state")])
    assert code == helper.EXIT_VERSION
    assert '"version_mismatch"' in capsys.readouterr().out


def test_version_mismatch_wins_over_unknown_options(helper, host, capsys):
    # A newer client may pass options this helper does not know yet.
    code = helper.main(["--expect-version", "nope", *host.args("state", "--no-such-option")])
    assert code == helper.EXIT_VERSION
    assert '"version_mismatch"' in capsys.readouterr().out


LUN = "functions/mass_storage.usb0/lun.0"


def test_start_builds_gadget_ejected_and_binds(run, host, fake_cmd):
    host.initialise()
    code, out = run("start")
    assert (code, out["result"]) == (
        0, {"state": "started", "changed": True, "media": {"inserted": False, "ro": False}}
    )  # fmt: skip
    assert host.attr("UDC") == UDC
    assert host.attr("idVendor") == "0x1d6b"
    assert not (host.gadget / LUN / "file").exists()  # a fresh install starts ejected
    assert host.attr(f"{LUN}/removable") == "1"
    assert (host.gadget / "functions/hid.usb0/report_desc").read_bytes()[:2] == b"\x05\x01"
    assert os.path.islink(host.gadget / "configs/c.1/hid.usb0")
    assert host.state()["udc"] == UDC


def test_start_adds_serial_port_to_an_existing_gadget(run, host, fake_cmd, helper):
    """A gadget built before the serial port existed gains it on the next start."""
    host.initialise()
    run("start")
    run("stop")
    (host.gadget / "configs/c.1/acm.usb0").unlink()
    (host.gadget / "functions/acm.usb0").rmdir()
    code, _ = run("start")
    assert code == 0
    assert (host.gadget / "configs/c.1/acm.usb0").resolve() == host.gadget / "functions/acm.usb0"
    assert [host.attr(a) for a in ("bDeviceClass", "bDeviceSubClass", "bDeviceProtocol")] == [
        "0xef", "0x02", "0x01"
    ]  # fmt: skip


def test_start_is_idempotent(run, host, fake_cmd):
    host.initialise()
    run("start")
    code, out = run("start")
    assert code == 0 and out["result"]["changed"] is False


def test_custom_ids(run, host, fake_cmd):
    host.initialise()
    run("start", "--vendor-id", "0x1234", "--product-id", "0x5678")
    assert (host.attr("idVendor"), host.attr("idProduct")) == ("0x1234", "0x5678")


def test_stop_unbinds_and_records_time(run, host, fake_cmd):
    host.initialise()
    run("start")
    code, out = run("stop")
    assert (code, out["result"]["changed"], out["warnings"]) == (0, True, [])
    assert host.attr("UDC") == ""
    assert host.state()["stopped_at"] > 0
    code, out = run("stop")
    assert out["result"]["changed"] is False


def test_stop_warns_if_udc_still_attached(run, host, fake_cmd):
    host.initialise()
    run("start")
    host.set_udc_state("configured")
    code, out = run("stop")
    assert code == 0
    assert "may still see the device" in out["warnings"][0]


def test_stop_of_never_built_gadget_is_a_no_op(run, host, fake_cmd):
    host.initialise()
    code, out = run("stop")
    assert (code, out["result"]) == (0, {"state": "stopped", "changed": False})
    assert not host.gadget.exists()


def test_start_waits_out_minimum_stop_time(run, host, fake_cmd, helper, monkeypatch):
    host.initialise(stopped_at=helper.time.time())
    slept = []
    monkeypatch.setattr(helper.time, "sleep", slept.append)
    run("start")
    assert slept and 0 < slept[0] <= helper.MIN_STOP_SECONDS


@pytest.mark.parametrize("ro", [False, True])
def test_mount_while_started_sets_ro_then_file(run, host, fake_cmd, helper, monkeypatch, ro):
    host.initialise()
    run("start")
    writes = []
    real = helper.write_attr
    monkeypatch.setattr(helper, "write_attr", lambda p, v: (writes.append((p, v)), real(p, v)))
    code, out = run("mount", *(["--ro"] if ro else []))
    assert (code, out["result"]) == (0, {"media": {"inserted": True, "ro": ro}, "changed": True})
    lun_writes = [(os.path.basename(p), v) for p, v in writes if p.startswith(str(host.gadget))]
    assert lun_writes == [("ro", int(ro)), ("file", str(host.image))]
    assert host.state()["media"] == {"inserted": True, "ro": ro}
    code, out = run("mount", *(["--ro"] if ro else []))
    assert (code, out["result"]["changed"]) == (0, False)


def test_mount_in_other_mode_needs_eject(run, host, fake_cmd):
    host.initialise()
    run("start")
    run("mount")
    code, out = run("mount", "--ro")
    assert (code, out["error"]["code"]) == (1, "wrong_state")
    assert "eject" in out["error"]["hint"]


def test_mount_needs_an_image(run, host, fake_cmd):
    host.initialise()
    host.image.unlink()
    code, out = run("mount")
    assert (code, out["error"]["code"]) == (1, "no_image")


def test_media_state_survives_rebuild(run, host, fake_cmd):
    """Media set while the gadget does not exist (e.g. after a reboot) is applied by start."""
    host.initialise()
    code, _ = run("mount", "--ro")
    assert code == 0 and not host.gadget.exists()
    run("start")
    assert host.attr(f"{LUN}/file") == str(host.image)
    assert host.attr(f"{LUN}/ro") == "1"
    run("stop")
    shutil.rmtree(host.gadget)  # what a reboot does to configfs
    run("start")
    assert (host.attr(f"{LUN}/file"), host.attr(f"{LUN}/ro")) == (str(host.image), "1")


def test_eject_clears_lun_and_state(run, host, fake_cmd):
    host.initialise()
    run("start")
    run("mount")
    code, out = run("eject")
    assert (code, out["result"]) == (
        0,
        {"media": {"inserted": False, "ro": False}, "changed": True},
    )
    assert host.attr(f"{LUN}/file") == ""
    assert host.state()["media"]["inserted"] is False
    code, out = run("eject")
    assert out["result"]["changed"] is False


def test_eject_reports_locked_media(run, host, fake_cmd, helper, monkeypatch):
    host.initialise()
    run("start")
    run("mount")
    lun_file = str(host.gadget / LUN / "file")

    def locked_open(path, mode="r", *a, **kw):
        if path == lun_file and "w" in mode:
            raise OSError(errno.EBUSY, "Device or resource busy")
        return open(path, mode, *a, **kw)

    monkeypatch.setattr(helper, "open", locked_open, raising=False)
    code, out = run("eject")
    assert (code, out["error"]["code"]) == (1, "media_locked")
    assert "--force" in out["error"]["hint"]
    assert host.state()["media"]["inserted"] is True


def test_eject_force_uses_forced_eject(run, host, fake_cmd, helper, monkeypatch):
    host.initialise()
    run("start")
    run("mount")
    real = helper.write_attr

    def kernel(path, value):
        real(path, value)
        if path.endswith("forced_eject"):  # what the kernel does
            real(os.path.join(os.path.dirname(path), "file"), "")

    monkeypatch.setattr(helper, "write_attr", kernel)
    code, out = run("eject", "--force")
    assert code == 0, out
    assert host.attr(f"{LUN}/forced_eject") == "1"
    assert host.attr(f"{LUN}/file") == ""


def test_target_initiated_eject_is_seen_and_saved(run, host, fake_cmd):
    host.initialise()
    run("start")
    run("mount")
    (host.gadget / LUN / "file").write_text("\n")  # the target ejected the media
    _code, out = run("state")
    assert out["result"]["inserted"] is False
    assert host.state()["media"]["inserted"] is False


def test_state(run, host, fake_cmd):
    host.initialise()
    fake_cmd.reply = lambda cmd: completed("      1 234 567 bytes free\n")
    _, out = run("state")
    assert out["result"]["state"] == "stopped"
    assert (out["result"]["inserted"], out["result"]["ro"]) == (False, None)
    assert out["result"]["free_bytes"] == 1234567
    run("start")
    run("mount", "--ro")
    host.set_udc_state("configured")
    _code, out = run("state")
    assert out["result"]["state"] == "started"
    assert (out["result"]["udc"], out["result"]["udc_state"]) == (UDC, "configured")
    assert (out["result"]["inserted"], out["result"]["ro"]) == (True, True)


@pytest.mark.parametrize(
    "ops, refused",
    [((), False), (("start",), False), (("mount",), False), (("start", "mount"), True),
     (("start", "mount", "eject"), False), (("start", "mount", "stop"), False)],
)  # fmt: skip
def test_img_put_refused_only_while_target_sees_media(run, host, fake_cmd, ops, refused):
    host.initialise()
    for op in ops:
        run(op)
    staged = host.path / "staging/abc"
    staged.write_bytes(b"data")
    code, out = run("img-put", "abc", "/x.txt")
    assert (code == 1 and out["error"]["code"] == "wrong_state") is refused
    assert not staged.exists()


def test_img_put_creates_parent_dirs(run, host, fake_cmd):
    host.initialise()
    (host.path / "staging/abc").write_bytes(b"data")
    fake_cmd.reply = lambda cmd: completed(returncode=1 if cmd[0] == "mdir" else 0)
    code, out = run("img-put", "abc", "/a/b/x.txt")
    assert code == 0
    assert out["result"] == {"dst": "/a/b/x.txt", "bytes": 4}
    cmds = [c[0] + " " + c[-1] for c in fake_cmd]
    assert cmds == ["mdir ::/a", "mmd ::/a", "mdir ::/a/b", "mmd ::/a/b", "mcopy ::/a/b/x.txt"]


@pytest.mark.parametrize("bad", ["../x", "/a/../../b", "a\\b"])
def test_image_path_rejects_traversal(helper, bad):
    with pytest.raises(helper.HelperError):
        helper.image_path(bad)


@pytest.mark.parametrize("bad", ["../x", "", "-rf", "a/b"])
def test_staging_names_validated(helper, host, bad):
    with pytest.raises(helper.HelperError):
        helper.staging_file(helper.Ctx(str(host.path)), bad)


def test_img_get_warns_when_inserted_rw(run, host, fake_cmd):
    host.initialise()
    run("start")
    run("mount")

    def reply(cmd):
        (host.path / "staging/out1").write_bytes(b"abc")
        return completed()

    fake_cmd.reply = reply
    code, out = run("img-get", "/x.txt", "out1")
    assert code == 0 and out["result"]["bytes"] == 3
    assert "unflushed" in out["warnings"][0]


def _type(run, host, monkeypatch, text, *extra):
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(text.encode())))
    return run("type", *extra)


def test_type_requires_configured_target(run, host, fake_cmd, monkeypatch):
    host.initialise()
    run("start")
    _code, out = _type(run, host, monkeypatch, "hi")
    assert out["error"]["code"] == "not_configured"


def test_type_sends_text(run, host, fake_cmd, monkeypatch):
    host.initialise()
    run("start")  # the keyboard works with the media ejected
    host.set_udc_state("configured")
    reader = FifoReader(host.make_hid())
    code, out = _type(run, host, monkeypatch, "Hi\n")
    assert (code, out["result"]) == (0, {"sent": 3})
    assert len(reader.close(3 * 2 * 8)) == 3 * 2 * 8


def test_type_timeout_reports_unsent(run, host, fake_cmd, monkeypatch):
    host.initialise()
    run("start")
    host.set_udc_state("configured")
    reader = FifoReader(host.make_hid(), read=False)
    try:
        text = "x" * 50000
        _code, out = _type(run, host, monkeypatch, text, "--timeout", "0.2")
    finally:
        reader.close()
    err = out["error"]
    assert err["code"] == "typing_timeout"
    assert 0 < err["sent"] < len(text)
    assert err["unsent"] == text[err["sent"] :]


def _mtools_fs(files: set, dirs: dict):
    """A fake mdel/mrd/mdeltree over a tiny in-memory tree, replicating mtools' exit codes."""

    def reply(cmd):
        tool, path = cmd[0], cmd[-1][2:]
        if tool == "mdel" and path in files:
            files.discard(path)
            return completed()
        if tool == "mrd" and path in dirs:
            if dirs[path]:
                return completed(stderr=f"Directory ::{path} non empty", returncode=1)
            del dirs[path]
            return completed()
        if tool == "mdeltree" and path in dirs:
            del dirs[path]
            return completed()
        return completed(stderr=f'{tool}: File "::{path}" not found', returncode=1)

    return reply


@pytest.mark.parametrize(
    "target, extra, kind, files_left, dirs_left",
    [
        ("/f.txt", (), "file", set(), {"/full": ["x"], "/empty": []}),
        ("/empty", (), "directory", {"/f.txt"}, {"/full": ["x"]}),
        ("/full", ("--recursive",), "directory", {"/f.txt"}, {"/empty": []}),
    ],
)
def test_img_rm(run, host, fake_cmd, target, extra, kind, files_left, dirs_left):
    host.initialise()
    files, dirs = {"/f.txt"}, {"/full": ["x"], "/empty": []}
    fake_cmd.reply = _mtools_fs(files, dirs)
    code, out = run("img-rm", target, *extra)
    assert (code, out["result"]) == (0, {"removed": target, "kind": kind})
    assert (files, dirs) == (files_left, dirs_left)


@pytest.mark.parametrize("target, code", [("/full", "not_empty"), ("/nope", "not_found")])
def test_img_rm_errors(run, host, fake_cmd, target, code):
    host.initialise()
    fake_cmd.reply = _mtools_fs({"/f.txt"}, {"/full": ["x"]})
    _, out = run("img-rm", target)
    assert out["error"]["code"] == code


def test_img_rm_refused_while_target_sees_media_or_root(run, host, fake_cmd):
    host.initialise()
    _, out = run("img-rm", "/")
    assert out["error"]["code"] == "bad_path"
    run("start")
    run("mount")
    fake_cmd.clear()
    _, out = run("img-rm", "/f.txt")
    assert out["error"]["code"] == "wrong_state"
    assert not [c for c in fake_cmd if c[0].startswith("m")]


CTRL_ALT = 0x01 | 0x04


@pytest.mark.parametrize(
    "extra, usages, pauses",
    [
        ((), [0x4C], 0),
        (("--fswitch", "3"), [0x3C, 0x4C], 1),
        (("--fswitch", "12"), [0x45, 0x4C], 1),
    ],
)
def test_reset_sends_keys(run, host, fake_cmd, helper, monkeypatch, extra, usages, pauses):
    host.initialise()
    run("start")
    host.set_udc_state("configured")
    reader = FifoReader(host.make_hid())
    slept = []
    monkeypatch.setattr(helper.time, "sleep", slept.append)
    code, out = run("reset", *extra)
    assert code == 0, out
    expected = b"".join(bytes([CTRL_ALT, 0, u, 0, 0, 0, 0, 0]) + bytes(8) for u in usages)
    assert reader.close(len(expected)) == expected
    assert slept.count(helper.VT_SWITCH_SECONDS) == pauses


def test_reset_rejects_bad_fswitch(helper, host):
    with pytest.raises(SystemExit):
        helper.main(host.args("reset", "--fswitch", "13"))

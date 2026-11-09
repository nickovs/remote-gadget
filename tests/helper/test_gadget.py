"""Gadget lifecycle, state, image operations and the JSON protocol."""

from __future__ import annotations

import io
import os
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


def test_plug_builds_gadget_and_binds(run, host, fake_cmd):
    host.initialise()
    code, out = run("plug")
    assert (code, out["result"]) == (0, {"state": "plugged", "ro": False, "changed": True})
    assert host.attr("UDC") == UDC
    assert host.attr("idVendor") == "0x1d6b"
    assert host.attr("functions/mass_storage.usb0/lun.0/file") == str(host.path / "image.img")
    assert (host.gadget / "functions/hid.usb0/report_desc").read_bytes()[:2] == b"\x05\x01"
    assert os.path.islink(host.gadget / "configs/c.1/hid.usb0")
    assert host.state()["udc"] == UDC


def test_plug_is_idempotent(run, host, fake_cmd):
    host.initialise()
    run("plug")
    code, out = run("plug")
    assert code == 0 and out["result"]["changed"] is False


def test_plug_with_other_mode_while_plugged_fails(run, host, fake_cmd):
    host.initialise()
    run("plug")
    code, out = run("plug", "--ro")
    assert code == 1
    assert out["error"]["code"] == "wrong_state"


def test_custom_ids(run, host, fake_cmd):
    host.initialise()
    run("plug", "--vendor-id", "0x1234", "--product-id", "0x5678")
    assert (host.attr("idVendor"), host.attr("idProduct")) == ("0x1234", "0x5678")


def test_unplug_unbinds_and_records_time(run, host, fake_cmd):
    host.initialise()
    run("plug")
    code, out = run("unplug")
    assert (code, out["result"]["changed"], out["warnings"]) == (0, True, [])
    assert host.attr("UDC") == ""
    assert host.state()["unplugged_at"] > 0
    code, out = run("unplug")
    assert out["result"]["changed"] is False


def test_unplug_warns_if_udc_still_attached(run, host, fake_cmd):
    host.initialise()
    run("plug")
    host.set_udc_state("configured")
    code, out = run("unplug")
    assert code == 0
    assert "may still see the device" in out["warnings"][0]


def test_unplug_starts_a_stopped_gadget(run, host, fake_cmd):
    host.initialise()
    code, out = run("unplug")
    assert (code, out["result"]["state"]) == (0, "unplugged")
    assert host.gadget.is_dir()


def test_read_only_plug_cycles_lun_file(run, host, fake_cmd, helper, monkeypatch):
    host.initialise()
    run("plug")
    run("unplug")
    writes = []
    real = helper.write_attr
    monkeypatch.setattr(helper, "write_attr", lambda p, v: (writes.append((p, v)), real(p, v)))
    monkeypatch.setattr(helper.time, "sleep", lambda s: None)
    _code, out = run("plug", "--ro")
    assert out["result"]["ro"] is True
    lun = str(host.gadget / "functions/mass_storage.usb0/lun.0")
    lun_writes = [(os.path.basename(p), v) for p, v in writes if p.startswith(lun)]
    assert lun_writes == [("file", ""), ("ro", 1), ("file", str(host.path / "image.img"))]


def test_plug_waits_out_minimum_unplug_time(run, host, fake_cmd, helper, monkeypatch):
    host.initialise(unplugged_at=helper.time.time())
    slept = []
    monkeypatch.setattr(helper.time, "sleep", slept.append)
    run("plug")
    assert slept and 0 < slept[0] <= helper.MIN_UNPLUG_SECONDS


def test_state(run, host, fake_cmd):
    host.initialise()
    fake_cmd.reply = lambda cmd: completed("      1 234 567 bytes free\n")
    _, out = run("state")
    assert out["result"]["state"] == "stopped"
    assert out["result"]["free_bytes"] == 1234567
    run("plug")
    host.set_udc_state("configured")
    _code, out = run("state")
    assert out["result"]["state"] == "plugged"
    assert (out["result"]["udc"], out["result"]["udc_state"]) == (UDC, "configured")


def test_img_put_refused_while_plugged_and_cleans_staging(run, host, fake_cmd):
    host.initialise()
    run("plug")
    staged = host.path / "staging/abc"
    staged.write_bytes(b"data")
    _code, out = run("img-put", "abc", "/x.txt")
    assert out["error"]["code"] == "wrong_state"
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


def test_img_get_warns_when_plugged_rw(run, host, fake_cmd):
    host.initialise()
    run("plug")

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
    run("plug")
    _code, out = _type(run, host, monkeypatch, "hi")
    assert out["error"]["code"] == "not_configured"


def test_type_sends_text(run, host, fake_cmd, monkeypatch):
    host.initialise()
    run("plug")
    host.set_udc_state("configured")
    reader = FifoReader(host.make_hid())
    code, out = _type(run, host, monkeypatch, "Hi\n")
    assert (code, out["result"]) == (0, {"sent": 3})
    assert len(reader.close(3 * 2 * 8)) == 3 * 2 * 8


def test_type_timeout_reports_unsent(run, host, fake_cmd, monkeypatch):
    host.initialise()
    run("plug")
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


def test_img_rm_refused_while_plugged_or_root(run, host, fake_cmd):
    host.initialise()
    _, out = run("img-rm", "/")
    assert out["error"]["code"] == "bad_path"
    run("plug")
    fake_cmd.clear()
    _, out = run("img-rm", "/f.txt")
    assert out["error"]["code"] == "wrong_state"
    assert not [c for c in fake_cmd if c[0].startswith("m")]

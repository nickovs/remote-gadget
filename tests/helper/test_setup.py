"""Doctor/setup checks and image creation."""

from __future__ import annotations

import shutil

import pytest
from conftest import completed

STOCK_CONFIG = """\
dtparam=audio=on
[cm4]
otg_mode=1
[cm5]
dtoverlay=dwc2,dr_mode=host
[all]
"""


@pytest.mark.parametrize(
    "text, enabled",
    [
        (STOCK_CONFIG, False),
        (STOCK_CONFIG + "dtoverlay=dwc2,dr_mode=peripheral\n", True),
        (STOCK_CONFIG + "dtoverlay=dwc2\n", True),
        (STOCK_CONFIG + "# dtoverlay=dwc2\n", False),
        ("dtoverlay=dwc2\n[pi4]\n", True),
        (STOCK_CONFIG + "dtoverlay=dwc2\n[pi5]\ndtoverlay=dwc2,dr_mode=host\n", True),
    ],
)
def test_overlay_enabled(helper, text, enabled):
    assert helper.overlay_enabled(text) is enabled


@pytest.fixture
def pi(host):
    (host.root / "proc/device-tree").mkdir(parents=True)
    (host.root / "proc/device-tree/model").write_text("Raspberry Pi Zero 2 W Rev 1.0\x00")
    (host.root / "boot/firmware").mkdir(parents=True)
    (host.root / "boot/firmware/config.txt").write_text(STOCK_CONFIG)
    return host


def test_setup_enables_overlay_and_creates_image(run, pi, fake_cmd, helper, monkeypatch):
    monkeypatch.setattr(helper, "which", lambda name: "/usr/bin/" + name)
    shutil.rmtree(pi.udc_dir)  # no UDC until the overlay is active
    code, out = run("setup", "--size", "16")
    assert code == 0, out
    res = out["result"]
    assert res["reboot_required"] is True
    udc = next(c for c in res["checks"] if c["name"] == "udc")
    assert udc["status"] == "warn"  # expected until the reboot, so not an error
    assert helper.OVERLAY_LINE in (pi.root / "boot/firmware/config.txt").read_text()
    assert list((pi.root / "boot/firmware").glob("config.txt.rgadget-*"))
    assert (pi.path / "image.img").stat().st_size == 16 << 20
    assert any(c[0] == "mkfs.vfat" for c in fake_cmd)
    assert pi.state()["size"] == 16
    # Second run changes nothing.
    code, out = run("setup", "--size", "16")
    assert [c["name"] for c in out["result"]["checks"] if c["fixed"]] == []


def test_setup_keeps_image_of_different_size(run, pi, fake_cmd, helper, monkeypatch):
    monkeypatch.setattr(helper, "which", lambda name: "/usr/bin/" + name)
    run("setup", "--size", "16")
    _code, out = run("setup", "--size", "32")
    image = out["result"]["checks"][-1]
    assert image["status"] == "warn" and "kept" in image["message"]
    assert (pi.path / "image.img").stat().st_size == 16 << 20


def test_setup_without_tools_fails_with_hint(run, pi, fake_cmd, helper, monkeypatch):
    monkeypatch.setattr(helper, "which", lambda name: None)
    _code, out = run("setup", "--size", "16")
    assert out["error"]["code"] == "setup_failed"
    tools = next(c for c in out["error"]["checks"] if c["name"] == "tools")
    assert "--install-packages" in tools["hint"]
    assert not any(c[0] == "apt-get" for c in fake_cmd)


def test_install_packages_waits_for_apt_lock(run, pi, fake_cmd, helper, monkeypatch):
    installed = []
    monkeypatch.setattr(helper, "which", lambda name: "/usr/bin/x" if installed else None)

    def reply(cmd):
        if "install" in cmd:
            installed.append(cmd)
        return completed()

    fake_cmd.reply = reply
    code, out = run("setup", "--size", "16", "--install-packages")
    assert code == 0, out
    assert "DPkg::Lock::Timeout=600" in installed[0]


def test_doctor_marks_non_pi_unknown(run, host, fake_cmd):
    (host.root / "etc/os-release").write_text('ID=fedora\nPRETTY_NAME="Fedora"\n')
    _code, out = run("doctor")
    checks = {c["name"]: c for c in out["result"]["checks"]}
    assert checks["boot-overlay"]["status"] == "unknown"
    assert checks["platform"]["status"] == "warn"
    assert out["result"]["debian"] is False

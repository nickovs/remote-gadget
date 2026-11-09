"""Keymaps, translation and HID report sending."""

from __future__ import annotations

import os

import pytest
from conftest import FIXTURES, FifoReader

SHIFT, RALT = 0x02, 0x40


def keymap(helper, name):
    return helper.parse_console_keymap((FIXTURES / "ckbcomp" / f"{name}.map").read_text())


def test_ckbcomp_us_agrees_with_builtin(helper):
    parsed = keymap(helper, "us")
    for ch, entry in helper.builtin_us().items():
        assert parsed[ch] == entry, ch


@pytest.mark.parametrize(
    "layout, ch, expected",
    [
        ("de", "z", (0, 0x1C)),  # QWERTZ: z is where US y is
        ("de", "@", (RALT, 0x14)),  # AltGr+q
        ("de", "ß", (0, 0x2D)),
        ("de", "<", (0, 0x64)),  # only on the ISO 102nd key
        ("gb", '"', (SHIFT, 0x1F)),  # Shift+2
        ("gb", "£", (SHIFT, 0x20)),  # Shift+3
        ("gb", "#", (0, 0x31)),
        ("gb", "\\", (0, 0x64)),
        ("fr", "a", (0, 0x14)),  # AZERTY
        ("fr", "1", (SHIFT, 0x1E)),
    ],
)
def test_layouts(helper, layout, ch, expected):
    assert keymap(helper, layout)[ch] == expected


def test_us_prefers_main_keys_over_102nd(helper):
    # us.map also puts "<" on keycode 86 unshifted; Shift+comma is the real US key.
    assert keymap(helper, "us")["<"] == (SHIFT, 0x36)


def test_translate_fails_before_sending(helper):
    with pytest.raises(helper.HelperError) as e:
        helper.translate("ok é", helper.builtin_us())
    assert e.value.code == "unmappable"
    assert "position 3" in e.value.message


def test_translate_skips_cr(helper):
    keys, positions = helper.translate("a\r\nb", helper.builtin_us())
    assert keys == [(0, 0x04), (0, 0x28), (0, 0x05)]
    assert positions == [0, 2, 3]


def test_bad_keymap_name(helper):
    with pytest.raises(helper.HelperError):
        helper.load_keymap("us; rm -rf /")


def test_send_keys_reports(helper, tmp_path):
    fifo = tmp_path / "hidg0"
    os.mkfifo(fifo)
    reader = FifoReader(fifo)
    keys = [(SHIFT, 0x04), (0, 0x04)]
    assert helper.send_keys(str(fifo), keys, timeout=2, delay=0) == 2
    release = bytes(8)
    expected = bytes([SHIFT, 0, 4, 0, 0, 0, 0, 0]) + release + bytes([0, 0, 4]) + bytes(5) + release
    assert reader.close(len(expected)) == expected


def test_send_keys_times_out(helper, tmp_path):
    fifo = tmp_path / "hidg0"
    os.mkfifo(fifo)
    reader = FifoReader(fifo, read=False)  # a reader that never reads
    try:
        keys = [(0, 0x04)] * 50000  # far more than a pipe buffer holds
        sent = helper.send_keys(str(fifo), keys, timeout=0.2, delay=0)
        assert 0 < sent < len(keys)
    finally:
        reader.close()


def test_keymap_cache(helper, tmp_path, monkeypatch):
    xkb = tmp_path / "xkb"
    xkb.mkdir()
    os.utime(xkb, (1, 1))
    monkeypatch.setattr(helper, "XKB_SYMBOLS", str(xkb))
    calls = []

    def fake(name):
        calls.append(name)
        return {"z": (0, 0x1C)}

    monkeypatch.setattr(helper, "_ckbcomp_keymap", fake)
    cache = tmp_path / "keymaps"
    assert helper.load_keymap("de:nodeadkeys", str(cache)) == {"z": (0, 0x1C)}
    assert helper.load_keymap("de:nodeadkeys", str(cache)) == {"z": (0, 0x1C)}
    assert calls == ["de:nodeadkeys"]
    assert (cache / "de_nodeadkeys.json").exists()
    os.utime(xkb)  # XKB data updated -> cache is stale
    helper.load_keymap("de:nodeadkeys", str(cache))
    assert len(calls) == 2

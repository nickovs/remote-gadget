"""The comment-preserving INI section editor."""

from __future__ import annotations

import configparser

from remote_gadget.inifile import set_section

ORIGINAL = """\
# my gadgets
[default]
profile = lab

[lab]
; the Pi on my desk
host = old.local   ; required
size = 64
keymap = gb
  continued

[other]
host = o
"""


def parse(text):
    p = configparser.ConfigParser(interpolation=None, inline_comment_prefixes=(";", "#"))
    p.read_string(text)
    return {s: dict(p.items(s)) for s in p.sections()}


def test_update_keeps_comments_and_other_sections():
    out = set_section(ORIGINAL, "lab", {"host": "new.local", "size": "64", "username": "pi"})
    assert (
        out
        == """\
# my gadgets
[default]
profile = lab

[lab]
; the Pi on my desk
host = new.local   ; required
size = 64
username = pi

[other]
host = o
"""
    )
    assert parse(out)["lab"] == {"host": "new.local", "size": "64", "username": "pi"}


def test_unchanged_values_are_byte_identical():
    values = parse(ORIGINAL)["lab"]
    assert set_section(ORIGINAL, "lab", values) == ORIGINAL


def test_new_section_appended():
    out = set_section("[a]\nhost = a", "b", {"host": "b"})
    assert out == "[a]\nhost = a\n\n[b]\nhost = b\n"
    assert set_section("", "b", {"host": "b"}) == "[b]\nhost = b\n"


def test_last_section_and_trailing_blank_lines():
    out = set_section("[a]\nhost = a\n\n\n", "a", {"host": "a", "size": "8"})
    assert out == "[a]\nhost = a\nsize = 8\n\n\n"


def test_section_name_is_exact():
    out = set_section("[lab2]\nhost = x\n", "lab", {"host": "y"})
    assert parse(out) == {"lab2": {"host": "x"}, "lab": {"host": "y"}}


def test_multiline_values():
    out = set_section(ORIGINAL, "lab", {"host": "old.local", "keymap": "de\nx"})
    assert "keymap = de\n  x\n" in out
    assert parse(out)["lab"]["keymap"] == "de\nx"
    out = set_section("[a]\n", "a", {"k": "1\n2"})
    assert parse(out)["a"]["k"] == "1\n2"

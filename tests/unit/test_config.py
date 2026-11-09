"""Config loading, merging and validation."""

from __future__ import annotations

import pytest

from remote_gadget.config import Config
from remote_gadget.errors import ConfigError


def write(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text)
    return p


@pytest.fixture(autouse=True)
def no_env_profile(monkeypatch):
    monkeypatch.delenv("RGADGET_PROFILE", raising=False)


def test_nearer_file_wins_per_key(tmp_path):
    far = write(tmp_path, "etc", "[pi]\nhost = far\nsize = 32\n")
    near = write(tmp_path, "cwd", "[pi]\nhost = near\n")
    cfg = Config.load(files=[far, near])
    assert (cfg.host, cfg.size, cfg.profile) == ("near", 32, "pi")


def test_missing_files_skipped(tmp_path):
    cfg = Config.load(files=[tmp_path / "nope"], overrides={"host": "h"})
    assert cfg.host == "h" and cfg.profile is None


def test_default_section_selects_profile(tmp_path):
    f = write(tmp_path, "c", "[default]\nprofile = b\n[a]\nhost = a\n[b]\nhost = b\n")
    assert Config.load(files=[f]).host == "b"
    assert Config.load(profile="a", files=[f]).host == "a"


def test_env_profile(tmp_path, monkeypatch):
    f = write(tmp_path, "c", "[a]\nhost = a\n[b]\nhost = b\n")
    monkeypatch.setenv("RGADGET_PROFILE", "b")
    assert Config.load(files=[f]).host == "b"


def test_ambiguous_profile(tmp_path):
    f = write(tmp_path, "c", "[a]\nhost = a\n[b]\nhost = b\n")
    with pytest.raises(ConfigError, match="several profiles"):
        Config.load(files=[f])


def test_unknown_profile(tmp_path):
    f = write(tmp_path, "c", "[a]\nhost = a\n")
    with pytest.raises(ConfigError, match=r"no profile \[x\]"):
        Config.load(profile="x", files=[f])


def test_default_section_only_profile(tmp_path):
    f = write(tmp_path, "c", "[default]\nprofile = a\nhost = x\n[a]\nhost = a\n")
    with pytest.raises(ConfigError, match="only contain"):
        Config.load(files=[f])


def test_unknown_key_warns(tmp_path):
    f = write(tmp_path, "c", "[a]\nhost = a\ncolour = red\n")
    with pytest.warns(UserWarning, match="colour"):
        Config.load(files=[f])


def test_types_parsed(tmp_path):
    f = write(
        tmp_path, "c",
        "[a]\nhost = a\nnosudo = yes\nreadonly = on\nsize = 128\nvendor_id = 0x1234\n",
    )  # fmt: skip
    cfg = Config.load(files=[f])
    assert (cfg.nosudo, cfg.readonly, cfg.size, cfg.vendor_id) == (True, True, 128, 0x1234)


def test_overrides_ignore_none(tmp_path):
    f = write(tmp_path, "c", "[a]\nhost = a\nkeymap = gb\n")
    cfg = Config.load(files=[f], overrides={"keymap": None, "size": 16})
    assert (cfg.keymap, cfg.size) == ("gb", 16)


def test_no_host(tmp_path):
    with pytest.raises(ConfigError, match="no host"):
        Config.load(files=[])


@pytest.mark.parametrize(
    "field, value",
    [
        ("host", "-oProxyCommand=x"),
        ("host", "a b"),
        ("username", "root;rm"),
        ("path", "relative"),
        ("path", "/a/../b"),
        ("path", "/a b"),
        ("size", 4),
        ("keymap", "us;x"),
        ("vendor_id", 0x10000),
        ("key", "/no/such/key"),
    ],
)
def test_validation(field, value):
    with pytest.raises(ConfigError):
        Config(**{"host": "h", field: value})


def test_bad_bool(tmp_path):
    f = write(tmp_path, "c", "[a]\nhost = a\nnosudo = maybe\n")
    with pytest.raises(ConfigError, match="boolean"):
        Config.load(files=[f])


def test_unparseable_file(tmp_path):
    f = write(tmp_path, "c", "host = a\n")
    with pytest.raises(ConfigError, match="bad config"):
        Config.load(files=[f])

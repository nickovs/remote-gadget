"""Config loading, merging and validation."""

from __future__ import annotations

import pytest

from remote_gadget.config import Config, save_profile
from remote_gadget.errors import ConfigError


def write(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text)
    return p


@pytest.fixture(autouse=True)
def no_env_profile(monkeypatch):
    monkeypatch.delenv("RGADGET_PROFILE", raising=False)


def test_nearest_file_supplies_whole_profile(tmp_path):
    far = write(tmp_path, "etc", "[pi]\nhost = far\nsize = 32\n[other]\nhost = o\n")
    near = write(tmp_path, "cwd", "[pi]\nhost = near\n")
    cfg = Config.load(profile="pi", files=[far, near])
    assert (cfg.host, cfg.size, cfg.profile) == ("near", 64, "pi")  # size not inherited
    assert Config.load(profile="other", files=[far, near]).host == "o"


def test_nearest_default_wins(tmp_path):
    far = write(tmp_path, "etc", "[default]\nprofile = a\n[a]\nhost = a\n")
    near = write(tmp_path, "cwd", "[default]\nprofile = b\n[b]\nhost = b\n")
    assert Config.load(files=[far, near]).host == "b"


def test_inline_comments(tmp_path):
    f = write(tmp_path, "c", "[a]\nhost = rgadget.local   ; required\nsize = 16 # MiB\n")
    cfg = Config.load(files=[f])
    assert (cfg.host, cfg.size) == ("rgadget.local", 16)


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


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    """Isolated ~ and cwd; /etc/rgadget redirected into tmp_path."""
    home, cwd, etc = tmp_path / "home", tmp_path / "cwd", tmp_path / "etc"
    for d in (home, cwd, etc):
        d.mkdir()
    monkeypatch.setattr(
        "remote_gadget.config.search_path",
        lambda: [etc / "rgadget", home / ".rgadget", cwd / ".rgadget"],
    )
    return home / ".rgadget", cwd / ".rgadget", etc / "rgadget"


def test_save_creates_in_cwd_by_default(dirs):
    home, cwd, _ = dirs
    assert save_profile("lab", {"host": "pi.local", "size": 32, "nosudo": None}) == cwd
    assert cwd.read_text() == "[lab]\nhost = pi.local\nsize = 32\n"
    assert not home.exists()


def test_save_user(dirs):
    home, _, _ = dirs
    assert save_profile("lab", {"host": "pi.local"}, user=True) == home


def test_existing_needs_update_or_overwrite(dirs):
    save_profile("lab", {"host": "a"})
    with pytest.raises(ConfigError, match="already exists"):
        save_profile("lab", {"host": "b"})


def test_update_and_unset(dirs):
    _, cwd, _ = dirs
    cwd.write_text("[lab]\n# keep me\nhost = a\nusername = pi\n")
    save_profile("lab", {"keymap": "gb", "readonly": True, "vendor_id": 0x1234},
                 unset=["username"], update=True)  # fmt: skip
    assert cwd.read_text() == (
        "[lab]\n# keep me\nhost = a\nkeymap = gb\nreadonly = true\nvendor_id = 0x1234\n"
    )
    assert Config.load(files=[cwd]).vendor_id == 0x1234


def test_overwrite_replaces_settings(dirs):
    _, cwd, _ = dirs
    cwd.write_text("[lab]\nhost = a\nusername = pi\n")
    save_profile("lab", {"host": "b"}, overwrite=True)
    assert cwd.read_text() == "[lab]\nhost = b\n"


def test_update_edits_home_copy_when_that_is_where_it_lives(dirs):
    home, cwd, _ = dirs
    home.write_text("[lab]\nhost = a\n")
    assert save_profile("lab", {"size": 16}, update=True) == home
    assert not cwd.exists()


def test_update_of_etc_profile_copies_it_locally(dirs):
    _, cwd, etc = dirs
    etc.write_text("[lab]\nhost = a\nusername = pi\n")
    assert save_profile("lab", {"size": 16}, update=True) == cwd
    assert cwd.read_text() == "[lab]\nhost = a\nusername = pi\nsize = 16\n"
    assert etc.read_text() == "[lab]\nhost = a\nusername = pi\n"


def test_user_warns_when_local_copy_shadows(dirs):
    home, cwd, _ = dirs
    cwd.write_text("[lab]\nhost = a\n")
    with pytest.warns(UserWarning, match="precedence"):
        save_profile("lab", {"host": "b"}, update=True, user=True)
    assert home.read_text() == "[lab]\nhost = b\n"


@pytest.mark.parametrize(
    "name, settings, kw, match",
    [
        ("lab", {"size": 16}, {}, "needs a host"),
        ("lab", {"host": "-oProxy"}, {}, "invalid host"),
        ("default", {"host": "a"}, {}, "invalid profile name"),
        ("a b", {"host": "a"}, {}, "invalid profile name"),
    ],
)
def test_save_validates_before_writing(dirs, name, settings, kw, match):
    _, cwd, _ = dirs
    with pytest.raises(ConfigError, match=match):
        save_profile(name, settings, **kw)
    assert not cwd.exists()


def test_unset_host_rejected(dirs):
    _, cwd, _ = dirs
    cwd.write_text("[lab]\nhost = a\n")
    with pytest.raises(ConfigError, match="needs a host"):
        save_profile("lab", {}, unset=["host"], update=True)
    assert cwd.read_text() == "[lab]\nhost = a\n"

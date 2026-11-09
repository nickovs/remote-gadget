"""Profile configuration: loading, merging and validation (docs/design.md section 5)."""

from __future__ import annotations

import configparser
import dataclasses
import os
import re
import warnings
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .errors import ConfigError

DEFAULT_PATH = "/var/lib/remote-gadget"
DEFAULT_VENDOR_ID = 0x1D6B
DEFAULT_PRODUCT_ID = 0x0104


def search_path() -> list[Path]:
    """Config files in increasing order of precedence."""
    return [Path("/etc/rgadget"), Path.home() / ".rgadget", Path.cwd() / ".rgadget"]


@dataclasses.dataclass(frozen=True)
class Config:
    """Settings for one gadget host.  Build with :meth:`load` or directly."""

    host: str
    username: str | None = None
    key: str | None = None
    nosudo: bool = False
    path: str = DEFAULT_PATH
    size: int = 64
    keymap: str = "us"
    readonly: bool = False
    vendor_id: int = DEFAULT_VENDOR_ID
    product_id: int = DEFAULT_PRODUCT_ID
    profile: str | None = None

    def __post_init__(self) -> None:
        for f in dataclasses.fields(self):
            validator = _VALIDATORS.get(f.name)
            value = getattr(self, f.name)
            if validator and value is not None:
                object.__setattr__(self, f.name, validator(value))

    @classmethod
    def load(
        cls,
        profile: str | None = None,
        overrides: Mapping[str, Any] | None = None,
        files: Sequence[Path] | None = None,
    ) -> Config:
        """Load ``profile`` from the merged config files, then apply ``overrides``.

        ``overrides`` values of ``None`` are ignored, so CLI defaults can be passed as-is.
        """
        parser = read_files(search_path() if files is None else files)
        name = select_profile(parser, profile)
        values: dict[str, Any] = {}
        if name is not None:
            for key, raw in parser.items(name):
                if key not in _FIELDS:
                    warnings.warn(f"unknown key {key!r} in profile [{name}]", stacklevel=2)
                    continue
                values[key] = _parse_value(key, raw)
        values.update({k: v for k, v in (overrides or {}).items() if v is not None})
        if not values.get("host"):
            where = f"profile [{name}]" if name else "configuration"
            raise ConfigError(f"no host in {where}", "set `host` or pass --host")
        return cls(profile=name, **values)


_FIELDS = {f.name for f in dataclasses.fields(Config)} - {"profile"}
_BOOLS = {"nosudo", "readonly"}
_INTS = {"size": 10, "vendor_id": 0, "product_id": 0}
_DEFAULT_SECTION = "\0none"  # stop configparser treating [default] as DEFAULTS


def read_files(files: Sequence[Path]) -> configparser.ConfigParser:
    """Read ``files`` in order; later files override earlier ones key by key."""
    parser = configparser.ConfigParser(interpolation=None, default_section=_DEFAULT_SECTION)
    for path in files:
        try:
            text = path.read_text()
        except FileNotFoundError:
            continue
        except OSError as e:
            raise ConfigError(f"cannot read {path}: {e.strerror}") from None
        try:
            parser.read_string(text, source=str(path))
        except configparser.Error as e:
            raise ConfigError(f"bad config file: {e}") from None
    if parser.has_section("default"):
        extra = set(parser.options("default")) - {"profile"}
        if extra:
            raise ConfigError(f"[default] may only contain `profile`, found: {sorted(extra)}")
    return parser


def select_profile(parser: configparser.ConfigParser, requested: str | None) -> str | None:
    """Pick the profile: argument, $RGADGET_PROFILE, [default] profile, or the only one."""
    profiles = [s for s in parser.sections() if s != "default"]
    name = (
        requested
        or os.environ.get("RGADGET_PROFILE")
        or parser.get("default", "profile", fallback=None)
    )
    if name:
        if name not in profiles:
            raise ConfigError(f"no profile [{name}]", f"known profiles: {', '.join(profiles)}")
        return name
    if len(profiles) == 1:
        return profiles[0]
    if profiles:
        raise ConfigError(
            "several profiles and no default", "pass --profile or set [default] profile"
        )
    return None


def _parse_value(key: str, raw: str) -> Any:
    if key in _BOOLS:
        try:
            return configparser.ConfigParser.BOOLEAN_STATES[raw.strip().lower()]
        except KeyError:
            raise ConfigError(f"{key}: not a boolean: {raw!r}") from None
    if key in _INTS:
        try:
            return int(raw.strip(), _INTS[key])
        except ValueError:
            raise ConfigError(f"{key}: not a number: {raw!r}") from None
    return raw.strip()


def _match(key: str, pattern: str) -> Any:
    def check(value: str) -> str:
        if not isinstance(value, str) or not re.fullmatch(pattern, value):
            raise ConfigError(f"invalid {key}: {value!r}")
        return value

    return check


def _check_key(value: str) -> str:
    path = os.path.expanduser(value)
    if not os.access(path, os.R_OK):
        raise ConfigError(f"ssh key {value!r} is not readable")
    return path


def _check_size(value: int) -> int:
    if not 8 <= value <= 32768:
        raise ConfigError(f"size must be 8-32768 MiB, not {value}")
    return value


def _check_u16(key: str) -> Any:
    def check(value: int) -> int:
        if not 0 < value <= 0xFFFF:
            raise ConfigError(f"{key} must be 0x0001-0xffff")
        return value

    return check


def _check_path(value: str) -> str:
    if not re.fullmatch(r"/[A-Za-z0-9._/-]*", value) or ".." in value.split("/"):
        raise ConfigError(f"path must be absolute with no special characters: {value!r}")
    return value.rstrip("/") or "/"


_VALIDATORS: dict[str, Any] = {
    # Must not start with "-" so it cannot be read as an ssh option.
    "host": _match("host", r"[A-Za-z0-9_.:%\[\]][A-Za-z0-9_.:%\[\]-]*"),
    "username": _match("username", r"(?i)[a-z_][a-z0-9_.-]*"),
    "key": _check_key,
    "path": _check_path,
    "size": _check_size,
    "keymap": _match("keymap", r"[A-Za-z0-9_-]+(:[A-Za-z0-9_-]+)?"),
    "vendor_id": _check_u16("vendor_id"),
    "product_id": _check_u16("product_id"),
}

"""Profile configuration: loading, saving and validation (docs/design.md section 5)."""

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
from .inifile import set_section

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
        """Load ``profile`` from the config files, then apply ``overrides``.

        Each profile comes whole from the nearest file that defines it.  ``overrides``
        values of ``None`` are ignored, so CLI defaults can be passed as-is.
        """
        found = read_files(search_path() if files is None else files)
        name = select_profile(found, profile)
        values: dict[str, Any] = {}
        if name is not None:
            for key, raw in found.values(name).items():
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


@dataclasses.dataclass
class ProfileSet:
    """Profiles found in the config files.

    ``sources[name]`` lists ``(file, raw values)`` for every file defining the profile,
    nearest first; the nearest one is the profile that is used.
    """

    sources: dict[str, list[tuple[Path, dict[str, str]]]]
    default: str | None

    def values(self, name: str) -> dict[str, str]:
        return self.sources[name][0][1]


def read_files(files: Sequence[Path]) -> ProfileSet:
    """Read ``files`` (in increasing precedence) into a :class:`ProfileSet`."""
    sources: dict[str, list[tuple[Path, dict[str, str]]]] = {}
    default = None
    for path in reversed(files):
        try:
            text = path.read_text()
        except FileNotFoundError:
            continue
        except OSError as e:
            raise ConfigError(f"cannot read {path}: {e.strerror}") from None
        parser = configparser.ConfigParser(
            interpolation=None, default_section=_DEFAULT_SECTION, inline_comment_prefixes=(";", "#")
        )
        try:
            parser.read_string(text, source=str(path))
        except configparser.Error as e:
            raise ConfigError(f"bad config file: {e}") from None
        for section in parser.sections():
            if section != "default":
                sources.setdefault(section, []).append((path, dict(parser.items(section))))
            elif default is None:
                extra = set(parser.options("default")) - {"profile"}
                if extra:
                    raise ConfigError(
                        f"{path}: [default] may only contain `profile`, found: {sorted(extra)}"
                    )
                default = parser.get("default", "profile", fallback=None)
    return ProfileSet(sources, default)


def select_profile(found: ProfileSet, requested: str | None) -> str | None:
    """Pick the profile: argument, $RGADGET_PROFILE, [default] profile, or the only one."""
    profiles = sorted(found.sources)
    name = requested or os.environ.get("RGADGET_PROFILE") or found.default
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


def save_profile(
    name: str,
    settings: Mapping[str, Any],
    unset: Sequence[str] = (),
    update: bool = False,
    overwrite: bool = False,
    user: bool = False,
) -> Path:
    """Create or change profile ``[name]``; return the file written.

    The file is the nearest of ./.rgadget and ~/.rgadget that already has the profile,
    else ./.rgadget (or ~/.rgadget with ``user``).  /etc/rgadget is never written.  An
    existing profile needs ``update`` (change the given keys) or ``overwrite`` (replace
    it).  ``settings`` values of ``None`` are ignored; values are validated before
    anything is written, and the rest of the file is kept as it is.
    """
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", name) or name == "default":
        raise ConfigError(f"invalid profile name {name!r}")
    _, home, cwd = search_path()
    found = read_files(search_path())
    sources = found.sources.get(name, [])
    if sources and not (update or overwrite):
        raise ConfigError(
            f"profile [{name}] already exists in {sources[0][0]}", "use --update or --overwrite"
        )
    writable = [path for path, _ in sources if path in (home, cwd)]
    target = home if user else (writable[0] if writable else cwd)
    if user and any(path == cwd for path, _ in sources):
        warnings.warn(f"[{name}] in {cwd} still takes precedence over {home}", stacklevel=2)

    values: dict[str, str] = {}
    if update and sources:
        # Start from the profile in the target file if it has one, else the one in use.
        values = dict(next((v for p, v in sources if p == target), sources[0][1]))
    values.update({k: _format_value(k, v) for k, v in settings.items() if v is not None})
    for key in unset:
        values.pop(key, None)
    if "host" not in values:
        raise ConfigError(f"profile [{name}] needs a host", "pass --host")
    Config(**{k: _parse_value(k, v) for k, v in values.items() if k in _FIELDS})

    try:
        text = target.read_text()
    except FileNotFoundError:
        text = ""
    except OSError as e:
        raise ConfigError(f"cannot read {target}: {e.strerror}") from None
    tmp = target.with_name(target.name + ".tmp")
    try:
        tmp.write_text(set_section(text, name, values))
        os.replace(tmp, target)
    except OSError as e:
        raise ConfigError(f"cannot write {target}: {e.strerror}") from None
    return target


def _format_value(key: str, value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if key in ("vendor_id", "product_id"):
        return f"0x{value:04x}"
    return str(value)


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

"""remote-gadget: control a Raspberry Pi USB gadget (keyboard + storage) over ssh."""

from importlib.metadata import PackageNotFoundError, version

from .api import Gadget, GadgetState
from .config import Config
from .errors import (
    ConfigError,
    LocalFileError,
    NotInitialisedError,
    RemoteError,
    RemoteGadgetError,
    SshConnectionError,
    TypingTimeoutError,
    UsageError,
    WrongStateError,
)

try:
    __version__ = version("remote-gadget")
except PackageNotFoundError:  # running from a source tree
    __version__ = "0+unknown"

__all__ = [
    "Config",
    "ConfigError",
    "Gadget",
    "GadgetState",
    "LocalFileError",
    "NotInitialisedError",
    "RemoteError",
    "RemoteGadgetError",
    "SshConnectionError",
    "TypingTimeoutError",
    "UsageError",
    "WrongStateError",
]

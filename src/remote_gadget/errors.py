"""Exception hierarchy.  The CLI maps each class to an exit status."""

from __future__ import annotations

from typing import Any


class RemoteGadgetError(Exception):
    """Base class for all expected failures."""

    exit_code = 1

    def __init__(self, message: str, hint: str | None = None):
        super().__init__(message)
        self.message = message
        self.hint = hint


class ConfigError(RemoteGadgetError):
    """Invalid or missing configuration."""

    exit_code = 2


class UsageError(RemoteGadgetError):
    """Invalid command-line usage."""

    exit_code = 2


class SshConnectionError(RemoteGadgetError):
    """ssh could not connect or authenticate."""

    exit_code = 3


class LocalFileError(RemoteGadgetError):
    """A local file is missing, unreadable or would be overwritten."""


class RemoteError(RemoteGadgetError):
    """The helper on the gadget host reported a failure."""

    def __init__(self, code: str, message: str, hint: str | None = None, **data: Any):
        super().__init__(message, hint)
        self.code = code
        self.data = data


class NotInitialisedError(RemoteError):
    """The gadget host has not been set up with ``init``."""


class WrongStateError(RemoteError):
    """The gadget is in the wrong state, e.g. ``write`` while plugged."""


class TypingTimeoutError(RemoteError):
    """The target stopped reading keystrokes; ``unsent`` holds the rest of the text."""

    @property
    def unsent(self) -> str:
        return str(self.data.get("unsent", ""))


REMOTE_ERRORS: dict[str, type[RemoteError]] = {
    "not_initialised": NotInitialisedError,
    "wrong_state": WrongStateError,
    "not_configured": WrongStateError,
    "typing_timeout": TypingTimeoutError,
}

"""Typed errors so every failure can be logged and recovered from precisely."""


class WingoError(Exception):
    """Base class for every error raised by this package."""


class ConfigError(WingoError):
    """Bad or missing configuration (env vars)."""


class ApiError(WingoError):
    """The data endpoint rejected a call or could not be reached."""

    def __init__(self, message: str, *, retryable: bool = True, payload=None):
        super().__init__(message)
        self.retryable = retryable
        self.payload = payload


class StorageError(WingoError):
    """SQLite failure."""


class AIError(WingoError):
    """Every AI provider in the chain failed."""


class SnapshotError(WingoError):
    """Snapshot rendering or delivery failed."""


class BotError(WingoError):
    """Telegram delivery failed."""

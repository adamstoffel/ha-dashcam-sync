"""Exception hierarchy."""

from __future__ import annotations


class DashcamError(Exception):
    """Base class for every error raised on purpose."""


class CameraError(DashcamError):
    """The camera misbehaved or answered with something unusable."""


class LinkLost(CameraError):
    """The camera vanished (timeout, reset, unreachable)."""


class ProtocolError(CameraError):
    """The camera answered, but not in a way the driver understands."""


class NothingBeyond(ProtocolError):
    """HTTP 416: the camera has no bytes at or after the requested offset."""

    def __init__(self, message: str, total: int | None = None) -> None:
        super().__init__(message)
        # The file's size when the 416 states it (Content-Range: bytes */N).
        self.total = total


class Incomplete(CameraError):
    """A download ended cleanly but short of the expected size; the partial file is kept."""


class UnsupportedCamera(DashcamError):
    """No driver claimed the camera."""


class StorageError(DashcamError):
    """A storage target is unusable."""


class StorageFull(StorageError):
    """Writing would push free space below ``min_free_percent``."""


class TargetOffline(StorageError):
    """A network target is not mounted or not writable."""


class ConfigError(DashcamError):
    """Options failed validation."""

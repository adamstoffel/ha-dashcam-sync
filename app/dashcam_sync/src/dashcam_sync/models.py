"""Plain data types shared by drivers, the engine, storage and the API."""

from __future__ import annotations

import datetime as dt
import enum
import re
from dataclasses import dataclass, field
from typing import Any


class Kind(enum.StrEnum):
    """Vendor-neutral clip category; retention and the UI only see these."""

    NORMAL = "normal"
    EVENT = "event"
    PARKING = "parking"
    PHOTO = "photo"


class Channel(enum.StrEnum):
    """Which lens a clip came from."""

    FRONT = "front"
    REAR = "rear"
    INTERIOR = "interior"


CHANNEL_ORDER = {Channel.FRONT: 0, Channel.REAR: 1, Channel.INTERIOR: 2}

_EXT_RE = re.compile(r"^[a-z0-9]{1,4}$")
SAFE_EXTENSIONS = frozenset({"mp4", "ts", "mov", "avi", "jpg", "jpeg", "png"})


def safe_extension(source_path: str) -> str:
    """Return a lower-case extension taken from a camera path, or ``mp4``.

    Camera supplied paths are never used as local file names, only the
    extension survives and only when it is on the allow list.
    """
    name = source_path.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    if "." not in name:
        return "mp4"
    ext = name.rsplit(".", 1)[-1].lower()
    if _EXT_RE.match(ext) and ext in SAFE_EXTENSIONS:
        return ext
    return "mp4"


@dataclass(frozen=True, slots=True)
class Recording:
    """One file on a camera, as listed by a driver."""

    path: str
    size: int
    start: dt.datetime
    kind: Kind
    channel: Channel = Channel.FRONT
    locked: bool = False
    duration: int | None = None

    @property
    def extension(self) -> str:
        return safe_extension(self.path)

    @property
    def is_photo(self) -> bool:
        return self.kind is Kind.PHOTO


@dataclass(frozen=True, slots=True)
class Caps:
    """What a driver can do. ``range`` records HTTP Range support."""

    list: bool = True
    range: bool = True
    delete: bool = False
    live: bool = False
    station_mode: bool = False
    gps: bool = False

    def as_dict(self) -> dict[str, bool]:
        return {
            "list": self.list,
            "range": self.range,
            "delete": self.delete,
            "live": self.live,
            "station_mode": self.station_mode,
            "gps": self.gps,
        }


@dataclass(frozen=True, slots=True)
class Probe:
    """Result of a successful read-only probe."""

    driver: str
    confidence: float
    host: str
    model: str | None = None
    firmware: str | None = None
    fingerprint: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class DeviceInfo:
    firmware: str | None = None
    model: str | None = None
    sd_state: str | None = None
    free_bytes: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "firmware": self.firmware,
            "model": self.model,
            "sd_state": self.sd_state,
            "free_bytes": self.free_bytes,
        }


def utc(ts: float) -> dt.datetime:
    return dt.datetime.fromtimestamp(ts, tz=dt.UTC)


def naive_local_to_utc(value: dt.datetime) -> dt.datetime:
    """Cameras report wall-clock time without a zone; assume the host zone."""
    if value.tzinfo is not None:
        return value.astimezone(dt.UTC)
    return value.astimezone().astimezone(dt.UTC)

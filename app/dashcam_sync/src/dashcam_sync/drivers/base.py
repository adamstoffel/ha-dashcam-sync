"""Driver interface.

Every camera family is a driver behind this one interface. Detection runs each
driver's cheap, read-only probe until one claims the camera.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import ClassVar, Protocol, runtime_checkable

from ..models import Caps, Channel, DeviceInfo, Kind, Probe, Recording, naive_local_to_utc
from .http import ByteStream, CameraHttp


@runtime_checkable
class Driver(Protocol):
    name: ClassVar[str]
    default_hosts: ClassVar[list[str]]
    capabilities: ClassVar[Caps]

    def attach(self, host: str) -> None: ...
    async def probe(self, host: str) -> Probe | None: ...  # read-only, <= 3 s
    async def info(self) -> DeviceInfo: ...
    async def prepare(self) -> None: ...  # e.g. stop recording, enter playback
    async def list_recordings(self) -> list[Recording]: ...
    async def open(self, rec: Recording, offset: int) -> ByteStream: ...
    async def delete(self, rec: Recording) -> None: ...
    async def restore(self) -> None: ...  # back to movie mode + recording; idempotent
    def live_url(self) -> str | None: ...
    async def close(self) -> None: ...


class BaseDriver:
    """Shared plumbing: host binding and the hardened HTTP helper."""

    name: ClassVar[str] = "base"
    default_hosts: ClassVar[list[str]] = []
    capabilities: ClassVar[Caps] = Caps()

    def __init__(self, http: CameraHttp) -> None:
        self.http = http
        self.host = ""

    def attach(self, host: str) -> None:
        self.host = host
        self.http.host = host

    @property
    def hostname(self) -> str:
        return self.host.rsplit(":", 1)[0] if ":" in self.host else self.host

    async def probe(self, host: str) -> Probe | None:
        raise NotImplementedError

    async def info(self) -> DeviceInfo:
        raise NotImplementedError

    async def prepare(self) -> None:
        raise NotImplementedError

    async def list_recordings(self) -> list[Recording]:
        raise NotImplementedError

    async def open(self, rec: Recording, offset: int) -> ByteStream:
        raise NotImplementedError

    async def restore(self) -> None:
        raise NotImplementedError

    async def close(self) -> None:
        return None

    def live_url(self) -> str | None:
        return None

    async def delete(self, rec: Recording) -> None:
        raise NotImplementedError(f"{self.name} cannot delete recordings")


_TS_PATTERNS = (
    re.compile(
        r"(?P<y>20\d{2})[-_]?(?P<mo>\d{2})[-_]?(?P<d>\d{2})[-_ T]?(?P<h>\d{2})[-_:]?(?P<mi>\d{2})[-_:]?(?P<s>\d{2})"
    ),
    re.compile(r"(?P<y>20\d{2})_(?P<mo>\d{2})(?P<d>\d{2})_(?P<h>\d{2})(?P<mi>\d{2})(?P<s>\d{2})"),
)

EPOCH = dt.datetime(1970, 1, 1, tzinfo=dt.UTC)


def timestamp_from_name(name: str) -> dt.datetime | None:
    """Pull a wall-clock stamp like ``20261122_101500`` out of a file name."""
    for pattern in _TS_PATTERNS:
        match = pattern.search(name)
        if match:
            parts = {k: int(v) for k, v in match.groupdict().items()}
            try:
                naive = dt.datetime(
                    parts["y"], parts["mo"], parts["d"], parts["h"], parts["mi"], parts["s"]
                )
            except ValueError:
                continue
            return naive_local_to_utc(naive)
    return None


def classify(path: str) -> tuple[Kind, Channel, bool]:
    """Map vendor folder and file naming onto kind, channel and a locked flag.

    Drivers translate folder names such as ``RO``, ``EMR`` or ``loop`` here so
    retention and the UI never see vendor words.
    """
    lowered = path.replace("\\", "/").lower()
    parts = [p for p in lowered.split("/") if p]
    name = parts[-1] if parts else ""
    folders = set(parts[:-1])
    stem = name.rsplit(".", 1)[0]
    ext = name.rsplit(".", 1)[-1] if "." in name else ""

    locked = False
    if ext in {"jpg", "jpeg", "png"} or "photo" in folders:
        kind = Kind.PHOTO
    elif folders & {"ro", "emr", "emergency", "event", "events", "lock", "locked", "impact"}:
        kind = Kind.EVENT
        locked = True
    elif folders & {"park", "parking", "parked"} or re.search(r"(^|_)(p|park|parking)(_|$)", stem):
        kind = Kind.PARKING
    elif re.search(r"(^|_)(e|evt|event|emr)(_|$)", stem):
        kind = Kind.EVENT
        locked = True
    else:
        kind = Kind.NORMAL

    channel = Channel.FRONT
    if re.search(r"(^|[_-])(r|rear|b|back)(\.|_|$)", stem) or stem.endswith(("_r", "_b")):
        channel = Channel.REAR
    elif re.search(r"(^|[_-])(i|in|inside|cabin|interior)(\.|_|$)", stem) or stem.endswith("_i"):
        channel = Channel.INTERIOR
    return kind, channel, locked

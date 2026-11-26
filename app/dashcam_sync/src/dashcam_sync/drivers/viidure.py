"""Viidure ``/app`` cameras (JSON over HTTP, files served as ``.ts``).

Endpoints follow community captures (dashgo-desktop, Action-Cam-Hacking):
``/app/getproductinfo`` identifies the camera and
``/app/getfilelist?folder=<f>&start=<a>&end=<b>`` lists a folder.

Firmware differs a lot between brands (Viidure, SGK GK720X, ...): the firmware
version may be only in ``/app/getdeviceattr``, card space is in MiB, the live
stream is named by ``/app/getmediainfo``, and some cameras have no event or
parking folder and no playback mode. Only the loop folder is required.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any, ClassVar
from urllib.parse import quote, urlsplit

from ..errors import CameraError, ProtocolError
from ..models import Caps, Channel, DeviceInfo, Kind, Probe, Recording, naive_local_to_utc, utc
from .base import EPOCH, BaseDriver, classify, timestamp_from_name
from .http import ByteStream

# Vendor folder name -> kind. ``loop`` is continuous recording.
FOLDERS: dict[str, Kind] = {
    "loop": Kind.NORMAL,
    "emr": Kind.EVENT,
    "event": Kind.EVENT,
    "park": Kind.PARKING,
    "photo": Kind.PHOTO,
}
REQUIRED_FOLDER = "loop"
MIB = 1024 * 1024
PAGE = 100
MAX_PAGES = 50

_LOGGER = logging.getLogger(__name__)


class ViidureDriver(BaseDriver):
    name: ClassVar[str] = "viidure_app"
    # 192.168.169.1: Viidure app cameras; 192.168.0.1: SGK (GK720X) firmware.
    default_hosts: ClassVar[list[str]] = ["192.168.169.1", "192.168.0.1"]
    capabilities: ClassVar[Caps] = Caps(
        list=True, range=True, delete=False, live=True, station_mode=False, gps=False
    )

    _live_path: str | None = None
    # Set when the camera has no playback mode: it keeps recording while we sync.
    _still_recording = False

    async def probe(self, host: str) -> Probe | None:
        self.attach(host)
        self._live_path = None
        try:
            data = await self.http.get_json("/app/getproductinfo")
        except Exception:
            return None
        if not isinstance(data, dict) or "result" not in data:
            return None
        info = data.get("info")
        if not isinstance(info, dict):
            return None
        firmware = _text(info.get("softver") or info.get("version"))
        try:  # extras: the camera is identified already, so a hiccup here must not undo that
            if firmware is None:
                firmware = _text((await self._optional_info("/app/getdeviceattr")).get("softver"))
            self._live_path = _rtsp_path(
                (await self._optional_info("/app/getmediainfo")).get("rtsp")
            )
        except CameraError as err:
            _LOGGER.debug("Optional camera details unavailable: %s", err)
        return Probe(
            driver=self.name,
            confidence=0.9,
            host=host,
            model=_text(info.get("model") or info.get("product")),
            firmware=firmware,
            fingerprint={"productinfo": sorted(info.keys())[:12]},
        )

    async def _optional_info(self, path: str) -> dict[str, Any]:
        """``info`` of an endpoint only some firmware has; ``{}`` when it is missing.

        A dropped link is still raised: only a missing or odd endpoint is ignored.
        """
        try:
            data = await self.http.get_json(path)
        except ProtocolError:
            return {}
        info = data.get("info") if isinstance(data, dict) else None
        return info if isinstance(info, dict) else {}

    async def info(self) -> DeviceInfo:
        data = await self.http.get_json("/app/getproductinfo")
        info = data.get("info", {}) if isinstance(data, dict) else {}
        firmware = _text(info.get("softver"))
        if firmware is None:
            firmware = _text((await self._optional_info("/app/getdeviceattr")).get("softver"))
        sd = await self._optional_info("/app/getsdinfo")
        return DeviceInfo(
            firmware=firmware,
            model=_text(info.get("model")),
            sd_state="ready",
            free_bytes=_sd_free_bytes(sd),
        )

    async def prepare(self) -> None:
        self._still_recording = not await self._get_ok("/app/playback?param=enter")

    async def restore(self) -> None:
        await self._get_ok("/app/playback?param=exit")
        await self._get_ok("/app/setparamvalue?param=rec&value=1")

    async def _get_ok(self, path: str) -> bool:
        """Call a mode switch; ``False`` when the firmware has no such endpoint."""
        try:
            data = await self.http.get_json(path)
        except ProtocolError as err:
            if ": HTTP 404" in str(err):
                # No such mode on this firmware: it keeps recording, nothing to switch.
                _LOGGER.debug("%s is not supported by this camera", path)
                return False
            raise
        if isinstance(data, dict) and data.get("result") not in (0, "0", None):
            raise ProtocolError(f"{path}: result {data.get('result')}")
        return True

    async def list_recordings(self) -> list[Recording]:
        recs: list[Recording] = []
        for folder in FOLDERS:
            try:
                recs.extend(await self._list_folder(folder))
            except ProtocolError as err:
                if folder == REQUIRED_FOLDER:
                    raise
                # Not every camera has every folder; a dropped link (LinkLost) still stops.
                _LOGGER.debug("Skipping the %s folder: %s", folder, err)
        if self._still_recording:
            recs = _without_clip_being_recorded(recs)
        return recs

    async def _list_folder(self, folder: str) -> list[Recording]:
        out: list[Recording] = []
        for page in range(MAX_PAGES):
            start, end = page * PAGE, page * PAGE + PAGE - 1
            data = await self.http.get_json(
                f"/app/getfilelist?folder={folder}&start={start}&end={end}"
            )
            files = _extract_files(data)
            for item in files:
                rec = self._parse(folder, item)
                if rec is not None:
                    out.append(rec)
            if len(files) < PAGE:
                break
        return out

    def _parse(self, folder: str, item: Any) -> Recording | None:
        if not isinstance(item, dict):
            return None
        # Some firmware gives a bare ``name`` next to the full ``path``: use the full one.
        candidates = [_text(item.get(k)) for k in ("name", "path", "filepath")]
        path = next((c for c in candidates if c and "/" in c), None) or next(
            (c for c in candidates if c), None
        )
        if not path:
            return None
        try:
            size = int(item.get("size", 0))
        except (TypeError, ValueError):
            return None
        kind = FOLDERS.get(folder, Kind.NORMAL)
        _, channel, locked = classify(path)
        if kind is Kind.EVENT:
            locked = True
        start = _parse_start(item.get("time", item.get("createtime")), path)
        # The listed size is not trustworthy for verification: SGK firmware lists
        # kilobytes for videos and an unrelated number for photos. It is kept as an
        # estimate; the download's Content-Length and Content-Range decide.
        return Recording(
            path=path, size=0, start=start, kind=kind, channel=channel, locked=locked,
            approx_size=_approx_bytes(size, kind),
        )  # fmt: skip

    @staticmethod
    def url_path(rec: Recording) -> str:
        path = rec.path
        for prefix in ("/mnt/mmc", "/mnt/sdcard", "/mnt/emmc"):
            if path.startswith(prefix):
                path = path[len(prefix) :]
                break
        if not path.startswith("/"):
            path = "/" + path
        return quote(path, safe="/")

    async def open(self, rec: Recording, offset: int) -> ByteStream:
        return await self.http.stream(self.url_path(rec), offset)

    def live_url(self) -> str | None:
        if not self.host:
            return None
        return f"rtsp://{self.hostname}:554{self._live_path or '/live'}"


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _without_clip_being_recorded(recs: list[Recording]) -> list[Recording]:
    """Leave out the newest loop clip: the camera is still writing it, so its size
    would change under the download. It is complete (and fetched) next visit."""
    loop = [r for r in recs if r.kind is Kind.NORMAL]
    if not loop:
        return recs
    newest = max(r.start for r in loop)
    return [r for r in recs if not (r.kind is Kind.NORMAL and r.start == newest)]


def _approx_bytes(listed: int, kind: Kind) -> int:
    """Best guess of a listed size in bytes: a video listed below 10 MB is in kB."""
    if listed <= 0:
        return 0
    if kind is not Kind.PHOTO and listed < 10_000_000:
        return listed * 1000
    return listed


def _rtsp_path(raw: Any) -> str | None:
    """The path of the camera's own live URL; the host is always the camera's."""
    text = _text(raw)
    if not text or not text.lower().startswith("rtsp://"):
        return None
    path = urlsplit(text).path
    return path if path.startswith("/") and len(path) > 1 else None


def _sd_free_bytes(sd: dict[str, Any]) -> int | None:
    """Free card space. These cameras report MiB (``free: 109450`` on a 128 GB card)."""
    try:
        free = int(sd["free"])
    except (KeyError, TypeError, ValueError):
        return None
    try:
        total = int(sd.get("total", 0))
    except (TypeError, ValueError):
        total = 0
    # A count in MiB stays well below 2^24 even for a 1 TB card; bytes never do.
    return free * MIB if max(free, total) < 1 << 24 else free


def _extract_files(data: Any) -> list[Any]:
    if not isinstance(data, dict):
        raise ProtocolError("file list is not an object")
    info = data.get("info")
    files: list[Any] = []
    if isinstance(info, list):
        for entry in info:
            if isinstance(entry, dict) and isinstance(entry.get("files"), list):
                files.extend(entry["files"])
    elif isinstance(info, dict) and isinstance(info.get("files"), list):
        files.extend(info["files"])
    return files


def _parse_start(raw: Any, path: str) -> dt.datetime:
    if isinstance(raw, (int, float)) and raw > 0:
        return utc(float(raw))
    if isinstance(raw, str):
        if raw.isdigit() and int(raw) > 946_684_800:
            return utc(float(raw))
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S"):
            try:
                return naive_local_to_utc(dt.datetime.strptime(raw, fmt))
            except ValueError:
                continue
    return timestamp_from_name(path) or EPOCH


# Re-exported so tests can reference the mapping.
__all__ = ["FOLDERS", "Channel", "ViidureDriver"]

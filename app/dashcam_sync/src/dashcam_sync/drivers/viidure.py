"""Viidure ``/app`` cameras (JSON over HTTP, files served as ``.ts``).

Endpoints follow community captures (dashgo-desktop, Action-Cam-Hacking):
``/app/getproductinfo`` identifies the camera and
``/app/getfilelist?folder=<f>&start=<a>&end=<b>`` lists a folder.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, ClassVar
from urllib.parse import quote

from ..errors import ProtocolError
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
PAGE = 100
MAX_PAGES = 50


class ViidureDriver(BaseDriver):
    name: ClassVar[str] = "viidure_app"
    default_hosts: ClassVar[list[str]] = ["192.168.169.1"]
    capabilities: ClassVar[Caps] = Caps(
        list=True, range=True, delete=False, live=True, station_mode=False, gps=False
    )

    async def probe(self, host: str) -> Probe | None:
        self.attach(host)
        try:
            data = await self.http.get_json("/app/getproductinfo")
        except Exception:
            return None
        if not isinstance(data, dict) or "result" not in data:
            return None
        info = data.get("info")
        if not isinstance(info, dict):
            return None
        return Probe(
            driver=self.name,
            confidence=0.9,
            host=host,
            model=_text(info.get("model") or info.get("product")),
            firmware=_text(info.get("softver") or info.get("version")),
            fingerprint={"productinfo": sorted(info.keys())[:12]},
        )

    async def info(self) -> DeviceInfo:
        data = await self.http.get_json("/app/getproductinfo")
        info = data.get("info", {}) if isinstance(data, dict) else {}
        free: int | None = None
        try:
            sd = await self.http.get_json("/app/getsdinfo")
            raw = (sd.get("info") or {}).get("free")
            free = int(raw) if raw is not None else None
        except Exception:
            pass
        return DeviceInfo(
            firmware=_text(info.get("softver")),
            model=_text(info.get("model")),
            sd_state="ready",
            free_bytes=free,
        )

    async def prepare(self) -> None:
        await self._get_ok("/app/playback?param=enter")

    async def restore(self) -> None:
        await self._get_ok("/app/playback?param=exit")
        await self._get_ok("/app/setparamvalue?param=rec&value=1")

    async def _get_ok(self, path: str) -> None:
        data = await self.http.get_json(path)
        if isinstance(data, dict) and data.get("result") not in (0, "0", None):
            raise ProtocolError(f"{path}: result {data.get('result')}")

    async def list_recordings(self) -> list[Recording]:
        recs: list[Recording] = []
        for folder in FOLDERS:
            recs.extend(await self._list_folder(folder))
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
        path = _text(item.get("name") or item.get("path"))
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
        start = _parse_start(item.get("time"), path)
        return Recording(
            path=path, size=size, start=start, kind=kind, channel=channel, locked=locked
        )

    @staticmethod
    def url_path(rec: Recording) -> str:
        path = rec.path
        for prefix in ("/mnt/mmc", "/mnt/sdcard"):
            if path.startswith(prefix):
                path = path[len(prefix) :]
                break
        if not path.startswith("/"):
            path = "/" + path
        return quote(path, safe="/")

    async def open(self, rec: Recording, offset: int) -> ByteStream:
        return await self.http.stream(self.url_path(rec), offset)

    def live_url(self) -> str | None:
        return f"rtsp://{self.hostname}:554/live" if self.host else None


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


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

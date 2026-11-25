"""Generic driver for any camera that serves an HTML directory listing."""

from __future__ import annotations

import datetime as dt
import re
from html.parser import HTMLParser
from typing import ClassVar
from urllib.parse import unquote, urljoin, urlsplit

from ..errors import ProtocolError
from ..models import Caps, DeviceInfo, Probe, Recording
from .base import EPOCH, BaseDriver, classify, timestamp_from_name
from .http import ByteStream

MEDIA_EXTENSIONS = (".mp4", ".ts", ".mov", ".avi", ".jpg", ".jpeg")
MAX_FOLDERS = 40
MAX_DEPTH = 4


class _Links(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            for key, value in attrs:
                if key == "href" and value:
                    self.links.append(value)


def parse_links(html: str) -> list[str]:
    parser = _Links()
    parser.feed(html)
    return parser.links


def _is_media(link: str) -> bool:
    return urlsplit(link).path.lower().endswith(MEDIA_EXTENSIONS)


class HttpIndexDriver(BaseDriver):
    name: ClassVar[str] = "http_index"
    default_hosts: ClassVar[list[str]] = []
    capabilities: ClassVar[Caps] = Caps(
        list=True, range=True, delete=False, live=False, station_mode=False, gps=False
    )

    async def probe(self, host: str) -> Probe | None:
        self.attach(host)
        try:
            status, headers, body = await self.http.get("/")
        except Exception:
            return None
        ctype = next((v for k, v in headers.items() if k.lower() == "content-type"), "")
        if "html" not in ctype.lower():
            return None
        text = body.decode("utf-8", errors="replace")
        links = parse_links(text)
        media = [x for x in links if _is_media(x)]
        folders = [x for x in links if urlsplit(x).path.endswith("/") and not x.startswith("..")]
        if not media and not folders:
            return None
        if not media and not _looks_like_listing(text):
            return None
        return Probe(
            driver=self.name,
            confidence=0.3 if not media else 0.5,
            host=host,
            fingerprint={"media_links": len(media), "folders": len(folders)},
        )

    async def info(self) -> DeviceInfo:
        return DeviceInfo(sd_state="ready")

    async def prepare(self) -> None:
        return None

    async def restore(self) -> None:
        return None

    async def list_recordings(self) -> list[Recording]:
        recs: list[Recording] = []
        seen: set[str] = set()
        queue: list[tuple[str, int]] = [("/", 0)]
        visited = 0
        while queue and visited < MAX_FOLDERS:
            path, depth = queue.pop(0)
            if path in seen:
                continue
            seen.add(path)
            visited += 1
            try:
                html = await self.http.get_text(path)
            except ProtocolError:
                continue
            for link in parse_links(html):
                target = urlsplit(urljoin(f"http://{self.host}{path}", link))
                if target.netloc and target.netloc != self.host:
                    continue  # never follow a link off the camera
                clean = target.path
                if _is_media(clean):
                    recs.append(await self._recording(clean))
                elif clean.endswith("/") and depth < MAX_DEPTH and clean not in seen:
                    if len(clean) > len(path) and clean.startswith(path):
                        queue.append((clean, depth + 1))
        return recs

    async def _recording(self, path: str) -> Recording:
        size = 0
        start = timestamp_from_name(unquote(path))
        try:
            _, headers, _ = await self._head(path)
            size = int(headers.get("Content-Length", "0"))
            modified = headers.get("Last-Modified")
            if start is None and modified:
                start = dt.datetime.strptime(modified, "%a, %d %b %Y %H:%M:%S GMT").replace(
                    tzinfo=dt.UTC
                )
        except Exception:
            pass
        kind, channel, locked = classify(unquote(path))
        return Recording(
            path=path, size=size, start=start or EPOCH, kind=kind, channel=channel, locked=locked
        )

    async def _head(self, path: str) -> tuple[int, dict[str, str], bytes]:
        async with self.http.lock:
            try:
                async with self.http.session.head(
                    self.http.url(path), allow_redirects=False
                ) as resp:
                    return resp.status, dict(resp.headers), b""
            except Exception as err:
                raise ProtocolError(f"HEAD {path}: {err}") from err

    async def open(self, rec: Recording, offset: int) -> ByteStream:
        return await self.http.stream(rec.path, offset)


def _looks_like_listing(text: str) -> bool:
    lowered = text.lower()
    return bool(re.search(r"index of|directory listing|parent directory", lowered))

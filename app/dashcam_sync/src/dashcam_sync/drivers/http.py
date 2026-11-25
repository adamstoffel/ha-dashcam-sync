"""Hardened HTTP access to cameras.

Every camera response is untrusted: bodies are capped, every request has a
timeout, and one request runs at a time because these cameras drop
connections under parallel load.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

import aiohttp
from defusedxml import ElementTree as SafeET

from ..errors import LinkLost, ProtocolError

MAX_BODY = 4 * 1024 * 1024
CHUNK = 64 * 1024
CONTROL_TIMEOUT = aiohttp.ClientTimeout(total=10, connect=4)
STREAM_TIMEOUT = aiohttp.ClientTimeout(total=None, connect=4, sock_connect=4, sock_read=20)

NETWORK_ERRORS = (aiohttp.ClientError, TimeoutError, OSError)


class ByteStream:
    """Async iterator over a download that remembers what was really served.

    ``start`` is the offset the first byte belongs to: it equals the requested
    offset when the camera honoured the Range header and ``0`` when it ignored
    it (answered 200 instead of 206), in which case the engine restarts the
    file.
    """

    def __init__(
        self,
        response: aiohttp.ClientResponse,
        *,
        start: int,
        content_length: int | None,
        range_honored: bool,
        on_close: Callable[[], None] | None = None,
    ) -> None:
        self._on_close = on_close
        self._response = response
        self._iter = response.content.iter_chunked(CHUNK)
        self.start = start
        self.content_length = content_length
        self.range_honored = range_honored
        self._closed = False

    def __aiter__(self) -> ByteStream:
        return self

    async def __anext__(self) -> bytes:
        if self._closed:
            raise StopAsyncIteration
        try:
            return await self._iter.__anext__()
        except StopAsyncIteration:
            await self.aclose()
            raise
        except NETWORK_ERRORS as err:
            await self.aclose()
            raise LinkLost(f"download interrupted: {err}") from err

    async def aclose(self) -> None:
        if not self._closed:
            self._closed = True
            self._response.release()
            if self._on_close is not None:
                self._on_close()


class CameraHttp:
    """Serialised, capped, time-limited GETs against one camera."""

    def __init__(self, session: aiohttp.ClientSession, host: str = "") -> None:
        self.session = session
        self.host = host
        self.lock = asyncio.Lock()

    def url(self, path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return path
        return f"http://{self.host}{path if path.startswith('/') else '/' + path}"

    async def _read_capped(self, resp: aiohttp.ClientResponse) -> bytes:
        declared = resp.content_length
        if declared is not None and declared > MAX_BODY:
            raise ProtocolError(f"response too large ({declared} bytes)")
        data = bytearray()
        async for chunk in resp.content.iter_chunked(CHUNK):
            data += chunk
            if len(data) > MAX_BODY:
                raise ProtocolError("response exceeded 4 MB cap")
        return bytes(data)

    async def get(
        self,
        path: str,
        *,
        serialise: bool = True,
        timeout: aiohttp.ClientTimeout = CONTROL_TIMEOUT,
        allow_status: tuple[int, ...] = (200,),
    ) -> tuple[int, dict[str, str], bytes]:
        async def run() -> tuple[int, dict[str, str], bytes]:
            try:
                async with self.session.get(
                    self.url(path), timeout=timeout, allow_redirects=False
                ) as resp:
                    if resp.status not in allow_status:
                        raise ProtocolError(f"{path}: HTTP {resp.status}")
                    body = await self._read_capped(resp)
                    return resp.status, dict(resp.headers), body
            except NETWORK_ERRORS as err:
                raise LinkLost(f"{path}: {err}") from err

        if serialise:
            async with self.lock:
                return await run()
        return await run()

    async def get_text(self, path: str, **kw: Any) -> str:
        _, _, body = await self.get(path, **kw)
        return body.decode("utf-8", errors="replace")

    async def get_json(self, path: str, **kw: Any) -> Any:
        _, _, body = await self.get(path, **kw)
        try:
            return json.loads(body.decode("utf-8", errors="replace"))
        except ValueError as err:
            raise ProtocolError(f"{path}: not JSON") from err

    async def get_xml(self, path: str, **kw: Any) -> Any:
        _, _, body = await self.get(path, **kw)
        try:
            return SafeET.fromstring(body)
        except Exception as err:  # defusedxml raises ParseError and its own forbidden-entity errors
            raise ProtocolError(f"{path}: not safe XML ({type(err).__name__})") from err

    async def stream(self, path: str, offset: int = 0) -> ByteStream:
        """Open a download, holding the request lock until the stream closes."""
        headers = {"Range": f"bytes={offset}-"} if offset > 0 else {}
        await self.lock.acquire()
        try:
            try:
                resp = await self.session.get(
                    self.url(path),
                    headers=headers,
                    timeout=STREAM_TIMEOUT,
                    allow_redirects=False,
                )
            except NETWORK_ERRORS as err:
                raise LinkLost(f"{path}: {err}") from err
            if resp.status not in (200, 206):
                resp.release()
                raise ProtocolError(f"{path}: HTTP {resp.status}")
            honored = resp.status == 206
            start = offset if honored else 0
            length = resp.content_length
            return ByteStream(
                resp,
                start=start,
                content_length=length,
                range_honored=honored,
                on_close=self._release,
            )
        except BaseException:
            self._release()
            raise

    def _release(self) -> None:
        if self.lock.locked():
            self.lock.release()


async def tcp_open(host: str, port: int, timeout: float = 1.5) -> bool:
    """True when a TCP connection succeeds."""
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
    except (TimeoutError, OSError):
        return False
    writer.close()
    try:
        await writer.wait_closed()
    except OSError:
        pass
    return True

"""A fake dashcam served over HTTP, with fault injection.

Speaks three dialects (novatek, viidure, index) so driver, detection, resume
and fault-injection tests share one server.
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass, field
from typing import Any

from aiohttp import web


def make_bytes(name: str, size: int) -> bytes:
    """Deterministic content so tests can verify downloads byte for byte."""
    block = hashlib.sha256(name.encode()).digest()
    reps = size // len(block) + 1
    return (block * reps)[:size]


@dataclass
class FakeFile:
    path: str  # the path the camera lists (vendor style)
    url: str  # the URL path it is served at
    data: bytes
    attr: int = 32
    time: str = ""
    list_size: int | None = None


@dataclass
class Faults:
    ignore_range: bool = False
    drop_after: int | None = None  # abort the connection after N body bytes, once per file
    wrong_length: bool = False  # send fewer bytes than the listing claims
    refuse_playback: bool = False
    list_error: bool = False
    chunk_delay: float = 0.0
    drops_done: set[str] = field(default_factory=set)
    # Interruptions that look different on the wire:
    eof_after: int | None = None  # end the body cleanly after N bytes, once per file
    stall_after: int | None = None  # stop sending after N bytes, keep the socket open, once
    hide_length: bool = False  # no Content-Length (chunked) and "/*" as the range total
    range_restart_206: bool = False  # answer a Range with 206 from byte 0 (honestly labelled)
    range_wrong_start: int = 0  # answer a Range from this many bytes earlier (labelled so)
    cuts_done: set[str] = field(default_factory=set)


class FakeCamera:
    def __init__(self, family: str = "novatek") -> None:
        self.family = family
        self.files: list[FakeFile] = []
        self.faults = Faults()
        self.recording = True
        self.mode = "movie"
        self.heartbeats = 0
        self.requests: list[str] = []
        self.concurrent = 0
        self.max_concurrent = 0
        self.deleted: list[str] = []
        self._runner: web.AppRunner | None = None
        self.host = ""
        self.restore_calls = 0
        self.fail_restore = False
        self.range_requests: list[tuple[str, str]] = []
        # Viidure only: behave like SGK GK720X firmware (dashcam_probe report, Nov 2026).
        self.sgk = False
        self.park_status = 404  # SGK: no parking folder; 500 = a folder that failed

    # ------------------------------------------------------------- populate

    def add_novatek(
        self,
        name: str,
        size: int,
        *,
        folder: str = "MOVIE",
        time: str = "2026/11/22 10:15:00",
        attr: int = 32,
        data: bytes | None = None,
    ) -> FakeFile:
        path = f"A:\\CARDV\\{folder}\\{name}"
        url = f"/CARDV/{folder}/{name}".replace("\\", "/")
        f = FakeFile(path, url, data if data is not None else make_bytes(name, size), attr, time)
        self.files.append(f)
        return f

    def add_viidure(self, folder: str, name: str, size: int, time: int) -> FakeFile:
        if self.sgk:
            sub = {"loop": "Normal", "photo": "Photo"}[folder]
            path = url = f"/EMMC/{sub}/F/{name}"
        else:
            path = f"/mnt/mmc/DCIM/{folder}/{name}"
            url = f"/DCIM/{folder}/{name}"
        f = FakeFile(path, url, make_bytes(name, size), 0, str(time))
        f.folder = folder  # type: ignore[attr-defined]
        self.files.append(f)
        return f

    def add_index(self, folder: str, name: str, size: int) -> FakeFile:
        url = f"/{folder}/{name}"
        f = FakeFile(url, url, make_bytes(name, size))
        self.files.append(f)
        return f

    # --------------------------------------------------------------- server

    async def start(self, port: int = 0) -> str:
        app = web.Application(middlewares=[self._track])
        app.router.add_route("*", "/{tail:.*}", self._dispatch)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", port)
        await site.start()
        assert site._server is not None and site._server.sockets
        bound = site._server.sockets[0].getsockname()[1]
        self.host = f"127.0.0.1:{bound}"
        return self.host

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    @web.middleware
    async def _track(self, request: web.Request, handler: Any) -> web.StreamResponse:
        self.requests.append(request.path_qs)
        is_beat = request.query.get("cmd") == "3016"
        if not is_beat:
            self.concurrent += 1
            self.max_concurrent = max(self.max_concurrent, self.concurrent)
        try:
            return await handler(request)  # type: ignore[no-any-return]
        finally:
            if not is_beat:
                self.concurrent -= 1

    async def _dispatch(self, request: web.Request) -> web.StreamResponse:
        for f in self.files:
            if request.path == f.url:
                return await self._serve_file(request, f)
        if self.family == "novatek":
            return await self._novatek(request)
        if self.family == "viidure":
            return await self._viidure(request)
        if self.family == "index":
            return self._index(request)
        return web.Response(status=404, text="not found")

    # ------------------------------------------------------------- file body

    async def _serve_file(self, request: web.Request, f: FakeFile) -> web.StreamResponse:
        data = f.data
        faults = self.faults
        if request.method == "HEAD":
            headers = {} if faults.hide_length else {"Content-Length": str(len(data))}
            return web.Response(headers=headers)
        start = 0
        status = 200
        rng = request.headers.get("Range")
        self.requests_files = getattr(self, "requests_files", [])
        self.requests_files.append(f.url)
        if rng:
            self.range_requests.append((f.url, rng))
        if rng and not faults.ignore_range:
            start = int(rng.split("=")[1].split("-")[0])
            if start >= len(data):
                return web.Response(status=416, headers={"Content-Range": f"bytes */{len(data)}"})
            if faults.range_restart_206:
                start = 0
            elif faults.range_wrong_start:
                start = max(0, start - faults.range_wrong_start)
            status = 206
        body = data[start:]
        declared = len(body)
        if faults.wrong_length:
            body = body[: max(0, len(body) - 10)]
        resp = web.StreamResponse(status=status)
        if not faults.hide_length:
            resp.content_length = declared
        if status == 206:
            total = "*" if faults.hide_length else str(len(data))
            resp.headers["Content-Range"] = f"bytes {start}-{len(data) - 1}/{total}"
        await resp.prepare(request)
        limit = faults.drop_after
        if limit is not None and f.url not in faults.drops_done and len(body) > limit:
            faults.drops_done.add(f.url)
            await resp.write(body[:limit])
            assert request.transport is not None
            request.transport.abort()
            return resp
        for cut, mode in ((faults.eof_after, "eof"), (faults.stall_after, "stall")):
            if cut is not None and f.url not in faults.cuts_done and len(body) > cut:
                faults.cuts_done.add(f.url)
                await resp.write(body[:cut])
                if mode == "stall":
                    # Send nothing more until the client gives up (its read timeout).
                    transport = request.transport
                    for _ in range(600):
                        if transport is None or transport.is_closing():
                            return resp
                        await asyncio.sleep(0.05)
                await resp.write_eof()
                return resp
        chunk = 16 * 1024
        for i in range(0, len(body), chunk):
            await resp.write(body[i : i + chunk])
            await asyncio.sleep(faults.chunk_delay)
        if faults.wrong_length:
            assert request.transport is not None
            request.transport.abort()
        else:
            await resp.write_eof()
        return resp

    # -------------------------------------------------------------- novatek

    async def _novatek(self, request: web.Request) -> web.StreamResponse:
        if request.query.get("custom") != "1":
            return web.Response(text="<html><body>camera</body></html>", content_type="text/html")
        cmd = request.query.get("cmd", "")
        par = request.query.get("par", "")
        status = "0"
        extra = ""
        if cmd == "3016":
            self.heartbeats += 1
        elif cmd == "3001":
            if par == "2" and self.faults.refuse_playback:
                status = "-1"
            else:
                self.mode = "playback" if par == "2" else "movie"
        elif cmd == "2001":
            if par == "0":
                self.recording = False
            elif par == "1":
                if self.fail_restore:
                    return web.Response(status=500)
                self.recording = True
                self.restore_calls += 1
        elif cmd == "3015":
            if self.faults.list_error:
                return web.Response(status=500)
            if self.mode != "playback":
                status = "-256"
            else:
                items = "".join(
                    f"<File><NAME>{f.path.rsplit(chr(92), 1)[-1]}</NAME><FPATH>{f.path}</FPATH>"
                    f"<SIZE>{f.list_size if f.list_size is not None else len(f.data)}</SIZE><TIMECODE>0</TIMECODE><TIME>{f.time}</TIME>"
                    f"<ATTR>{f.attr}</ATTR></File>"
                    for f in self.files
                )
                body = (
                    '<?xml version="1.0" encoding="UTF-8" ?><LIST><ALLFile>'
                    f"{items}</ALLFile></LIST>"
                )
                return web.Response(text=body, content_type="text/xml")
        elif cmd == "3017":
            extra = "<Value>8000000000</Value>"
        elif cmd == "3012":
            extra = "<String>FW-1.2.3</String>"
        elif cmd == "4003":
            self.deleted.append(request.query.get("str", ""))
            self.files = [f for f in self.files if f.path != request.query.get("str")]
        body = (
            f'<?xml version="1.0" encoding="UTF-8" ?><Function><Cmd>{cmd}</Cmd>'
            f"<Status>{status}</Status>{extra}</Function>"
        )
        return web.Response(text=body, content_type="text/xml")

    # -------------------------------------------------------------- viidure

    async def _viidure(self, request: web.Request) -> web.StreamResponse:
        if self.sgk:
            sgk = self._sgk(request)
            if sgk is not None:
                return sgk
        if request.path == "/app/getproductinfo":
            return web.json_response(
                {"result": 0, "info": {"model": "V-100", "softver": "1.0.9", "company": "viidure"}}
            )
        if request.path == "/app/getsdinfo":  # MiB, like the real cameras
            return web.json_response({"result": 0, "info": {"free": 4768, "total": 30436}})
        if request.path == "/app/playback":
            self.mode = "playback" if request.query.get("param") == "enter" else "movie"
            if self.mode == "movie":
                self.restore_calls += 0
            return web.json_response({"result": 0})
        if request.path == "/app/setparamvalue":
            self.recording = request.query.get("value") == "1"
            if self.recording:
                self.restore_calls += 1
            return web.json_response({"result": 0})
        if request.path == "/app/getfilelist":
            folder = request.query.get("folder", "")
            start = int(request.query.get("start", "0"))
            end = int(request.query.get("end", "99"))
            files = [f for f in self.files if getattr(f, "folder", "") == folder]
            page = files[start : end + 1]
            return web.json_response(
                {
                    "result": 0,
                    "info": [
                        {
                            "folder": folder,
                            "count": len(page),
                            "files": [
                                {"name": f.path, "size": len(f.data), "time": int(f.time)}
                                for f in page
                            ],
                        }
                    ],
                }
            )
        return web.Response(status=404)

    def _sgk(self, request: web.Request) -> web.StreamResponse | None:
        """What differs on SGK firmware; ``None`` falls through to the common answers."""
        path, folder = request.path, request.query.get("folder", "")
        if path == "/app/getproductinfo":
            return web.json_response(
                {"result": 0, "info": {"model": "DVR", "company": "SGK", "soc": "GK720X"}}
            )
        if path == "/app/getdeviceattr":
            return web.json_response(
                {
                    "result": 0,
                    "info": {"softver": "V7_X83_260126", "ssid": "CARDV-5A3F", "camnum": 1},
                }
            )
        if path == "/app/getmediainfo":
            return web.json_response(
                {"result": 0, "info": {"rtsp": "rtsp://192.168.0.1/live/tcp/ch1", "port": "6035"}}
            )
        if path == "/app/getsdinfo":
            return web.json_response(
                {"result": 0, "info": {"status": 0, "free": 109450, "total": 119217}}
            )
        if path in ("/app/playback", "/app/setparamvalue", "/app/getrecduration"):
            return web.Response(status=404)
        if path == "/app/getfilelist" and folder == "event":
            return web.json_response({"result": -1})
        if path == "/app/getfilelist" and folder == "emr":
            folder = "photo"  # the real camera answers a folder it lacks with the photos
        if path == "/app/getfilelist" and folder == "park":
            return web.Response(status=self.park_status)
        if path == "/app/getfilelist":
            start = int(request.query.get("start", "0"))
            end = int(request.query.get("end", "99"))
            files = [f for f in self.files if getattr(f, "folder", "") == folder][start : end + 1]
            listing = [
                {
                    "name": f.url.rsplit("/", 1)[-1],
                    "path": f.path,
                    # Like the real camera: kB for videos, an unrelated number for photos.
                    "size": 73400 if f.path.endswith(".jpg") else len(f.data) // 1000,
                    "createtime": int(f.time),
                }
                for f in files
            ]
            return web.json_response({"result": 0, "info": [{"folder": folder, "files": listing}]})
        return None

    # ---------------------------------------------------------------- index

    def _all_folders(self) -> set[str]:
        out: set[str] = set()
        for f in self.files:
            parts = f.url.split("/")[1:-1]
            for i in range(1, len(parts) + 1):
                out.add("/" + "/".join(parts[:i]) + "/")
        return out

    def _index(self, request: web.Request) -> web.StreamResponse:
        path = request.path
        folders = sorted({f.url.rsplit("/", 1)[0] + "/" for f in self.files})
        if path == "/":
            links = "".join(f'<a href="{d}">{d}</a><br>' for d in folders if d.count("/") == 2)
            links += "".join(
                f'<a href="{d}">{d}</a><br>'
                for d in self._all_folders()
                if d.count("/") == 2 and d not in folders
            )
            html = f"<html><title>Index of /</title><body><h1>Index of /</h1>{links}</body></html>"
            return web.Response(text=html, content_type="text/html")
        if path in self._all_folders():
            links = "".join(
                f'<a href="{d}">{d}</a><br>'
                for d in self._all_folders()
                if d != path and d.startswith(path) and d.count("/") == path.count("/") + 1
            )
            links += "".join(
                f'<a href="{f.url}">{f.url.rsplit("/", 1)[-1]}</a><br>'
                for f in self.files
                if f.url.rsplit("/", 1)[0] + "/" == path
            )
            return web.Response(
                text=f"<html><body><h1>Index of {path}</h1>{links}</body></html>",
                content_type="text/html",
            )
        return web.Response(status=404)

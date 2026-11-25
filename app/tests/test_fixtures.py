"""Replay every recorded device profile against every driver.

A new driver must claim its own family's fixture and must not claim anyone
else's. This is how false positives are caught in CI.
"""

from __future__ import annotations

import base64
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import aiohttp
import pytest
import pytest_asyncio
from aiohttp import web

from dashcam_sync.detect import detect
from dashcam_sync.drivers import DRIVERS
from dashcam_sync.drivers.http import CameraHttp
from dashcam_sync.errors import UnsupportedCamera
from dashcam_sync.profile import PROBE_URLS, capture_profile, redact

FIXTURES = Path(__file__).parent / "fixtures"
FAMILIES = ["novatek", "viidure_app", "http_index", "unsupported"]


class Replay:
    def __init__(self, bundle: dict[str, Any]) -> None:
        self.responses: dict[str, Any] = bundle["responses"]
        self.runner: web.AppRunner | None = None
        self.host = ""

    async def start(self) -> None:
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", self._handle)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.host = f"127.0.0.1:{site._server.sockets[0].getsockname()[1]}"  # type: ignore[union-attr]

    async def stop(self) -> None:
        if self.runner:
            await self.runner.cleanup()

    async def _handle(self, request: web.Request) -> web.StreamResponse:
        entry = self.responses.get(f"GET {request.path_qs}")
        if not entry or "status" not in entry:
            return web.Response(status=404, text="not found")
        headers = {
            k: v for k, v in entry.get("headers", {}).items() if k.lower() in {"content-type"}
        }
        body = (
            entry["text"].encode()
            if "text" in entry
            else base64.b64decode(entry.get("base64_head", ""))
        )
        return web.Response(status=entry["status"], body=body, headers=headers)


@pytest_asyncio.fixture(params=FAMILIES)
async def replay(request: pytest.FixtureRequest) -> AsyncIterator[tuple[str, Replay]]:
    bundle = json.loads((FIXTURES / f"{request.param}.json").read_text())
    server = Replay(bundle)
    await server.start()
    yield request.param, server
    await server.stop()


async def test_each_driver_claims_only_its_own_family(
    replay: tuple[str, Replay], session: aiohttp.ClientSession
) -> None:
    family, server = replay
    claimed = []
    for cls in DRIVERS:
        driver = cls(CameraHttp(session))
        if await driver.probe(server.host) is not None:
            claimed.append(cls.name)
    if family == "unsupported":
        assert claimed == []
    elif family == "http_index":
        assert claimed == ["http_index"]
    else:
        # The generic index driver may also match, but the specific one comes first
        # and no *other* specific driver may claim this family.
        assert family in claimed
        assert [c for c in claimed if c not in (family, "http_index")] == []


async def test_detection_picks_the_specific_driver(
    replay: tuple[str, Replay], session: aiohttp.ClientSession
) -> None:
    family, server = replay
    if family == "unsupported":
        with pytest.raises(UnsupportedCamera):
            await detect(session, [server.host])
        return
    detection = await detect(session, [server.host])
    assert detection.probe.driver == family


def test_fixtures_contain_no_private_data() -> None:
    for path in FIXTURES.glob("*.json"):
        text = path.read_text()
        assert "set-cookie" not in text.lower()
        assert not any(len(part) == 17 and part.count(":") == 5 for part in text.split('"'))


def test_redaction() -> None:
    text = (
        '{"ssid":"HomeWifi","password":"hunter2","mac":"AA:BB:CC:DD:EE:FF",'
        '"ip":"192.168.1.254","other":"10.0.0.7"} <SSID>CarCam</SSID> '
        "<Passphrase>12345678</Passphrase> /set?ssid=Home&pwd=secret&x=1"
    )
    out = redact(text, "192.168.1.254")
    for secret in ("HomeWifi", "hunter2", "AA:BB:CC", "10.0.0.7", "CarCam", "12345678", "secret"):
        assert secret not in out
    assert "192.168.1.254" in out and "x=1" in out and "REDACTED" in out


async def test_capture_profile_is_read_only_and_bounded(session: aiohttp.ClientSession) -> None:
    seen: list[tuple[str, str]] = []

    async def handler(request: web.Request) -> web.StreamResponse:
        seen.append((request.method, request.path_qs))
        if request.path == "/":
            return web.Response(
                body=b"<html>" + b"A" * 200_000,
                content_type="text/html",
                headers={"Set-Cookie": "sid=abc"},
            )
        if request.path == "/index.html":
            return web.Response(body=bytes(range(256)) * 4, content_type="application/octet-stream")
        return web.Response(status=404)

    app = web.Application()
    app.router.add_route("*", "/{t:.*}", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    try:
        bundle = await capture_profile(session, f"127.0.0.1:{port}")
    finally:
        await runner.cleanup()
    assert {m for m, _ in seen} == {"GET"}
    assert len(seen) == len(PROBE_URLS)
    root = bundle["responses"]["GET /"]
    assert len(root["text"]) <= 64 * 1024 and "Set-Cookie" not in root["headers"]
    assert "base64_head" in bundle["responses"]["GET /index.html"]
    assert bundle["profile_version"] == 1 and bundle["ports"]

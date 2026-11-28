"""The real API client against a small aiohttp server."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import aiohttp
import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestServer

from custom_components.dashcam_sync.api import (
    DashcamSyncApi,
    DashcamSyncAuthError,
    DashcamSyncConnectionError,
    DashcamSyncRequestError,
)
from custom_components.dashcam_sync.const import INTEGRATION_HEADER

pytestmark = pytest.mark.usefixtures("socket_enabled")  # real localhost sockets

TOKEN = "secret-token"


@pytest_asyncio.fixture
async def server() -> AsyncIterator[tuple[TestServer, list[tuple[str, str, Any]]]]:
    seen: list[tuple[str, str, Any]] = []

    @web.middleware
    async def auth(request: web.Request, handler: Any) -> web.StreamResponse:
        if request.headers.get("Authorization") != f"Bearer {TOKEN}":
            return web.json_response({"error": "no"}, status=401)
        return await handler(request)  # type: ignore[no-any-return]

    async def generic(request: web.Request) -> web.Response:
        body = await request.json() if request.can_read_body else None
        seen.append((request.method, request.path_qs, body))
        path = request.path
        if path.endswith("/info"):
            return web.json_response(
                {"api_version": 1, "integration": request.headers.get(INTEGRATION_HEADER)}
            )
        if path.endswith("/status"):
            return web.json_response({"cameras": {}})
        if path.endswith("/clips"):
            return web.json_response([{"id": 1}])
        if path.endswith("/clip-days"):
            return web.json_response({"camera": "c", "days": ["2026-11-22"]})
        if path.endswith("/sessions"):
            return web.json_response([{"id": 1}])
        if path.endswith("/boom"):
            return web.json_response({"error": "kaboom"}, status=500)
        if path.endswith("/plain"):
            return web.Response(text="not json", status=502)
        if path.endswith("/forbidden"):
            return web.json_response({}, status=403)
        return web.json_response({"ok": True, "path": path})

    async def ws(request: web.Request) -> web.StreamResponse:
        sock = web.WebSocketResponse()
        await sock.prepare(request)
        seen.append(("WS", request.path, request.headers.get(INTEGRATION_HEADER)))
        await sock.send_json({"type": "snapshot"})
        await sock.send_str("not-json-object")  # ignored: not a JSON object
        await sock.send_json(["list"])
        await sock.send_json({"type": "camera"})
        await sock.close()
        return sock

    app = web.Application(middlewares=[auth])
    app.router.add_get("/api/v1/ws", ws)
    app.router.add_route("*", "/api/v1/{tail:.*}", generic)
    srv = TestServer(app)
    await srv.start_server()
    yield srv, seen
    await srv.close()


@pytest_asyncio.fixture
async def api(
    server: tuple[TestServer, list[tuple[str, str, Any]]],
) -> AsyncIterator[DashcamSyncApi]:
    srv, _ = server
    async with aiohttp.ClientSession() as session:
        yield DashcamSyncApi(session, "127.0.0.1", srv.port, TOKEN)


async def test_reads(
    api: DashcamSyncApi, server: tuple[TestServer, list[tuple[str, str, Any]]]
) -> None:
    _, seen = server
    assert (await api.async_info())["api_version"] == 1
    assert await api.async_status() == {"cameras": {}}
    assert await api.async_clips(camera="c", kind=None, limit=5) == [{"id": 1}]
    assert ("GET", "/api/v1/clips?camera=c&limit=5", None) in seen  # None filters are dropped
    assert (await api.async_clip(3))["path"].endswith("/clips/3")
    assert await api.async_clip_days("c") == ["2026-11-22"]
    assert await api.async_sessions(10) == [{"id": 1}]
    assert (await api.async_options())["ok"] is True
    assert (await api.async_wifi("c"))["path"].endswith("/cameras/c/wifi")


async def test_actions(
    api: DashcamSyncApi, server: tuple[TestServer, list[tuple[str, str, Any]]]
) -> None:
    _, seen = server
    await api.async_sync("c")
    await api.async_restore("c")
    await api.async_patch_camera("c", enabled=False)
    assert (await api.async_overlap("c", ["1.2.3.4"], probe=False))["ok"]
    await api.async_hint("c")
    await api.async_hint()
    await api.async_keep(4, True)
    await api.async_keep(4, False)
    assert (await api.async_delete(4))["ok"]
    assert (await api.async_purge(dry_run=True))["ok"]
    assert [(m, p) for m, p, _ in seen] == [
        ("POST", "/api/v1/cameras/c/sync"),
        ("POST", "/api/v1/cameras/c/restore"),
        ("PATCH", "/api/v1/cameras/c"),
        ("POST", "/api/v1/cameras/c/overlap"),
        ("POST", "/api/v1/hint"),
        ("POST", "/api/v1/hint"),
        ("POST", "/api/v1/clips/4/keep"),
        ("POST", "/api/v1/clips/4/unkeep"),
        ("DELETE", "/api/v1/clips/4"),
        ("POST", "/api/v1/purge"),
    ]
    assert seen[3][2] == {"known_device_ips": ["1.2.3.4"], "probe": False}
    assert seen[4][2] == {"camera": "c"} and seen[5][2] == {}
    assert seen[-1][2] == {"dry_run": True}


async def test_errors(server: tuple[TestServer, list[tuple[str, str, Any]]]) -> None:
    srv, _ = server
    async with aiohttp.ClientSession() as session:
        bad = DashcamSyncApi(session, "127.0.0.1", srv.port, "wrong")
        with pytest.raises(DashcamSyncAuthError):
            await bad.async_info()
        good = DashcamSyncApi(session, "127.0.0.1", srv.port, TOKEN)
        with pytest.raises(DashcamSyncRequestError, match="kaboom") as err:
            await good._request("GET", "boom")
        assert err.value.status == 500
        with pytest.raises(DashcamSyncRequestError):
            await good._request("GET", "plain")
        with pytest.raises(DashcamSyncAuthError):
            await good._request("GET", "forbidden")
        dead = DashcamSyncApi(session, "127.0.0.1", 1, TOKEN)
        with pytest.raises(DashcamSyncConnectionError):
            await dead.async_status()


async def test_websocket_messages(api: DashcamSyncApi) -> None:
    messages = [m async for m in api.async_messages()]
    assert messages == [{"type": "snapshot"}, {"type": "camera"}]  # non-objects are skipped


async def test_websocket_errors(server: tuple[TestServer, list[tuple[str, str, Any]]]) -> None:
    srv, _ = server
    async with aiohttp.ClientSession() as session:
        with pytest.raises(DashcamSyncAuthError):
            async for _ in DashcamSyncApi(session, "127.0.0.1", srv.port, "wrong").async_messages():
                pass
        with pytest.raises(DashcamSyncConnectionError):
            async for _ in DashcamSyncApi(session, "127.0.0.1", 1, TOKEN).async_messages():
                pass


async def test_the_integration_says_which_release_it_is(
    server: tuple[TestServer, list[tuple[str, str, Any]]],
) -> None:
    """The app's panel shows whether the integration is connected, and the same release."""
    srv, seen = server
    async with aiohttp.ClientSession() as session:
        named = DashcamSyncApi(session, "127.0.0.1", srv.port, TOKEN, "2026.11.0b2")
        assert (await named.async_info())["integration"] == "2026.11.0b2"
        async for _message in named.async_messages():
            break
        assert ("WS", "/api/v1/ws", "2026.11.0b2") in seen  # the push socket says it too
        unnamed = DashcamSyncApi(session, "127.0.0.1", srv.port, TOKEN)
        assert (await unnamed.async_info())["integration"] is None

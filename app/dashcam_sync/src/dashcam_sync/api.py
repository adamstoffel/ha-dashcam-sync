"""REST and WebSocket API (port 8100) and the ingress UI server (port 8099).

The same route table is served twice: on 8100 behind a bearer token for the
integration, and on 8099 for the ingress panel, where only the Supervisor's
ingress proxy may connect and Home Assistant has already done the login.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import aiohttp
from aiohttp import web
from pydantic import ValidationError

from . import API_VERSION, __version__
from .catalog import TARGET_LOCAL, TARGET_NETWORK
from .detect import detect
from .errors import ConfigError, DashcamError, UnsupportedCamera
from .hostcheck import validate_camera_host
from .models import Kind
from .options import CameraOptions, StorageOptions
from .profile import capture_profile
from .storage import attached_storage
from .supervisor import SupervisorError

if TYPE_CHECKING:
    from .daemon import Daemon

_LOGGER = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).parent / "ui_static"
DAEMON = web.AppKey("daemon", object)
SECRETS = web.AppKey("expose_secrets", bool)
SOCKETS: web.AppKey[Any] = web.AppKey("sockets", object)
API = "/api/v1"


def _daemon(request: web.Request) -> Daemon:
    return cast("Daemon", request.app[DAEMON])


def _error(status: int, message: str, **extra: Any) -> web.Response:
    return web.json_response({"error": message, **extra}, status=status)


def _invalid(err: ValueError) -> str:
    """A readable first problem from a validation error: ``field: reason``."""
    if isinstance(err, ValidationError) and err.errors():
        first = err.errors()[0]
        where = ".".join(str(p) for p in first.get("loc", ()))
        reason = str(first.get("msg", "invalid")).removeprefix("Value error, ")
        return f"{where}: {reason}" if where else reason
    return str(err).splitlines()[0]


async def _json(request: web.Request) -> dict[str, Any]:
    if not request.can_read_body:
        return {}
    try:
        data = await request.json()
    except ValueError as err:
        raise web.HTTPBadRequest(
            text=json.dumps({"error": "invalid JSON"}), content_type="application/json"
        ) from err
    if not isinstance(data, dict):
        raise web.HTTPBadRequest(
            text=json.dumps({"error": "expected an object"}), content_type="application/json"
        )
    return data


# --------------------------------------------------------------- middlewares


def token_middleware(token: str) -> Any:
    @web.middleware
    async def middleware(request: web.Request, handler: Any) -> web.StreamResponse:
        if request.path == "/healthz":
            return await handler(request)  # type: ignore[no-any-return]
        header = request.headers.get("Authorization", "")
        given = header[7:] if header.startswith("Bearer ") else ""
        if not given or not hmac.compare_digest(given.encode(), token.encode()):
            return _error(401, "invalid or missing token")
        return await handler(request)  # type: ignore[no-any-return]

    return middleware


def ingress_middleware(allowed: tuple[str, ...]) -> Any:
    @web.middleware
    async def middleware(request: web.Request, handler: Any) -> web.StreamResponse:
        if request.path == "/healthz":
            return await handler(request)  # type: ignore[no-any-return]
        if request.remote not in allowed:
            return _error(403, "ingress only")
        return await handler(request)  # type: ignore[no-any-return]

    return middleware


@web.middleware
async def errors_middleware(request: web.Request, handler: Any) -> web.StreamResponse:
    try:
        return await handler(request)  # type: ignore[no-any-return]
    except web.HTTPException:
        raise
    except ConfigError as err:
        return _error(400, str(err))
    except DashcamError as err:
        return _error(502, str(err))


# ------------------------------------------------------------------ handlers


async def healthz(request: web.Request) -> web.Response:
    return web.json_response({"ok": True, "version": __version__})


async def info(request: web.Request) -> web.Response:
    d = _daemon(request)
    return web.json_response(
        {
            "api_version": API_VERSION,
            "app_version": __version__,
            "name": "Dashcam Sync",
            "instance": d.instance_id,
            "cameras": len(d.workers),
        }
    )


async def status(request: web.Request) -> web.Response:
    return web.json_response(await _daemon(request).status_with_rev())


async def websocket(request: web.Request) -> web.StreamResponse:
    d = _daemon(request)
    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(request)
    request.app[SOCKETS].add(ws)
    queue = d.bus.subscribe()
    await ws.send_json({"type": "snapshot", **await d.status_with_rev()})

    async def pump() -> None:
        while True:
            event = await queue.get()
            await ws.send_json(event)

    task = asyncio.create_task(pump())
    try:
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.TEXT and msg.data == "snapshot":
                await ws.send_json({"type": "snapshot", **await d.status_with_rev()})
            elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE):
                break
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
        d.bus.unsubscribe(queue)
        request.app[SOCKETS].discard(ws)
    return ws


def _redacted_camera(cam: CameraOptions, expose: bool) -> dict[str, Any]:
    data = cam.model_dump()
    if not expose:
        data["wifi_password"] = "********" if cam.wifi_password else None
    return data


async def cameras(request: web.Request) -> web.Response:
    d = _daemon(request)
    expose = request.app[SECRETS]
    out = []
    for cam in d.options.cameras:
        worker = d.workers.get(cam.id)
        out.append(
            {
                "options": _redacted_camera(cam, expose),
                "status": worker.status.as_dict() if worker else None,
            }
        )
    return web.json_response(out)


def _camera(request: web.Request) -> tuple[Daemon, CameraOptions]:
    d = _daemon(request)
    cam = d.options.camera(request.match_info["camera"])
    if cam is None:
        raise web.HTTPNotFound(
            text=json.dumps({"error": "unknown camera"}), content_type="application/json"
        )
    return d, cam


async def camera_get(request: web.Request) -> web.Response:
    d, cam = _camera(request)
    worker = d.workers.get(cam.id)
    return web.json_response(
        {
            "options": _redacted_camera(cam, request.app[SECRETS]),
            "status": worker.status.as_dict() if worker else None,
            "overlap": d.overlap[cam.id].as_dict() if cam.id in d.overlap else None,
            "sessions": d.cat.recent_sessions(10, cam.id),
        }
    )


async def camera_sync(request: web.Request) -> web.Response:
    d, cam = _camera(request)
    worker = d.workers.get(cam.id)
    if worker is None or not cam.enabled or not worker.status.connected:
        return _error(409, "camera is not connected or is paused")
    worker.request_sync()
    return web.json_response({"ok": True})


async def camera_restore(request: web.Request) -> web.Response:
    d, cam = _camera(request)
    worker = d.workers.get(cam.id)
    if worker is None:
        return _error(404, "unknown camera")
    worker.request_restore()
    return web.json_response({"ok": True})


async def camera_patch(request: web.Request) -> web.Response:
    d, cam = _camera(request)
    body = await _json(request)
    allowed = {"enabled", "sync_mode", "name"}
    unknown = set(body) - allowed
    if unknown:
        return _error(400, f"cannot change {sorted(unknown)}")
    try:  # validate the merged entry: a bad value must not make the camera vanish
        CameraOptions.model_validate({**cam.model_dump(exclude_none=True), **body})
    except ValueError as err:
        return _error(400, _invalid(err))

    def mutate(raw: dict[str, Any]) -> None:
        for entry in raw.get("cameras", []):
            if entry.get("id") == cam.id:
                entry.update(body)

    await d.update_raw_options(mutate)
    return web.json_response({"ok": True})


async def camera_delete(request: web.Request) -> web.Response:
    d, cam = _camera(request)

    def mutate(raw: dict[str, Any]) -> None:
        raw["cameras"] = [c for c in raw.get("cameras", []) if c.get("id") != cam.id]

    await d.update_raw_options(mutate)
    return web.json_response({"ok": True})


async def camera_wifi(request: web.Request) -> web.Response:
    """Hotspot credentials and the address block, for the integration only."""
    d, cam = _camera(request)
    if not request.app[SECRETS]:
        return _error(403, "credentials are only served on the token API")
    if cam.connection != "hotspot":
        return _error(400, "not a hotspot camera")
    block = d.block_for(cam)
    return web.json_response(
        {
            "interface": cam.interface,
            "ssid": cam.ssid,
            "password": cam.wifi_password,
            "static_address": block.static_address,
            "block": str(block.network),
            "camera": str(block.camera),
            "ipv4": cam.effective_ipv4,
        }
    )


async def camera_overlap(request: web.Request) -> web.Response:
    d, cam = _camera(request)
    body = await _json(request)
    known = body.get("known_device_ips")
    if known is not None and not isinstance(known, list):
        return _error(400, "known_device_ips must be a list")
    report = await d.run_overlap_check(
        cam.id,
        known_device_ips=[str(x) for x in known] if known is not None else None,
        probe=bool(body.get("probe", True)),
    )
    return web.json_response(report.as_dict())


async def camera_overlap_get(request: web.Request) -> web.Response:
    d, cam = _camera(request)
    report = d.overlap.get(cam.id)
    if report is None:
        report = await d.run_overlap_check(cam.id, probe=False)
    return web.json_response(report.as_dict())


async def hint(request: web.Request) -> web.Response:
    d = _daemon(request)
    body = await _json(request)
    target = body.get("camera")
    count = 0
    for cam_id, worker in d.workers.items():
        if target in (None, cam_id):
            worker.arrival_hint()
            count += 1
    return web.json_response({"ok": True, "cameras": count})


async def _camera_host(d: Daemon, host: str) -> str:
    return await validate_camera_host(host, allow_loopback=d.config.allow_loopback_cameras)


def _clip_filters(request: web.Request) -> dict[str, Any]:
    q = request.query
    kind = None
    if q.get("kind"):
        try:
            kind = Kind(q["kind"])
        except ValueError as err:
            raise web.HTTPBadRequest(
                text=json.dumps({"error": "unknown kind"}), content_type="application/json"
            ) from err
    try:
        limit = min(int(q.get("limit", "200")), 1000)
        offset = int(q.get("offset", "0"))
    except ValueError as err:
        raise web.HTTPBadRequest(
            text=json.dumps({"error": "limit and offset must be numbers"}),
            content_type="application/json",
        ) from err
    return {
        "camera": q.get("camera"),
        "kind": kind,
        "day": q.get("day"),
        "limit": limit,
        "offset": offset,
    }


async def clips(request: web.Request) -> web.Response:
    d = _daemon(request)
    try:
        rows = d.cat.list_clips(**_clip_filters(request))
    except ValueError:
        return _error(400, "day must be YYYY-MM-DD")
    return web.json_response([d.clip_payload(r.id) for r in rows])


async def clip_days(request: web.Request) -> web.Response:
    d = _daemon(request)
    camera = request.query.get("camera")
    if not camera:
        return _error(400, "camera is required")
    return web.json_response({"camera": camera, "days": d.cat.clip_days(camera)})


def _clip_id(request: web.Request) -> int:
    try:
        return int(request.match_info["clip"])
    except ValueError as err:
        raise web.HTTPNotFound(
            text=json.dumps({"error": "unknown clip"}), content_type="application/json"
        ) from err


async def clip_get(request: web.Request) -> web.Response:
    payload = _daemon(request).clip_payload(_clip_id(request))
    if payload is None:
        return _error(404, "unknown clip")
    return web.json_response(payload)


async def clip_keep(request: web.Request) -> web.Response:
    d = _daemon(request)
    keep = request.path.endswith("/keep")
    if not d.cat.set_keep(_clip_id(request), keep):
        return _error(404, "unknown clip")
    d.bus.publish("clip_keep", clip_id=_clip_id(request), keep=keep)
    return web.json_response({"ok": True, "keep": keep})


async def clip_delete(request: web.Request) -> web.Response:
    d = _daemon(request)
    clip_id = _clip_id(request)
    if d.cat.get_clip(clip_id) is None:
        return _error(404, "unknown clip")
    freed = await d.delete_clip(clip_id)
    d.bus.publish("clip_deleted", clip_id=clip_id)
    return web.json_response({"ok": True, "bytes_freed": freed})


def _clip_file(d: Daemon, clip_id: int, suffix: str | None) -> Path | None:
    """The best existing copy of a clip (or its thumbnail), from the catalogue only."""
    payload = d.clip_payload(clip_id)
    if payload is None or not payload["path"]:
        return None
    path = Path(payload["path"])
    if suffix:
        path = path.with_suffix(suffix)
    roots = (d.config.media_root.resolve(), d.config.share_root.resolve())
    try:
        resolved = path.resolve(strict=True)
    except OSError:
        return None
    if not any(resolved.is_relative_to(root) for root in roots):
        return None
    return resolved


async def clip_file(request: web.Request) -> web.StreamResponse:
    path = _clip_file(_daemon(request), _clip_id(request), None)
    if path is None:
        return _error(404, "file not found")
    return web.FileResponse(path)


async def clip_thumb(request: web.Request) -> web.StreamResponse:
    path = _clip_file(_daemon(request), _clip_id(request), ".jpg")
    if path is None:
        return _error(404, "no thumbnail")
    return web.FileResponse(path)


async def purge(request: web.Request) -> web.Response:
    d = _daemon(request)
    body = await _json(request)
    report = await d.run_purge(dry_run=bool(body.get("dry_run", False)))
    return web.json_response(report.as_dict())


async def storage(request: web.Request) -> web.Response:
    return web.json_response(await asyncio.to_thread(_daemon(request).storage_payload))


LOCATION_FIELDS = ("target", "local_folder", "network_folder")


async def storage_location_get(request: web.Request) -> web.Response:
    """The current location settings and the disks and shares that can be chosen."""
    d = _daemon(request)
    opts = d.options.storage
    attached = await asyncio.to_thread(
        attached_storage, d.config.media_root, d.config.share_root, d.config.mountinfo
    )
    return web.json_response(
        {
            "current": {k: getattr(opts, k) for k in LOCATION_FIELDS},
            "attached": attached,
        }
    )


async def storage_location_set(request: web.Request) -> web.Response:
    """Change where new clips go; existing clips are moved separately (``migration``)."""
    d = _daemon(request)
    if d.migration.running:
        return _error(409, "clips are being moved; cancel that first")
    body = await _json(request)
    changes = {k: body[k] for k in LOCATION_FIELDS if k in body}
    if not changes:
        return _error(400, "nothing to change")
    merged = {**d.options.storage.model_dump(), **changes}
    try:
        checked = StorageOptions.model_validate(merged)
    except ValueError as err:
        return _error(400, _invalid(err))
    if checked.target in ("network", "both") and not checked.network_folder:
        return _error(400, "choose a network share or disk")

    def mutate(raw: dict[str, Any]) -> None:
        storage = raw.setdefault("storage", {})
        for key in changes:
            storage[key] = getattr(checked, key)

    await d.update_raw_options(mutate)
    plan = await d.migration_plan()
    return web.json_response(d.migration_payload(plan))


async def migration_get(request: web.Request) -> web.Response:
    d = _daemon(request)
    plan = None if d.migration.running else await d.migration_plan()
    return web.json_response(d.migration_payload(plan))


async def migration_start(request: web.Request) -> web.Response:
    d = _daemon(request)
    try:
        plan = await d.start_migration()
    except ValueError as err:
        return _error(409, str(err))
    return web.json_response(d.migration_payload(plan), status=202)


async def migration_cancel(request: web.Request) -> web.Response:
    d = _daemon(request)
    d.migration.cancel()
    return web.json_response(d.migration_payload())


async def sessions(request: web.Request) -> web.Response:
    d = _daemon(request)
    try:
        limit = max(1, min(int(request.query.get("limit", "50")), 200))
    except ValueError:
        limit = 50
    return web.json_response(d.cat.recent_sessions(limit, request.query.get("camera")))


async def logs(request: web.Request) -> web.Response:
    d = _daemon(request)
    try:
        n = max(1, min(int(request.query.get("lines", "300")), 1000))
    except ValueError:
        n = 300
    return web.json_response({"lines": list(d.logs.lines)[-n:]})


async def options_get(request: web.Request) -> web.Response:
    d = _daemon(request)
    data = d.options.model_dump()
    expose = request.app[SECRETS]
    data["cameras"] = [_redacted_camera(c, expose) for c in d.options.cameras]
    return web.json_response(data)


# ------------------------------------------------------------------- wizard


async def wizard_wifi_scan(request: web.Request) -> web.Response:
    d = _daemon(request)
    interface = request.query.get("interface", "")
    if d.supervisor is None or not d.supervisor.available:
        return web.json_response({"available": False, "accesspoints": []})
    try:
        aps = await d.supervisor.access_points(interface)
    except SupervisorError as err:
        # The default role may not be allowed to scan: the wizard asks for the SSID by hand.
        return web.json_response({"available": False, "accesspoints": [], "reason": str(err)})
    return web.json_response(
        {
            "available": True,
            "accesspoints": [
                {"ssid": ap.get("ssid"), "signal": ap.get("signal"), "mode": ap.get("mode")}
                for ap in aps
            ],
        }
    )


async def wizard_probe(request: web.Request) -> web.Response:
    d = _daemon(request)
    body = await _json(request)
    host = str(body.get("host") or "").strip()
    forced = str(body.get("driver") or "auto")
    from .detect import candidate_hosts

    if host:
        await _camera_host(d, host)
    hosts = [host] if host else candidate_hosts(None)
    assert d.session is not None
    try:
        detection = await detect(d.session, hosts, forced=forced)
    except UnsupportedCamera:
        return web.json_response({"supported": False, "hosts": hosts})
    await detection.driver.close()
    probe = detection.probe
    return web.json_response(
        {
            "supported": True,
            "driver": probe.driver,
            "host": probe.host,
            "model": probe.model,
            "firmware": probe.firmware,
            "confidence": probe.confidence,
            "capabilities": detection.driver.capabilities.as_dict(),
        }
    )


async def wizard_block(request: web.Request) -> web.Response:
    d = _daemon(request)
    body = await _json(request)
    ip = str(body.get("camera_ip") or "")
    from .netblock import compute_block

    block = compute_block(ip)
    homes = d.network.home_networks({c.interface for c in d.options.cameras if c.interface})
    return web.json_response(
        {
            "camera": str(block.camera),
            "block": str(block.network),
            "first": str(block.first),
            "last": str(block.last),
            "home_address": str(block.host),
            "static_address": block.static_address,
            "home_networks": [str(n) for n in homes],
            "overlaps_home_network": any(block.network.overlaps(n) for n in homes),
        }
    )


async def wizard_storage_test(request: web.Request) -> web.Response:
    d = _daemon(request)
    health = await asyncio.to_thread(d.store_.health)
    return web.json_response(
        {
            "targets": {k: v.as_dict() for k, v in health.items()},
            "network_folder_mounted": (
                d.store_.network_mount_ok(d.options.storage.network_folder)
                if d.options.storage.network_folder
                else None
            ),
        }
    )


async def wizard_save_camera(request: web.Request) -> web.Response:
    d = _daemon(request)
    body = await _json(request)
    try:
        cam = CameraOptions.model_validate(body)
    except ValueError as err:
        return _error(400, _invalid(err))
    if cam.host:
        await _camera_host(d, cam.host)
    entry = {k: v for k, v in cam.model_dump().items() if v is not None}
    if cam.connection == "hotspot" and not entry.get("static_address") and cam.ipv4 != "auto":
        ip = d.camera_ip(cam)
        if ip:
            from .netblock import compute_block

            entry["static_address"] = compute_block(ip).static_address

    def mutate(raw: dict[str, Any]) -> None:
        cams = raw.setdefault("cameras", [])
        for i, existing in enumerate(cams):
            if existing.get("id") == cam.id:
                cams[i] = entry
                return
        cams.append(entry)

    await d.update_raw_options(mutate)
    return web.json_response({"ok": True, "camera": entry["id"]})


async def wizard_capture_profile(request: web.Request) -> web.Response:
    d = _daemon(request)
    body = await _json(request)
    host = str(body.get("host") or "").strip()
    if not host:
        return _error(400, "host is required")
    await _camera_host(d, host)
    assert d.session is not None
    bundle = await capture_profile(d.session, host)
    return web.json_response(bundle)


async def wizard_station(request: web.Request) -> web.Response:
    """Novatek: send home Wi-Fi credentials and switch the camera to station mode."""
    d = _daemon(request)
    body = await _json(request)
    host, ssid, password = (str(body.get(k) or "") for k in ("host", "ssid", "password"))
    if not (host and ssid and password):
        return _error(400, "host, ssid and password are required")
    await _camera_host(d, host)
    from .drivers.http import CameraHttp
    from .drivers.novatek import NovatekDriver

    assert d.session is not None
    driver = NovatekDriver(CameraHttp(d.session))
    driver.attach(host)
    if await driver.probe(host) is None:
        return _error(400, "this is not a Novatek camera")
    await driver.move_to_station(ssid, password)
    return web.json_response({"ok": True})


# --------------------------------------------------------------------- apps


async def _close_sockets(app: web.Application) -> None:
    """Close push sockets on shutdown so a connected integration cannot delay the stop."""
    for ws in list(app[SOCKETS]):
        await ws.close(code=aiohttp.WSCloseCode.GOING_AWAY, message=b"shutting down")


def _add_routes(app: web.Application) -> None:
    app[SOCKETS] = set()
    app.on_shutdown.append(_close_sockets)
    r = app.router
    r.add_get("/healthz", healthz)
    r.add_get(f"{API}/info", info)
    r.add_get(f"{API}/status", status)
    r.add_get(f"{API}/ws", websocket)
    r.add_get(f"{API}/cameras", cameras)
    r.add_get(f"{API}/cameras/{{camera}}", camera_get)
    r.add_patch(f"{API}/cameras/{{camera}}", camera_patch)
    r.add_delete(f"{API}/cameras/{{camera}}", camera_delete)
    r.add_post(f"{API}/cameras/{{camera}}/sync", camera_sync)
    r.add_post(f"{API}/cameras/{{camera}}/restore", camera_restore)
    r.add_get(f"{API}/cameras/{{camera}}/wifi", camera_wifi)
    r.add_get(f"{API}/cameras/{{camera}}/overlap", camera_overlap_get)
    r.add_post(f"{API}/cameras/{{camera}}/overlap", camera_overlap)
    r.add_post(f"{API}/hint", hint)
    r.add_get(f"{API}/clips", clips)
    r.add_get(f"{API}/clip-days", clip_days)
    r.add_get(f"{API}/clips/{{clip}}", clip_get)
    r.add_post(f"{API}/clips/{{clip}}/keep", clip_keep)
    r.add_post(f"{API}/clips/{{clip}}/unkeep", clip_keep)
    r.add_delete(f"{API}/clips/{{clip}}", clip_delete)
    r.add_get(f"{API}/clips/{{clip}}/file", clip_file)
    r.add_get(f"{API}/clips/{{clip}}/thumb", clip_thumb)
    r.add_post(f"{API}/purge", purge)
    r.add_get(f"{API}/storage", storage)
    r.add_get(f"{API}/storage/location", storage_location_get)
    r.add_patch(f"{API}/storage/location", storage_location_set)
    r.add_get(f"{API}/storage/migration", migration_get)
    r.add_post(f"{API}/storage/migration", migration_start)
    r.add_delete(f"{API}/storage/migration", migration_cancel)
    r.add_get(f"{API}/logs", logs)
    r.add_get(f"{API}/sessions", sessions)
    r.add_get(f"{API}/options", options_get)
    r.add_get(f"{API}/wizard/wifi-scan", wizard_wifi_scan)
    r.add_post(f"{API}/wizard/probe", wizard_probe)
    r.add_post(f"{API}/wizard/block", wizard_block)
    r.add_get(f"{API}/wizard/storage-test", wizard_storage_test)
    r.add_post(f"{API}/wizard/camera", wizard_save_camera)
    r.add_post(f"{API}/wizard/capture-profile", wizard_capture_profile)
    r.add_post(f"{API}/wizard/station", wizard_station)


def build_api_app(daemon: Daemon) -> web.Application:
    """Integration API: bearer token on every route except /healthz."""
    app = web.Application(middlewares=[token_middleware(daemon.token), errors_middleware])
    app[DAEMON] = daemon
    app[SECRETS] = True
    _add_routes(app)
    return app


def build_ui_app(daemon: Daemon) -> web.Application:
    """Ingress app: static SPA plus the same routes, Supervisor proxy only."""
    app = web.Application(
        middlewares=[ingress_middleware(daemon.config.ingress_allow), errors_middleware]
    )
    app[DAEMON] = daemon
    app[SECRETS] = False
    _add_routes(app)

    async def index(request: web.Request) -> web.StreamResponse:
        return web.FileResponse(STATIC_DIR / "index.html")

    app.router.add_get("/", index)
    app.router.add_static("/static", STATIC_DIR, show_index=False)
    return app


__all__ = ["TARGET_LOCAL", "TARGET_NETWORK", "build_api_app", "build_ui_app"]

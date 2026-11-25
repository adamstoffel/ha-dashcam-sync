"""``dashcam-syncd``: wires every module together and runs until stopped."""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import json
import logging
import os
import secrets
import signal
import socket
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import aiohttp
from aiohttp import web

from . import API_VERSION, __version__
from .catalog import TARGET_LOCAL, TARGET_NETWORK, Catalog
from .detect import candidate_hosts
from .drivers import DRIVER_BY_NAME
from .engine import CameraWorker, SyncContext, Timing
from .errors import ConfigError
from .events import EventBus
from .netblock import (
    CameraBlock,
    OverlapReport,
    block_from_static,
    check_overlap,
    compute_block,
    tcp_probe,
)
from .network import NetworkMonitor, camera_reachable
from .options import (
    AppOptions,
    CameraOptions,
    invalid_camera_entries,
    parse_options,
    validate_cameras,
)
from .postprocess import PostProcessor
from .restoreflag import RestoreFlags
from .retention import PurgeReport, RetentionService, seconds_until
from .storage import StorageManager
from .supervisor import (
    FileOptionsStore,
    OptionsStore,
    SupervisorClient,
    SupervisorError,
    SupervisorOptionsStore,
)

_LOGGER = logging.getLogger(__name__)

NETWORK_POLL = 15.0
NETWORK_POLL_JOINED = 5.0
OVERLAP_RECHECK = 6 * 3600.0
SHIP_INTERVAL = 60.0
NETWORK_OFFLINE_REPAIR_AFTER = 3600.0
OPTIONS_POLL = 2.0


@dataclass(slots=True)
class DaemonConfig:
    """Paths and ports; the defaults match the app container."""

    data_dir: Path = Path("/data")
    media_root: Path = Path("/media")
    share_root: Path = Path("/share")
    options_path: Path | None = None
    api_port: int = 8100
    ui_port: int = 8099
    bind_host: str = "0.0.0.0"
    ingress_allow: tuple[str, ...] = ("172.30.32.2",)
    timing: Timing = field(default_factory=Timing)
    network_poll: float = NETWORK_POLL
    hub_push_interval: float = 30.0
    use_supervisor: bool = True
    mountinfo: str | None = None
    api_token: str | None = None

    def __post_init__(self) -> None:
        if self.options_path is None:
            self.options_path = self.data_dir / "options.json"


class LogRing(logging.Handler):
    """Keeps the last log lines for the ingress Logs page."""

    def __init__(self, size: int = 1000) -> None:
        super().__init__()
        self.lines: deque[str] = deque(maxlen=size)
        self.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s", "%H:%M:%S")
        )

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(self.format(record))


class Daemon:
    def __init__(self, config: DaemonConfig) -> None:
        self.config = config
        self.options = AppOptions()
        self.raw_options: dict[str, Any] = {}
        self.parse_problems: dict[str, str] = {}
        self.validation: dict[str, list[str]] = {}
        self.workers: dict[str, CameraWorker] = {}
        self.bus = EventBus()
        self.logs = LogRing()
        self.token = ""
        self.session: aiohttp.ClientSession | None = None
        self.catalog: Catalog | None = None
        self.storage: StorageManager | None = None
        self.retention: RetentionService | None = None
        self.post: PostProcessor | None = None
        self.flags: RestoreFlags | None = None
        self.network = NetworkMonitor(None)
        self.supervisor: SupervisorClient | None = None
        self.store: OptionsStore | None = None
        self.known_device_ips: list[str] = []
        self.overlap: dict[str, OverlapReport] = {}
        self.overlap_checked: dict[str, float] = {}
        self.route_unsafe: dict[str, str] = {}
        self.last_purge: dict[str, Any] | None = None
        self._tasks: list[asyncio.Task[None]] = []
        self._background: set[asyncio.Task[None]] = set()
        self._runners: list[web.AppRunner] = []
        self._options_mtime = 0.0
        self._stop = asyncio.Event()
        self._started = time.time()
        self.discovery_uuid: str | None = None

    # ------------------------------------------------------------ properties

    @property
    def cat(self) -> Catalog:
        assert self.catalog is not None
        return self.catalog

    @property
    def store_(self) -> StorageManager:
        assert self.storage is not None
        return self.storage

    # ------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        cfg = self.config
        cfg.data_dir.mkdir(parents=True, exist_ok=True)
        logging.getLogger().addHandler(self.logs)
        self.token = self._load_token()
        self.session = aiohttp.ClientSession()
        token = os.environ.get("SUPERVISOR_TOKEN", "")
        if cfg.use_supervisor and token:
            self.supervisor = SupervisorClient(self.session, token)
            self.network = NetworkMonitor(self.supervisor)
            assert cfg.options_path is not None
            self.store = SupervisorOptionsStore(self.supervisor, cfg.options_path)
        else:
            assert cfg.options_path is not None
            self.store = FileOptionsStore(cfg.options_path)
        self.catalog = Catalog(cfg.data_dir / "catalog.db")
        self.flags = RestoreFlags(cfg.data_dir / "restore")
        await self._load_options(initial=True)
        self.post = PostProcessor(
            self.cat,
            lambda: self.options.sync,
            lambda: self.options.storage,
            self._on_post_done,
        )
        self.post.start()
        self._build_storage()
        await self.network.refresh()
        self._sync_workers()

        from .api import build_api_app, build_ui_app

        await self._serve(build_api_app(self), cfg.api_port)
        await self._serve(build_ui_app(self), cfg.ui_port)

        for coro in (
            self._options_loop(),
            self._network_loop(),
            self._overlap_loop(),
            self._retention_loop(),
            self._ship_loop(),
            self._hub_loop(),
        ):
            self._tasks.append(asyncio.create_task(coro))
        await self._announce()
        _LOGGER.info("Dashcam Sync %s started with %d camera(s)", __version__, len(self.workers))

    async def _serve(self, app: web.Application, port: int) -> None:
        runner = web.AppRunner(app, access_log=None, shutdown_timeout=5.0)
        await runner.setup()
        site = web.TCPSite(runner, self.config.bind_host, port)
        await site.start()
        self._runners.append(runner)

    def bound_port(self, index: int) -> int:
        """Actual port of the nth server (for tests that bind port 0)."""
        site = next(iter(self._runners[index].sites))
        server = site._server
        assert isinstance(server, asyncio.Server) and server.sockets
        return int(server.sockets[0].getsockname()[1])

    async def stop(self) -> None:
        """Graceful stop: every camera is restored before the app exits."""
        self._stop.set()
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        await asyncio.gather(*(w.stop() for w in self.workers.values()), return_exceptions=True)
        if self.post is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self.post.stop(), 10)
        for runner in self._runners:
            await runner.cleanup()
        if self.supervisor is not None and self.discovery_uuid:
            with contextlib.suppress(SupervisorError):
                await self.supervisor.delete_discovery(self.discovery_uuid)
        if self.session is not None:
            await self.session.close()
        if self.catalog is not None:
            self.catalog.close()
        logging.getLogger().removeHandler(self.logs)

    async def wait(self) -> None:
        await self._stop.wait()

    def request_stop(self) -> None:
        self._stop.set()

    def _load_token(self) -> str:
        if self.config.api_token:
            return self.config.api_token
        path = self.config.data_dir / "api_token"
        try:
            token = path.read_text("utf-8").strip()
        except FileNotFoundError:
            token = ""
        if len(token) < 32:
            token = secrets.token_urlsafe(32)
            path.write_text(token, "utf-8")
            path.chmod(0o600)
        return token

    async def _announce(self) -> None:
        """Offer ourselves to Core through Supervisor discovery."""
        if self.supervisor is None or not self.supervisor.available:
            return
        host = socket.gethostname()
        try:
            data = await self.supervisor._request("GET", "/addons/self/info")
            host = (data or {}).get("hostname") or host
        except SupervisorError:
            pass
        try:
            self.discovery_uuid = await self.supervisor.post_discovery(
                "dashcam_sync",
                {"host": host, "port": self.config.api_port, "token": self.token},
            )
        except SupervisorError as err:
            _LOGGER.warning("Discovery failed (manual setup still works): %s", err)

    # ---------------------------------------------------------------- options

    async def _load_options(self, initial: bool = False) -> bool:
        assert self.store is not None and self.config.options_path is not None
        path = self.config.options_path
        try:
            mtime = path.stat().st_mtime
        except FileNotFoundError:
            mtime = 0.0
        if not initial and mtime == self._options_mtime:
            return False
        self._options_mtime = mtime
        raw = await self.store.read()
        previous = self.options
        self.raw_options = raw
        self.parse_problems = invalid_camera_entries(raw)
        try:
            self.options = parse_options(raw)
        except ValueError as err:
            _LOGGER.error("Options rejected, keeping the previous ones: %s", err)
            self.options = previous
            return False
        level = getattr(logging, self.options.log_level.upper(), logging.INFO)
        logging.getLogger().setLevel(level)
        if not initial and previous.log_level != self.options.log_level:
            _LOGGER.info("log_level changed: restarting the app")
            if self.supervisor is not None:
                with contextlib.suppress(SupervisorError):
                    await self.supervisor.restart_self()
            else:
                self.request_stop()
        self._revalidate()
        return True

    def _revalidate(self) -> None:
        storage = self.storage
        self.validation = validate_cameras(
            self.options,
            network_mount_ok=storage.network_mount_ok if storage else None,
        )

    def _build_storage(self) -> None:
        self.storage = StorageManager(
            self.cat,
            self.options.storage,
            media_root=self.config.media_root,
            share_root=self.config.share_root,
            min_free_percent=self.options.retention.min_free_percent,
            mountinfo=self.config.mountinfo,
        )
        self.retention = RetentionService(self.cat, self.storage, self.options.retention)
        self._revalidate()

    async def _options_loop(self) -> None:
        while True:
            await asyncio.sleep(OPTIONS_POLL)
            try:
                if await self._load_options():
                    self._build_storage()
                    self._sync_workers()
                    self.overlap_checked.clear()
                    self.bus.publish("options")
            except Exception:
                _LOGGER.exception("Reloading options failed")

    def _sync_workers(self) -> None:
        """Hot reload: add, update and remove camera workers without a restart."""
        wanted = {c.id: c for c in self.options.cameras}
        for cam_id in list(self.workers):
            if cam_id not in wanted:
                removed = self.workers.pop(cam_id)
                task = asyncio.create_task(removed.stop())
                self._background.add(task)
                task.add_done_callback(self._background.discard)
        for cam_id, cam in wanted.items():
            existing = self.workers.get(cam_id)
            if existing is None:
                created = CameraWorker(self._context(), cam)
                self.workers[cam_id] = created
                created.start()
            else:
                existing.update(cam)

    def _context(self) -> SyncContext:
        assert self.session is not None and self.flags is not None and self.post is not None
        return SyncContext(
            session=self.session,
            catalog=self.cat,
            storage=lambda: self.store_,
            bus=self.bus,
            flags=self.flags,
            post=self.post,
            sync_options=lambda: self.options.sync,
            link_up=self._link_up,
            blocked=self.blocked_reasons,
            gateway=self._gateway_for,
            after_session=self._after_session,
            timing=self.config.timing,
        )

    # ------------------------------------------------------------- link state

    async def _link_up(self, cam: CameraOptions) -> bool:
        matches = self.network.link_matches(cam)
        if matches is False:
            return False
        return await camera_reachable(cam, self._gateway_for(cam))

    def _gateway_for(self, cam: CameraOptions) -> str | None:
        iface = self.network.interface(cam.interface) if cam.interface else None
        return iface.gateway if iface else None

    def blocked_reasons(self, camera_id: str) -> list[str]:
        reasons = list(self.validation.get(camera_id, []))
        report = self.overlap.get(camera_id)
        if report is not None and report.blocked:
            reasons.append(f"overlap_{report.result}")
        if camera_id in self.route_unsafe:
            reasons.append("default_route_changed")
        return reasons

    # ----------------------------------------------------------------- hooks

    async def _on_post_done(self, clip: Any, path: Path) -> None:
        assert self.storage is not None
        camera = clip.camera
        thumb = path.with_suffix(".jpg")
        self.bus.publish(
            "clip",
            camera=camera,
            clip=self.clip_payload(clip.id),
            event_clip=clip.kind.value == "event",
            thumbnail=str(thumb) if thumb.exists() else None,
        )
        if clip.kind.value == "event":
            owner = self.workers.get(camera)
            if owner is not None:
                owner.status.last_event_clip = clip.start.isoformat()
        await asyncio.to_thread(self._ship_safely)

    def _ship_safely(self) -> None:
        try:
            if self.storage is not None:
                self.storage.ship_pending()
        except OSError as err:
            _LOGGER.warning("Shipping to the network target failed: %s", err)

    async def _after_session(self) -> None:
        """Retention runs after each sync as well as daily."""
        await self.run_purge(dry_run=False)

    async def run_purge(self, *, dry_run: bool) -> PurgeReport:
        assert self.retention is not None
        report = await asyncio.to_thread(self.retention.run, dry_run=dry_run)
        if not dry_run:
            self.last_purge = {"ts": time.time(), **report.as_dict()}
            self.last_purge.pop("clips", None)
            self.bus.publish(
                "purge_completed", count=len(report.deleted), bytes_freed=report.bytes_freed
            )
        return report

    # ------------------------------------------------------------- background

    async def _network_loop(self) -> None:
        while True:
            await self.network.refresh()
            self._check_route_safety()
            joined = any(self.network.link_matches(c) for c in self.options.cameras if c.enabled)
            await asyncio.sleep(
                min(self.config.network_poll, NETWORK_POLL_JOINED)
                if joined
                else self.config.network_poll
            )

    def _check_route_safety(self) -> None:
        """After joining a hotspot, the default route must still be wired."""
        hotspot_ifaces = {c.interface for c in self.options.cameras if c.interface}
        joined = [c for c in self.options.cameras if c.enabled and self.network.link_matches(c)]
        before = dict(self.route_unsafe)
        route_ok = self.network.default_route_ok({i for i in hotspot_ifaces if i})
        for cam in joined:
            if route_ok is False:
                self.route_unsafe[cam.id] = "default_route_moved"
            else:
                self.route_unsafe.pop(cam.id, None)
        for cam_id in list(self.route_unsafe):
            if cam_id not in {c.id for c in joined}:
                self.route_unsafe.pop(cam_id)
        if before != self.route_unsafe:
            self.bus.publish("safety", route_unsafe=sorted(self.route_unsafe))

    async def verify_router(self) -> bool:
        return await self.network.router_answers()

    async def _overlap_loop(self) -> None:
        while True:
            for cam in self.options.cameras:
                if not cam.enabled or cam.connection != "hotspot":
                    continue
                last = self.overlap_checked.get(cam.id, 0.0)
                if time.time() - last < OVERLAP_RECHECK:
                    continue
                if self.network.link_matches(cam):
                    continue  # the car is here: probing Ethernet would hit the camera
                try:
                    await self.run_overlap_check(cam.id)
                except ConfigError as err:
                    _LOGGER.debug("Overlap check skipped for %s: %s", cam.id, err)
            await asyncio.sleep(30)

    def camera_ip(self, cam: CameraOptions) -> str | None:
        if cam.host:
            return cam.host.rsplit(":", 1)[0] if cam.host.count(":") == 1 else cam.host
        cached = self.cat.kv_get(f"driver:{cam.id}")
        if cached and cached.get("host"):
            return str(cached["host"]).split(":")[0]
        if cam.driver != "auto":
            return (
                DRIVER_BY_NAME[cam.driver].default_hosts[0]
                if (DRIVER_BY_NAME[cam.driver].default_hosts)
                else None
            )
        hosts = candidate_hosts(None)
        return hosts[0] if hosts else None

    def block_for(self, cam: CameraOptions, camera_ip: str | None = None) -> CameraBlock:
        ip = camera_ip or self.camera_ip(cam)
        if not ip:
            raise ConfigError("the camera's address is not known yet")
        if cam.static_address:
            return block_from_static(cam.static_address, ip)
        return compute_block(ip)

    async def run_overlap_check(
        self,
        camera_id: str,
        *,
        known_device_ips: list[str] | None = None,
        probe: bool = True,
    ) -> OverlapReport:
        cam = self.options.camera(camera_id)
        if cam is None:
            raise ConfigError(f"unknown camera {camera_id}")
        if known_device_ips is not None:
            self.known_device_ips = known_device_ips
        block = self.block_for(cam)
        exclude = {c.interface for c in self.options.cameras if c.interface}
        homes = self.network.home_networks({i for i in exclude if i})
        joined = bool(self.network.link_matches(cam))
        report = await check_overlap(
            block,
            homes,
            known_device_ips=self.known_device_ips,
            probe=tcp_probe if (probe and not joined) else None,
        )
        previous = self.overlap.get(camera_id)
        self.overlap[camera_id] = report
        self.overlap_checked[camera_id] = time.time()
        if previous is None or previous.result != report.result:
            self.bus.publish("overlap", camera=camera_id, report=report.as_dict())
        return report

    async def _retention_loop(self) -> None:
        while True:
            await asyncio.sleep(seconds_until(self.options.retention.purge_time))
            try:
                await self.run_purge(dry_run=False)
            except Exception:
                _LOGGER.exception("Scheduled purge failed")
            await asyncio.sleep(61)

    async def _hub_loop(self) -> None:
        """Push hub-level state (storage, issues, network, overlap) when it changes.

        Camera statuses are pushed as they change; everything else would go stale
        for a subscriber that never polls, so it is pushed here.
        """
        last = ""
        while True:
            try:
                payload = await asyncio.to_thread(self._hub_snapshot)
                encoded = json.dumps(payload, sort_keys=True, default=str)
                if encoded != last:
                    last = encoded
                    self.bus.publish("hub", **payload)
            except Exception:
                _LOGGER.exception("Building the hub snapshot failed")
            await asyncio.sleep(self.config.hub_push_interval)

    def _hub_snapshot(self) -> dict[str, Any]:
        full = self.status_payload()
        return {k: full[k] for k in ("storage", "issues", "network", "overlap")}

    async def _ship_loop(self) -> None:
        while True:
            await asyncio.sleep(SHIP_INTERVAL)
            await asyncio.to_thread(self._ship_safely)

    # --------------------------------------------------------------- payloads

    def clip_payload(self, clip_id: int) -> dict[str, Any] | None:
        clip = self.cat.get_clip(clip_id)
        if clip is None:
            return None
        copies = self.cat.copies(clip_id)
        best = None
        for target in (TARGET_LOCAL, TARGET_NETWORK, "spool"):
            best = next((c for c in copies if c.target == target and c.path), None)
            if best:
                break
        data = clip.as_dict()
        data["copies"] = [
            {"target": c.target, "path": c.path, "size": c.size, "state": c.state} for c in copies
        ]
        data["path"] = best.path if best else None
        data["thumbnail"] = str(Path(best.path).with_suffix(".jpg")) if best and best.path else None
        return data

    def issues(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []

        def add(key: str, camera: str | None = None, **placeholders: Any) -> None:
            out.append({"key": key, "camera": camera, "placeholders": placeholders})

        hotspot = [c for c in self.options.cameras if c.enabled and c.connection == "hotspot"]
        if hotspot and self.network.known and not self.network.has_wifi_adapter:
            add("no_wifi_adapter")
        for cam_id, reasons in self.validation.items():
            add("camera_invalid", cam_id, reasons=reasons)
        for name, problem in self.parse_problems.items():
            add("camera_invalid", None, entry=name, reasons=[problem])
        for cam_id, report in self.overlap.items():
            if report.blocked:
                add(
                    "subnet_overlap",
                    cam_id,
                    result=report.result,
                    conflicts=report.conflicts,
                    block=report.block.describe(),
                )
        for cam_id, why in self.route_unsafe.items():
            add("default_route_changed", cam_id, reason=why)
        for cam_id, worker in self.workers.items():
            status = worker.status
            if status.restore_pending and status.sync_status in ("idle", "error"):
                add("camera_playback_mode", cam_id)
            if status.unsupported:
                add("unsupported_camera", cam_id)
            if self.cat.consecutive_sessions_with(cam_id, "list_failed") >= 3:
                add("listing_keeps_failing", cam_id)
        if self.storage is not None:
            since = self.storage.network_offline_since
            if since is not None and time.time() - since > NETWORK_OFFLINE_REPAIR_AFTER:
                add("network_target_offline")
            for name, health in self.storage.health().items():
                floor = (
                    self.options.retention.network_min_free_percent
                    if name == TARGET_NETWORK
                    else self.options.retention.min_free_percent
                )
                if health.online and health.total_bytes:
                    percent = 100.0 * health.free_bytes / health.total_bytes
                    if floor and percent < floor:
                        add("storage_low", None, target=name, free_percent=round(percent, 1))
        return out

    def storage_payload(self) -> dict[str, Any]:
        health = {k: v.as_dict() for k, v in self.store_.health().items()}
        since = self.store_.network_offline_since
        return {
            "targets": health,
            "mode": self.options.storage.target,
            "network_offline_since": since,
            "network_online": bool(health.get(TARGET_NETWORK, {}).get("online"))
            if TARGET_NETWORK in health
            else None,
            "last_purge": self.last_purge,
        }

    def status_payload(self) -> dict[str, Any]:
        return {
            "api_version": API_VERSION,
            "app_version": __version__,
            "uptime": round(time.time() - self._started),
            "cameras": {cid: w.status.as_dict() for cid, w in self.workers.items()},
            "storage": self.storage_payload(),
            "issues": self.issues(),
            "network": {
                "known": self.network.known,
                "wifi_adapter": self.network.has_wifi_adapter if self.network.known else None,
                "interfaces": [i.as_dict() for i in self.network.interfaces],
                "error": self.network.error,
            },
            "overlap": {cid: r.as_dict() for cid, r in self.overlap.items()},
        }

    # -------------------------------------------------------- option editing

    async def update_raw_options(self, mutate: Any) -> None:
        """Read-modify-write the raw options; the file watcher applies them."""
        assert self.store is not None
        raw = json.loads(json.dumps(await self.store.read() or self.raw_options))
        mutate(raw)
        await self.store.write(raw)
        # Apply immediately instead of waiting for the poll.
        if isinstance(self.store, FileOptionsStore):
            self._options_mtime = 0.0
        await self._load_options()
        self._build_storage()
        self._sync_workers()
        self.overlap_checked.clear()
        self.bus.publish("options")


def now_iso() -> str:
    return dt.datetime.now(dt.UTC).isoformat()


def main() -> None:  # pragma: no cover - process entry point
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    async def run() -> None:
        daemon = Daemon(
            DaemonConfig(
                data_dir=Path(os.environ.get("DASHCAM_DATA", "/data")),
                media_root=Path(os.environ.get("DASHCAM_MEDIA", "/media")),
                share_root=Path(os.environ.get("DASHCAM_SHARE", "/share")),
            )
        )
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, daemon.request_stop)
        await daemon.start()
        await daemon.wait()
        await daemon.stop()

    asyncio.run(run())


if __name__ == "__main__":  # pragma: no cover
    main()

"""Shared fixtures: a fake Dashcam Sync app API with a controllable push socket."""

from __future__ import annotations

import asyncio
import copy
from collections.abc import AsyncIterator, Generator
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.dashcam_sync.api import (
    DashcamSyncAuthError,
    DashcamSyncConnectionError,
    DashcamSyncRequestError,
)
from custom_components.dashcam_sync.const import (
    CONF_HOST,
    CONF_PORT,
    CONF_TOKEN,
    DOMAIN,
)

pytest_plugins = "pytest_homeassistant_custom_component"

CAMERA: dict[str, Any] = {
    "id": "polestar_2",
    "name": "Polestar 2",
    "enabled": True,
    "connected": True,
    "sync_status": "idle",
    "driver": "novatek",
    "model": "NT96",
    "firmware": "FW-1.2",
    "clips_remaining": 3,
    "bytes_remaining": 123456,
    "speed_bps": 500000,
    "eta_seconds": 20,
    "last_sync": "2026-11-22T10:20:00+00:00",
    "last_event_clip": "2026-11-22T10:16:00+00:00",
    "sd_free_bytes": 8_000_000_000,
    "restore_pending": False,
    "unsupported": False,
    "blocked_reasons": [],
    "error": None,
    "live_url": "rtsp://192.168.1.254/xxx.mov",
    "capabilities": {"list": True, "range": True, "delete": True, "live": True},
    "arrival_hint": False,
    "current_file": None,
    "sync_mode": "events_first",
}

STATUS: dict[str, Any] = {
    "api_version": 1,
    "app_version": "2026.11.0",
    "uptime": 100,
    "cameras": {"polestar_2": CAMERA},
    "storage": {
        "mode": "local",
        "targets": {
            "local": {
                "name": "local",
                "root": "/media/dashcam_sync",
                "online": True,
                "writable": True,
                "mounted": True,
                "total_bytes": 100_000_000_000,
                "free_bytes": 60_000_000_000,
                "clip_bytes": 5_000_000_000,
                "reason": None,
            }
        },
        "network_offline_since": None,
        "network_online": None,
        "last_purge": None,
    },
    "issues": [],
    "network": {"known": True, "wifi_adapter": True, "interfaces": [], "error": None},
    "overlap": {},
}

EVENT_CLIP: dict[str, Any] = {
    "id": 7,
    "camera": "polestar_2",
    "size": 1000,
    "start": "2026-11-22T10:16:00+00:00",
    "kind": "event",
    "channel": "front",
    "locked": True,
    "duration": 60.0,
    "status": "done",
    "keep": False,
    "sha256": "abc",
    "post_status": "done",
    "copies": [{"target": "local", "path": "/media/x/a.mp4", "size": 1000, "state": "present"}],
    "path": "/media/x/a.mp4",
    "thumbnail": "/media/x/a.jpg",
}


class FakeApi:
    """In-memory stand-in for ``DashcamSyncApi`` (same method names)."""

    def __init__(self) -> None:
        self.host = "local-dashcam-sync"
        self.port = 8100
        self.status = copy.deepcopy(STATUS)
        self.info: dict[str, Any] = {
            "api_version": 1,
            "app_version": "2026.11.0",
            "instance": "inst123",
            "name": "Dashcam Sync",
            "cameras": 1,
        }
        self.clips: list[dict[str, Any]] = [copy.deepcopy(EVENT_CLIP)]
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.error: Exception | None = None  # raised by every call when set
        self.status_error: Exception | None = None
        self.options: dict[str, Any] = {
            "cameras": [{"id": "polestar_2", "connection": "hotspot", "interface": "wlan0"}]
        }
        self.wifi: dict[str, Any] = {
            "interface": "wlan0",
            "ssid": "CAMWIFI",
            "password": "secretpw",
            "static_address": "192.168.1.253/30",
            "block": "192.168.1.252/30",
            "camera": "192.168.1.254",
            "ipv4": "static",
        }
        self.overlap: dict[str, Any] = {
            "result": "overlap_block_free",
            "blocked": False,
            "conflicts": [],
            "block": "192.168.1.252/30",
        }
        self.days = ["2026-11-22", "2026-11-21"]
        self.queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self.ws_error: Exception | None = None
        self.ws_connects = 0

    def _rec(self, name: str, *args: Any, **kwargs: Any) -> None:
        self.calls.append((name, args, kwargs))
        if self.error is not None:
            raise self.error

    def calls_named(self, name: str) -> list[tuple[Any, ...]]:
        return [c[1] for c in self.calls if c[0] == name]

    async def async_info(self) -> dict[str, Any]:
        self._rec("info")
        return self.info

    async def async_status(self) -> dict[str, Any]:
        self._rec("status")
        if self.status_error is not None:
            raise self.status_error
        return copy.deepcopy(self.status)

    async def async_clips(self, **filters: Any) -> list[dict[str, Any]]:
        self._rec("clips", **filters)
        out = [c for c in self.clips if filters.get("kind") in (None, c["kind"])]
        if filters.get("camera"):
            out = [c for c in out if c["camera"] == filters["camera"]]
        return copy.deepcopy(out)

    async def async_clip(self, clip_id: int) -> dict[str, Any]:
        self._rec("clip", clip_id)
        for clip in self.clips:
            if clip["id"] == clip_id:
                return copy.deepcopy(clip)
        raise DashcamSyncRequestError(404, "unknown clip")

    async def async_clip_days(self, camera: str) -> list[str]:
        self._rec("clip_days", camera)
        return list(self.days)

    async def async_sessions(self, limit: int = 50) -> list[dict[str, Any]]:
        self._rec("sessions", limit)
        return [{"id": 1, "camera": "polestar_2", "result": "ok", "clips": 2, "bytes": 10,
                 "error": "link to 192.168.1.254 lost", "driver": "novatek"}]  # fmt: skip

    async def async_options(self) -> dict[str, Any]:
        self._rec("options")
        return copy.deepcopy(self.options)

    async def async_wifi(self, camera: str) -> dict[str, Any]:
        self._rec("wifi", camera)
        return dict(self.wifi)

    async def async_sync(self, camera: str) -> None:
        self._rec("sync", camera)

    async def async_restore(self, camera: str) -> None:
        self._rec("restore", camera)

    async def async_patch_camera(self, camera: str, **changes: Any) -> None:
        self._rec("patch_camera", camera, **changes)

    async def async_overlap(
        self, camera: str, known_device_ips: list[str], probe: bool = True
    ) -> dict[str, Any]:
        self._rec("overlap", camera, known_device_ips, probe)
        return dict(self.overlap)

    async def async_hint(self, camera: str | None = None) -> None:
        self._rec("hint", camera)

    async def async_join_result(self, camera: str, state: str, reason: str | None) -> None:
        self._rec("join_result", camera, state, reason)

    async def async_scan_result(
        self, request: str, accesspoints: list[dict[str, Any]], error: str | None
    ) -> None:
        self._rec("scan_result", request, accesspoints, error)

    async def async_keep(self, clip_id: int, keep: bool = True) -> None:
        self._rec("keep", clip_id, keep)

    async def async_delete(self, clip_id: int) -> dict[str, Any]:
        self._rec("delete", clip_id)
        return {"ok": True, "bytes_freed": 1}

    async def async_purge(self, dry_run: bool = False) -> dict[str, Any]:
        self._rec("purge", dry_run)
        return {"dry_run": dry_run, "count": 2, "bytes_freed": 99, "clips": []}

    async def async_messages(self) -> AsyncIterator[dict[str, Any]]:
        self.ws_connects += 1
        if self.ws_error is not None:
            error, self.ws_error = self.ws_error, None
            raise error
        while True:
            message = await self.queue.get()
            if message is None:
                return
            yield message

    def push(self, message: dict[str, Any]) -> None:
        self.queue.put_nowait(message)


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Every test may load the custom integration."""


@pytest.fixture
def fake_api() -> Generator[FakeApi]:
    api = FakeApi()
    with (
        patch("custom_components.dashcam_sync.DashcamSyncApi", return_value=api),
        patch("custom_components.dashcam_sync.config_flow.DashcamSyncApi", return_value=api),
    ):
        yield api


@pytest.fixture
def entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="Dashcam Sync",
        unique_id="inst123",
        data={
            CONF_HOST: "local-dashcam-sync",
            CONF_PORT: 8100,
            CONF_TOKEN: "tok",
            "slug": "abc_dashcam_sync",
        },
    )


async def setup_entry(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


@pytest.fixture
async def loaded(hass: HomeAssistant, fake_api: FakeApi, entry: MockConfigEntry) -> MockConfigEntry:
    await setup_entry(hass, entry)
    return entry


__all__ = [
    "CAMERA",
    "DashcamSyncAuthError",
    "DashcamSyncConnectionError",
    "FakeApi",
    "setup_entry",
]

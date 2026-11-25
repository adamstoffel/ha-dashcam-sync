"""Coordinator: WebSocket pushes with a 30 s poll as the fallback."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    DashcamSyncApi,
    DashcamSyncAuthError,
    DashcamSyncConnectionError,
    DashcamSyncError,
)
from .const import DOMAIN, POLL_INTERVAL, WS_RECONNECT_MAX, WS_STALE_AFTER

if TYPE_CHECKING:
    pass

_LOGGER = logging.getLogger(__name__)

type Payload = dict[str, Any]
type EventListener = Callable[[Payload], None]
type DashcamSyncConfigEntry = ConfigEntry[DashcamSyncRuntimeData]


class DashcamSyncCoordinator(DataUpdateCoordinator[Payload]):
    """Holds the app's status snapshot and fans out pushed events."""

    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, api: DashcamSyncApi) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            config_entry=entry,
            update_interval=POLL_INTERVAL,
        )
        self.api = api
        self.latest_events: dict[str, Payload] = {}
        self._event_listeners: set[EventListener] = set()
        self._last_push = 0.0
        self._ws_task: asyncio.Task[None] | None = None
        self.ws_connected = False
        self.unsafe_handler: Callable[[list[str]], Any] | None = None

    # ---------------------------------------------------------------- polling

    async def _async_update_data(self) -> Payload:
        if (
            self.data is not None
            and self.ws_connected
            and time.monotonic() - self._last_push < WS_STALE_AFTER
        ):
            return self.data
        try:
            data = await self.api.async_status()
        except DashcamSyncAuthError as err:
            raise ConfigEntryAuthFailed from err
        except DashcamSyncConnectionError as err:
            raise UpdateFailed(f"cannot reach the Dashcam Sync app: {err}") from err
        except DashcamSyncError as err:
            raise UpdateFailed(str(err)) from err
        await self._async_refresh_latest_events(data)
        return data

    async def _async_refresh_latest_events(self, data: Payload) -> None:
        """Remember the newest event clip per camera (for the image entity)."""
        for camera_id, status in data.get("cameras", {}).items():
            stamp = status.get("last_event_clip")
            current = self.latest_events.get(camera_id)
            if current is not None and current.get("start") == stamp:
                continue
            try:
                clips = await self.api.async_clips(camera=camera_id, kind="event", limit=1)
            except DashcamSyncError:
                return
            if clips:
                self.latest_events[camera_id] = clips[0]

    # -------------------------------------------------------------- websocket

    @callback
    def async_start(self) -> None:
        """Start the push socket as a background task of the config entry."""
        if self._ws_task is None:
            self._ws_task = self.config_entry.async_create_background_task(
                self.hass, self._async_ws_loop(), name="dashcam_sync_ws"
            )

    @callback
    def async_stop(self) -> None:
        if self._ws_task is not None:
            self._ws_task.cancel()
            self._ws_task = None

    async def _async_ws_loop(self) -> None:
        delay = 1.0
        while True:
            try:
                async for message in self.api.async_messages():
                    self.ws_connected = True
                    delay = 1.0
                    self._last_push = time.monotonic()
                    self._handle(message)
            except DashcamSyncAuthError:
                self.ws_connected = False
                self.config_entry.async_start_reauth(self.hass)
                return
            except DashcamSyncError as err:
                _LOGGER.debug("Push socket error: %s", err)
            self.ws_connected = False
            await asyncio.sleep(delay)
            delay = min(delay * 2, WS_RECONNECT_MAX)

    @callback
    def _handle(self, message: Payload) -> None:
        kind = message.get("type")
        data = self.data
        if kind == "snapshot":
            snapshot = {k: v for k, v in message.items() if k not in ("type", "ts")}
            self.async_set_updated_data(snapshot)
            self.hass.async_create_task(self._async_refresh_latest_events(snapshot))
            return
        if data is None:
            return
        if kind == "camera":
            cameras = dict(data.get("cameras", {}))
            cameras[message["camera"]] = message["status"]
            self.async_set_updated_data({**data, "cameras": cameras})
        elif kind == "clip":
            clip = message.get("clip")
            if message.get("event_clip") and clip:
                self.latest_events[message["camera"]] = clip
            self._dispatch(message)
            self.hass.async_create_task(self.async_request_refresh())
        elif kind == "session":
            self._dispatch(message)
        elif kind == "safety":
            unsafe = list(message.get("route_unsafe", []))
            if unsafe and self.unsafe_handler is not None:
                self.hass.async_create_task(self.unsafe_handler(unsafe))
            self.hass.async_create_task(self.async_request_refresh())
        elif kind in (
            "overlap",
            "options",
            "purge_completed",
            "issue",
            "clip_keep",
            "clip_deleted",
        ):
            self.hass.async_create_task(self.async_request_refresh())

    # ----------------------------------------------------------------- events

    @callback
    def async_add_event_listener(self, listener: EventListener) -> Callable[[], None]:
        self._event_listeners.add(listener)

        @callback
        def remove() -> None:
            self._event_listeners.discard(listener)

        return remove

    @callback
    def _dispatch(self, message: Payload) -> None:
        for listener in list(self._event_listeners):
            listener(message)

    # ---------------------------------------------------------------- helpers

    def camera(self, camera_id: str) -> Payload | None:
        if self.data is None:
            return None
        return self.data.get("cameras", {}).get(camera_id)  # type: ignore[no-any-return]

    @property
    def hub(self) -> Payload:
        return self.data or {}


@dataclass
class DashcamSyncRuntimeData:
    """Stored in ``entry.runtime_data``."""

    api: DashcamSyncApi
    coordinator: DashcamSyncCoordinator
    cleanups: list[Callable[[], None]] = field(default_factory=list)

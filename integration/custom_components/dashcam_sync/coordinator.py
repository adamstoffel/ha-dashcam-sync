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


def _valid_hub(data: Payload, *, require_cameras: bool = False) -> bool:
    """The shapes the entities rely on, so one bad message cannot poison the state."""
    checks = {"cameras": dict, "storage": dict, "issues": list, "network": dict, "overlap": dict}
    if require_cameras and not isinstance(data.get("cameras"), dict):
        return False
    return all(isinstance(data[key], kind) for key, kind in checks.items() if key in data)


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
        self._force_next = False
        # Pushes that arrive while a status read is in flight; the read's answer is older.
        self._in_flight: list[list[Payload]] = []
        self._ws_task: asyncio.Task[None] | None = None
        self.ws_connected = False
        self.unsafe_handler: Callable[[list[str]], Any] | None = None
        self.join_handler: Callable[[str], Any] | None = None
        self.scan_handler: Callable[[str, str], Any] | None = None

    # ---------------------------------------------------------------- polling

    async def _async_update_data(self) -> Payload:
        if (
            not self._force_next
            and self.data is not None
            and self.ws_connected
            and time.monotonic() - self._last_push < WS_STALE_AFTER
        ):
            return self.data
        self._force_next = False
        pushed: list[Payload] = []
        self._in_flight.append(pushed)
        try:
            data = await self.api.async_status()
        except DashcamSyncAuthError as err:
            raise ConfigEntryAuthFailed from err
        except DashcamSyncConnectionError as err:
            raise UpdateFailed(f"cannot reach the Dashcam Sync app: {err}") from err
        except DashcamSyncError as err:
            raise UpdateFailed(str(err)) from err
        finally:
            self._in_flight = [p for p in self._in_flight if p is not pushed]
        data = self._newer_of(data, pushed)
        await self._async_refresh_latest_events(data)
        return data

    def _newer_of(self, answer: Payload, pushed: list[Payload]) -> Payload:
        """Apply the pushes received while the read was in flight that are newer than it.

        The app numbers pushes and stamps the status with the newest number it reflects
        (``rev``). Only pushes numbered above the answer are newer; the rest are already in
        it. Without numbers (an older app) every push received meanwhile is taken as newer.
        """
        answer_rev = answer.get("rev")
        for message in pushed:
            rev = message.get("rev")
            if isinstance(answer_rev, int) and isinstance(rev, int) and rev <= answer_rev:
                continue
            kind = message["type"]
            if kind == "snapshot":
                answer = {k: v for k, v in message.items() if k not in ("type", "ts")}
            elif kind == "camera":
                cameras = {**answer.get("cameras", {}), message["camera"]: message["status"]}
                answer = {**answer, "cameras": cameras}
            else:
                answer = {**answer, **message["hub"]}
        return answer

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

    @callback
    def _note_push(self, message: Payload) -> None:
        for pushed in self._in_flight:
            pushed.append(message)

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
                    try:
                        self._handle(message)
                    except Exception:  # one malformed message must not end live updates
                        _LOGGER.exception("Ignoring a malformed push message: %r", message)
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
            if not _valid_hub(snapshot, require_cameras=True):
                _LOGGER.warning("Ignoring a snapshot with an unexpected shape")
                return
            self._note_push(message)
            self.async_set_updated_data(snapshot)
            self.hass.async_create_task(self._async_refresh_latest_events(snapshot))
            return
        if data is None:
            return
        if kind == "camera":
            if "camera" not in message or not isinstance(message.get("status"), dict):
                return
            cameras = dict(data.get("cameras", {}))
            cameras[message["camera"]] = message["status"]
            self._note_push(message)
            self.async_set_updated_data({**data, "cameras": cameras})
        elif kind == "hub":
            hub: Payload = {
                k: message[k] for k in ("storage", "issues", "network", "overlap") if k in message
            }
            if not _valid_hub(hub):
                _LOGGER.warning("Ignoring a hub update with an unexpected shape")
                return
            self._note_push({"type": "hub", "rev": message.get("rev"), "hub": hub})
            self.async_set_updated_data({**data, **hub})
        elif kind == "clip":
            clip = message.get("clip")
            if not isinstance(clip, dict) or "camera" not in message:
                return
            if message.get("event_clip"):
                self.latest_events[message["camera"]] = clip
            self._dispatch(message)
            self.hass.async_create_task(self.async_force_refresh(immediate=False))
        elif kind == "session":
            self._dispatch(message)
        elif kind == "safety":
            unsafe = list(message.get("route_unsafe", []))
            if unsafe and self.unsafe_handler is not None:
                self.hass.async_create_task(self.unsafe_handler(unsafe))
            self.hass.async_create_task(self.async_force_refresh(immediate=False))
        elif kind == "scan_wifi":
            interface, request = message.get("interface"), message.get("request")
            if isinstance(interface, str) and isinstance(request, str) and self.scan_handler:
                # Tied to the entry: an unload cancels it instead of leaving it on a stale session.
                self.config_entry.async_create_background_task(
                    self.hass, self.scan_handler(interface, request), "dashcam_sync_scan"
                )
        elif kind == "join_wifi":
            camera = message.get("camera")
            if isinstance(camera, str) and self.join_handler is not None:
                self.config_entry.async_create_background_task(
                    self.hass, self.join_handler(camera), f"dashcam_sync_join_{camera}"
                )
        elif kind in (
            "overlap",
            "options",
            "purge_completed",
            "issue",
            "clip_keep",
            "clip_deleted",
        ):
            self.hass.async_create_task(self.async_force_refresh(immediate=False))

    async def async_force_refresh(self, *, immediate: bool = True) -> None:
        """Read fresh state even while the socket is live.

        After our own writes (``immediate``) this must not wait: the coordinator's
        debounced ``async_request_refresh`` holds a 10 s cooldown after any refresh,
        which would leave a switch showing the old value.
        """
        self._force_next = True
        if immediate:
            await self.async_refresh()
        else:
            await self.async_request_refresh()

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
    hub_device_id: str | None = None

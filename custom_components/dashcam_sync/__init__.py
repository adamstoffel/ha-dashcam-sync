"""Dashcam Sync: devices, entities, a live camera and a clip library for the app."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.typing import ConfigType
from homeassistant.loader import async_get_integration

from .api import (
    DashcamSyncApi,
    DashcamSyncAuthError,
    DashcamSyncConnectionError,
    DashcamSyncError,
)
from .const import (
    API_VERSION,
    CONF_HOST,
    CONF_PORT,
    CONF_PRESENCE_ENTITY,
    CONF_TOKEN,
    DOMAIN,
    ISSUE_ANNOUNCED_ADAPTER,
    ISSUE_API_MISMATCH,
    ISSUE_RELEASE_MISMATCH,
    MIN_API_VERSION,
)
from .coordinator import DashcamSyncConfigEntry, DashcamSyncCoordinator, DashcamSyncRuntimeData
from .issues import IssueSync
from .services import async_setup_services
from .views import ClipFileView, ClipThumbView
from .wifi import (
    WifiUnavailable,
    async_announced_wifi_adapters,
    async_disconnect,
    async_join_camera,
    async_scan_access_points,
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.CAMERA,
    Platform.EVENT,
    Platform.IMAGE,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

ARRIVAL_STATES = {"home", "on", "open", "opening"}


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register actions and the clip views once, whatever the number of entries."""
    async_setup_services(hass)
    hass.http.register_view(ClipFileView())
    hass.http.register_view(ClipThumbView())
    return True


async def async_setup_entry(hass: HomeAssistant, entry: DashcamSyncConfigEntry) -> bool:
    integration_version = str((await async_get_integration(hass, DOMAIN)).version or "")
    api = DashcamSyncApi(
        async_get_clientsession(hass),
        entry.data[CONF_HOST],
        entry.data[CONF_PORT],
        entry.data[CONF_TOKEN],
        integration_version or None,
    )
    try:
        info = await api.async_info()
    except DashcamSyncAuthError as err:
        raise ConfigEntryAuthFailed from err
    except DashcamSyncConnectionError as err:
        raise ConfigEntryNotReady(f"cannot reach the Dashcam Sync app: {err}") from err
    except DashcamSyncError as err:
        raise ConfigEntryNotReady(str(err)) from err

    coordinator = DashcamSyncCoordinator(hass, entry, api)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = DashcamSyncRuntimeData(api=api, coordinator=coordinator)

    # Camera devices point at the hub (via_device_id), so the hub must exist first.
    hub_device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.entry_id)},
        name="Dashcam Sync",
        manufacturer="Dashcam Sync",
        model="Dashcam Sync app",
        sw_version=coordinator.hub.get("app_version"),
    )
    entry.runtime_data.hub_device_id = hub_device.id

    issues = IssueSync(hass, entry)
    entry.runtime_data.cleanups.append(issues.async_clear)
    app_api = int(info.get("api_version", 0))
    issues.async_set_own(
        ISSUE_API_MISMATCH,
        not MIN_API_VERSION <= app_api <= API_VERSION,
        app_api=str(app_api),
        integration_api=str(API_VERSION),
    )
    # The app and the integration are released together; a different release on either
    # side is the usual cause of a feature that "does nothing" (an older app, say, that
    # never answers a request this integration sends).
    setup_release = info.get("release")

    @callback
    def _check_release() -> None:
        # Again on every update: an app restarted on another release reconnects the push
        # socket without setting this entry up again.
        pairing = coordinator.hub.get("integration")
        app_release = pairing.get("app_release") if isinstance(pairing, dict) else setup_release
        issues.async_set_own(
            ISSUE_RELEASE_MISMATCH,
            bool(integration_version) and app_release != integration_version,
            app_release=str(app_release or "older than 2026.11.0b2"),
            integration_release=integration_version,
        )

    _check_release()
    entry.async_on_unload(coordinator.async_add_listener(_check_release))
    announced = await async_announced_wifi_adapters(hass)
    issues.async_set_own(ISSUE_ANNOUNCED_ADAPTER, bool(announced), adapters=", ".join(announced))

    @callback
    def _sync_issues() -> None:
        issues.async_update()

    _sync_issues()
    entry.async_on_unload(coordinator.async_add_listener(_sync_issues))

    async def _disconnect_unsafe(camera_ids: list[str]) -> None:
        """Kill switch: the default route moved, so drop the hotspot profile now."""
        for camera_id in camera_ids:
            try:
                wifi = await api.async_wifi(camera_id)
                await async_disconnect(hass, str(wifi["interface"]))
            except Exception as err:  # keep going: the remaining cameras still need disconnecting
                _LOGGER.warning("Could not disconnect the Wi-Fi for %s: %s", camera_id, err)

    coordinator.unsafe_handler = _disconnect_unsafe

    join_locks: dict[str, asyncio.Lock] = {}

    async def _join(camera_id: str) -> None:
        """The panel asked to join a hotspot camera: do it and report back.

        One join per camera at a time: a retry while a join is still running must not
        write the interface twice.
        """
        async with join_locks.setdefault(camera_id, asyncio.Lock()):
            state, reason = await async_join_camera(hass, api, camera_id)
            _LOGGER.info("Joining the Wi-Fi for %s: %s %s", camera_id, state, reason or "")
            try:
                await api.async_join_result(camera_id, state, reason)
            except Exception as err:  # the panel times out and shows the manual steps
                _LOGGER.warning("Could not report the Wi-Fi join for %s: %s", camera_id, err)

    coordinator.join_handler = _join

    async def _scan(interface: str, request: str) -> None:
        """The panel's wizard asked for the networks an interface can see."""
        try:
            found, error = await async_scan_access_points(hass, interface), None
        except WifiUnavailable as err:
            found, error = [], str(err)
        try:
            await api.async_scan_result(request, found, error)
        except Exception as err:  # the wizard times out and asks for the SSID by hand
            _LOGGER.warning("Could not report the Wi-Fi scan for %s: %s", interface, err)

    coordinator.scan_handler = _scan

    presence = entry.options.get(CONF_PRESENCE_ENTITY)
    if presence:

        @callback
        def _arrival(event: Event[EventStateChangedData]) -> None:
            new = event.data["new_state"]
            old = event.data["old_state"]
            if new is None or new.state not in ARRIVAL_STATES:
                return
            if old is not None and old.state in ARRIVAL_STATES:
                return
            hass.async_create_task(_send_hint(api))

        entry.async_on_unload(async_track_state_change_event(hass, [presence], _arrival))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    coordinator.async_start()
    entry.async_on_unload(coordinator.async_stop)
    entry.async_on_unload(entry.add_update_listener(_async_reload_on_options))
    return True


async def _send_hint(api: DashcamSyncApi) -> None:
    """Probe every 3 s for 5 minutes instead of every 15 s."""
    try:
        await api.async_hint()
    except DashcamSyncError as err:
        _LOGGER.debug("Arrival hint failed: %s", err)


async def _async_reload_on_options(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: DashcamSyncConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        for cleanup in entry.runtime_data.cleanups:
            cleanup()
    return unloaded


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: DashcamSyncConfigEntry, device_entry: Any
) -> bool:
    """A camera device can be removed once the app no longer lists it; the hub never can."""
    cameras = entry.runtime_data.coordinator.hub.get("cameras", {})
    return not any(
        domain == DOMAIN and (ident in cameras or ident == entry.entry_id)
        for domain, ident in device_entry.identifiers
    )

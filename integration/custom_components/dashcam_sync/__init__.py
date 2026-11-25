"""Dashcam Sync: devices, entities, a live camera and a clip library for the app."""

from __future__ import annotations

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
    MIN_API_VERSION,
)
from .coordinator import DashcamSyncConfigEntry, DashcamSyncCoordinator, DashcamSyncRuntimeData
from .issues import IssueSync
from .services import async_setup_services
from .views import ClipFileView, ClipThumbView
from .wifi import WifiUnavailable, async_announced_wifi_adapters, async_disconnect

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
    api = DashcamSyncApi(
        async_get_clientsession(hass),
        entry.data[CONF_HOST],
        entry.data[CONF_PORT],
        entry.data[CONF_TOKEN],
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

    # Camera devices point at the hub with via_device, so the hub must exist first.
    dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.entry_id)},
        name="Dashcam Sync",
        manufacturer="Dashcam Sync",
        model="Dashcam Sync app",
        sw_version=coordinator.hub.get("app_version"),
    )

    issues = IssueSync(hass, entry)
    entry.runtime_data.cleanups.append(issues.async_clear)
    app_api = int(info.get("api_version", 0))
    issues.async_set_own(
        ISSUE_API_MISMATCH,
        not MIN_API_VERSION <= app_api <= API_VERSION,
        app_api=str(app_api),
        integration_api=str(API_VERSION),
    )
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
            except (DashcamSyncError, WifiUnavailable) as err:
                _LOGGER.warning("Could not disconnect the Wi-Fi for %s: %s", camera_id, err)

    coordinator.unsafe_handler = _disconnect_unsafe

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
    """A camera device can be removed once the app no longer lists the camera."""
    cameras = entry.runtime_data.coordinator.hub.get("cameras", {})
    return not any(
        domain == DOMAIN and ident in cameras for domain, ident in device_entry.identifiers
    )

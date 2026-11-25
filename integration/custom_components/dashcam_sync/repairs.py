"""Fix flows for repair issues that can be fixed with one confirmation."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant import data_entry_flow
from homeassistant.components.repairs import RepairsFlow
from homeassistant.core import HomeAssistant

from .api import DashcamSyncError
from .const import DOMAIN
from .wifi import WifiUnavailable, async_disconnect, known_device_ips


class DashcamSyncFixFlow(RepairsFlow):
    """One confirm step, then the matching action."""

    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data

    async def async_step_init(
        self, user_input: dict[str, str] | None = None
    ) -> data_entry_flow.FlowResult:
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, str] | None = None
    ) -> data_entry_flow.FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            error = await self._async_fix()
            if error is None:
                return self.async_create_entry(data={})
            errors["base"] = error
        return self.async_show_form(
            step_id="confirm",
            data_schema=vol.Schema({}),
            errors=errors,
            description_placeholders={"camera": self._data.get("camera") or "Dashcam Sync"},
        )

    async def _async_fix(self) -> str | None:
        entry = self.hass.config_entries.async_get_entry(self._data["entry_id"])
        if entry is None or not hasattr(entry, "runtime_data"):
            return "app_unavailable"
        api = entry.runtime_data.api
        key, camera = self._data["key"], self._data.get("camera")
        try:
            if key == "camera_playback_mode" and camera:
                await api.async_restore(camera)
            elif key == "subnet_overlap" and camera:
                report = await api.async_overlap(camera, known_device_ips(self.hass))
                if report.get("blocked"):
                    return "still_blocked"
            elif key == "default_route_changed" and camera:
                wifi = await api.async_wifi(camera)
                await async_disconnect(self.hass, str(wifi["interface"]))
            elif key == "storage_low":
                await api.async_purge()
            else:
                return "not_fixable"
        except WifiUnavailable:
            return "wifi_unavailable"
        except DashcamSyncError:
            return "app_unavailable"
        await entry.runtime_data.coordinator.async_request_refresh()
        return None


async def async_create_fix_flow(
    hass: HomeAssistant, issue_id: str, data: dict[str, str | int | float | None] | None
) -> RepairsFlow:
    """Create the fix flow for an issue the integration raised."""
    assert data is not None and DOMAIN
    return DashcamSyncFixFlow(dict(data))

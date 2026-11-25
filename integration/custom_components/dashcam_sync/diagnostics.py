"""Diagnostics: redacted options, drivers, the last 50 sessions and storage health."""

from __future__ import annotations

import re
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from .api import DashcamSyncError
from .coordinator import DashcamSyncConfigEntry

TO_REDACT = {
    "token",
    "host",
    "ssid",
    "wifi_password",
    "password",
    "static_address",
    "ip",
    "camera_ip",
    "home_address",
    "block",
    "block_first",
    "block_last",
    "interface",
    "interfaces",
    "addresses",
    "gateway",
    "live_url",
    "conflicts",
    "root",
    "path",
    "src_path",
    "error",
}
_IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")


def _scrub(value: Any) -> Any:
    """Redact IPv4 addresses hiding inside free-form strings."""
    if isinstance(value, str):
        return _IPV4.sub("x.x.x.x", value)
    if isinstance(value, dict):
        return {k: _scrub(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    return value


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: DashcamSyncConfigEntry
) -> dict[str, Any]:
    coordinator = entry.runtime_data.coordinator
    api = entry.runtime_data.api
    try:
        sessions = await api.async_sessions(50)
        options = await api.async_options()
    except DashcamSyncError as err:
        sessions, options = [], {"error": str(err)}
    status = coordinator.hub
    cameras = {
        camera_id: {
            "driver": cam.get("driver"),
            "model": cam.get("model"),
            "firmware": cam.get("firmware"),
            "capabilities": cam.get("capabilities"),
            "sync_status": cam.get("sync_status"),
            "connected": cam.get("connected"),
            "restore_pending": cam.get("restore_pending"),
            "unsupported": cam.get("unsupported"),
            "blocked_reasons": cam.get("blocked_reasons"),
            "last_sync": cam.get("last_sync"),
        }
        for camera_id, cam in status.get("cameras", {}).items()
    }
    data = {
        "entry": async_redact_data(dict(entry.data), TO_REDACT),
        "entry_options": dict(entry.options),
        "app_version": status.get("app_version"),
        "api_version": status.get("api_version"),
        "websocket_connected": coordinator.ws_connected,
        "options": async_redact_data(options, TO_REDACT),
        "cameras": async_redact_data(cameras, TO_REDACT),
        "storage": async_redact_data(status.get("storage", {}), TO_REDACT),
        "issues": async_redact_data(status.get("issues", []), TO_REDACT),
        "sessions": async_redact_data(sessions, TO_REDACT),
    }
    return _scrub(data)  # type: ignore[no-any-return]

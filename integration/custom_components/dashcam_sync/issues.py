"""Mirror the app's issues (and the integration's own) as repair issues."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN, ISSUE_ANNOUNCED_ADAPTER, ISSUE_API_MISMATCH, ISSUE_APP_UNREACHABLE
from .coordinator import DashcamSyncConfigEntry

ERRORS = {
    "subnet_overlap",
    "default_route_changed",
    "camera_playback_mode",
    "no_wifi_adapter",
}
FIXABLE = {"camera_playback_mode", "subnet_overlap", "default_route_changed", "storage_low"}


def _placeholders(issue: dict[str, Any], hub_name: str) -> dict[str, str]:
    values: dict[str, str] = {"camera": issue.get("camera") or hub_name}
    for key, value in (issue.get("placeholders") or {}).items():
        if isinstance(value, list):
            values[key] = ", ".join(str(v) for v in value)
        else:
            values[key] = str(value)
    return values


def issue_id(key: str, camera: str | None) -> str:
    return f"{key}_{camera or 'hub'}"


class IssueSync:
    """Creates and deletes repair issues so they track the app's current state."""

    def __init__(self, hass: HomeAssistant, entry: DashcamSyncConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self._active: set[str] = set()
        self._own: set[str] = set()

    @callback
    def async_update(self) -> None:
        coordinator = self.entry.runtime_data.coordinator
        wanted: dict[str, dict[str, Any]] = {}
        for issue in coordinator.hub.get("issues", []):
            wanted[issue_id(issue["key"], issue.get("camera"))] = issue
        for stale in self._active - set(wanted):
            ir.async_delete_issue(self.hass, DOMAIN, stale)
        for iid, issue in wanted.items():
            key = issue["key"]
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                iid,
                is_fixable=key in FIXABLE,
                is_persistent=False,
                severity=ir.IssueSeverity.ERROR if key in ERRORS else ir.IssueSeverity.WARNING,
                translation_key=key,
                translation_placeholders=_placeholders(issue, "Dashcam Sync"),
                data={"entry_id": self.entry.entry_id, "key": key, "camera": issue.get("camera")},
            )
        self._active = set(wanted)

    @callback
    def async_set_own(self, key: str, active: bool, **placeholders: str) -> None:
        """Integration-side issues: API mismatch, announced adapter, app unreachable."""
        if active:
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                key,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key=key,
                translation_placeholders=placeholders or None,
            )
            self._own.add(key)
        elif key in self._own:
            ir.async_delete_issue(self.hass, DOMAIN, key)
            self._own.discard(key)

    @callback
    def async_clear(self) -> None:
        for iid in self._active | self._own:
            ir.async_delete_issue(self.hass, DOMAIN, iid)
        self._active.clear()
        self._own.clear()


__all__ = [
    "ISSUE_ANNOUNCED_ADAPTER",
    "ISSUE_API_MISMATCH",
    "ISSUE_APP_UNREACHABLE",
    "IssueSync",
    "issue_id",
]

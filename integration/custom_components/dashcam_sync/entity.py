"""Base entity and the helper that adds entities as cameras appear."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import DashcamSyncConfigEntry, DashcamSyncCoordinator


class DashcamSyncEntity(CoordinatorEntity[DashcamSyncCoordinator]):
    """A camera entity (``camera_id`` set) or a hub entity (``None``)."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: DashcamSyncCoordinator,
        key: str,
        camera_id: str | None = None,
    ) -> None:
        super().__init__(coordinator)
        self.camera_id = camera_id
        self._attr_translation_key = key
        entry = coordinator.config_entry
        self._attr_unique_id = f"{camera_id or 'hub'}_{key}"
        if camera_id is None:
            hub = coordinator.hub
            self._attr_device_info = DeviceInfo(
                identifiers={(DOMAIN, entry.entry_id)},
                name="Dashcam Sync",
                manufacturer="Dashcam Sync",
                model="Dashcam Sync app",
                sw_version=hub.get("app_version"),
                configuration_url=_config_url(entry.data.get("slug")),
            )
        else:
            status = coordinator.camera(camera_id) or {}
            self._attr_device_info = DeviceInfo(
                identifiers={(DOMAIN, camera_id)},
                name=status.get("name") or camera_id,
                manufacturer="Dashcam Sync",
                model=status.get("model") or status.get("driver"),
                sw_version=status.get("firmware"),
                via_device_id=entry.runtime_data.hub_device_id,
            )

    @property
    def camera_status(self) -> dict[str, Any]:
        if self.camera_id is None:
            return {}
        return self.coordinator.camera(self.camera_id) or {}

    @property
    def available(self) -> bool:
        if not super().available:
            return False
        return self.camera_id is None or self.coordinator.camera(self.camera_id) is not None


def _config_url(slug: str | None) -> str | None:
    return f"homeassistant://hassio/ingress/{slug}" if slug else None


@callback
def async_add_entities_for_cameras(
    entry: DashcamSyncConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
    camera_factory: Callable[[DashcamSyncCoordinator, str], list[Entity]],
    hub_factory: Callable[[DashcamSyncCoordinator], list[Entity]] | None = None,
) -> None:
    """Add entities now and whenever the app reports a new camera."""
    coordinator = entry.runtime_data.coordinator
    known: set[str] = set()
    known_hub: set[str] = set()

    @callback
    def _check_hub() -> None:
        if hub_factory is None:
            return
        fresh = [e for e in hub_factory(coordinator) if e.unique_id not in known_hub]
        known_hub.update(str(e.unique_id) for e in fresh)
        if fresh:
            async_add_entities(fresh)

    @callback
    def _check() -> None:
        _check_hub()
        new = set(coordinator.hub.get("cameras", {})) - known
        if not new:
            return
        known.update(new)
        entities: list[Entity] = []
        for camera_id in sorted(new):
            entities.extend(camera_factory(coordinator, camera_id))
        async_add_entities(entities)

    _check()
    entry.async_on_unload(coordinator.async_add_listener(_check))

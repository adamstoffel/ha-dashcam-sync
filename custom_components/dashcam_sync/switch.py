"""Auto sync switch (pauses one camera without deleting it)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import DashcamSyncError
from .coordinator import DashcamSyncConfigEntry, DashcamSyncCoordinator
from .entity import DashcamSyncEntity, async_add_entities_for_cameras

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: DashcamSyncConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    def cameras(coordinator: DashcamSyncCoordinator, camera_id: str) -> list[Entity]:
        return [AutoSyncSwitch(coordinator, camera_id)]

    async_add_entities_for_cameras(entry, async_add_entities, cameras)


class AutoSyncSwitch(DashcamSyncEntity, SwitchEntity):
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: DashcamSyncCoordinator, camera_id: str) -> None:
        super().__init__(coordinator, "auto_sync", camera_id)

    @property
    def is_on(self) -> bool:
        return bool(self.camera_status.get("enabled"))

    async def _set(self, enabled: bool) -> None:
        assert self.camera_id is not None
        try:
            await self.coordinator.api.async_patch_camera(self.camera_id, enabled=enabled)
        except DashcamSyncError as err:
            raise HomeAssistantError(
                translation_domain="dashcam_sync",
                translation_key="action_failed",
                translation_placeholders={"error": str(err)},
            ) from err
        await self.coordinator.async_force_refresh()

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set(False)

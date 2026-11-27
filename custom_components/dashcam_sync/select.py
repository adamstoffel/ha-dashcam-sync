"""Sync mode select."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import DashcamSyncError
from .const import SYNC_MODES
from .coordinator import DashcamSyncConfigEntry, DashcamSyncCoordinator
from .entity import DashcamSyncEntity, async_add_entities_for_cameras

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: DashcamSyncConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    def cameras(coordinator: DashcamSyncCoordinator, camera_id: str) -> list[Entity]:
        return [SyncModeSelect(coordinator, camera_id)]

    async_add_entities_for_cameras(entry, async_add_entities, cameras)


class SyncModeSelect(DashcamSyncEntity, SelectEntity):
    _attr_entity_category = EntityCategory.CONFIG
    _attr_options = SYNC_MODES

    def __init__(self, coordinator: DashcamSyncCoordinator, camera_id: str) -> None:
        super().__init__(coordinator, "sync_mode", camera_id)

    @property
    def current_option(self) -> str | None:
        mode = self.camera_status.get("sync_mode")
        return mode if mode in SYNC_MODES else None

    async def async_select_option(self, option: str) -> None:
        assert self.camera_id is not None
        try:
            await self.coordinator.api.async_patch_camera(self.camera_id, sync_mode=option)
        except DashcamSyncError as err:
            raise HomeAssistantError(
                translation_domain="dashcam_sync",
                translation_key="action_failed",
                translation_placeholders={"error": str(err)},
            ) from err
        await self.coordinator.async_force_refresh()

"""Buttons: sync now, restore recording, purge now."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from homeassistant.components.button import ButtonEntity
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
        api = coordinator.api
        return [
            DashcamButton(coordinator, "sync_now", camera_id, lambda: api.async_sync(camera_id)),
            DashcamButton(
                coordinator,
                "restore_recording",
                camera_id,
                lambda: api.async_restore(camera_id),
                EntityCategory.DIAGNOSTIC,
            ),
        ]

    def hub(coordinator: DashcamSyncCoordinator) -> list[Entity]:
        async def purge() -> None:
            await coordinator.api.async_purge()

        return [DashcamButton(coordinator, "purge_now", None, purge)]

    async_add_entities_for_cameras(entry, async_add_entities, cameras, hub)


class DashcamButton(DashcamSyncEntity, ButtonEntity):
    def __init__(
        self,
        coordinator: DashcamSyncCoordinator,
        key: str,
        camera_id: str | None,
        action: Callable[[], Awaitable[None]],
        category: EntityCategory | None = None,
    ) -> None:
        super().__init__(coordinator, key, camera_id)
        self._action = action
        self._attr_entity_category = category

    async def async_press(self) -> None:
        try:
            await self._action()
        except DashcamSyncError as err:
            raise HomeAssistantError(
                translation_domain="dashcam_sync",
                translation_key="action_failed",
                translation_placeholders={"error": str(err)},
            ) from err
        await self.coordinator.async_force_refresh()

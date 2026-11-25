"""Image entity: the thumbnail of the newest event clip."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from homeassistant.components.image import ImageEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from .coordinator import DashcamSyncConfigEntry, DashcamSyncCoordinator
from .entity import DashcamSyncEntity, async_add_entities_for_cameras
from .paths import resolve_clip_path

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: DashcamSyncConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    def cameras(coordinator: DashcamSyncCoordinator, camera_id: str) -> list[Entity]:
        return [LatestEventImage(hass, coordinator, camera_id)]

    async_add_entities_for_cameras(entry, async_add_entities, cameras)


class LatestEventImage(DashcamSyncEntity, ImageEntity):
    _attr_content_type = "image/jpeg"

    def __init__(
        self, hass: HomeAssistant, coordinator: DashcamSyncCoordinator, camera_id: str
    ) -> None:
        DashcamSyncEntity.__init__(self, coordinator, "latest_event", camera_id)
        ImageEntity.__init__(self, hass)

    @property
    def _clip(self) -> dict | None:
        return self.coordinator.latest_events.get(self.camera_id or "")

    @property
    def image_last_updated(self) -> datetime | None:
        clip = self._clip
        if clip is None:
            return None
        return dt_util.parse_datetime(clip.get("start", "")) or dt_util.utcnow()

    @property
    def available(self) -> bool:
        return super().available and self._clip is not None

    async def async_image(self) -> bytes | None:
        clip = self._clip
        if clip is None or not clip.get("thumbnail"):
            return None
        path = resolve_clip_path(clip["thumbnail"])
        if path is None:
            return None
        return await self.hass.async_add_executor_job(Path.read_bytes, path)

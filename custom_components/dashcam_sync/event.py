"""Event entity: clips and sync results as they happen."""

from __future__ import annotations

from typing import Any

from homeassistant.components.event import EventEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import EVENT_TYPES, MEDIA_PREFIX
from .coordinator import DashcamSyncConfigEntry, DashcamSyncCoordinator
from .entity import DashcamSyncEntity, async_add_entities_for_cameras

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: DashcamSyncConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    def cameras(coordinator: DashcamSyncCoordinator, camera_id: str) -> list[Entity]:
        return [ClipEvent(coordinator, camera_id)]

    async_add_entities_for_cameras(entry, async_add_entities, cameras)


class ClipEvent(DashcamSyncEntity, EventEntity):
    """Fires ``event_clip``, ``clip``, ``sync_completed`` and ``sync_failed``."""

    _attr_event_types = EVENT_TYPES

    def __init__(self, coordinator: DashcamSyncCoordinator, camera_id: str) -> None:
        super().__init__(coordinator, "clip", camera_id)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self.coordinator.async_add_event_listener(self._on_message))

    @callback
    def _on_message(self, message: dict[str, Any]) -> None:
        if message.get("camera") != self.camera_id:
            return
        kind = message.get("type")
        if kind == "clip":
            clip = message.get("clip")
            if not isinstance(clip, dict) or clip.get("id") is None:
                return
            event_type = "event_clip" if message.get("event_clip") else "clip"
            self._trigger_event(
                event_type,
                {
                    "path": clip.get("path"),
                    "kind": clip.get("kind"),
                    "channel": clip.get("channel"),
                    "duration": clip.get("duration"),
                    "media_content_id": f"{MEDIA_PREFIX}clip/{clip.get('id')}",
                },
            )
        elif kind == "session" and message.get("result") in ("sync_completed", "sync_failed"):
            attrs: dict[str, Any] = {}
            if message["result"] == "sync_completed":
                attrs = {"clips": message.get("clips"), "bytes": message.get("bytes")}
            else:
                attrs = {"reason": message.get("reason")}
            self._trigger_event(message["result"], attrs)
        else:
            return
        self.async_write_ha_state()

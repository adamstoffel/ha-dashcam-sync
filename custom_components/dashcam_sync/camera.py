"""Live camera entity: Core's stream worker opens the camera's RTSP URL directly."""

from __future__ import annotations

from homeassistant.components.camera import Camera
from homeassistant.components.camera.const import CameraEntityFeature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import CONF_LIVE_CAMERA
from .coordinator import DashcamSyncConfigEntry, DashcamSyncCoordinator
from .entity import DashcamSyncEntity, async_add_entities_for_cameras

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: DashcamSyncConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    if not entry.options.get(CONF_LIVE_CAMERA, True):
        return

    def cameras(coordinator: DashcamSyncCoordinator, camera_id: str) -> list[Entity]:
        return [LiveCamera(coordinator, camera_id)]

    async_add_entities_for_cameras(entry, async_add_entities, cameras)


class LiveCamera(DashcamSyncEntity, Camera):
    """Unavailable while the camera is out of range."""

    _attr_supported_features = CameraEntityFeature.STREAM

    def __init__(self, coordinator: DashcamSyncCoordinator, camera_id: str) -> None:
        DashcamSyncEntity.__init__(self, coordinator, "live", camera_id)
        Camera.__init__(self)

    @property
    def available(self) -> bool:
        return super().available and bool(
            self.camera_status.get("connected") and self.camera_status.get("live_url")
        )

    async def stream_source(self) -> str | None:
        url = self.camera_status.get("live_url")
        return str(url) if url else None

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        return None

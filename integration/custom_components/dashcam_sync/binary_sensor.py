"""Binary sensors: camera reachable, network storage offline."""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.components.binary_sensor.const import BinarySensorDeviceClass
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import DashcamSyncConfigEntry, DashcamSyncCoordinator
from .entity import DashcamSyncEntity, async_add_entities_for_cameras

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: DashcamSyncConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    def cameras(coordinator: DashcamSyncCoordinator, camera_id: str) -> list[Entity]:
        return [ConnectedSensor(coordinator, camera_id)]

    def hub(coordinator: DashcamSyncCoordinator) -> list[Entity]:
        return [NetworkTargetProblem(coordinator)]

    async_add_entities_for_cameras(entry, async_add_entities, cameras, hub)


class ConnectedSensor(DashcamSyncEntity, BinarySensorEntity):
    """The camera is reachable right now."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, coordinator: DashcamSyncCoordinator, camera_id: str) -> None:
        super().__init__(coordinator, "connected", camera_id)

    @property
    def is_on(self) -> bool:
        return bool(self.camera_status.get("connected"))


class NetworkTargetProblem(DashcamSyncEntity, BinarySensorEntity):
    """Problem when the network storage target is configured and offline."""

    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(self, coordinator: DashcamSyncCoordinator) -> None:
        super().__init__(coordinator, "network_target")

    @property
    def available(self) -> bool:
        if not super().available:
            return False
        return "network" in self.coordinator.hub.get("storage", {}).get("targets", {})

    @property
    def is_on(self) -> bool:
        target = self.coordinator.hub.get("storage", {}).get("targets", {}).get("network", {})
        return not target.get("online", True)

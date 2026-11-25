"""Sensors: sync progress, speed, timestamps and storage."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import EntityCategory, UnitOfDataRate, UnitOfInformation
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import SYNC_STATUSES
from .coordinator import DashcamSyncConfigEntry, DashcamSyncCoordinator
from .entity import DashcamSyncEntity, async_add_entities_for_cameras

PARALLEL_UPDATES = 0


def _timestamp(value: Any) -> datetime | None:
    return dt_util.parse_datetime(value) if isinstance(value, str) else None


@dataclass(frozen=True, kw_only=True)
class CameraSensorDescription(SensorEntityDescription):
    value_fn: Callable[[dict[str, Any]], Any]


@dataclass(frozen=True, kw_only=True)
class HubSensorDescription(SensorEntityDescription):
    value_fn: Callable[[dict[str, Any]], Any]
    network_only: bool = False


CAMERA_SENSORS: tuple[CameraSensorDescription, ...] = (
    CameraSensorDescription(
        key="sync_status",
        translation_key="sync_status",
        device_class=SensorDeviceClass.ENUM,
        options=SYNC_STATUSES,
        value_fn=lambda s: s.get("sync_status"),
    ),
    CameraSensorDescription(
        key="clips_remaining",
        translation_key="clips_remaining",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="clips",
        value_fn=lambda s: s.get("clips_remaining"),
    ),
    CameraSensorDescription(
        key="data_remaining",
        translation_key="data_remaining",
        device_class=SensorDeviceClass.DATA_SIZE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfInformation.BYTES,
        suggested_unit_of_measurement=UnitOfInformation.MEGABYTES,
        suggested_display_precision=1,
        value_fn=lambda s: s.get("bytes_remaining"),
    ),
    CameraSensorDescription(
        key="download_speed",
        translation_key="download_speed",
        device_class=SensorDeviceClass.DATA_RATE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfDataRate.BYTES_PER_SECOND,
        suggested_unit_of_measurement=UnitOfDataRate.KILOBYTES_PER_SECOND,
        suggested_display_precision=0,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda s: s.get("speed_bps"),
    ),
    CameraSensorDescription(
        key="last_sync",
        translation_key="last_sync",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda s: _timestamp(s.get("last_sync")),
    ),
    CameraSensorDescription(
        key="last_event_clip",
        translation_key="last_event_clip",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda s: _timestamp(s.get("last_event_clip")),
    ),
    CameraSensorDescription(
        key="sd_card_free",
        translation_key="sd_card_free",
        device_class=SensorDeviceClass.DATA_SIZE,
        native_unit_of_measurement=UnitOfInformation.BYTES,
        suggested_unit_of_measurement=UnitOfInformation.GIGABYTES,
        suggested_display_precision=1,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda s: s.get("sd_free_bytes"),
    ),
)


def _target(hub: dict[str, Any], name: str) -> dict[str, Any]:
    return hub.get("storage", {}).get("targets", {}).get(name, {})  # type: ignore[no-any-return]


def _storage_value(target: str, field: str) -> Callable[[dict[str, Any]], Any]:
    def value(hub: dict[str, Any]) -> Any:
        return _target(hub, target).get(field)

    return value


def _storage(key: str, target: str, field: str, network_only: bool = False) -> HubSensorDescription:
    return HubSensorDescription(
        key=key,
        translation_key=key,
        device_class=SensorDeviceClass.DATA_SIZE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfInformation.BYTES,
        suggested_unit_of_measurement=UnitOfInformation.GIGABYTES,
        suggested_display_precision=1,
        value_fn=_storage_value(target, field),
        network_only=network_only,
    )


HUB_SENSORS: tuple[HubSensorDescription, ...] = (
    _storage("local_storage_used", "local", "clip_bytes"),
    _storage("local_storage_free", "local", "free_bytes"),
    _storage("network_storage_used", "network", "clip_bytes", True),
    _storage("network_storage_free", "network", "free_bytes", True),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: DashcamSyncConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    def cameras(coordinator: DashcamSyncCoordinator, camera_id: str) -> list[Entity]:
        return [CameraSensor(coordinator, camera_id, d) for d in CAMERA_SENSORS]

    def hub(coordinator: DashcamSyncCoordinator) -> list[Entity]:
        has_network = "network" in coordinator.hub.get("storage", {}).get("targets", {})
        return [HubSensor(coordinator, d) for d in HUB_SENSORS if has_network or not d.network_only]

    async_add_entities_for_cameras(entry, async_add_entities, cameras, hub)


class CameraSensor(DashcamSyncEntity, SensorEntity):
    entity_description: CameraSensorDescription

    def __init__(
        self,
        coordinator: DashcamSyncCoordinator,
        camera_id: str,
        description: CameraSensorDescription,
    ) -> None:
        super().__init__(coordinator, description.key, camera_id)
        self.entity_description = description

    @property
    def native_value(self) -> Any:
        return self.entity_description.value_fn(self.camera_status)


class HubSensor(DashcamSyncEntity, SensorEntity):
    entity_description: HubSensorDescription

    def __init__(
        self, coordinator: DashcamSyncCoordinator, description: HubSensorDescription
    ) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> Any:
        return self.entity_description.value_fn(self.coordinator.hub)

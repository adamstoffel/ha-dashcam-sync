"""Actions: sync, keep_clip, unkeep_clip, delete_clip and purge."""

from __future__ import annotations

from typing import Any

import probatio as vol  # Home Assistant's validation engine since 2026.10
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.target import TargetSelection, async_extract_referenced_entity_ids

from .api import DashcamSyncApi, DashcamSyncError
from .const import (
    ATTR_DRY_RUN,
    ATTR_MEDIA_CONTENT_ID,
    DOMAIN,
    MEDIA_PREFIX,
    SERVICE_DELETE_CLIP,
    SERVICE_KEEP_CLIP,
    SERVICE_PURGE,
    SERVICE_SYNC,
    SERVICE_UNKEEP_CLIP,
)

CLIP_SCHEMA = vol.Schema({vol.Required(ATTR_MEDIA_CONTENT_ID): cv.string})
PURGE_SCHEMA = vol.Schema({vol.Optional(ATTR_DRY_RUN, default=False): cv.boolean})


def _api(hass: HomeAssistant) -> DashcamSyncApi:
    for entry in hass.config_entries.async_loaded_entries(DOMAIN):
        return entry.runtime_data.api  # type: ignore[no-any-return]
    raise ServiceValidationError(translation_domain=DOMAIN, translation_key="not_loaded")


def parse_clip_id(media_content_id: str) -> int:
    """``media-source://dashcam_sync/clip/<id>`` to the clip id."""
    if not media_content_id.startswith(f"{MEDIA_PREFIX}clip/"):
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="invalid_media_content_id",
            translation_placeholders={"value": media_content_id},
        )
    tail = media_content_id.removeprefix(f"{MEDIA_PREFIX}clip/")
    if not tail.isdigit():
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="invalid_media_content_id",
            translation_placeholders={"value": media_content_id},
        )
    return int(tail)


async def camera_ids_from_call(hass: HomeAssistant, call: ServiceCall) -> set[str]:
    """Resolve entity and device targets to camera ids."""
    referenced = async_extract_referenced_entity_ids(hass, TargetSelection(call.data))
    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)
    device_ids = set(referenced.referenced_devices)
    for entity_id in referenced.referenced | referenced.indirectly_referenced:
        entity = ent_reg.async_get(entity_id)
        if entity is not None and entity.device_id:
            device_ids.add(entity.device_id)
    known: set[str] = set()
    for entry in hass.config_entries.async_loaded_entries(DOMAIN):
        known.update(entry.runtime_data.coordinator.hub.get("cameras", {}))
    cameras: set[str] = set()
    for device_id in device_ids:
        device = dev_reg.async_get(device_id)
        if device is None:
            continue
        cameras.update(
            ident for domain, ident in device.identifiers if domain == DOMAIN and ident in known
        )
    return cameras


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    async def sync(call: ServiceCall) -> None:
        api = _api(hass)
        cameras = await camera_ids_from_call(hass, call)
        if not cameras:
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="no_camera")
        for camera in sorted(cameras):
            try:
                await api.async_sync(camera)
            except DashcamSyncError as err:
                raise _failed(err) from err

    async def keep(call: ServiceCall) -> None:
        clip_id = parse_clip_id(call.data[ATTR_MEDIA_CONTENT_ID])
        await _run(_api(hass).async_keep(clip_id, True))

    async def unkeep(call: ServiceCall) -> None:
        clip_id = parse_clip_id(call.data[ATTR_MEDIA_CONTENT_ID])
        await _run(_api(hass).async_keep(clip_id, False))

    async def delete(call: ServiceCall) -> None:
        clip_id = parse_clip_id(call.data[ATTR_MEDIA_CONTENT_ID])
        await _run(_api(hass).async_delete(clip_id))

    async def purge(call: ServiceCall) -> dict[str, Any]:
        result = await _run(_api(hass).async_purge(dry_run=call.data[ATTR_DRY_RUN]))
        return dict(result)

    hass.services.async_register(DOMAIN, SERVICE_SYNC, sync)
    hass.services.async_register(DOMAIN, SERVICE_KEEP_CLIP, keep, schema=CLIP_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_UNKEEP_CLIP, unkeep, schema=CLIP_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_DELETE_CLIP, delete, schema=CLIP_SCHEMA)
    hass.services.async_register(
        DOMAIN,
        SERVICE_PURGE,
        purge,
        schema=PURGE_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )


def _failed(err: Exception) -> HomeAssistantError:
    return HomeAssistantError(
        translation_domain=DOMAIN,
        translation_key="action_failed",
        translation_placeholders={"error": str(err)},
    )


async def _run(awaitable: Any) -> Any:
    try:
        return await awaitable
    except DashcamSyncError as err:
        raise _failed(err) from err

from __future__ import annotations

import pathlib
from unittest.mock import patch

import pytest
from homeassistant.components.media_player import BrowseError
from homeassistant.components.media_source import (
    Unresolvable,
    async_browse_media,
    async_resolve_media,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from custom_components.dashcam_sync.api import DashcamSyncRequestError
from custom_components.dashcam_sync.const import DOMAIN
from custom_components.dashcam_sync.services import parse_clip_id

from .conftest import FakeApi, setup_entry

CLIP_ID = "media-source://dashcam_sync/clip/7"


# ------------------------------------------------------------------- services


async def test_keep_unkeep_delete(
    hass: HomeAssistant, loaded: MockConfigEntry, fake_api: FakeApi
) -> None:
    for service in ("keep_clip", "unkeep_clip", "delete_clip"):
        await hass.services.async_call(
            DOMAIN, service, {"media_content_id": CLIP_ID}, blocking=True
        )
    assert fake_api.calls_named("keep") == [(7, True), (7, False)]
    assert fake_api.calls_named("delete") == [(7,)]


@pytest.mark.parametrize(
    "bad",
    [
        "https://example.com/x",
        "media-source://dashcam_sync/clip/abc",
        "media-source://other/clip/1",
    ],
)
async def test_bad_media_content_ids_are_rejected(
    hass: HomeAssistant, loaded: MockConfigEntry, bad: str
) -> None:
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, "keep_clip", {"media_content_id": bad}, blocking=True
        )
    with pytest.raises(ServiceValidationError):
        parse_clip_id(bad)


async def test_purge_returns_a_response_and_dry_run(
    hass: HomeAssistant, loaded: MockConfigEntry, fake_api: FakeApi
) -> None:
    result = await hass.services.async_call(
        DOMAIN, "purge", {"dry_run": True}, blocking=True, return_response=True
    )
    assert result["dry_run"] is True and result["count"] == 2
    await hass.services.async_call(DOMAIN, "purge", {}, blocking=True)
    assert fake_api.calls_named("purge") == [(True,), (False,)]


async def test_service_errors_are_translated(
    hass: HomeAssistant, loaded: MockConfigEntry, fake_api: FakeApi
) -> None:
    fake_api.error = DashcamSyncRequestError(404, "unknown clip")
    with pytest.raises(HomeAssistantError, match="unknown clip"):
        await hass.services.async_call(
            DOMAIN, "delete_clip", {"media_content_id": CLIP_ID}, blocking=True
        )


async def test_sync_action_targets_devices_and_entities(
    hass: HomeAssistant, loaded: MockConfigEntry, fake_api: FakeApi
) -> None:
    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, "family_car"), loaded.entry_id
    )
    assert device is not None
    await hass.services.async_call(DOMAIN, "sync", {"device_id": device.id}, blocking=True)
    await hass.services.async_call(
        DOMAIN, "sync", {"entity_id": "sensor.family_car_sync_status"}, blocking=True
    )
    assert fake_api.calls_named("sync") == [("family_car",), ("family_car",)]
    hub = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, loaded.entry_id), loaded.entry_id
    )
    assert hub is not None
    with pytest.raises(ServiceValidationError):  # the hub is not a camera
        await hass.services.async_call(DOMAIN, "sync", {"device_id": hub.id}, blocking=True)
    fake_api.error = DashcamSyncRequestError(409, "camera is not connected")
    with pytest.raises(HomeAssistantError, match="not connected"):
        await hass.services.async_call(DOMAIN, "sync", {"device_id": device.id}, blocking=True)


async def test_actions_need_a_loaded_entry(hass: HomeAssistant, fake_api: FakeApi) -> None:
    assert await async_setup_component(hass, DOMAIN, {})
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(DOMAIN, "purge", {}, blocking=True)


# --------------------------------------------------------------- media source


async def test_browse_cameras_days_filters_and_clips(
    hass: HomeAssistant, loaded: MockConfigEntry, fake_api: FakeApi
) -> None:
    assert await async_setup_component(hass, "media_source", {})
    root = await async_browse_media(hass, "media-source://dashcam_sync")
    assert [c.title for c in root.children] == ["Family car"]

    cam = await async_browse_media(hass, "media-source://dashcam_sync/family_car")
    assert [c.title for c in cam.children] == ["2026-11-22", "2026-11-21"]  # newest first

    day = await async_browse_media(hass, "media-source://dashcam_sync/family_car/2026-11-22")
    assert [c.title for c in day.children] == ["Event", "All"]

    clips = await async_browse_media(
        hass, "media-source://dashcam_sync/family_car/2026-11-22/event"
    )
    assert len(clips.children) == 1
    item = clips.children[0]
    assert item.can_play and item.media_content_id == CLIP_ID
    assert item.media_class == "video" and "event · front" in item.title
    assert (
        item.thumbnail
        and "/api/dashcam_sync/clip/7/thumb" in item.thumbnail
        and "authSig" in item.thumbnail
    )
    assert fake_api.calls_named("clips")  # day and kind filters reach the app
    all_clips = await async_browse_media(
        hass, "media-source://dashcam_sync/family_car/2026-11-22/all"
    )
    assert len(all_clips.children) == 1


async def test_browse_photo_and_clip_without_thumbnail(
    hass: HomeAssistant, loaded: MockConfigEntry, fake_api: FakeApi
) -> None:
    assert await async_setup_component(hass, "media_source", {})
    fake_api.clips.append({**fake_api.clips[0], "id": 8, "kind": "photo", "thumbnail": None})
    clips = await async_browse_media(hass, "media-source://dashcam_sync/family_car/2026-11-22/all")
    photo = next(c for c in clips.children if c.media_content_id.endswith("/8"))
    assert photo.media_class == "image" and photo.thumbnail is None


async def test_browse_errors(
    hass: HomeAssistant, loaded: MockConfigEntry, fake_api: FakeApi
) -> None:
    assert await async_setup_component(hass, "media_source", {})
    with pytest.raises(BrowseError):
        await async_browse_media(hass, "media-source://dashcam_sync/a/b/c/d")
    with pytest.raises(BrowseError):
        await async_browse_media(hass, "media-source://dashcam_sync/a/b/bogus")
    fake_api.error = DashcamSyncRequestError(500, "boom")
    with pytest.raises(BrowseError):
        await async_browse_media(hass, "media-source://dashcam_sync/family_car")


async def test_browse_when_the_integration_is_not_loaded(hass: HomeAssistant) -> None:
    assert await async_setup_component(hass, "media_source", {})
    assert await async_setup_component(hass, DOMAIN, {})
    with pytest.raises(BrowseError):
        await async_browse_media(hass, "media-source://dashcam_sync")


async def test_resolve(hass: HomeAssistant, loaded: MockConfigEntry, fake_api: FakeApi) -> None:
    assert await async_setup_component(hass, "media_source", {})
    media = await async_resolve_media(hass, CLIP_ID, None)
    assert media.mime_type == "video/mp4" and "/api/dashcam_sync/clip/7/file" in media.url
    # Signing happens in the media browser's websocket layer, so the relative URL is returned.
    fake_api.clips[0]["path"] = "/media/x/a.ts"
    assert (await async_resolve_media(hass, CLIP_ID, None)).mime_type == "video/mp2t"
    for bad in (
        "media-source://dashcam_sync/clip/999",
        "media-source://dashcam_sync/nope",
        "media-source://dashcam_sync/clip/x",
    ):
        with pytest.raises(Unresolvable):
            await async_resolve_media(hass, bad, None)
    fake_api.clips[0]["path"] = None
    with pytest.raises(Unresolvable):
        await async_resolve_media(hass, CLIP_ID, None)


# ----------------------------------------------------------------------- views


async def test_file_and_thumbnail_views(
    hass: HomeAssistant,
    fake_api: FakeApi,
    entry: MockConfigEntry,
    hass_client: ClientSessionGenerator,
    tmp_path: pathlib.Path,
) -> None:
    media = tmp_path / "media"
    media.mkdir()
    (media / "a.mp4").write_bytes(b"VIDEO-BYTES")
    (media / "a.jpg").write_bytes(b"JPEG")
    fake_api.clips[0]["path"] = str(media / "a.mp4")
    fake_api.clips[0]["thumbnail"] = str(media / "a.jpg")
    outside = tmp_path / "secret.txt"
    outside.write_text("nope")
    fake_api.clips.append({**fake_api.clips[0], "id": 9, "path": str(outside), "thumbnail": None})

    real = __import__("custom_components.dashcam_sync.paths", fromlist=["x"]).resolve_clip_path
    with patch(
        "custom_components.dashcam_sync.views.resolve_clip_path",
        side_effect=lambda p: real(p, (str(media),)),
    ):
        await setup_entry(hass, entry)
        client = await hass_client()
        resp = await client.get("/api/dashcam_sync/clip/7/file")
        assert resp.status == 200 and await resp.read() == b"VIDEO-BYTES"
        assert resp.headers["Content-Type"] == "video/mp4"
        resp = await client.get("/api/dashcam_sync/clip/7/thumb")
        assert resp.status == 200 and await resp.read() == b"JPEG"
        assert (await client.get("/api/dashcam_sync/clip/9/file")).status == 404  # outside /media
        assert (await client.get("/api/dashcam_sync/clip/404/file")).status == 404
        assert (await client.get("/api/dashcam_sync/clip/abc/file")).status == 404
        fake_api.clips[0]["thumbnail"] = None
        assert (await client.get("/api/dashcam_sync/clip/7/thumb")).status == 404


async def test_views_require_authentication(
    hass: HomeAssistant, loaded: MockConfigEntry, hass_client_no_auth: ClientSessionGenerator
) -> None:
    client = await hass_client_no_auth()
    assert (await client.get("/api/dashcam_sync/clip/7/file")).status == 401


async def test_view_404_when_not_loaded(
    hass: HomeAssistant, hass_client: ClientSessionGenerator
) -> None:
    assert await async_setup_component(hass, DOMAIN, {})
    client = await hass_client()
    assert (await client.get("/api/dashcam_sync/clip/1/file")).status == 404

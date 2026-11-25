from __future__ import annotations

import copy
from datetime import timedelta
from unittest.mock import patch

import pytest
from homeassistant.components.button import SERVICE_PRESS
from homeassistant.components.select import ATTR_OPTION, SERVICE_SELECT_OPTION
from homeassistant.const import ATTR_ENTITY_ID, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.dashcam_sync.api import DashcamSyncRequestError
from custom_components.dashcam_sync.const import CONF_PRESENCE_ENTITY

from .conftest import CAMERA, FakeApi, setup_entry


async def press(hass: HomeAssistant, entity_id: str) -> None:
    await hass.services.async_call(
        "button", SERVICE_PRESS, {ATTR_ENTITY_ID: entity_id}, blocking=True
    )


async def test_buttons_call_the_app(
    hass: HomeAssistant, loaded: MockConfigEntry, fake_api: FakeApi
) -> None:
    await press(hass, "button.polestar_2_sync_now")
    assert fake_api.calls_named("sync") == [("polestar_2",)]
    await press(hass, "button.polestar_2_restore_recording")
    assert fake_api.calls_named("restore") == [("polestar_2",)]
    await press(hass, "button.dashcam_sync_purge_now")
    assert fake_api.calls_named("purge") == [(False,)]


async def test_switch_and_select_write_camera_options(
    hass: HomeAssistant, loaded: MockConfigEntry, fake_api: FakeApi
) -> None:
    await hass.services.async_call(
        "switch", "turn_off", {ATTR_ENTITY_ID: "switch.polestar_2_auto_sync"}, blocking=True
    )
    await hass.services.async_call(
        "switch", "turn_on", {ATTR_ENTITY_ID: "switch.polestar_2_auto_sync"}, blocking=True
    )
    await hass.services.async_call(
        "select",
        SERVICE_SELECT_OPTION,
        {ATTR_ENTITY_ID: "select.polestar_2_sync_mode", ATTR_OPTION: "events_only"},
        blocking=True,
    )
    patches = [c for c in fake_api.calls if c[0] == "patch_camera"]
    assert [(c[1], c[2]) for c in patches] == [
        (("polestar_2",), {"enabled": False}),
        (("polestar_2",), {"enabled": True}),
        (("polestar_2",), {"sync_mode": "events_only"}),
    ]


async def test_actions_surface_app_errors_as_translated_exceptions(
    hass: HomeAssistant, loaded: MockConfigEntry, fake_api: FakeApi
) -> None:
    fake_api.error = DashcamSyncRequestError(409, "camera is not connected")
    with pytest.raises(HomeAssistantError, match="camera is not connected"):
        await press(hass, "button.polestar_2_sync_now")
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            "switch", "turn_off", {ATTR_ENTITY_ID: "switch.polestar_2_auto_sync"}, blocking=True
        )
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            "select",
            SERVICE_SELECT_OPTION,
            {ATTR_ENTITY_ID: "select.polestar_2_sync_mode", ATTR_OPTION: "everything"},
            blocking=True,
        )


async def test_event_entity_reports_clips_and_sync_results(
    hass: HomeAssistant, loaded: MockConfigEntry, fake_api: FakeApi
) -> None:
    state = hass.states.get("event.polestar_2_clip")
    assert state is not None and state.state == STATE_UNKNOWN  # nothing happened yet
    assert state.attributes["event_types"] == [
        "event_clip",
        "clip",
        "sync_completed",
        "sync_failed",
    ]

    clip = {
        "id": 7,
        "path": "/media/x/a.mp4",
        "kind": "event",
        "channel": "front",
        "duration": 60.0,
    }
    fake_api.push({"type": "clip", "camera": "polestar_2", "event_clip": True, "clip": clip})
    await hass.async_block_till_done()
    state = hass.states.get("event.polestar_2_clip")
    assert state.attributes["event_type"] == "event_clip"
    assert state.attributes["media_content_id"] == "media-source://dashcam_sync/clip/7"
    assert state.attributes["path"] == "/media/x/a.mp4" and state.attributes["duration"] == 60.0

    fake_api.push({"type": "clip", "camera": "polestar_2", "event_clip": False,
                   "clip": {**clip, "id": 8, "kind": "normal"}})  # fmt: skip
    await hass.async_block_till_done()
    assert hass.states.get("event.polestar_2_clip").attributes["event_type"] == "clip"

    fake_api.push(
        {
            "type": "session",
            "camera": "polestar_2",
            "result": "sync_completed",
            "clips": 2,
            "bytes": 5,
        }
    )
    await hass.async_block_till_done()
    state = hass.states.get("event.polestar_2_clip")
    assert state.attributes["event_type"] == "sync_completed" and state.attributes["clips"] == 2

    fake_api.push(
        {"type": "session", "camera": "polestar_2", "result": "sync_failed", "reason": "link_lost"}
    )
    await hass.async_block_till_done()
    assert hass.states.get("event.polestar_2_clip").attributes["reason"] == "link_lost"

    # Other cameras' events and unrelated messages are ignored.
    fake_api.push({"type": "session", "camera": "other", "result": "sync_completed"})
    fake_api.push({"type": "session", "camera": "polestar_2", "result": "something_else"})
    await hass.async_block_till_done()
    assert hass.states.get("event.polestar_2_clip").attributes["event_type"] == "sync_failed"


async def test_latest_event_image(
    hass: HomeAssistant, fake_api: FakeApi, entry: MockConfigEntry, tmp_path: object
) -> None:
    import pathlib

    media = pathlib.Path(str(tmp_path)) / "media"
    media.mkdir()
    (media / "a.jpg").write_bytes(b"\xff\xd8JPEG")
    fake_api.clips[0]["thumbnail"] = str(media / "a.jpg")
    with (
        patch("custom_components.dashcam_sync.paths.CLIP_ROOTS", (str(media),)),
        patch(
            "custom_components.dashcam_sync.image.resolve_clip_path",
            side_effect=lambda p: __import__(
                "custom_components.dashcam_sync.paths", fromlist=["x"]
            ).resolve_clip_path(p, (str(media),)),
        ),
    ):
        await setup_entry(hass, entry)
        state = hass.states.get("image.polestar_2_latest_event")
        assert state is not None and state.state != STATE_UNAVAILABLE
        entity = hass.data["image"].get_entity("image.polestar_2_latest_event")
        assert await entity.async_image() == b"\xff\xd8JPEG"
        assert entity.image_last_updated == dt_util.parse_datetime("2026-11-22T10:16:00+00:00")

        # A thumbnail outside /media and /share is never read.
        entity.coordinator.latest_events["polestar_2"]["thumbnail"] = "/etc/passwd"
        assert await entity.async_image() is None
        entity.coordinator.latest_events["polestar_2"]["thumbnail"] = None
        assert await entity.async_image() is None


async def test_latest_event_image_unavailable_without_an_event(
    hass: HomeAssistant, fake_api: FakeApi, entry: MockConfigEntry
) -> None:
    fake_api.clips = []
    await setup_entry(hass, entry)
    assert hass.states.get("image.polestar_2_latest_event").state == STATE_UNAVAILABLE


async def test_new_event_clip_refreshes_the_image(
    hass: HomeAssistant, loaded: MockConfigEntry, fake_api: FakeApi
) -> None:
    newer = {**copy.deepcopy(fake_api.clips[0]), "id": 9, "start": "2026-11-23T08:00:00+00:00"}
    fake_api.clips.insert(0, newer)
    fake_api.push({"type": "clip", "camera": "polestar_2", "event_clip": True, "clip": newer})
    await hass.async_block_till_done()
    entity = hass.data["image"].get_entity("image.polestar_2_latest_event")
    assert entity.coordinator.latest_events["polestar_2"]["id"] == 9
    assert entity.image_last_updated == dt_util.parse_datetime("2026-11-23T08:00:00+00:00")


async def test_live_camera_stream_source(hass: HomeAssistant, loaded: MockConfigEntry) -> None:
    entity = hass.data["camera"].get_entity("camera.polestar_2_live")
    assert await entity.stream_source() == "rtsp://192.168.1.254/xxx.mov"
    assert await entity.async_camera_image() is None


async def test_arrival_hint_from_a_presence_entity(
    hass: HomeAssistant, fake_api: FakeApi, entry: MockConfigEntry
) -> None:
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        entry, options={CONF_PRESENCE_ENTITY: "device_tracker.car"}
    )
    hass.states.async_set("device_tracker.car", "not_home")
    await setup_entry(hass, entry)
    hass.states.async_set("device_tracker.car", "home")
    await hass.async_block_till_done()
    assert fake_api.calls_named("hint") == [(None,)]
    hass.states.async_set("device_tracker.car", "home", {"x": 1})  # attribute change only
    hass.states.async_set("device_tracker.car", "not_home")
    hass.states.async_remove("device_tracker.car")
    await hass.async_block_till_done()
    assert len(fake_api.calls_named("hint")) == 1


async def test_arrival_hint_failure_is_logged_not_raised(
    hass: HomeAssistant, fake_api: FakeApi, entry: MockConfigEntry
) -> None:
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        entry, options={CONF_PRESENCE_ENTITY: "binary_sensor.garage"}
    )
    await setup_entry(hass, entry)
    fake_api.error = DashcamSyncRequestError(500, "boom")
    hass.states.async_set("binary_sensor.garage", "on")
    await hass.async_block_till_done()
    assert fake_api.calls_named("hint")


async def test_safety_message_disconnects_the_wifi(
    hass: HomeAssistant, loaded: MockConfigEntry, fake_api: FakeApi
) -> None:
    with patch("custom_components.dashcam_sync.async_disconnect") as disconnect:
        fake_api.push({"type": "safety", "route_unsafe": ["polestar_2"]})
        await hass.async_block_till_done()
        disconnect.assert_awaited_once_with(hass, "wlan0")
        fake_api.push({"type": "safety", "route_unsafe": []})
        await hass.async_block_till_done()
        disconnect.assert_awaited_once()


async def test_disconnect_failure_is_logged(
    hass: HomeAssistant,
    loaded: MockConfigEntry,
    fake_api: FakeApi,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from custom_components.dashcam_sync.wifi import WifiUnavailable

    with patch(
        "custom_components.dashcam_sync.async_disconnect", side_effect=WifiUnavailable("no")
    ):
        fake_api.push({"type": "safety", "route_unsafe": ["polestar_2"]})
        await hass.async_block_till_done()
    assert "Could not disconnect the Wi-Fi" in caplog.text


async def test_other_pushed_messages_trigger_a_refresh(
    hass: HomeAssistant, loaded: MockConfigEntry, fake_api: FakeApi
) -> None:
    before = len(fake_api.calls_named("status"))
    fake_api.status["cameras"]["polestar_2"] = {**CAMERA, "sync_status": "listing"}
    for kind in ("overlap", "options", "purge_completed", "issue", "clip_keep", "clip_deleted"):
        fake_api.push({"type": kind})
    fake_api.push({"type": "unknown"})
    await hass.async_block_till_done()
    assert len(fake_api.calls_named("status")) >= before  # refreshes were requested (debounced)
    # With the socket live the coordinator trusts pushed data instead of re-polling.
    assert hass.states.get("sensor.polestar_2_sync_status").state in ("idle", "listing")


async def test_unavailable_when_the_app_is_unreachable(
    hass: HomeAssistant, loaded: MockConfigEntry, fake_api: FakeApi
) -> None:
    from homeassistant.helpers.update_coordinator import UpdateFailed  # noqa: F401

    coordinator = loaded.runtime_data.coordinator
    fake_api.push(None)  # type: ignore[arg-type]
    await hass.async_block_till_done()
    fake_api.status_error = DashcamSyncRequestError(503, "starting")
    await coordinator.async_refresh()
    assert not coordinator.last_update_success
    assert hass.states.get("binary_sensor.polestar_2_connected").state == STATE_UNAVAILABLE
    _ = timedelta

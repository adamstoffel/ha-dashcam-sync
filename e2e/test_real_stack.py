from __future__ import annotations

import asyncio
import pathlib
from unittest.mock import patch

import pytest
from homeassistant.components.media_player import BrowseError
from homeassistant.components.media_source import (
    async_browse_media,
    async_resolve_media,
)
from homeassistant.const import STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from custom_components.dashcam_sync.const import DOMAIN
from custom_components.dashcam_sync.paths import resolve_clip_path
from fakecam import make_bytes

from conftest import Stack


async def wait_for(hass: HomeAssistant, predicate, timeout: float = 15.0) -> None:  # type: ignore[no-untyped-def]
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.05)
        await hass.async_block_till_done()
    raise AssertionError("timed out waiting for the stack")


async def synced(hass: HomeAssistant, stack: Stack) -> None:
    await wait_for(
        hass, lambda: stack.daemon.workers["polestar_2"].status.last_sync is not None
    )
    await stack.daemon.post.drain()  # type: ignore[union-attr]
    await wait_for(
        hass,
        lambda: (
            (s := hass.states.get("sensor.polestar_2_last_sync")) is not None
            and s.state not in ("unknown", "unavailable")
        ),
    )


async def test_camera_to_entities(hass: HomeAssistant, stack: Stack) -> None:
    await synced(hass, stack)
    assert (
        stack.camera.recording and stack.camera.mode == "movie"
    )  # restored after the session
    assert hass.states.get("binary_sensor.polestar_2_connected").state == STATE_ON
    assert hass.states.get("sensor.polestar_2_sync_status").state == "idle"
    assert hass.states.get("sensor.polestar_2_clips_remaining").state == "0"
    assert hass.states.get("select.polestar_2_sync_mode").state == "everything"
    assert hass.states.get("switch.polestar_2_auto_sync").state == STATE_ON
    assert float(hass.states.get("sensor.dashcam_sync_local_storage_used").state) >= 0
    assert hass.states.get("camera.polestar_2_live") is not None
    event = hass.states.get("sensor.polestar_2_last_event_clip")
    assert event.state.startswith("2026-11-22T")  # the RO/ clip is an event


async def test_media_browser_resolves_real_clips(
    hass: HomeAssistant, stack: Stack
) -> None:
    await synced(hass, stack)
    assert await async_setup_component(hass, "media_source", {})
    root = await async_browse_media(hass, "media-source://dashcam_sync")
    assert [c.title for c in root.children] == ["Polestar 2"]
    days = await async_browse_media(hass, "media-source://dashcam_sync/polestar_2")
    assert [c.title for c in days.children] == ["2026-11-22"]
    clips = await async_browse_media(
        hass, "media-source://dashcam_sync/polestar_2/2026-11-22/event"
    )
    assert len(clips.children) == 1 and "event" in clips.children[0].title
    everything = await async_browse_media(
        hass, "media-source://dashcam_sync/polestar_2/2026-11-22/all"
    )
    assert len(everything.children) == 2
    media = await async_resolve_media(hass, clips.children[0].media_content_id, None)
    assert media.mime_type == "video/mp4"
    with pytest.raises(BrowseError):
        await async_browse_media(
            hass, "media-source://dashcam_sync/polestar_2/2026-11-22/bogus"
        )


async def test_clip_file_is_served_byte_for_byte(
    hass: HomeAssistant, stack: Stack, hass_client: ClientSessionGenerator
) -> None:
    await synced(hass, stack)
    clip = next(
        c
        for c in stack.daemon.cat.list_clips(camera="polestar_2")
        if c.kind.value == "event"
    )
    media = stack.media
    real = resolve_clip_path
    with patch(
        "custom_components.dashcam_sync.views.resolve_clip_path",
        side_effect=lambda p: real(p, (str(media),)),
    ):
        client = await hass_client()
        resp = await client.get(f"/api/dashcam_sync/clip/{clip.id}/file")
        assert resp.status == 200
        assert await resp.read() == make_bytes("20261122_101600_F.MP4", 30_000)
        assert (await client.get("/api/dashcam_sync/clip/99999/file")).status == 404


async def test_actions_reach_the_real_app(hass: HomeAssistant, stack: Stack) -> None:
    await synced(hass, stack)
    clip = next(
        c
        for c in stack.daemon.cat.list_clips(camera="polestar_2")
        if c.kind.value == "normal"
    )
    content_id = f"media-source://dashcam_sync/clip/{clip.id}"
    await hass.services.async_call(
        DOMAIN, "keep_clip", {"media_content_id": content_id}, blocking=True
    )
    assert stack.daemon.cat.get_clip(clip.id).keep is True  # type: ignore[union-attr]
    await hass.services.async_call(
        DOMAIN, "unkeep_clip", {"media_content_id": content_id}, blocking=True
    )
    assert stack.daemon.cat.get_clip(clip.id).keep is False  # type: ignore[union-attr]

    result = await hass.services.async_call(
        DOMAIN, "purge", {"dry_run": True}, blocking=True, return_response=True
    )
    assert result["dry_run"] is True and result["count"] == 0

    path = pathlib.Path((stack.daemon.clip_payload(clip.id) or {})["path"])
    assert path.exists()
    await hass.services.async_call(
        DOMAIN, "delete_clip", {"media_content_id": content_id}, blocking=True
    )
    assert stack.daemon.cat.get_clip(clip.id).status == "deleted" and not path.exists()  # type: ignore[union-attr]

    # The camera still lists the file on its card: a new sync must not bring the clip back.
    stack.camera.requests_files.clear()  # type: ignore[attr-defined]
    await hass.services.async_call(
        "button", "press", {"entity_id": "button.polestar_2_sync_now"}, blocking=True
    )
    await wait_for(
        hass,
        lambda: (
            stack.daemon.workers["polestar_2"].status.sync_status == "idle"
            and len(stack.daemon.cat.recent_sessions(10)) >= 2
        ),
    )
    await asyncio.sleep(0.3)
    assert not path.exists()
    assert not any(str(clip.id) in str(u) for u in stack.camera.requests_files)  # type: ignore[attr-defined]
    assert all(
        c.id != clip.id for c in stack.daemon.cat.list_clips(camera="polestar_2")
    )


async def test_switch_and_select_round_trip_through_the_app_options(
    hass: HomeAssistant, stack: Stack
) -> None:
    await synced(hass, stack)
    await hass.services.async_call("select", "select_option",
                                   {"entity_id": "select.polestar_2_sync_mode", "option": "events_only"}, blocking=True)  # fmt: skip
    await wait_for(
        hass,
        lambda: hass.states.get("select.polestar_2_sync_mode").state == "events_only",
    )
    assert stack.daemon.options.camera("polestar_2").sync_mode == "events_only"  # type: ignore[union-attr]

    await hass.services.async_call(
        "switch",
        "turn_off",
        {"entity_id": "switch.polestar_2_auto_sync"},
        blocking=True,
    )
    await wait_for(
        hass, lambda: hass.states.get("switch.polestar_2_auto_sync").state == "off"
    )
    assert stack.daemon.options.camera("polestar_2").enabled is False  # type: ignore[union-attr]


async def test_push_updates_arrive_from_the_real_websocket(
    hass: HomeAssistant, stack: Stack
) -> None:
    await synced(hass, stack)
    coordinator = stack.entry.runtime_data.coordinator
    await wait_for(hass, lambda: coordinator.ws_connected)
    events: list[dict] = []
    stack.entry.runtime_data.coordinator.async_add_event_listener(events.append)
    await hass.services.async_call(
        "button", "press", {"entity_id": "button.polestar_2_sync_now"}, blocking=True
    )
    await wait_for(hass, lambda: any(e.get("type") == "session" for e in events))
    assert any(
        e.get("result") == "sync_completed" for e in events if e["type"] == "session"
    )


async def test_sync_now_when_out_of_range_is_a_clean_error(
    hass: HomeAssistant, stack: Stack
) -> None:
    from homeassistant.exceptions import HomeAssistantError

    await synced(hass, stack)

    # The car drives away: the camera stops answering. Simulated at the link check, not by
    # shutting the fake camera's server down, whose shutdown time varies between machines.
    async def out_of_range(_camera: object) -> bool:
        return False

    stack.daemon.workers["polestar_2"].ctx.link_up = out_of_range  # type: ignore[assignment]
    await wait_for(
        hass,
        lambda: hass.states.get("binary_sensor.polestar_2_connected").state == "off",
        20,
    )
    with pytest.raises(HomeAssistantError, match="not connected"):
        await hass.services.async_call(
            "button",
            "press",
            {"entity_id": "button.polestar_2_sync_now"},
            blocking=True,
        )


async def test_app_issues_become_repairs(hass: HomeAssistant, stack: Stack) -> None:
    await synced(hass, stack)
    stack.daemon.workers["polestar_2"].status.unsupported = True
    await stack.entry.runtime_data.coordinator.async_refresh()
    registry = ir.async_get(hass)
    await wait_for(
        hass,
        lambda: (
            registry.async_get_issue(DOMAIN, "unsupported_camera_polestar_2")
            is not None
        ),
    )

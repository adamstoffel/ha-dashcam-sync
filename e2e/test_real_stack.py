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


async def wait_for(hass: HomeAssistant, predicate, timeout: float = 15.0, describe=None) -> None:  # type: ignore[no-untyped-def]
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.05)
        await hass.async_block_till_done()
    detail = f": {describe()}" if describe else ""
    raise AssertionError(f"timed out waiting for the stack{detail}")


def stack_state(hass: HomeAssistant, stack: Stack) -> str:
    """Everything needed to see where an update got stuck between the app and Home Assistant."""
    coordinator = stack.entry.runtime_data.coordinator
    worker = stack.daemon.workers["family_car"].status
    cam = (coordinator.data or {}).get("cameras", {}).get("family_car", {})
    state = hass.states.get("sensor.family_car_last_sync")
    return (
        f"app last_sync={worker.last_sync} sync_status={worker.sync_status}; "
        f"ws_connected={coordinator.ws_connected} bus_subscribers={stack.daemon.bus.subscribers} "
        f"last_update_success={coordinator.last_update_success}; "
        f"coordinator last_sync={cam.get('last_sync')} sync_status={cam.get('sync_status')}; "
        f"entity state={state.state if state else None}"
    )


async def synced(hass: HomeAssistant, stack: Stack) -> None:
    await wait_for(
        hass,
        lambda: stack.daemon.workers["family_car"].status.last_sync is not None,
        describe=lambda: stack_state(hass, stack),
    )
    await stack.daemon.post.drain()  # type: ignore[union-attr]
    await wait_for(
        hass,
        lambda: (
            (s := hass.states.get("sensor.family_car_last_sync")) is not None
            and s.state not in ("unknown", "unavailable")
        ),
        describe=lambda: stack_state(hass, stack),
    )


async def test_camera_to_entities(hass: HomeAssistant, stack: Stack) -> None:
    await synced(hass, stack)
    assert (
        stack.camera.recording and stack.camera.mode == "movie"
    )  # restored after the session
    assert hass.states.get("binary_sensor.family_car_connected").state == STATE_ON
    assert hass.states.get("sensor.family_car_sync_status").state == "idle"
    assert hass.states.get("sensor.family_car_clips_remaining").state == "0"
    assert hass.states.get("select.family_car_sync_mode").state == "everything"
    assert hass.states.get("switch.family_car_auto_sync").state == STATE_ON
    assert float(hass.states.get("sensor.dashcam_sync_local_storage_used").state) >= 0
    assert hass.states.get("camera.family_car_live") is not None
    event = hass.states.get("sensor.family_car_last_event_clip")
    assert event.state.startswith("2026-11-22T")  # the RO/ clip is an event
    # The real integration tells the real app which release it is: a matched pair, so the
    # panel shows no pairing warning and Home Assistant raises no release repair.
    pairing = stack.daemon.integration_payload()
    assert pairing["connected"] and pairing["matches"], pairing
    from homeassistant.helpers import issue_registry as ir

    assert ir.async_get(hass).async_get_issue("dashcam_sync", "release_mismatch") is None


async def test_media_browser_resolves_real_clips(
    hass: HomeAssistant, stack: Stack
) -> None:
    await synced(hass, stack)
    assert await async_setup_component(hass, "media_source", {})
    root = await async_browse_media(hass, "media-source://dashcam_sync")
    assert [c.title for c in root.children] == ["Family car"]
    days = await async_browse_media(hass, "media-source://dashcam_sync/family_car")
    assert [c.title for c in days.children] == ["2026-11-22"]
    clips = await async_browse_media(
        hass, "media-source://dashcam_sync/family_car/2026-11-22/event"
    )
    assert len(clips.children) == 1 and "event" in clips.children[0].title
    everything = await async_browse_media(
        hass, "media-source://dashcam_sync/family_car/2026-11-22/all"
    )
    assert len(everything.children) == 2
    media = await async_resolve_media(hass, clips.children[0].media_content_id, None)
    assert media.mime_type == "video/mp4"
    with pytest.raises(BrowseError):
        await async_browse_media(
            hass, "media-source://dashcam_sync/family_car/2026-11-22/bogus"
        )


async def test_clip_file_is_served_byte_for_byte(
    hass: HomeAssistant, stack: Stack, hass_client: ClientSessionGenerator
) -> None:
    await synced(hass, stack)
    clip = next(
        c
        for c in stack.daemon.cat.list_clips(camera="family_car")
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
        for c in stack.daemon.cat.list_clips(camera="family_car")
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
        "button", "press", {"entity_id": "button.family_car_sync_now"}, blocking=True
    )
    await wait_for(
        hass,
        lambda: (
            stack.daemon.workers["family_car"].status.sync_status == "idle"
            and len(stack.daemon.cat.recent_sessions(10)) >= 2
        ),
    )
    await asyncio.sleep(0.3)
    assert not path.exists()
    assert not any(str(clip.id) in str(u) for u in stack.camera.requests_files)  # type: ignore[attr-defined]
    assert all(
        c.id != clip.id for c in stack.daemon.cat.list_clips(camera="family_car")
    )


async def test_switch_and_select_round_trip_through_the_app_options(
    hass: HomeAssistant, stack: Stack
) -> None:
    await synced(hass, stack)
    await hass.services.async_call("select", "select_option",
                                   {"entity_id": "select.family_car_sync_mode", "option": "events_only"}, blocking=True)  # fmt: skip
    await wait_for(
        hass,
        lambda: hass.states.get("select.family_car_sync_mode").state == "events_only",
    )
    assert stack.daemon.options.camera("family_car").sync_mode == "events_only"  # type: ignore[union-attr]

    await hass.services.async_call(
        "switch",
        "turn_off",
        {"entity_id": "switch.family_car_auto_sync"},
        blocking=True,
    )
    await wait_for(
        hass, lambda: hass.states.get("switch.family_car_auto_sync").state == "off"
    )
    assert stack.daemon.options.camera("family_car").enabled is False  # type: ignore[union-attr]


async def test_push_updates_arrive_from_the_real_websocket(
    hass: HomeAssistant, stack: Stack
) -> None:
    await synced(hass, stack)
    coordinator = stack.entry.runtime_data.coordinator
    await wait_for(hass, lambda: coordinator.ws_connected)
    events: list[dict] = []
    stack.entry.runtime_data.coordinator.async_add_event_listener(events.append)
    await hass.services.async_call(
        "button", "press", {"entity_id": "button.family_car_sync_now"}, blocking=True
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

    stack.daemon.workers["family_car"].ctx.link_up = out_of_range  # type: ignore[assignment]
    await wait_for(
        hass,
        lambda: hass.states.get("binary_sensor.family_car_connected").state == "off",
        20,
    )
    with pytest.raises(HomeAssistantError, match="not connected"):
        await hass.services.async_call(
            "button",
            "press",
            {"entity_id": "button.family_car_sync_now"},
            blocking=True,
        )


async def test_app_issues_become_repairs(hass: HomeAssistant, stack: Stack) -> None:
    await synced(hass, stack)
    stack.daemon.workers["family_car"].status.unsupported = True
    await stack.entry.runtime_data.coordinator.async_refresh()
    registry = ir.async_get(hass)
    await wait_for(
        hass,
        lambda: (
            registry.async_get_issue(DOMAIN, "unsupported_camera_family_car")
            is not None
        ),
    )


async def test_the_panel_asks_home_assistant_to_join_a_hotspot(
    hass: HomeAssistant, stack: Stack
) -> None:
    """Panel → app → push → integration joins (Supervisor stubbed) → result → panel."""
    from unittest.mock import AsyncMock

    from custom_components.dashcam_sync.wifi import WifiInterface

    await synced(hass, stack)
    await wait_for(hass, lambda: stack.entry.runtime_data.coordinator.ws_connected)

    def add(raw: dict) -> None:
        raw["cameras"].append(
            {"id": "hot", "name": "Hot", "connection": "hotspot", "interface": "wlan0",
             "ssid": "CARDV", "wifi_password": "pw", "host": "127.0.0.1:9",
             "driver": "auto", "enabled": True}  # fmt: skip
        )

    await stack.daemon.update_raw_options(add)
    cam = stack.daemon.options.camera("hot")
    assert cam is not None
    apply = AsyncMock()

    def adapters(*_args: object, **_kw: object) -> list[WifiInterface]:
        # The link comes up once the profile is applied: the integration checks for that.
        up = apply.await_count > 0
        return [WifiInterface("wlan0", up, True, "CARDV" if up else None, ready=up)]

    W = "custom_components.dashcam_sync.wifi"
    seen: list[str] = []
    record = stack.daemon.set_join_result

    def recording(camera_id: str, state: str, reason: str | None) -> None:
        seen.append(state)
        record(camera_id, state, reason)

    async def ask() -> dict:
        assert stack.daemon.request_join(cam)["state"] == "requested"  # type: ignore[arg-type]
        await wait_for(
            hass, lambda: stack.daemon.joins["hot"]["state"] not in ("requested", "connecting")
        )
        return stack.daemon.joins["hot"]

    with (
        patch(f"{W}.async_list_wifi_interfaces", side_effect=adapters),
        patch(f"{W}.async_apply_profile", apply),
        patch(f"{W}.JOIN_VERIFY_POLL", 0.05),
        patch.object(stack.daemon, "set_join_result", recording),
    ):
        # The real address check: 127.0.0.1 collides with this machine, so no join.
        refused = await ask()
        assert refused["state"] == "failed" and refused["reason"] == "camera_collides"
        apply.assert_not_awaited()

        # A camera whose block is free (the live address probe is stubbed: it depends
        # on the network the tests run on) is joined, and the panel sees it.
        from dashcam_sync.netblock import OverlapReport

        async def free(camera_id: str, **_kw: object) -> OverlapReport:
            return OverlapReport("no_overlap", stack.daemon.block_for(cam))

        with patch.object(stack.daemon, "run_overlap_check", free):
            joined = await ask()
    assert joined["state"] == "joined", joined
    assert seen[-2:] == ["connecting", "joined"]  # the panel shows the link coming up
    profile = apply.await_args.args[1]
    assert profile.ssid == "CARDV" and profile.password == "pw" and profile.prefix == 30


async def test_the_wizard_scans_through_home_assistant(hass: HomeAssistant, stack: Stack) -> None:
    """Wizard → app → push → integration scans (Supervisor client stubbed) → app → wizard."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    await synced(hass, stack)
    await wait_for(hass, lambda: stack.entry.runtime_data.coordinator.ws_connected)
    aps = [SimpleNamespace(ssid="CARDV-1234", signal=58, mode="infrastructure")]
    client = SimpleNamespace(network=SimpleNamespace(access_points=AsyncMock(return_value=aps)))
    with patch("custom_components.dashcam_sync.wifi._client", return_value=client):
        task = hass.async_create_task(stack.daemon.scan_via_home_assistant("wlp4s0"))
        await wait_for(hass, task.done)
    assert task.result() == {
        "accesspoints": [{"ssid": "CARDV-1234", "signal": 58, "mode": "infrastructure"}]
    }
    client.network.access_points.assert_awaited_once_with("wlp4s0")

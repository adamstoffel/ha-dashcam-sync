"""Regression tests for defects found by an independent code review."""

from __future__ import annotations

import asyncio
import copy
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant import config_entries
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.dashcam_sync.api import DashcamSyncRequestError
from custom_components.dashcam_sync.const import CONF_LIVE_CAMERA, CONF_PRESENCE_ENTITY, DOMAIN

from .conftest import CAMERA, FakeApi, setup_entry


async def test_malformed_push_messages_do_not_end_live_updates(
    hass: HomeAssistant,
    loaded: MockConfigEntry,
    fake_api: FakeApi,
    caplog: pytest.LogCaptureFixture,
) -> None:
    for bad in (
        {"type": "camera"},
        {"type": "camera", "camera": "family_car", "status": "not a dict"},
        {"type": "clip", "camera": "family_car", "event_clip": True, "clip": None},
        {"type": "clip", "event_clip": True, "clip": {"id": 1}},
        {"type": "hub", "storage": 5, "issues": "x"},  # wrong shapes
        {"type": "snapshot", "cameras": None},
    ):
        fake_api.push(bad)
    await hass.async_block_till_done()
    # The socket is still alive: a good message is applied afterwards.
    fake_api.push(
        {"type": "camera", "camera": "family_car", "status": {**CAMERA, "sync_status": "listing"}}
    )
    await hass.async_block_till_done()
    assert hass.states.get("sensor.family_car_sync_status").state == "listing"
    assert hass.config_entries.async_get_entry(loaded.entry_id).state.value == "loaded"


async def test_pushed_events_cause_a_real_refresh_even_with_a_live_socket(
    hass: HomeAssistant, loaded: MockConfigEntry, fake_api: FakeApi
) -> None:
    before = len(fake_api.calls_named("status"))
    fake_api.status["cameras"]["family_car"] = {**CAMERA, "sync_status": "downloading"}
    fake_api.push({"type": "safety", "route_unsafe": []})
    await hass.async_block_till_done()
    assert len(fake_api.calls_named("status")) > before  # used to be answered from the stale cache
    assert hass.states.get("sensor.family_car_sync_status").state == "downloading"


async def test_a_clip_message_without_a_clip_fires_no_event(
    hass: HomeAssistant, loaded: MockConfigEntry, fake_api: FakeApi
) -> None:
    from homeassistant.const import STATE_UNKNOWN

    fake_api.push({"type": "clip", "camera": "family_car", "event_clip": True, "clip": None})
    await hass.async_block_till_done()
    assert hass.states.get("event.family_car_clip").state == STATE_UNKNOWN  # nothing fired


async def test_rotating_the_token_does_not_change_the_identity(
    hass: HomeAssistant, fake_api: FakeApi, loaded: MockConfigEntry
) -> None:
    """The unique id is the app's instance id, which survives a token change."""
    result = await loaded.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"host": "local-dashcam-sync", "port": 8100, "token": "a-brand-new-token"},
    )
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "reconfigure_successful"
    assert loaded.data["token"] == "a-brand-new-token" and loaded.unique_id == "inst123"


async def test_wrong_shaped_responses_fail_cleanly(hass: HomeAssistant, fake_api: FakeApi) -> None:
    from custom_components.dashcam_sync.api import _as_dict, _as_list

    for convert, bad in ((_as_dict, None), (_as_dict, []), (_as_list, {}), (_as_list, None)):
        with pytest.raises(DashcamSyncRequestError):
            convert(bad)
    # A flow pointed at something that is not the app reports an error, it does not crash.
    fake_api.error = DashcamSyncRequestError(502, "the app did not answer with JSON")
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"host": "h", "port": 80, "token": "t"}
    )
    assert result["type"] is FlowResultType.FORM and result["errors"] == {"base": "unknown"}
    # Setup keeps retrying instead of failing for good.
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id="x", data={"host": "h", "port": 8100, "token": "t"}
    )
    entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is config_entries.ConfigEntryState.SETUP_RETRY


async def test_the_arrival_hint_entity_can_be_cleared(
    hass: HomeAssistant, loaded: MockConfigEntry
) -> None:
    hass.config_entries.async_update_entry(
        loaded, options={CONF_PRESENCE_ENTITY: "device_tracker.car", CONF_LIVE_CAMERA: True}
    )
    result = await hass.config_entries.options.async_init(loaded.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "general"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_LIVE_CAMERA: True}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert CONF_PRESENCE_ENTITY not in loaded.options  # emptied field means cleared


async def test_the_kill_switch_keeps_going_after_one_camera_fails(
    hass: HomeAssistant, loaded: MockConfigEntry, fake_api: FakeApi
) -> None:
    attempted: list[str] = []

    async def wifi(camera: str) -> dict:
        attempted.append(camera)
        if camera == "first":
            return {}  # no "interface" key: used to raise KeyError and skip the rest
        return {"interface": "wlan1"}

    fake_api.async_wifi = wifi  # type: ignore[method-assign]
    with patch("custom_components.dashcam_sync.async_disconnect", new=AsyncMock()) as disconnect:
        fake_api.push({"type": "safety", "route_unsafe": ["first", "second"]})
        await hass.async_block_till_done()
    assert attempted == ["first", "second"]
    disconnect.assert_awaited_once_with(hass, "wlan1")


async def test_unknown_overlap_results_get_a_translated_error(
    hass: HomeAssistant, loaded: MockConfigEntry, fake_api: FakeApi
) -> None:
    fake_api.overlap = {"result": "something_new", "blocked": True, "conflicts": []}
    result = await hass.config_entries.options.async_init(loaded.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "wifi"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"camera": "family_car"}
    )
    assert result["errors"] == {"base": "overlap_blocked"}
    fake_api.overlap = {"blocked": True}  # no result at all
    result = await hass.config_entries.options.async_init(loaded.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "wifi"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"camera": "family_car"}
    )
    assert result["errors"] == {"base": "overlap_blocked"}


async def test_network_storage_entities_appear_when_the_target_is_added_later(
    hass: HomeAssistant, loaded: MockConfigEntry, fake_api: FakeApi
) -> None:
    assert hass.states.get("sensor.dashcam_sync_network_storage_used") is None
    storage = copy.deepcopy(fake_api.status["storage"])
    storage["targets"]["network"] = {"name": "network", "online": True, "total_bytes": 10**12,
                                     "free_bytes": 5 * 10**11, "clip_bytes": 3 * 10**9, "reason": None}  # fmt: skip
    fake_api.push({"type": "hub", "storage": storage})
    await hass.async_block_till_done()
    state = hass.states.get("sensor.dashcam_sync_network_storage_used")
    assert state is not None and state.state != STATE_UNAVAILABLE
    assert float(state.state) == pytest.approx(3.0)


async def test_ids_are_unique_per_entity(hass: HomeAssistant, loaded: MockConfigEntry) -> None:
    from homeassistant.helpers import entity_registry as er

    entries = er.async_entries_for_config_entry(er.async_get(hass), loaded.entry_id)
    ids = [e.unique_id for e in entries]
    assert len(ids) == len(set(ids))


async def test_the_image_path_check_runs_off_the_event_loop(
    hass: HomeAssistant, fake_api: FakeApi, entry: MockConfigEntry
) -> None:
    await setup_entry(hass, entry)
    entity = hass.data["image"].get_entity("image.family_car_latest_event")
    with patch.object(hass, "async_add_executor_job", wraps=hass.async_add_executor_job) as job:
        await entity.async_image()
    assert any(
        call.args and call.args[0].__name__ == "resolve_clip_path" for call in job.call_args_list
    )


async def test_a_write_right_after_a_refresh_is_not_delayed_by_the_debounce_cooldown(
    hass: HomeAssistant, loaded: MockConfigEntry, fake_api: FakeApi
) -> None:
    """A push that triggers a (debounced) refresh must not make our own write wait 10 s."""
    fake_api.push({"type": "options"})  # starts the coordinator's refresh cooldown
    await hass.async_block_till_done()
    fake_api.status["cameras"]["family_car"] = {**CAMERA, "enabled": False}
    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": "switch.family_car_auto_sync"}, blocking=True
    )
    assert hass.states.get("switch.family_car_auto_sync").state == "off"  # immediately, no sleeping


async def test_a_status_read_that_lands_after_a_push_does_not_undo_it(
    hass: HomeAssistant, loaded: MockConfigEntry, fake_api: FakeApi
) -> None:
    """The app answers a status read, then a sync finishes and is pushed, then the answer arrives.

    The older answer used to replace the pushed state, and with the socket live nothing read
    the status again until the next change, so a finished sync showed as never having run.
    """
    coordinator = loaded.runtime_data.coordinator
    old = copy.deepcopy(fake_api.status)
    done = {**CAMERA, "sync_status": "idle", "last_sync": "2026-11-25T10:00:00+00:00"}
    real = fake_api.async_status

    async def answered_before_the_push() -> dict:
        await real()
        fake_api.status["cameras"]["family_car"] = done
        fake_api.push({"type": "camera", "camera": "family_car", "status": done})
        for _ in range(20):  # let the socket task deliver the push while this read is in flight
            await asyncio.sleep(0)
        assert coordinator.data["cameras"]["family_car"]["last_sync"] == done["last_sync"]
        return old

    with patch.object(fake_api, "async_status", answered_before_the_push):
        await coordinator.async_force_refresh()
    await hass.async_block_till_done()
    assert coordinator.data["cameras"]["family_car"]["last_sync"] == done["last_sync"]
    assert hass.states.get("sensor.family_car_last_sync").state != "unknown"


@pytest.mark.parametrize("kind", ["snapshot", "hub"])
async def test_a_snapshot_or_hub_push_during_a_status_read_also_wins(
    hass: HomeAssistant, loaded: MockConfigEntry, fake_api: FakeApi, kind: str
) -> None:
    coordinator = loaded.runtime_data.coordinator
    old = copy.deepcopy(fake_api.status)
    storage = {**old["storage"], "used_bytes": 123_456_789}
    real = fake_api.async_status

    async def answered_before_the_push() -> dict:
        await real()
        if kind == "snapshot":
            fake_api.push({"type": "snapshot", **{**old, "storage": storage}})
        else:
            fake_api.push({"type": "hub", "storage": storage})
        for _ in range(20):
            await asyncio.sleep(0)
        return old

    with patch.object(fake_api, "async_status", answered_before_the_push):
        await coordinator.async_force_refresh()
    await hass.async_block_till_done()
    assert coordinator.data["storage"]["used_bytes"] == 123_456_789


@pytest.mark.parametrize(("push_rev", "wins"), [(9, False), (10, False), (11, True)])
async def test_only_pushes_numbered_after_the_status_answer_override_it(
    hass: HomeAssistant, loaded: MockConfigEntry, fake_api: FakeApi, push_rev: int, wins: bool
) -> None:
    """A push received during a read can still be older than the answer (it was queued first).

    The app stamps the answer with the newest push it reflects; only later pushes win.
    """
    coordinator = loaded.runtime_data.coordinator
    answer = {**copy.deepcopy(fake_api.status), "rev": 10}
    answer["storage"] = {**answer["storage"], "used_bytes": 1}
    pushed_storage = {**answer["storage"], "used_bytes": 2}
    real = fake_api.async_status

    async def read_with_a_push_in_flight() -> dict:
        await real()
        fake_api.push({"type": "hub", "rev": push_rev, "storage": pushed_storage})
        for _ in range(20):
            await asyncio.sleep(0)
        return answer

    with patch.object(fake_api, "async_status", read_with_a_push_in_flight):
        await coordinator.async_force_refresh()
    await hass.async_block_till_done()
    assert coordinator.data["storage"]["used_bytes"] == (2 if wins else 1)

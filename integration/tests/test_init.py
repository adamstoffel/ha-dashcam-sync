from __future__ import annotations

import asyncio
import copy

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.dashcam_sync.api import (
    DashcamSyncAuthError,
    DashcamSyncConnectionError,
    DashcamSyncRequestError,
)
from custom_components.dashcam_sync.const import DOMAIN, POLL_INTERVAL

from .conftest import CAMERA, FakeApi, setup_entry


async def test_setup_creates_devices_and_entities(
    hass: HomeAssistant, loaded: MockConfigEntry
) -> None:
    assert loaded.state is ConfigEntryState.LOADED
    dev_reg = dr.async_get(hass)
    hub = dev_reg.async_get_device(identifiers={(DOMAIN, loaded.entry_id)})
    camera = dev_reg.async_get_device(identifiers={(DOMAIN, "polestar_2")})
    assert hub is not None and camera is not None
    assert (
        camera.via_device_id == hub.id and camera.model == "NT96" and camera.sw_version == "FW-1.2"
    )
    assert hub.configuration_url == "homeassistant://hassio/ingress/abc_dashcam_sync"

    ent_reg = er.async_get(hass)
    unique_ids = {e.unique_id for e in er.async_entries_for_config_entry(ent_reg, loaded.entry_id)}
    assert {
        "polestar_2_connected",
        "polestar_2_sync_status",
        "polestar_2_clips_remaining",
        "polestar_2_data_remaining",
        "polestar_2_download_speed",
        "polestar_2_last_sync",
        "polestar_2_last_event_clip",
        "polestar_2_sd_card_free",
        "polestar_2_clip",
        "polestar_2_latest_event",
        "polestar_2_live",
        "polestar_2_sync_now",
        "polestar_2_restore_recording",
        "polestar_2_auto_sync",
        "polestar_2_sync_mode",
        "hub_local_storage_used",
        "hub_local_storage_free",
        "hub_purge_now",
        "hub_network_target",
    } <= unique_ids
    assert "hub_network_storage_used" not in unique_ids  # no network target configured


async def test_entity_states(hass: HomeAssistant, loaded: MockConfigEntry) -> None:
    assert hass.states.get("binary_sensor.polestar_2_connected").state == STATE_ON
    assert hass.states.get("sensor.polestar_2_sync_status").state == "idle"
    assert hass.states.get("sensor.polestar_2_clips_remaining").state == "3"
    assert float(hass.states.get("sensor.polestar_2_data_remaining").state) == pytest.approx(
        0.1, abs=0.1
    )
    assert hass.states.get("sensor.polestar_2_last_sync").state == "2026-11-22T10:20:00+00:00"
    assert hass.states.get("sensor.polestar_2_last_event_clip").state == "2026-11-22T10:16:00+00:00"
    # Diagnostics that are rarely useful are disabled by default.
    registry = er.async_get(hass)
    for key in ("download_speed", "sd_card_free"):
        entity_id = registry.async_get_entity_id("sensor", DOMAIN, f"polestar_2_{key}")
        assert registry.async_get(entity_id).disabled_by is er.RegistryEntryDisabler.INTEGRATION
        assert hass.states.get(entity_id) is None
    assert hass.states.get("switch.polestar_2_auto_sync").state == STATE_ON
    assert hass.states.get("select.polestar_2_sync_mode").state == "events_first"
    assert float(hass.states.get("sensor.dashcam_sync_local_storage_used").state) == pytest.approx(
        5.0
    )
    assert hass.states.get("camera.polestar_2_live").state != STATE_UNAVAILABLE
    assert hass.states.get("image.polestar_2_latest_event") is not None
    # No network target configured: the problem sensor is unavailable rather than lying.
    assert hass.states.get("binary_sensor.dashcam_sync_network_storage").state == STATE_UNAVAILABLE


async def test_network_target_entities_appear_when_configured(
    hass: HomeAssistant, fake_api: FakeApi, entry: MockConfigEntry
) -> None:
    fake_api.status["storage"]["targets"]["network"] = {
        "name": "network", "online": False, "total_bytes": 10**12, "free_bytes": 5 * 10**11,
        "clip_bytes": 10**10, "reason": "not_mounted",
    }  # fmt: skip
    await setup_entry(hass, entry)
    assert (
        hass.states.get("binary_sensor.dashcam_sync_network_storage").state == STATE_ON
    )  # problem
    assert float(
        hass.states.get("sensor.dashcam_sync_network_storage_used").state
    ) == pytest.approx(10.0)
    assert hass.states.get("sensor.dashcam_sync_network_storage_free") is not None


async def test_unload_and_reload(hass: HomeAssistant, loaded: MockConfigEntry) -> None:
    assert await hass.config_entries.async_unload(loaded.entry_id)
    await hass.async_block_till_done()
    assert loaded.state is ConfigEntryState.NOT_LOADED
    assert hass.states.get("sensor.polestar_2_sync_status").state == STATE_UNAVAILABLE
    assert await hass.config_entries.async_setup(loaded.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.polestar_2_sync_status").state == "idle"


@pytest.mark.parametrize(
    ("error", "state"),
    [
        (DashcamSyncAuthError("bad"), ConfigEntryState.SETUP_ERROR),
        (DashcamSyncConnectionError("down"), ConfigEntryState.SETUP_RETRY),
        (DashcamSyncRequestError(500, "boom"), ConfigEntryState.SETUP_RETRY),
    ],
)
async def test_setup_failures(
    hass: HomeAssistant,
    fake_api: FakeApi,
    entry: MockConfigEntry,
    error: Exception,
    state: ConfigEntryState,
) -> None:
    fake_api.error = error
    entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is state
    if state is ConfigEntryState.SETUP_ERROR:
        flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
        assert any(f["context"]["source"] == "reauth" for f in flows)


async def test_first_refresh_failure_retries(
    hass: HomeAssistant, fake_api: FakeApi, entry: MockConfigEntry
) -> None:
    fake_api.status_error = DashcamSyncConnectionError("down")
    entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_poll_fallback_goes_unavailable_and_recovers(
    hass: HomeAssistant, loaded: MockConfigEntry, fake_api: FakeApi, freezer: object
) -> None:
    fake_api.push(None)  # type: ignore[arg-type]  # close the socket: no live push any more
    await hass.async_block_till_done()
    fake_api.status_error = DashcamSyncConnectionError("down")
    async_fire_time_changed(hass, dt_util.utcnow() + POLL_INTERVAL * 2)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.polestar_2_sync_status").state == STATE_UNAVAILABLE
    fake_api.status_error = None
    async_fire_time_changed(hass, dt_util.utcnow() + POLL_INTERVAL * 4)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.polestar_2_sync_status").state == "idle"


async def test_poll_auth_error_starts_reauth(
    hass: HomeAssistant, loaded: MockConfigEntry, fake_api: FakeApi
) -> None:
    fake_api.push(None)  # type: ignore[arg-type]
    await hass.async_block_till_done()
    fake_api.status_error = DashcamSyncAuthError("revoked")
    async_fire_time_changed(hass, dt_util.utcnow() + POLL_INTERVAL * 2)
    await hass.async_block_till_done()
    assert any(
        f["context"]["source"] == "reauth"
        for f in hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    )


async def test_new_cameras_appear_without_a_reload(
    hass: HomeAssistant, loaded: MockConfigEntry, fake_api: FakeApi
) -> None:
    assert hass.states.get("sensor.dashcam_2_sync_status") is None
    second = {**copy.deepcopy(CAMERA), "id": "dashcam_2", "name": "Dashcam 2"}
    fake_api.push({"type": "camera", "camera": "dashcam_2", "status": second})
    await hass.async_block_till_done()
    assert hass.states.get("sensor.dashcam_2_sync_status").state == "idle"
    assert hass.states.get("binary_sensor.dashcam_2_connected").state == STATE_ON


async def test_a_camera_that_vanishes_is_unavailable_and_its_device_removable(
    hass: HomeAssistant, loaded: MockConfigEntry, fake_api: FakeApi
) -> None:
    status = copy.deepcopy(fake_api.status)
    status["cameras"] = {}
    fake_api.push({"type": "snapshot", **status})
    await hass.async_block_till_done()
    assert hass.states.get("sensor.polestar_2_sync_status").state == STATE_UNAVAILABLE
    from custom_components.dashcam_sync import async_remove_config_entry_device

    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get_device(identifiers={(DOMAIN, "polestar_2")})
    assert device is not None
    assert await async_remove_config_entry_device(hass, loaded, device)
    hub = dev_reg.async_get_device(identifiers={(DOMAIN, loaded.entry_id)})
    assert hub is not None and await async_remove_config_entry_device(hass, loaded, hub)


async def test_push_updates_state_without_polling(
    hass: HomeAssistant, loaded: MockConfigEntry, fake_api: FakeApi
) -> None:
    updated = {**CAMERA, "sync_status": "downloading", "connected": False, "clips_remaining": 1}
    fake_api.push({"type": "camera", "camera": "polestar_2", "status": updated})
    await hass.async_block_till_done()
    assert hass.states.get("sensor.polestar_2_sync_status").state == "downloading"
    assert hass.states.get("binary_sensor.polestar_2_connected").state == STATE_OFF
    assert hass.states.get("camera.polestar_2_live").state == STATE_UNAVAILABLE  # out of range


async def test_socket_reconnects_after_an_error(
    hass: HomeAssistant, fake_api: FakeApi, entry: MockConfigEntry
) -> None:
    fake_api.ws_error = DashcamSyncConnectionError("refused")
    await setup_entry(hass, entry)
    await hass.async_block_till_done()
    first = fake_api.ws_connects
    assert first >= 1
    # The reconnect delay is an asyncio sleep inside the background task.
    for _ in range(50):
        await asyncio.sleep(0.05)
        if fake_api.ws_connects > first:
            break
    assert fake_api.ws_connects > first


async def test_socket_auth_error_starts_reauth(
    hass: HomeAssistant, fake_api: FakeApi, entry: MockConfigEntry
) -> None:
    fake_api.ws_error = DashcamSyncAuthError("revoked")
    await setup_entry(hass, entry)
    await hass.async_block_till_done()
    assert any(
        f["context"]["source"] == "reauth"
        for f in hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    )

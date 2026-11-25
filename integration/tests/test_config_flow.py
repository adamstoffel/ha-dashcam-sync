from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.service_info.hassio import HassioServiceInfo
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.dashcam_sync.api import (
    DashcamSyncAuthError,
    DashcamSyncConnectionError,
    DashcamSyncRequestError,
)
from custom_components.dashcam_sync.const import (
    CONF_HOST,
    CONF_LIVE_CAMERA,
    CONF_PORT,
    CONF_PRESENCE_ENTITY,
    CONF_TOKEN,
    DOMAIN,
)
from custom_components.dashcam_sync.wifi import WifiInterface, WifiUnavailable

from .conftest import FakeApi

USER_INPUT = {CONF_HOST: "local-dashcam-sync", CONF_PORT: 8100, CONF_TOKEN: "tok"}


def discovery(**config: object) -> HassioServiceInfo:
    base = {"host": "abc-dashcam-sync", "port": 8100, "token": "tok2", "addon": "Dashcam Sync"}
    return HassioServiceInfo(
        config={**base, **config}, name="Dashcam Sync", slug="abc_dashcam_sync", uuid="u1"
    )


async def test_manual_setup(hass: HomeAssistant, fake_api: FakeApi) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "user"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Dashcam Sync" and result["data"] == USER_INPUT
    assert result["result"].unique_id == "inst123"


@pytest.mark.parametrize(
    ("error", "key"),
    [
        (DashcamSyncAuthError("x"), "invalid_auth"),
        (DashcamSyncConnectionError("x"), "cannot_connect"),
        (DashcamSyncRequestError(500, "x"), "unknown"),
    ],
)
async def test_manual_errors_then_recovery(
    hass: HomeAssistant, fake_api: FakeApi, error: Exception, key: str
) -> None:
    fake_api.error = error
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["type"] is FlowResultType.FORM and result["errors"] == {"base": key}
    fake_api.error = None
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_manual_rejects_incompatible_api(hass: HomeAssistant, fake_api: FakeApi) -> None:
    fake_api.info["api_version"] = 99
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["errors"] == {"base": "incompatible_api"}


async def test_manual_duplicate_aborts(
    hass: HomeAssistant, fake_api: FakeApi, loaded: MockConfigEntry
) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "already_configured"


async def test_hassio_discovery_flow(hass: HomeAssistant, fake_api: FakeApi) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_HASSIO}, data=discovery()
    )
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "hassio_confirm"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {
        CONF_HOST: "abc-dashcam-sync", CONF_PORT: 8100, CONF_TOKEN: "tok2", "slug": "abc_dashcam_sync"
    }  # fmt: skip


async def test_hassio_discovery_updates_existing_entry(
    hass: HomeAssistant, fake_api: FakeApi, entry: MockConfigEntry
) -> None:
    entry.add_to_hass(hass)
    with patch("custom_components.dashcam_sync.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_HASSIO},
            data=discovery(host="new-host"),
        )
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "already_configured"
    assert entry.data[CONF_HOST] == "new-host" and entry.data[CONF_TOKEN] == "tok2"


async def test_hassio_discovery_bad_payload_and_unreachable(
    hass: HomeAssistant, fake_api: FakeApi
) -> None:
    bad = HassioServiceInfo(config={"host": "h"}, name="x", slug="s", uuid="u")
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_HASSIO}, data=bad
    )
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "invalid_discovery"
    fake_api.error = DashcamSyncConnectionError("down")
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_HASSIO}, data=discovery()
    )
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "cannot_connect"


async def test_reauth(hass: HomeAssistant, fake_api: FakeApi, loaded: MockConfigEntry) -> None:
    result = await loaded.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"
    fake_api.error = DashcamSyncAuthError("no")
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_TOKEN: "bad"})
    assert result["errors"] == {"base": "invalid_auth"}
    fake_api.error = None
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_TOKEN: "new"})
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "reauth_successful"
    assert loaded.data[CONF_TOKEN] == "new"


async def test_reconfigure(hass: HomeAssistant, fake_api: FakeApi, loaded: MockConfigEntry) -> None:
    result = await loaded.start_reconfigure_flow(hass)
    assert result["step_id"] == "reconfigure"
    fake_api.error = DashcamSyncConnectionError("down")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**USER_INPUT, CONF_HOST: "elsewhere"}
    )
    assert result["errors"] == {"base": "cannot_connect"}
    fake_api.error = None
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**USER_INPUT, CONF_HOST: "elsewhere"}
    )
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "reconfigure_successful"
    assert loaded.data[CONF_HOST] == "elsewhere"


async def test_reconfigure_rejects_a_different_app(
    hass: HomeAssistant, fake_api: FakeApi, loaded: MockConfigEntry
) -> None:
    result = await loaded.start_reconfigure_flow(hass)
    fake_api.info["instance"] = "someone-else"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "unique_id_mismatch"


# ---------------------------------------------------------------- options flow


async def test_options_general(hass: HomeAssistant, loaded: MockConfigEntry) -> None:
    result = await hass.config_entries.options.async_init(loaded.entry_id)
    assert result["type"] is FlowResultType.MENU
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "general"}
    )
    assert result["step_id"] == "general"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_PRESENCE_ENTITY: "device_tracker.car", CONF_LIVE_CAMERA: False}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert loaded.options == {CONF_PRESENCE_ENTITY: "device_tracker.car", CONF_LIVE_CAMERA: False}
    await hass.async_block_till_done()  # the update listener reloaded the entry
    live = hass.states.get("camera.polestar_2_live")
    assert live is None or live.state == "unavailable"  # live camera no longer provided


async def start_wifi(hass: HomeAssistant, entry: MockConfigEntry) -> dict:
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "wifi"}
    )
    return await hass.config_entries.options.async_configure(
        result["flow_id"], {"camera": "polestar_2"}
    )


async def test_wifi_applies_the_profile(
    hass: HomeAssistant, fake_api: FakeApi, loaded: MockConfigEntry
) -> None:
    adapters = [WifiInterface("wlan0", False, True, None)]
    with (
        patch(
            "custom_components.dashcam_sync.config_flow.async_list_wifi_interfaces",
            return_value=adapters,
        ),
        patch(
            "custom_components.dashcam_sync.config_flow.async_apply_profile", new=AsyncMock()
        ) as apply,
    ):
        result = await start_wifi(hass, loaded)
        assert result["step_id"] == "wifi_apply" and result["type"] is FlowResultType.FORM
        placeholders = result["description_placeholders"]
        assert placeholders["ssid"] == "CAMWIFI" and placeholders["address"] == "192.168.1.253"
        assert placeholders["prefix"] == "30" and placeholders["block"] == "192.168.1.252/30"
        result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    profile = apply.call_args.args[1]
    assert profile.interface == "wlan0" and profile.password == "secretpw"
    assert fake_api.calls_named("overlap")  # the check ran before anything was connected


@pytest.mark.parametrize(
    ("result_key", "error"),
    [
        ("device_in_block", "overlap_device_in_block"),
        ("address_in_use", "overlap_address_in_use"),
        ("camera_collides", "overlap_camera_collides"),
    ],
)
async def test_wifi_never_connects_when_the_block_is_blocked(
    hass: HomeAssistant, fake_api: FakeApi, loaded: MockConfigEntry, result_key: str, error: str
) -> None:
    fake_api.overlap = {"result": result_key, "blocked": True, "conflicts": ["192.168.1.255"]}
    with patch(
        "custom_components.dashcam_sync.config_flow.async_apply_profile", new=AsyncMock()
    ) as apply:
        result = await start_wifi(hass, loaded)
        assert result["type"] is FlowResultType.FORM and result["errors"] == {"base": error}
        assert result["description_placeholders"]["conflicts"] == "192.168.1.255"
    apply.assert_not_called()


async def test_wifi_no_adapter(hass: HomeAssistant, loaded: MockConfigEntry) -> None:
    with patch(
        "custom_components.dashcam_sync.config_flow.async_list_wifi_interfaces", return_value=[]
    ):
        result = await start_wifi(hass, loaded)
    assert result["errors"] == {"base": "no_adapter"}


async def test_wifi_falls_back_to_manual_steps(
    hass: HomeAssistant, loaded: MockConfigEntry
) -> None:
    with patch(
        "custom_components.dashcam_sync.config_flow.async_list_wifi_interfaces",
        side_effect=WifiUnavailable("no supervisor"),
    ):
        result = await start_wifi(hass, loaded)
    assert result["step_id"] == "wifi_manual"
    assert result["description_placeholders"]["address"] == "192.168.1.253"
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_wifi_supervisor_refusal_also_falls_back(
    hass: HomeAssistant, loaded: MockConfigEntry
) -> None:
    adapters = [WifiInterface("wlan0", False, True, None)]
    with (
        patch(
            "custom_components.dashcam_sync.config_flow.async_list_wifi_interfaces",
            return_value=adapters,
        ),
        patch(
            "custom_components.dashcam_sync.config_flow.async_apply_profile",
            side_effect=WifiUnavailable("no"),
        ),
    ):
        result = await start_wifi(hass, loaded)
        result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert result["step_id"] == "wifi_manual"


async def test_wifi_without_hotspot_cameras_or_app(
    hass: HomeAssistant, fake_api: FakeApi, loaded: MockConfigEntry
) -> None:
    fake_api.options = {"cameras": [{"id": "x", "connection": "lan"}]}
    result = await hass.config_entries.options.async_init(loaded.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "wifi"}
    )
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "no_hotspot_cameras"

    fake_api.options = {"cameras": [{"id": "polestar_2", "connection": "hotspot"}]}
    result = await hass.config_entries.options.async_init(loaded.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "wifi"}
    )
    fake_api.error = DashcamSyncConnectionError("down")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"camera": "polestar_2"}
    )
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "app_unavailable"

"""Config flow: Supervisor discovery, manual setup, reauth and options."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.service_info.hassio import HassioServiceInfo

from .api import (
    DashcamSyncApi,
    DashcamSyncAuthError,
    DashcamSyncConnectionError,
    DashcamSyncError,
)
from .const import (
    API_VERSION,
    CONF_HOST,
    CONF_LIVE_CAMERA,
    CONF_PORT,
    CONF_PRESENCE_ENTITY,
    CONF_TOKEN,
    DEFAULT_PORT,
    DOMAIN,
    MIN_API_VERSION,
)
from .coordinator import DashcamSyncConfigEntry
from .wifi import (
    WifiUnavailable,
    async_apply_profile,
    async_list_wifi_interfaces,
    known_device_ips,
    profile_from_api,
)

_LOGGER = logging.getLogger(__name__)

NETWORK_SETTINGS_URL = "https://my.home-assistant.io/redirect/network/"

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): str,
        vol.Required(CONF_PORT, default=DEFAULT_PORT): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=65535)
        ),
        vol.Required(CONF_TOKEN): selector.TextSelector(
            selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
        ),
    }
)
TOKEN_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_TOKEN): selector.TextSelector(
            selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
        )
    }
)


async def _validate(
    flow: ConfigFlow, host: str, port: int, token: str
) -> tuple[dict[str, Any] | None, str | None]:
    """Ask the app who it is. Returns (info, error key)."""
    api = DashcamSyncApi(async_get_clientsession(flow.hass), host, port, token)
    try:
        info = await api.async_info()
    except DashcamSyncAuthError:
        return None, "invalid_auth"
    except DashcamSyncConnectionError:
        return None, "cannot_connect"
    except DashcamSyncError:
        return None, "unknown"
    version = int(info.get("api_version", 0))
    if not MIN_API_VERSION <= version <= API_VERSION:
        return None, "incompatible_api"
    return info, None


class DashcamSyncConfigFlow(ConfigFlow, domain=DOMAIN):
    """Set up the integration for the Dashcam Sync app."""

    VERSION = 1

    def __init__(self) -> None:
        self._discovered: dict[str, Any] = {}

    # ------------------------------------------------------------- discovery

    async def async_step_hassio(self, discovery_info: HassioServiceInfo) -> ConfigFlowResult:
        """The app posted itself to the Supervisor's /discovery endpoint."""
        config = discovery_info.config
        try:
            host, port, token = str(config["host"]), int(config["port"]), str(config["token"])
        except (KeyError, TypeError, ValueError):
            return self.async_abort(reason="invalid_discovery")
        info, error = await _validate(self, host, port, token)
        if info is None:
            return self.async_abort(reason=error or "cannot_connect")
        await self.async_set_unique_id(info["instance"])
        self._abort_if_unique_id_configured(
            updates={CONF_HOST: host, CONF_PORT: port, CONF_TOKEN: token}
        )
        self._discovered = {
            CONF_HOST: host,
            CONF_PORT: port,
            CONF_TOKEN: token,
            "slug": discovery_info.slug,
        }
        self.context["title_placeholders"] = {"name": discovery_info.name}
        return await self.async_step_hassio_confirm()

    async def async_step_hassio_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(title="Dashcam Sync", data=self._discovered)
        return self.async_show_form(
            step_id="hassio_confirm",
            description_placeholders={
                "addon": self.context.get("title_placeholders", {}).get("name", "Dashcam Sync")
            },
        )

    # ---------------------------------------------------------------- manual

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            info, error = await _validate(
                self, user_input[CONF_HOST], user_input[CONF_PORT], user_input[CONF_TOKEN]
            )
            if info is not None:
                await self.async_set_unique_id(info["instance"])
                self._abort_if_unique_id_configured(updates=user_input)
                return self.async_create_entry(title="Dashcam Sync", data=user_input)
            errors["base"] = error or "unknown"
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                STEP_USER_SCHEMA, user_input or {CONF_HOST: "local-dashcam-sync"}
            ),
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            info, error = await _validate(
                self, user_input[CONF_HOST], user_input[CONF_PORT], user_input[CONF_TOKEN]
            )
            if info is not None:
                await self.async_set_unique_id(info["instance"])
                self._abort_if_unique_id_mismatch()
                return self.async_update_reload_and_abort(entry, data_updates=user_input)
            errors["base"] = error or "unknown"
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                STEP_USER_SCHEMA, user_input or dict(entry.data)
            ),
            errors=errors,
        )

    # ----------------------------------------------------------------- reauth

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            info, error = await _validate(
                self, entry.data[CONF_HOST], entry.data[CONF_PORT], user_input[CONF_TOKEN]
            )
            if info is not None:
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_TOKEN: user_input[CONF_TOKEN]}
                )
            errors["base"] = error or "unknown"
        return self.async_show_form(
            step_id="reauth_confirm", data_schema=TOKEN_SCHEMA, errors=errors
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: DashcamSyncConfigEntry) -> OptionsFlow:
        return DashcamSyncOptionsFlow()


class DashcamSyncOptionsFlow(OptionsFlow):
    """Things that live in Core: the arrival hint, the live camera and the Wi-Fi adapter."""

    def __init__(self) -> None:
        self._camera: str | None = None
        self._profile: dict[str, str] = {}

    @property
    def _entry(self) -> DashcamSyncConfigEntry:
        return self.config_entry  # type: ignore[return-value]

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_show_menu(step_id="init", menu_options=["general", "wifi"])

    async def async_step_general(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data={**self.config_entry.options, **user_input})
        schema = vol.Schema(
            {
                vol.Optional(CONF_PRESENCE_ENTITY): selector.EntitySelector(
                    selector.EntitySelectorConfig(
                        domain=["device_tracker", "person", "binary_sensor", "cover", "sensor"]
                    )
                ),
                vol.Required(CONF_LIVE_CAMERA, default=True): bool,
            }
        )
        return self.async_show_form(
            step_id="general",
            data_schema=self.add_suggested_values_to_schema(schema, self.config_entry.options),
        )

    # -------------------------------------------------------------------- wifi

    async def async_step_wifi(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Pick which hotspot camera to connect."""
        try:
            options = await self._entry.runtime_data.api.async_options()
        except DashcamSyncError:
            return self.async_abort(reason="app_unavailable")
        hotspots = [c["id"] for c in options.get("cameras", []) if c.get("connection") == "hotspot"]
        if not hotspots:
            return self.async_abort(reason="no_hotspot_cameras")
        if user_input is not None:
            self._camera = user_input["camera"]
            return await self.async_step_wifi_apply()
        schema = vol.Schema(
            {
                vol.Required("camera", default=hotspots[0]): selector.SelectSelector(
                    selector.SelectSelectorConfig(options=hotspots)
                )
            }
        )
        return self.async_show_form(step_id="wifi", data_schema=schema)

    async def async_step_wifi_apply(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Check the address block, then write the Wi-Fi profile through the Supervisor."""
        assert self._camera is not None
        api = self._entry.runtime_data.api
        try:
            profile = profile_from_api(await api.async_wifi(self._camera))
            report = await api.async_overlap(self._camera, known_device_ips(self.hass))
        except (DashcamSyncError, KeyError, ValueError):
            return self.async_abort(reason="app_unavailable")
        placeholders = {**profile.as_placeholders(), "camera": self._camera,
                        "conflicts": ", ".join(report.get("conflicts") or []) or "-"}  # fmt: skip
        if report.get("blocked"):
            # Never connect and hope: a failed check blocks the camera.
            return self.async_show_form(
                step_id="wifi_apply",
                errors={"base": f"overlap_{report['result']}"},
                description_placeholders=placeholders,
            )
        try:
            adapters = await async_list_wifi_interfaces(self.hass)
        except WifiUnavailable:
            self._profile = placeholders
            return await self.async_step_wifi_manual()
        if profile.interface not in {a.name for a in adapters}:
            return self.async_show_form(
                step_id="wifi_apply",
                errors={"base": "no_adapter"},
                description_placeholders=placeholders,
            )
        if user_input is None:
            return self.async_show_form(step_id="wifi_apply", description_placeholders=placeholders)
        try:
            await async_apply_profile(self.hass, profile)
        except WifiUnavailable:
            self._profile = placeholders
            return await self.async_step_wifi_manual()
        return self.async_create_entry(data=dict(self.config_entry.options))

    async def async_step_wifi_manual(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Guided manual setup: the exact values to type under Settings, System, Network."""
        if user_input is not None:
            return self.async_create_entry(data=dict(self.config_entry.options))
        return self.async_show_form(
            step_id="wifi_manual",
            description_placeholders={**self._profile, "network_url": NETWORK_SETTINGS_URL},
        )

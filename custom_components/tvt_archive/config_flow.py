from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import (
    CONF_HOST,
    CONF_NAME,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_URL,
    CONF_USERNAME,
)
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    EntitySelector,
    EntitySelectorConfig,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
from homeassistant.helpers.service_info.hassio import HassioServiceInfo

from .api import TVTArchiveApi, TVTArchiveApiError, TVTArchiveAuthError
from .const import (
    CONF_CAMERA_ID,
    CONF_MODE,
    CONF_MOTION,
    CONF_RECORDING_AUDIO,
    CONF_TOKEN,
    DEFAULT_PORT,
    DOMAIN,
    MODE_BUILTIN,
)
from .local import (
    async_bridge_api,
    async_local_bridge,
    bridge_key,
    camera_login,
    camera_uid,
    is_builtin,
)
from .motion import auto_motion_entities

NEW_CAMERA = "new"

RECORDING_AUDIO_MODES = {
    "auto": "Auto (recommended)",
    "on": "Always expect audio",
    "off": "Disabled",
}

PASSWORD = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))


def _camera_schema(
    defaults: Mapping[str, Any] | None = None, *, editing: bool = False
) -> vol.Schema:
    defaults = defaults or {}
    password = vol.Optional(CONF_PASSWORD, default="") if editing else vol.Required(CONF_PASSWORD)
    return vol.Schema(
        {
            vol.Required(CONF_NAME, default=defaults.get(CONF_NAME, "")): str,
            vol.Required(CONF_HOST, default=defaults.get(CONF_HOST, "")): str,
            vol.Required(CONF_USERNAME, default=defaults.get(CONF_USERNAME, "admin")): str,
            password: PASSWORD,
            vol.Required(CONF_PORT, default=int(defaults.get(CONF_PORT, DEFAULT_PORT))): vol.All(
                vol.Coerce(int), vol.Range(min=1, max=65535)
            ),
            vol.Required(
                CONF_RECORDING_AUDIO, default=defaults.get(CONF_RECORDING_AUDIO, "auto")
            ): vol.In(RECORDING_AUDIO_MODES),
        }
    )


def _camera_error(error: Exception) -> str:
    text = str(error)
    if "rejected the login" in text:
        return "camera_auth_failed"
    if "Wait for this camera" in text:
        return "camera_busy"
    return "camera_connection_failed"


class TVTArchiveConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 3

    def __init__(self) -> None:
        self._bridge: dict[str, Any] = {}
        self._api: TVTArchiveApi | None = None
        self._defaults: dict[str, Any] = {}
        self._orphans: dict[str, dict[str, Any]] = {}
        self._adopt: dict[str, Any] = {}

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry):
        return TVTArchiveOptionsFlow()

    def _remote_entries(self) -> list[config_entries.ConfigEntry]:
        return [e for e in self._async_current_entries() if CONF_URL in e.data]

    async def async_step_user(self, user_input=None):
        return self.async_show_menu(step_id="user", menu_options=["builtin", "remote"])

    async def async_step_builtin(self, user_input=None):
        try:
            self._api = await async_local_bridge(self.hass).async_start()
            await self._api.health()
        except Exception:
            return self.async_abort(reason="builtin_failed")
        self._bridge = {CONF_MODE: MODE_BUILTIN}
        return await self.async_step_pick()

    async def async_step_remote(self, user_input=None):
        errors = {}
        known = {e.data[CONF_URL].lower(): e.data[CONF_TOKEN] for e in self._remote_entries()}
        if user_input is not None:
            url = str(user_input[CONF_URL]).rstrip("/")
            token = str(user_input.get(CONF_TOKEN) or known.get(url.lower(), ""))
            api = TVTArchiveApi(async_get_clientsession(self.hass), url, token)
            try:
                await api.health()
            except TVTArchiveAuthError:
                errors["base"] = "invalid_auth"
            except TVTArchiveApiError:
                errors["base"] = "cannot_connect"
            except Exception:
                errors["base"] = "unknown"
            else:
                self._api, self._bridge = api, {CONF_URL: url, CONF_TOKEN: token}
                return await self.async_step_pick()
        default_url = next(iter(known), "") if user_input is None else user_input[CONF_URL]
        return self.async_show_form(
            step_id="remote",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_URL, default=default_url): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.URL)
                    ),
                    vol.Optional(CONF_TOKEN): PASSWORD,
                }
            ),
            errors=errors,
        )

    async def async_step_hassio(self, discovery_info: HassioServiceInfo):
        config = discovery_info.config
        url = f"http://{config['host']}:{config['port']}"
        token = str(config["token"])
        existing = [e for e in self._remote_entries() if e.data[CONF_URL].lower() == url.lower()]
        for entry in existing:
            if entry.data[CONF_TOKEN] != token:
                self.hass.config_entries.async_update_entry(
                    entry, data={**entry.data, CONF_TOKEN: token}
                )
                self.hass.config_entries.async_schedule_reload(entry.entry_id)
        if existing:
            return self.async_abort(reason="already_configured")
        await self.async_set_unique_id(url.lower())
        self._abort_if_unique_id_configured()
        self._bridge = {CONF_URL: url, CONF_TOKEN: token}
        self.context["title_placeholders"] = {"name": discovery_info.name}
        return await self.async_step_hassio_confirm()

    async def async_step_hassio_confirm(self, user_input=None):
        if user_input is None:
            return self.async_show_form(step_id="hassio_confirm")
        api = TVTArchiveApi(
            async_get_clientsession(self.hass), self._bridge[CONF_URL], self._bridge[CONF_TOKEN]
        )
        try:
            await api.health()
        except Exception:
            return self.async_abort(reason="cannot_connect")
        self._api = api
        return await self.async_step_pick()

    async def async_step_integration_discovery(self, discovery_info: dict[str, Any]):
        host, port = discovery_info[CONF_HOST], int(discovery_info.get(CONF_PORT, DEFAULT_PORT))
        await self.async_set_unique_id(camera_uid(host, port))
        self._abort_if_unique_id_configured()
        self._defaults = {
            CONF_NAME: discovery_info.get(CONF_NAME, host),
            CONF_HOST: host,
            CONF_PORT: port,
            CONF_USERNAME: discovery_info.get(CONF_USERNAME, "admin"),
        }
        self.context["title_placeholders"] = {"name": self._defaults[CONF_NAME]}
        return await self.async_step_user()

    async def async_step_import(self, import_data: dict[str, Any]):
        data = dict(import_data)
        if uid := data.pop("unique_id"):
            await self.async_set_unique_id(uid, raise_on_progress=False)
        title, motion = data.pop("title"), data.pop(CONF_MOTION, None)
        return self.async_create_entry(
            title=title, data=data, options={CONF_MOTION: motion} if motion is not None else {}
        )

    async def async_step_pick(self, user_input=None):
        """Offer cameras the bridge already has but no entry uses yet."""
        assert self._api is not None
        if user_input is None:
            try:
                cameras = (await self._api.cameras()).get("cameras", [])
            except TVTArchiveApiError:
                return self.async_abort(reason="cannot_connect")
            key = bridge_key(self._bridge)
            shared = [e for e in self._async_current_entries() if bridge_key(e.data) == key]
            used = {str(e.data.get(CONF_CAMERA_ID)) for e in shared}
            if any(CONF_CAMERA_ID not in e.data for e in shared):
                used.update(str(c["id"]) for c in cameras)  # Not split yet, so all taken.
            self._orphans = {str(c["id"]): c for c in cameras if str(c["id"]) not in used}
            if not self._orphans or self._defaults:
                return await self.async_step_camera()
            choices = {i: str(c.get("name", i)) for i, c in self._orphans.items()}
            return self.async_show_form(
                step_id="pick",
                data_schema=vol.Schema(
                    {
                        vol.Required(CONF_CAMERA_ID, default=next(iter(choices))): vol.In(
                            {**choices, NEW_CAMERA: "Add a new camera"}
                        )
                    }
                ),
            )
        camera = self._orphans.get(str(user_input[CONF_CAMERA_ID]))
        if camera is None:
            return await self.async_step_camera()
        await self.async_set_unique_id(
            camera_uid(camera["host"], camera.get("port", DEFAULT_PORT)), raise_on_progress=False
        )
        self._abort_if_unique_id_configured()
        self._adopt = camera
        return await self.async_step_adopt()

    async def async_step_adopt(self, user_input=None):
        """Confirm the login of a camera the bridge already has."""
        assert self._api is not None
        camera, errors = self._adopt, {}
        if user_input is not None:
            try:
                await self._api.update_camera(str(camera["id"]), dict(user_input))
            except TVTArchiveApiError as error:
                errors["base"] = _camera_error(error)
            except Exception:
                errors["base"] = "unknown"
            else:
                return self.async_create_entry(
                    title=str(camera.get("name", camera["id"])),
                    data={
                        **self._bridge,
                        CONF_CAMERA_ID: str(camera["id"]),
                        **camera_login({**camera, **user_input}, user_input[CONF_PASSWORD]),
                    },
                )
        return self.async_show_form(
            step_id="adopt",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_USERNAME, default=camera.get(CONF_USERNAME, "admin")): str,
                    vol.Required(CONF_PASSWORD): PASSWORD,
                }
            ),
            description_placeholders={"camera": str(camera.get("name", camera["id"]))},
            errors=errors,
        )

    async def async_step_camera(self, user_input=None):
        if self._api is None or not self._bridge:
            return self.async_abort(reason="cannot_connect")
        errors = {}
        if user_input is not None:
            await self.async_set_unique_id(
                camera_uid(user_input[CONF_HOST], user_input[CONF_PORT]), raise_on_progress=False
            )
            self._abort_if_unique_id_configured()
            try:
                result = await self._api.add_camera(dict(user_input))
            except TVTArchiveApiError as error:
                errors["base"] = _camera_error(error)
            except Exception:
                errors["base"] = "unknown"
            else:
                return self.async_create_entry(
                    title=user_input[CONF_NAME],
                    data={
                        **self._bridge,
                        CONF_CAMERA_ID: str(result["camera"]["id"]),
                        **camera_login(user_input, user_input[CONF_PASSWORD]),
                    },
                )
        return self.async_show_form(
            step_id="camera",
            data_schema=_camera_schema(user_input or self._defaults),
            errors=errors,
        )

    async def _entry_camera(self, entry: config_entries.ConfigEntry) -> dict[str, Any]:
        self._api = await async_bridge_api(self.hass, entry.data)
        cameras = (await self._api.cameras()).get("cameras", [])
        camera_id = str(entry.data[CONF_CAMERA_ID])
        return next((c for c in cameras if str(c["id"]) == camera_id), {})

    async def async_step_reauth(self, entry_data: Mapping[str, Any]):
        if not is_builtin(entry_data):
            api = TVTArchiveApi(
                async_get_clientsession(self.hass), entry_data[CONF_URL], entry_data[CONF_TOKEN]
            )
            try:
                await api.health()
            except TVTArchiveAuthError:
                return await self.async_step_reauth_token()
            except Exception:  # noqa: BLE001
                pass
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input=None):
        entry = self._get_reauth_entry()
        errors = {}
        if user_input is not None:
            try:
                assert self._api is not None
                await self._api.update_camera(str(entry.data[CONF_CAMERA_ID]), dict(user_input))
            except TVTArchiveApiError as error:
                errors["base"] = _camera_error(error)
            except Exception:
                errors["base"] = "unknown"
            else:
                return self.async_update_reload_and_abort(entry, data_updates=dict(user_input))
        else:
            try:
                camera = await self._entry_camera(entry)
            except Exception:  # noqa: BLE001
                return self.async_abort(reason="cannot_connect")
            self._defaults = {CONF_USERNAME: camera.get(CONF_USERNAME, "admin")}
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_USERNAME, default=self._defaults.get(CONF_USERNAME, "admin")
                    ): str,
                    vol.Required(CONF_PASSWORD): PASSWORD,
                }
            ),
            description_placeholders={"camera": entry.title},
            errors=errors,
        )

    async def async_step_reauth_token(self, user_input=None):
        entry = self._get_reauth_entry()
        errors = {}
        if user_input is not None:
            token = str(user_input[CONF_TOKEN])
            api = TVTArchiveApi(async_get_clientsession(self.hass), entry.data[CONF_URL], token)
            try:
                await api.health()
            except TVTArchiveAuthError:
                errors["base"] = "invalid_auth"
            except TVTArchiveApiError:
                errors["base"] = "cannot_connect"
            except Exception:
                errors["base"] = "unknown"
            else:
                for other in self._remote_entries():
                    if other is not entry and bridge_key(other.data) == bridge_key(entry.data):
                        self.hass.config_entries.async_update_entry(
                            other, data={**other.data, CONF_TOKEN: token}
                        )
                        self.hass.config_entries.async_schedule_reload(other.entry_id)
                return self.async_update_reload_and_abort(entry, data_updates={CONF_TOKEN: token})
        return self.async_show_form(
            step_id="reauth_token",
            data_schema=vol.Schema({vol.Required(CONF_TOKEN): PASSWORD}),
            errors=errors,
        )

    async def async_step_reconfigure(self, user_input=None):
        entry = self._get_reconfigure_entry()
        errors = {}
        if user_input is not None:
            uid = camera_uid(user_input[CONF_HOST], user_input[CONF_PORT])
            if any(
                e.unique_id == uid and e.entry_id != entry.entry_id
                for e in self._async_current_entries()
            ):
                errors["base"] = "already_configured"
            else:
                try:
                    assert self._api is not None
                    await self._api.update_camera(str(entry.data[CONF_CAMERA_ID]), dict(user_input))
                except TVTArchiveApiError as error:
                    errors["base"] = _camera_error(error)
                except Exception:
                    errors["base"] = "unknown"
                else:
                    return self.async_update_reload_and_abort(
                        entry,
                        title=user_input[CONF_NAME],
                        unique_id=uid,
                        data_updates=camera_login(user_input, user_input.get(CONF_PASSWORD, "")),
                    )
            defaults = {**self._defaults, **user_input}
        else:
            try:
                camera = await self._entry_camera(entry)
            except Exception:  # noqa: BLE001
                return self.async_abort(reason="cannot_connect")
            if not camera:
                return self.async_abort(reason="camera_not_found")
            defaults = self._defaults = {
                CONF_NAME: camera.get("name", entry.title),
                CONF_HOST: camera.get("host", ""),
                CONF_PORT: camera.get("port", DEFAULT_PORT),
                CONF_USERNAME: camera.get("username", "admin"),
                CONF_RECORDING_AUDIO: camera.get("recording_audio", "auto"),
            }
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_camera_schema(defaults, editing=True),
            errors=errors,
        )


class TVTArchiveOptionsFlow(config_entries.OptionsFlow):
    async def async_step_init(self, user_input=None):
        entry = self.config_entry
        if user_input is not None:
            return self.async_create_entry(
                title="", data={**entry.options, CONF_MOTION: list(user_input.get(CONF_MOTION, []))}
            )
        current = entry.options.get(CONF_MOTION)
        if not isinstance(current, list):
            host = None
            data = self.hass.data.get(DOMAIN, {}).get(entry.entry_id)
            if data:
                host = next(iter(data["coordinator"].data.get("cameras", [])), {}).get("host")
            current = auto_motion_entities(self.hass, host)
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_MOTION, default=current): EntitySelector(
                        EntitySelectorConfig(
                            domain="binary_sensor",
                            device_class=["motion", "occupancy", "presence"],
                            multiple=True,
                        )
                    )
                }
            ),
            description_placeholders={"camera_name": entry.title},
        )

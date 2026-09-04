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
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
from homeassistant.helpers.service_info.hassio import HassioServiceInfo

from .api import TVTArchiveApi, TVTArchiveApiError, TVTArchiveAuthError
from .const import CONF_TOKEN, DOMAIN

CONF_CAMERA_ID = "camera_id"
CONF_CONFIRM = "confirm"
CONF_RECORDING_AUDIO = "recording_audio"

RECORDING_AUDIO_MODES = {
    "auto": "Auto (recommended)",
    "on": "Always expect audio",
    "off": "Disabled",
}

PASSWORD = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))


def _camera_schema(defaults: dict[str, Any] | None = None, *, editing: bool = False) -> vol.Schema:
    defaults = defaults or {}
    password = vol.Optional(CONF_PASSWORD, default="") if editing else vol.Required(CONF_PASSWORD)
    return vol.Schema(
        {
            vol.Required(CONF_NAME, default=defaults.get(CONF_NAME, "")): str,
            vol.Required(CONF_HOST, default=defaults.get(CONF_HOST, "")): str,
            vol.Required(CONF_USERNAME, default=defaults.get(CONF_USERNAME, "admin")): str,
            password: PASSWORD,
            vol.Required(CONF_PORT, default=int(defaults.get(CONF_PORT, 9008))): vol.All(
                vol.Coerce(int), vol.Range(min=1, max=65535)
            ),
            vol.Required(
                CONF_RECORDING_AUDIO, default=defaults.get(CONF_RECORDING_AUDIO, "auto")
            ): vol.In(RECORDING_AUDIO_MODES),
        }
    )


def _camera_defaults(camera: dict[str, Any]) -> dict[str, Any]:
    return {
        CONF_NAME: camera.get("name", camera.get("id", "")),
        CONF_HOST: camera.get("host", ""),
        CONF_USERNAME: camera.get("username", "admin"),
        CONF_PORT: camera.get("port", 9008),
        CONF_RECORDING_AUDIO: camera.get("recording_audio", "auto"),
    }


def _entry_title(count: int) -> str:
    return f"TVT Archive ({count} camera{'s' if count != 1 else ''})"


class TVTArchiveConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 2

    def __init__(self) -> None:
        self._url: str | None = None
        self._token: str | None = None
        self._api: TVTArchiveApi | None = None

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry):
        return TVTArchiveOptionsFlow(config_entry)

    async def async_step_user(self, user_input=None):
        errors = {}
        if user_input is not None:
            url = str(user_input[CONF_URL]).rstrip("/")
            token = str(user_input[CONF_TOKEN])
            api = TVTArchiveApi(async_get_clientsession(self.hass), url, token)
            try:
                await api.health()
                cameras = await api.cameras()
            except TVTArchiveAuthError:
                errors["base"] = "invalid_auth"
            except TVTArchiveApiError:
                errors["base"] = "cannot_connect"
            except Exception:
                errors["base"] = "unknown"
            else:
                await self.async_set_unique_id(url.lower())
                self._abort_if_unique_id_configured()
                count = len(cameras.get("cameras", []))
                if count == 0:
                    self._url, self._token, self._api = url, token, api
                    return await self.async_step_camera()
                return self.async_create_entry(
                    title=_entry_title(count), data={CONF_URL: url, CONF_TOKEN: token}
                )
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_URL): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.URL)
                    ),
                    vol.Required(CONF_TOKEN): PASSWORD,
                }
            ),
            errors=errors,
        )

    async def async_step_hassio(self, discovery_info: HassioServiceInfo):
        config = discovery_info.config
        url = f"http://{config['host']}:{config['port']}"
        token = str(config["token"])
        await self.async_set_unique_id(url.lower())
        self._abort_if_unique_id_configured(updates={CONF_URL: url, CONF_TOKEN: token})
        self._url, self._token = url, token
        self.context["title_placeholders"] = {"name": discovery_info.name}
        return await self.async_step_hassio_confirm()

    async def async_step_hassio_confirm(self, user_input=None):
        if self._url is None or self._token is None:
            return self.async_abort(reason="cannot_connect")
        if user_input is None:
            return self.async_show_form(step_id="hassio_confirm")
        api = TVTArchiveApi(async_get_clientsession(self.hass), self._url, self._token)
        try:
            await api.health()
            cameras = await api.cameras()
        except Exception:
            return self.async_abort(reason="cannot_connect")
        count = len(cameras.get("cameras", []))
        if count == 0:
            self._api = api
            return await self.async_step_camera()
        return self.async_create_entry(
            title=_entry_title(count), data={CONF_URL: self._url, CONF_TOKEN: self._token}
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]):
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input=None):
        entry = self._get_reauth_entry()
        errors = {}
        if user_input is not None:
            token = str(user_input[CONF_TOKEN])
            api = TVTArchiveApi(async_get_clientsession(self.hass), entry.data[CONF_URL], token)
            try:
                await api.cameras()
            except TVTArchiveAuthError:
                errors["base"] = "invalid_auth"
            except TVTArchiveApiError:
                errors["base"] = "cannot_connect"
            except Exception:
                errors["base"] = "unknown"
            else:
                return self.async_update_reload_and_abort(entry, data_updates={CONF_TOKEN: token})
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_TOKEN): PASSWORD}),
            errors=errors,
        )

    async def async_step_camera(self, user_input=None):
        if self._api is None or self._url is None or self._token is None:
            return self.async_abort(reason="cannot_connect")
        errors = {}
        if user_input is not None:
            try:
                await self._api.add_camera(dict(user_input))
            except TVTArchiveApiError:
                errors["base"] = "camera_connection_failed"
            except Exception:
                errors["base"] = "unknown"
            else:
                return self.async_create_entry(
                    title=_entry_title(1), data={CONF_URL: self._url, CONF_TOKEN: self._token}
                )
        return self.async_show_form(
            step_id="camera", data_schema=_camera_schema(user_input), errors=errors
        )


class TVTArchiveOptionsFlow(config_entries.OptionsFlow):
    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._entry = config_entry
        self._cameras: dict[str, dict[str, Any]] = {}
        self._camera_id: str | None = None

    def _api(self) -> TVTArchiveApi:
        loaded = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id)
        if loaded:
            return loaded["api"]
        return TVTArchiveApi(
            async_get_clientsession(self.hass),
            self._entry.data[CONF_URL],
            self._entry.data[CONF_TOKEN],
        )

    async def _refresh_cameras(self) -> None:
        payload = await self._api().cameras()
        self._cameras = {str(camera["id"]): camera for camera in payload.get("cameras", [])}

    async def _finish(self):
        await self._refresh_cameras()
        self.hass.config_entries.async_update_entry(
            self._entry, title=_entry_title(len(self._cameras))
        )
        self.hass.async_create_task(self.hass.config_entries.async_reload(self._entry.entry_id))
        return self.async_create_entry(title="", data={})

    def _camera_choices(self) -> vol.Schema:
        choices = {
            camera_id: str(camera.get("name", camera_id))
            for camera_id, camera in self._cameras.items()
        }
        return vol.Schema({vol.Required(CONF_CAMERA_ID): vol.In(choices)})

    async def async_step_init(self, user_input=None):
        try:
            await self._refresh_cameras()
        except TVTArchiveApiError:
            return self.async_abort(reason="cannot_connect")
        options = ["add_camera"]
        if self._cameras:
            options += ["edit_camera", "remove_camera"]
        return self.async_show_menu(step_id="init", menu_options=options)

    async def async_step_add_camera(self, user_input=None):
        errors = {}
        if user_input is not None:
            try:
                await self._api().add_camera(dict(user_input))
            except TVTArchiveApiError:
                errors["base"] = "camera_connection_failed"
            except Exception:
                errors["base"] = "unknown"
            else:
                return await self._finish()
        return self.async_show_form(
            step_id="add_camera", data_schema=_camera_schema(user_input), errors=errors
        )

    async def async_step_edit_camera(self, user_input=None):
        if user_input is not None:
            self._camera_id = str(user_input[CONF_CAMERA_ID])
            return await self.async_step_edit_camera_details()
        return self.async_show_form(step_id="edit_camera", data_schema=self._camera_choices())

    async def async_step_edit_camera_details(self, user_input=None):
        camera_id = self._camera_id
        if not camera_id or camera_id not in self._cameras:
            return self.async_abort(reason="camera_not_found")
        defaults = _camera_defaults(self._cameras[camera_id])
        errors = {}
        if user_input is not None:
            try:
                await self._api().update_camera(camera_id, dict(user_input))
            except TVTArchiveApiError:
                errors["base"] = "camera_connection_failed"
            except Exception:
                errors["base"] = "unknown"
            else:
                return await self._finish()
            defaults.update(user_input)
        return self.async_show_form(
            step_id="edit_camera_details",
            data_schema=_camera_schema(defaults, editing=True),
            errors=errors,
        )

    async def async_step_remove_camera(self, user_input=None):
        if user_input is not None:
            self._camera_id = str(user_input[CONF_CAMERA_ID])
            return await self.async_step_confirm_remove()
        return self.async_show_form(step_id="remove_camera", data_schema=self._camera_choices())

    async def async_step_confirm_remove(self, user_input=None):
        camera_id = self._camera_id
        if not camera_id or camera_id not in self._cameras:
            return self.async_abort(reason="camera_not_found")
        errors = {}
        if user_input is not None:
            if not user_input[CONF_CONFIRM]:
                return await self.async_step_init()
            try:
                await self._api().delete_camera(camera_id)
            except TVTArchiveApiError:
                errors["base"] = "camera_remove_failed"
            except Exception:
                errors["base"] = "unknown"
            else:
                return await self._finish()
        return self.async_show_form(
            step_id="confirm_remove",
            data_schema=vol.Schema({vol.Required(CONF_CONFIRM, default=False): bool}),
            description_placeholders={
                "camera_name": str(self._cameras[camera_id].get("name", camera_id))
            },
            errors=errors,
        )

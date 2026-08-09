from __future__ import annotations

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

from .api import TVTArchiveApi, TVTArchiveApiError
from .const import CONF_TOKEN, DOMAIN

CONF_CAMERA_ID = "camera_id"
CONF_CHANNEL = "channel"
CONF_CONFIRM = "confirm"
CONF_ARCHIVE_BACKEND = "archive_backend"
CONF_RECORDING_AUDIO = "recording_audio"
CONF_RTSP_PORT = "rtsp_port"
CONF_RTSP_STREAM_TYPE = "rtsp_stream_type"
CONF_RTSP_TRANSPORT = "rtsp_transport"
CONF_RTSP_FPS = "rtsp_fps"

BACKENDS = {
    "native_9008": "Native TCP/9008",
    "rtsp": "Recorded RTSP",
}
RECORDING_AUDIO_MODES = {"auto": "Auto (recommended)", "on": "Always expect audio", "off": "Disabled"}
RTSP_STREAMS = {"main": "Main stream", "sub": "Sub stream"}
RTSP_TRANSPORTS = {"tcp": "TCP", "udp": "UDP"}


def _camera_schema(
    defaults: dict[str, Any] | None = None,
    *,
    editing: bool = False,
) -> vol.Schema:
    """Common camera fields; RTSP-only details live on a second UI page."""
    defaults = defaults or {}
    password_field = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))
    password_key = vol.Optional(CONF_PASSWORD, default="") if editing else vol.Required(CONF_PASSWORD)
    fields: dict[Any, Any] = {
        vol.Required(CONF_NAME, default=defaults.get(CONF_NAME, "")): str,
        vol.Required(CONF_HOST, default=defaults.get(CONF_HOST, "")): str,
        vol.Required(
            CONF_ARCHIVE_BACKEND,
            default=defaults.get(CONF_ARCHIVE_BACKEND, "native_9008"),
        ): vol.In(BACKENDS),
        vol.Required(CONF_RECORDING_AUDIO, default=defaults.get(CONF_RECORDING_AUDIO, "auto")): vol.In(RECORDING_AUDIO_MODES),
        vol.Required(CONF_PORT, default=int(defaults.get(CONF_PORT, 9008))): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=65535)
        ),
        vol.Required(CONF_USERNAME, default=defaults.get(CONF_USERNAME, "admin")): str,
        password_key: password_field,
    }
    return vol.Schema(fields)


def _rtsp_schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
    """Advanced settings shown only when Recorded RTSP is selected."""
    defaults = defaults or {}
    return vol.Schema(
        {
            vol.Required(CONF_CHANNEL, default=int(defaults.get(CONF_CHANNEL, 0))): vol.All(
                vol.Coerce(int), vol.Range(min=0, max=255)
            ),
            vol.Required(CONF_RTSP_PORT, default=int(defaults.get(CONF_RTSP_PORT, 554))): vol.All(
                vol.Coerce(int), vol.Range(min=1, max=65535)
            ),
            vol.Required(
                CONF_RTSP_STREAM_TYPE,
                default=defaults.get(CONF_RTSP_STREAM_TYPE, "main"),
            ): vol.In(RTSP_STREAMS),
            vol.Required(
                CONF_RTSP_TRANSPORT,
                default=defaults.get(CONF_RTSP_TRANSPORT, "tcp"),
            ): vol.In(RTSP_TRANSPORTS),
            vol.Required(CONF_RTSP_FPS, default=float(defaults.get(CONF_RTSP_FPS, 25.0))): vol.All(
                vol.Coerce(float), vol.Range(min=5, max=120)
            ),
        }
    )

def _camera_defaults(camera: dict[str, Any]) -> dict[str, Any]:
    return {
        CONF_NAME: camera.get("name", camera.get("id", "")),
        CONF_HOST: camera.get("host", ""),
        CONF_ARCHIVE_BACKEND: camera.get("archive_backend", "native_9008"),
        CONF_RECORDING_AUDIO: camera.get("recording_audio", "auto"),
        CONF_PORT: camera.get("port", 9008),
        CONF_CHANNEL: camera.get("channel", 0),
        CONF_USERNAME: camera.get("username", "admin"),
        CONF_RTSP_PORT: camera.get("rtsp_port", 554),
        CONF_RTSP_STREAM_TYPE: camera.get("rtsp_stream_type", "main"),
        CONF_RTSP_TRANSPORT: camera.get("rtsp_transport", "tcp"),
        CONF_RTSP_FPS: camera.get("rtsp_fps", 25.0),
    }


class TVTArchiveConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 2

    def __init__(self) -> None:
        self._setup_url: str | None = None
        self._setup_token: str | None = None
        self._setup_api: TVTArchiveApi | None = None
        self._pending_camera: dict[str, Any] | None = None

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
            except TVTArchiveApiError:
                errors["base"] = "cannot_connect"
            except Exception:
                errors["base"] = "unknown"
            else:
                await self.async_set_unique_id(url.lower())
                self._abort_if_unique_id_configured()
                count = len(cameras.get("cameras", []))
                if count == 0:
                    self._setup_url = url
                    self._setup_token = token
                    self._setup_api = api
                    return await self.async_step_camera()
                return self.async_create_entry(
                    title=f"TVT Archive ({count} camera{'s' if count != 1 else ''})",
                    data={CONF_URL: url, CONF_TOKEN: token},
                )
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_URL): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.URL)
                    ),
                    vol.Required(CONF_TOKEN): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.PASSWORD)
                    ),
                }
            ),
            errors=errors,
        )

    async def async_step_camera(self, user_input=None):
        if self._setup_api is None or self._setup_url is None or self._setup_token is None:
            return self.async_abort(reason="cannot_connect")
        errors = {}
        if user_input is not None:
            payload = dict(user_input)
            if payload.get(CONF_ARCHIVE_BACKEND) == "rtsp":
                self._pending_camera = payload
                return await self.async_step_camera_rtsp()
            try:
                await self._setup_api.add_camera(payload)
            except TVTArchiveApiError:
                errors["base"] = "camera_connection_failed"
            except Exception:
                errors["base"] = "unknown"
            else:
                return self.async_create_entry(
                    title="TVT Archive (1 camera)",
                    data={CONF_URL: self._setup_url, CONF_TOKEN: self._setup_token},
                )
        return self.async_show_form(
            step_id="camera",
            data_schema=_camera_schema(user_input),
            errors=errors,
        )

    async def async_step_camera_rtsp(self, user_input=None):
        if self._setup_api is None or self._pending_camera is None:
            return self.async_abort(reason="cannot_connect")
        errors = {}
        defaults = dict(user_input or {})
        if user_input is not None:
            payload = {**self._pending_camera, **dict(user_input)}
            try:
                await self._setup_api.add_camera(payload)
            except TVTArchiveApiError:
                errors["base"] = "camera_connection_failed"
            except Exception:
                errors["base"] = "unknown"
            else:
                return self.async_create_entry(
                    title="TVT Archive (1 camera)",
                    data={CONF_URL: self._setup_url, CONF_TOKEN: self._setup_token},
                )
        return self.async_show_form(
            step_id="camera_rtsp",
            data_schema=_rtsp_schema(defaults),
            errors=errors,
        )


class TVTArchiveOptionsFlow(config_entries.OptionsFlow):
    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._entry = config_entry
        self._cameras: dict[str, dict[str, Any]] = {}
        self._selected_camera_id: str | None = None
        self._pending_camera: dict[str, Any] | None = None

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
        self._cameras = {
            str(camera["id"]): camera for camera in payload.get("cameras", [])
        }

    async def _finish_change(self) -> None:
        await self._refresh_cameras()
        count = len(self._cameras)
        self.hass.config_entries.async_update_entry(
            self._entry,
            title=f"TVT Archive ({count} camera{'s' if count != 1 else ''})",
        )
        self.hass.async_create_task(
            self.hass.config_entries.async_reload(self._entry.entry_id)
        )

    def _selected_camera(self) -> dict[str, Any]:
        if not self._selected_camera_id or self._selected_camera_id not in self._cameras:
            raise KeyError("camera_not_found")
        return self._cameras[self._selected_camera_id]

    async def async_step_init(self, user_input=None):
        try:
            await self._refresh_cameras()
        except TVTArchiveApiError:
            return self.async_abort(reason="cannot_connect")
        options = ["add_camera"]
        if self._cameras:
            options.extend(["edit_camera", "remove_camera"])
        return self.async_show_menu(step_id="init", menu_options=options)

    async def async_step_add_camera(self, user_input=None):
        errors = {}
        if user_input is not None:
            payload = dict(user_input)
            if payload.get(CONF_ARCHIVE_BACKEND) == "rtsp":
                self._pending_camera = payload
                return await self.async_step_add_camera_rtsp()
            try:
                await self._api().add_camera(payload)
            except TVTArchiveApiError:
                errors["base"] = "camera_connection_failed"
            except Exception:
                errors["base"] = "unknown"
            else:
                await self._finish_change()
                return self.async_create_entry(title="", data={})
        return self.async_show_form(
            step_id="add_camera",
            data_schema=_camera_schema(user_input),
            errors=errors,
        )

    async def async_step_add_camera_rtsp(self, user_input=None):
        if self._pending_camera is None:
            return self.async_abort(reason="camera_not_found")
        errors = {}
        defaults = dict(user_input or {})
        if user_input is not None:
            payload = {**self._pending_camera, **dict(user_input)}
            try:
                await self._api().add_camera(payload)
            except TVTArchiveApiError:
                errors["base"] = "camera_connection_failed"
            except Exception:
                errors["base"] = "unknown"
            else:
                await self._finish_change()
                return self.async_create_entry(title="", data={})
        return self.async_show_form(
            step_id="add_camera_rtsp",
            data_schema=_rtsp_schema(defaults),
            errors=errors,
        )

    async def async_step_edit_camera(self, user_input=None):
        if not self._cameras:
            await self._refresh_cameras()
        if user_input is not None:
            self._selected_camera_id = str(user_input[CONF_CAMERA_ID])
            return await self.async_step_edit_camera_details()
        choices = {
            camera_id: str(camera.get("name", camera_id))
            for camera_id, camera in self._cameras.items()
        }
        return self.async_show_form(
            step_id="edit_camera",
            data_schema=vol.Schema({vol.Required(CONF_CAMERA_ID): vol.In(choices)}),
        )

    async def async_step_edit_camera_details(self, user_input=None):
        camera_id = self._selected_camera_id
        if not camera_id or camera_id not in self._cameras:
            return self.async_abort(reason="camera_not_found")
        defaults = _camera_defaults(self._cameras[camera_id])
        errors = {}
        if user_input is not None:
            payload = dict(user_input)
            if payload.get(CONF_ARCHIVE_BACKEND) == "rtsp":
                self._pending_camera = payload
                return await self.async_step_edit_camera_rtsp()
            try:
                await self._api().update_camera(camera_id, payload)
            except TVTArchiveApiError:
                errors["base"] = "camera_connection_failed"
            except Exception:
                errors["base"] = "unknown"
            else:
                await self._finish_change()
                return self.async_create_entry(title="", data={})
            defaults.update(user_input)
        return self.async_show_form(
            step_id="edit_camera_details",
            data_schema=_camera_schema(defaults, editing=True),
            errors=errors,
        )

    async def async_step_edit_camera_rtsp(self, user_input=None):
        camera_id = self._selected_camera_id
        if not camera_id or camera_id not in self._cameras or self._pending_camera is None:
            return self.async_abort(reason="camera_not_found")
        defaults = _camera_defaults(self._cameras[camera_id])
        defaults.update(user_input or {})
        errors = {}
        if user_input is not None:
            payload = {**self._pending_camera, **dict(user_input)}
            try:
                await self._api().update_camera(camera_id, payload)
            except TVTArchiveApiError:
                errors["base"] = "camera_connection_failed"
            except Exception:
                errors["base"] = "unknown"
            else:
                await self._finish_change()
                return self.async_create_entry(title="", data={})
        return self.async_show_form(
            step_id="edit_camera_rtsp",
            data_schema=_rtsp_schema(defaults),
            errors=errors,
        )

    async def async_step_remove_camera(self, user_input=None):
        if not self._cameras:
            await self._refresh_cameras()
        if user_input is not None:
            self._selected_camera_id = str(user_input[CONF_CAMERA_ID])
            return await self.async_step_confirm_remove()
        choices = {
            camera_id: str(camera.get("name", camera_id))
            for camera_id, camera in self._cameras.items()
        }
        return self.async_show_form(
            step_id="remove_camera",
            data_schema=vol.Schema({vol.Required(CONF_CAMERA_ID): vol.In(choices)}),
        )

    async def async_step_confirm_remove(self, user_input=None):
        camera_id = self._selected_camera_id
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
                await self._finish_change()
                return self.async_create_entry(title="", data={})
        return self.async_show_form(
            step_id="confirm_remove",
            data_schema=vol.Schema({vol.Required(CONF_CONFIRM, default=False): bool}),
            description_placeholders={
                "camera_name": str(self._cameras[camera_id].get("name", camera_id))
            },
            errors=errors,
        )

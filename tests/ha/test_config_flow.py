from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from common import CAMERA, TOKEN, URL, camera_entry
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.service_info.hassio import HassioServiceInfo

from custom_components.tvt_archive.api import TVTArchiveApiError, TVTArchiveAuthError
from custom_components.tvt_archive.const import DOMAIN

CAMERA_FORM = {
    "name": "Front",
    "host": "192.0.2.10",
    "username": "admin",
    "password": "secret",
    "port": 9008,
    "recording_audio": "auto",
}
LOGIN = {"host": "192.0.2.10", "port": 9008, "username": "admin", "recording_audio": "auto"}
SAVED = {**LOGIN, "password": "secret"}
NO_SETUP = patch("custom_components.tvt_archive.async_setup_entry", return_value=True)


def _local(api: AsyncMock) -> MagicMock:
    bridge = MagicMock(token="local-token", api=api)
    bridge.async_start = AsyncMock(return_value=api)
    return bridge


async def _menu(hass: HomeAssistant, choice: str, bridge: MagicMock | None = None, **context):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER, **context}
    )
    assert result["type"] is FlowResultType.MENU
    assert result["menu_options"] == ["builtin", "remote"]
    with patch("custom_components.tvt_archive.config_flow.async_local_bridge", return_value=bridge):
        return await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": choice}
        )


async def test_builtin_flow_adds_a_camera(hass: HomeAssistant, api: AsyncMock) -> None:
    api.cameras.return_value = {"cameras": []}
    result = await _menu(hass, "builtin", _local(api))
    assert result["step_id"] == "camera"
    with NO_SETUP:
        result = await hass.config_entries.flow.async_configure(result["flow_id"], CAMERA_FORM)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Front"
    assert result["data"] == {"mode": "builtin", "camera_id": "front", **SAVED}
    assert result["result"].unique_id == "192.0.2.10:9008"
    api.add_camera.assert_awaited_once_with(CAMERA_FORM)


async def test_bridge_cameras_without_an_entry_are_offered(
    hass: HomeAssistant, api: AsyncMock
) -> None:
    result = await _menu(hass, "builtin", _local(api))
    assert result["step_id"] == "pick"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"camera_id": "front"}
    )
    assert result["step_id"] == "adopt"
    with NO_SETUP:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"username": "admin", "password": "secret"}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {"mode": "builtin", "camera_id": "front", **SAVED}
    api.update_camera.assert_awaited_once_with("front", {"username": "admin", "password": "secret"})
    api.add_camera.assert_not_called()


async def test_pick_can_add_a_new_camera(hass: HomeAssistant, api: AsyncMock) -> None:
    result = await _menu(hass, "builtin", _local(api))
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"camera_id": "new"})
    assert result["step_id"] == "camera"


async def test_same_camera_twice_is_aborted(hass: HomeAssistant, api: AsyncMock) -> None:
    camera_entry(mode="builtin").add_to_hass(hass)
    api.cameras.return_value = {"cameras": []}
    result = await _menu(hass, "builtin", _local(api))
    result = await hass.config_entries.flow.async_configure(result["flow_id"], CAMERA_FORM)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    api.add_camera.assert_not_called()


async def test_builtin_start_failure_aborts(hass: HomeAssistant, api: AsyncMock) -> None:
    bridge = _local(api)
    bridge.async_start.side_effect = RuntimeError("ffmpeg was not found")
    result = await _menu(hass, "builtin", bridge)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "builtin_failed"


async def test_camera_error_is_shown(hass: HomeAssistant, api: AsyncMock) -> None:
    api.cameras.return_value = {"cameras": []}
    api.add_camera.side_effect = TVTArchiveApiError("The camera rejected the login")
    result = await _menu(hass, "builtin", _local(api))
    result = await hass.config_entries.flow.async_configure(result["flow_id"], CAMERA_FORM)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "camera_auth_failed"}


async def test_remote_flow(hass: HomeAssistant, api: AsyncMock) -> None:
    api.cameras.return_value = {"cameras": []}
    result = await _menu(hass, "remote")
    assert result["step_id"] == "remote"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"url": URL + "/", "token": TOKEN}
    )
    assert result["step_id"] == "camera"
    with NO_SETUP:
        result = await hass.config_entries.flow.async_configure(result["flow_id"], CAMERA_FORM)
    assert result["data"] == {"url": URL, "token": TOKEN, "camera_id": "front", **SAVED}


async def test_remote_flow_reuses_a_known_token(hass: HomeAssistant, api: AsyncMock) -> None:
    camera_entry().add_to_hass(hass)
    api.cameras.return_value = {"cameras": [CAMERA]}
    result = await _menu(hass, "remote")
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"url": URL})
    assert result["step_id"] == "camera"


async def test_remote_flow_errors(hass: HomeAssistant, api: AsyncMock) -> None:
    result = await _menu(hass, "remote")
    # The bridge answers /api/health without a token; the camera list checks it.
    api.cameras.side_effect = TVTArchiveAuthError("Missing or invalid access token")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"url": URL, "token": "wrong"}
    )
    assert result["errors"] == {"base": "invalid_auth"}
    api.cameras.side_effect = TVTArchiveApiError("Bridge returned HTTP 502")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"url": URL, "token": TOKEN}
    )
    assert result["errors"] == {"base": "cannot_connect"}


async def test_reauth_updates_the_camera_login(hass: HomeAssistant, api: AsyncMock) -> None:
    entry = camera_entry()
    entry.add_to_hass(hass)
    result = await entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"
    api.update_camera.side_effect = [TVTArchiveApiError("The camera rejected the login"), {}]
    login = {"username": "admin", "password": "new"}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], login)
    assert result["errors"] == {"base": "camera_auth_failed"}
    with NO_SETUP:
        result = await hass.config_entries.flow.async_configure(result["flow_id"], login)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    api.update_camera.assert_awaited_with("front", login)
    assert entry.data["password"] == "new"


async def test_reauth_updates_the_bridge_token(hass: HomeAssistant, api: AsyncMock) -> None:
    entry = camera_entry()
    entry.add_to_hass(hass)
    bad = TVTArchiveAuthError("Missing or invalid access token")
    api.cameras.side_effect = [bad, bad, {"cameras": [CAMERA]}]
    result = await entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_token"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"token": "wrong"})
    assert result["errors"] == {"base": "invalid_auth"}
    with NO_SETUP:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"token": "fresh"}
        )
    assert result["reason"] == "reauth_successful"
    assert entry.data["token"] == "fresh"


async def test_reconfigure_moves_the_camera(hass: HomeAssistant, api: AsyncMock) -> None:
    entry = camera_entry()
    entry.add_to_hass(hass)
    result = await entry.start_reconfigure_flow(hass)
    assert result["step_id"] == "reconfigure"
    form = {**CAMERA_FORM, "name": "Porch", "host": "192.0.2.20", "password": ""}
    with NO_SETUP:
        result = await hass.config_entries.flow.async_configure(result["flow_id"], form)
    assert result["reason"] == "reconfigure_successful"
    assert entry.title == "Porch"
    assert entry.unique_id == "192.0.2.20:9008"
    assert entry.data["host"] == "192.0.2.20" and "password" not in entry.data
    api.update_camera.assert_awaited_once_with("front", form)


async def test_reconfigure_rejects_a_taken_address(hass: HomeAssistant, api: AsyncMock) -> None:
    entry = camera_entry()
    entry.add_to_hass(hass)
    camera_entry({**CAMERA, "id": "other", "host": "192.0.2.20"}).add_to_hass(hass)
    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**CAMERA_FORM, "host": "192.0.2.20"}
    )
    assert result["errors"] == {"base": "already_configured"}
    api.update_camera.assert_not_called()


async def test_addon_discovery(hass: HomeAssistant, api: AsyncMock) -> None:
    api.cameras.return_value = {"cameras": []}
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_HASSIO},
        data=HassioServiceInfo(
            config={"host": "a1b2c3d4-tvt-archive", "port": 8099, "token": "addon-token"},
            name="TVT Archive",
            slug="tvt_archive",
            uuid="1234",
        ),
    )
    assert result["step_id"] == "hassio_confirm"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["step_id"] == "camera"
    with NO_SETUP:
        result = await hass.config_entries.flow.async_configure(result["flow_id"], CAMERA_FORM)
    assert result["data"] == {
        "url": "http://a1b2c3d4-tvt-archive:8099",
        "token": "addon-token",
        "camera_id": "front",
        **SAVED,
    }


async def test_addon_discovery_updates_tokens(hass: HomeAssistant, api: AsyncMock) -> None:
    url = "http://a1b2c3d4-tvt-archive:8099"
    entry = camera_entry(url=url, token="old")
    entry.add_to_hass(hass)
    with patch("homeassistant.config_entries.ConfigEntries.async_schedule_reload"):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_HASSIO},
            data=HassioServiceInfo(
                config={"host": "a1b2c3d4-tvt-archive", "port": 8099, "token": "new"},
                name="TVT Archive",
                slug="tvt_archive",
                uuid="1234",
            ),
        )
    assert result["reason"] == "already_configured"
    assert entry.data["token"] == "new"


async def test_onvif_discovery(hass: HomeAssistant, api: AsyncMock) -> None:
    api.cameras.return_value = {"cameras": []}
    found = {"name": "Front Door", "host": "192.0.2.10", "port": 9008, "username": "viewer"}
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_INTEGRATION_DISCOVERY}, data=found
    )
    assert result["type"] is FlowResultType.MENU
    with patch(
        "custom_components.tvt_archive.config_flow.async_local_bridge", return_value=_local(api)
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": "builtin"}
        )
    assert result["step_id"] == "camera"
    defaults = {str(k): k.default() for k in result["data_schema"].schema if callable(k.default)}
    assert defaults["host"] == "192.0.2.10" and defaults["username"] == "viewer"
    hass.config_entries.flow.async_abort(result["flow_id"])
    camera_entry(mode="builtin").add_to_hass(hass)
    again = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_INTEGRATION_DISCOVERY}, data=found
    )
    assert again["reason"] == "already_configured"


async def test_options_choose_motion_sensors(hass: HomeAssistant, api: AsyncMock) -> None:
    entry = camera_entry()
    entry.add_to_hass(hass)
    hass.states.async_set("binary_sensor.front_motion", "off", {"device_class": "motion"})
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["step_id"] == "init"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"motion": ["binary_sensor.front_motion"]}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {"motion": ["binary_sensor.front_motion"]}

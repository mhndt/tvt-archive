from __future__ import annotations

from unittest.mock import AsyncMock, patch

from common import CAMERA, TOKEN, URL
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

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


def _entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        version=2,
        unique_id=URL.lower(),
        data={"url": URL, "token": TOKEN},
        title="TVT Archive (1 camera)",
    )


async def _start_user_flow(hass: HomeAssistant):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    return result


async def test_user_flow_with_existing_cameras(hass: HomeAssistant, api: AsyncMock) -> None:
    api.cameras.return_value = {"cameras": [CAMERA]}
    result = await _start_user_flow(hass)
    with patch("custom_components.tvt_archive.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"url": URL + "/", "token": TOKEN}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "TVT Archive (1 camera)"
    assert result["data"] == {"url": URL, "token": TOKEN}
    api.add_camera.assert_not_called()


async def test_user_flow_asks_for_first_camera(hass: HomeAssistant, api: AsyncMock) -> None:
    result = await _start_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"url": URL, "token": TOKEN}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "camera"
    with patch("custom_components.tvt_archive.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], CAMERA_FORM)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "TVT Archive (1 camera)"
    api.add_camera.assert_awaited_once_with(CAMERA_FORM)


async def test_user_flow_camera_error_is_shown(hass: HomeAssistant, api: AsyncMock) -> None:
    api.add_camera.side_effect = TVTArchiveApiError("login failed")
    result = await _start_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"url": URL, "token": TOKEN}
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], CAMERA_FORM)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "camera_connection_failed"}


async def test_user_flow_invalid_token(hass: HomeAssistant, api: AsyncMock) -> None:
    api.health.side_effect = TVTArchiveAuthError("Missing or invalid access token")
    result = await _start_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"url": URL, "token": "wrong"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}


async def test_user_flow_cannot_connect(hass: HomeAssistant, api: AsyncMock) -> None:
    api.health.side_effect = TVTArchiveApiError("Bridge returned HTTP 502")
    result = await _start_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"url": URL, "token": TOKEN}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


async def test_same_bridge_twice_is_aborted(hass: HomeAssistant, api: AsyncMock) -> None:
    api.cameras.return_value = {"cameras": [CAMERA]}
    _entry().add_to_hass(hass)
    result = await _start_user_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"url": URL.upper(), "token": TOKEN}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reauth_updates_token(hass: HomeAssistant, api: AsyncMock) -> None:
    entry = _entry()
    entry.add_to_hass(hass)
    result = await entry.start_reauth_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"
    api.cameras.side_effect = [TVTArchiveAuthError("bad"), {"cameras": [CAMERA]}]
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"token": "wrong"})
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}
    with (
        patch("custom_components.tvt_archive.async_setup_entry", return_value=True),
        patch("custom_components.tvt_archive.async_unload_entry", return_value=True),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"token": "fresh"}
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data["token"] == "fresh"


async def test_options_add_camera(hass: HomeAssistant, api: AsyncMock) -> None:
    entry = _entry()
    entry.add_to_hass(hass)
    api.cameras.side_effect = [{"cameras": []}, {"cameras": [CAMERA]}]
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.MENU
    assert result["menu_options"] == ["add_camera"]
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "add_camera"}
    )
    assert result["type"] is FlowResultType.FORM
    with patch("homeassistant.config_entries.ConfigEntries.async_reload", return_value=True):
        result = await hass.config_entries.options.async_configure(result["flow_id"], CAMERA_FORM)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    api.add_camera.assert_awaited_once_with(CAMERA_FORM)
    assert entry.title == "TVT Archive (1 camera)"


async def test_options_remove_camera(hass: HomeAssistant, api: AsyncMock) -> None:
    entry = _entry()
    entry.add_to_hass(hass)
    api.cameras.side_effect = [{"cameras": [CAMERA]}, {"cameras": []}]
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["menu_options"] == ["add_camera", "edit_camera", "remove_camera"]
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "remove_camera"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"camera_id": "front"}
    )
    assert result["step_id"] == "confirm_remove"
    with patch("homeassistant.config_entries.ConfigEntries.async_reload", return_value=True):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"confirm": True}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    api.delete_camera.assert_awaited_once_with("front")
    assert entry.title == "TVT Archive (0 cameras)"

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import ClientConnectionError
from common import BACK, CAMERA, TOKEN, URL, camera_entry
from homeassistant import config_entries
from homeassistant.components.media_player import BrowseError
from homeassistant.components.media_source import async_browse_media
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.tvt_archive.api import TVTArchiveApi, TVTArchiveApiError
from custom_components.tvt_archive.const import DOMAIN
from custom_components.tvt_archive.discovery import _async_scan


def _old_entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN, version=2, unique_id=URL, data={"url": URL, "token": TOKEN}
    )


async def test_unreachable_bridge_is_an_api_error() -> None:
    session = MagicMock()
    session.request.side_effect = ClientConnectionError("refused")
    with pytest.raises(TVTArchiveApiError):
        await TVTArchiveApi(session, URL, TOKEN).cameras()


async def test_offline_bridge_retries_the_split(hass: HomeAssistant, api: AsyncMock) -> None:
    api.cameras.side_effect = TVTArchiveApiError("Bridge is unreachable")
    entry = _old_entry()
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert "camera_id" not in entry.data


async def test_empty_bridge_keeps_the_entry(hass: HomeAssistant, api: AsyncMock) -> None:
    api.cameras.return_value = {"cameras": []}
    entry = _old_entry()
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert hass.config_entries.async_get_entry(entry.entry_id) is entry


async def test_split_survives_a_discovery_flow(hass: HomeAssistant, api: AsyncMock) -> None:
    api.cameras.return_value = {"cameras": [CAMERA, BACK]}
    found = {"name": "Back", "host": BACK["host"], "port": 9008, "username": "admin"}
    await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_INTEGRATION_DISCOVERY}, data=found
    )
    entry = _old_entry()
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    cameras = {e.data.get("camera_id") for e in hass.config_entries.async_entries(DOMAIN)}
    assert cameras == {"front", "back"}


async def test_split_keeps_cameras_sharing_an_address(hass: HomeAssistant, api: AsyncMock) -> None:
    api.cameras.return_value = {"cameras": [CAMERA, {**CAMERA, "id": "front2"}]}
    entry = _old_entry()
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    entries = hass.config_entries.async_entries(DOMAIN)
    assert {e.data["camera_id"] for e in entries} == {"front", "front2"}
    assert sorted(str(e.unique_id) for e in entries) == ["192.0.2.10:9008", "None"]


async def test_pick_ignores_cameras_of_an_unsplit_entry(
    hass: HomeAssistant, api: AsyncMock
) -> None:
    _old_entry().add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "remote"}
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"url": URL})
    assert result["step_id"] == "camera"


async def test_discovery_waits_for_the_split(hass: HomeAssistant) -> None:
    _old_entry().add_to_hass(hass)
    onvif = MockConfigEntry(domain="onvif", title="Back", data={"host": BACK["host"]})
    with (
        patch("custom_components.tvt_archive.discovery.async_greets", return_value=True) as greet,
        patch("custom_components.tvt_archive.discovery.discovery_flow.async_create_flow") as create,
    ):
        await _async_scan(hass, [onvif])
    greet.assert_not_called()
    create.assert_not_called()


async def test_unreachable_bridge_while_streaming() -> None:
    session = MagicMock()
    session.get = AsyncMock(side_effect=ClientConnectionError("refused"))
    with pytest.raises(TVTArchiveApiError):
        await TVTArchiveApi(session, URL, TOKEN).open_frames("a" * 32)


async def test_non_json_answer_is_an_api_error() -> None:
    response = MagicMock(status=200)
    response.json = AsyncMock(side_effect=ValueError("not json"))
    response.text = AsyncMock(return_value="<html>login</html>")
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=response)
    context.__aexit__ = AsyncMock(return_value=False)
    session = MagicMock()
    session.request.return_value = context
    with pytest.raises(TVTArchiveApiError):
        await TVTArchiveApi(session, URL, TOKEN).cameras()


async def test_media_browser_reports_bridge_errors(hass: HomeAssistant, api: AsyncMock) -> None:
    api.cameras.return_value = {"cameras": [CAMERA]}
    api.status.return_value["availability"]["days_with_recordings"] = ["2026-08-23"]
    api.timeline.side_effect = TVTArchiveApiError("Bridge is unreachable")
    assert await async_setup_component(hass, "media_source", {})
    entry = camera_entry()
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    with pytest.raises(BrowseError):
        await async_browse_media(hass, f"media-source://{DOMAIN}/{entry.entry_id}/front/2026-08-23")


async def test_last_entry_removes_the_panel(hass: HomeAssistant, api: AsyncMock) -> None:
    api.cameras.return_value = {"cameras": [CAMERA]}
    entry = camera_entry()
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert "tvt-archive" in hass.data["frontend_panels"]
    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert "tvt-archive" not in hass.data["frontend_panels"]

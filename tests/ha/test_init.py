from __future__ import annotations

from unittest.mock import AsyncMock

from common import CAMERA, TOKEN, URL
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.tvt_archive.api import TVTArchiveAuthError
from custom_components.tvt_archive.const import DOMAIN


async def _setup(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN, version=2, unique_id=URL, data={"url": URL, "token": TOKEN}
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_entities_are_created(hass: HomeAssistant, api: AsyncMock) -> None:
    api.cameras.return_value = {"cameras": [CAMERA]}
    entry = await _setup(hass)
    assert entry.state is ConfigEntryState.LOADED
    assert hass.states.get("binary_sensor.front_recording").state == "on"
    assert hass.states.get("sensor.front_recorded_today").state == "2.5"
    assert hass.states.get("sensor.front_available_history").state == "76.9"
    assert hass.states.get("sensor.front_latest_recording").state is not None


async def test_bad_token_starts_reauth(hass: HomeAssistant, api: AsyncMock) -> None:
    api.cameras.side_effect = TVTArchiveAuthError("bad")
    entry = await _setup(hass)
    assert entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert flows and flows[0]["context"]["source"] == "reauth"

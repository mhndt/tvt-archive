from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from common import BACK, CAMERA, TOKEN, URL, camera_entry
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.tvt_archive.api import TVTArchiveAuthError
from custom_components.tvt_archive.const import DOMAIN


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> MockConfigEntry:
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _bridge(api: AsyncMock) -> MagicMock:
    bridge = MagicMock(token="local-token", api=api)
    bridge.async_start = AsyncMock(return_value=api)
    bridge.async_stop = AsyncMock()
    return bridge


async def test_entities_are_created(hass: HomeAssistant, api: AsyncMock) -> None:
    api.status.return_value["sd_card"] = {"status": "no_sd_card", "total_gb": None}
    entry = await _setup(hass, camera_entry())
    assert entry.state is ConfigEntryState.LOADED
    sd_card = hass.states.get("sensor.front_sd_card")
    assert sd_card.state == "no_sd_card"
    assert sd_card.attributes["total_gb"] is None
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"sd_card_{entry.entry_id}")
    assert hass.states.get("binary_sensor.front_recording").state == "on"
    assert hass.states.get("sensor.front_recorded_today").state == "2.5"
    assert hass.states.get("sensor.front_available_history").state == "76.9"


async def test_bad_token_starts_reauth(hass: HomeAssistant, api: AsyncMock) -> None:
    api.cameras.side_effect = api.health.side_effect = TVTArchiveAuthError("bad")
    entry = await _setup(hass, camera_entry())
    assert entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert flows and flows[0]["step_id"] == "reauth_token"


async def test_rejected_camera_login_starts_reauth(hass: HomeAssistant, api: AsyncMock) -> None:
    api.status.return_value["auth_failed"] = True
    entry = await _setup(hass, camera_entry())
    assert entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert flows and flows[0]["step_id"] == "reauth_confirm"


async def test_builtin_bridge_is_shared(hass: HomeAssistant, api: AsyncMock) -> None:
    api.cameras.return_value = {"cameras": [CAMERA, BACK]}
    bridge = _bridge(api)
    front = camera_entry(mode="builtin")
    back = camera_entry(BACK, mode="builtin")
    with patch("custom_components.tvt_archive.async_local_bridge", return_value=bridge):
        await _setup(hass, front)
        await _setup(hass, back)
        assert front.state is back.state is ConfigEntryState.LOADED
        await hass.config_entries.async_unload(front.entry_id)
        bridge.async_stop.assert_not_awaited()
        await hass.config_entries.async_unload(back.entry_id)
    bridge.async_stop.assert_awaited_once()


async def test_removing_an_entry_removes_its_camera(hass: HomeAssistant, api: AsyncMock) -> None:
    api.cameras.return_value = {"cameras": [CAMERA, BACK]}
    front = await _setup(hass, camera_entry())
    await _setup(hass, camera_entry(BACK))
    await hass.config_entries.async_remove(front.entry_id)
    await hass.async_block_till_done()
    api.delete_camera.assert_awaited_once_with("front")


async def test_bridge_entry_is_split_per_camera(hass: HomeAssistant, api: AsyncMock) -> None:
    api.cameras.return_value = {"cameras": [CAMERA, BACK]}
    old = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        unique_id=URL,
        title="TVT Archive (2 cameras)",
        data={"url": URL, "token": TOKEN},
        options={"motion": {"back": ["binary_sensor.back_motion"]}},
    )
    old.add_to_hass(hass)
    entities, devices = er.async_get(hass), dr.async_get(hass)
    for camera in (CAMERA, BACK):
        devices.async_get_or_create(
            config_entry_id=old.entry_id,
            identifiers={(DOMAIN, f"{old.entry_id}:{camera['id']}")},
            name=camera["name"],
        )
        entities.async_get_or_create(
            "sensor",
            DOMAIN,
            f"{old.entry_id}:{camera['id']}:recorded_today",
            config_entry=old,
            suggested_object_id=f"{camera['id']}_recorded_today",
        )
    await hass.config_entries.async_setup(old.entry_id)
    await hass.async_block_till_done()

    entries = {e.data["camera_id"]: e for e in hass.config_entries.async_entries(DOMAIN)}
    assert set(entries) == {"front", "back"}
    assert entries["front"] is old and old.version == 3
    assert old.title == "Front" and old.unique_id == "192.0.2.10:9008"
    new = entries["back"]
    assert new.state is ConfigEntryState.LOADED
    assert new.title == "Back" and new.unique_id == "192.0.2.11:9008"
    assert new.data == {
        "url": URL,
        "token": TOKEN,
        "camera_id": "back",
        "host": "192.0.2.11",
        "port": 9008,
        "username": "admin",
        "recording_audio": "auto",
    }
    assert new.options == {"motion": ["binary_sensor.back_motion"]}
    assert old.options == {}
    moved = entities.async_get("sensor.back_recorded_today")
    assert moved.config_entry_id == new.entry_id
    assert moved.unique_id == f"{new.entry_id}:back:recorded_today"
    device = devices.async_get_device(identifiers={(DOMAIN, f"{new.entry_id}:back")})
    assert device and device.config_entries == {new.entry_id}
    kept = entities.async_get("sensor.front_recorded_today")
    assert kept.config_entry_id == old.entry_id


async def test_lost_camera_is_restored(hass: HomeAssistant, api: AsyncMock) -> None:
    api.cameras.return_value = {"cameras": []}
    login = {"host": "192.0.2.10", "port": 9008, "username": "admin", "password": "secret"}
    entry = await _setup(hass, camera_entry(**login))
    assert entry.state is ConfigEntryState.LOADED
    api.add_camera.assert_awaited_once_with(
        {"id": "front", "name": "Front", **login, "recording_audio": "auto"}
    )


async def test_lost_camera_without_a_saved_login(hass: HomeAssistant, api: AsyncMock) -> None:
    api.cameras.return_value = {"cameras": []}
    entry = await _setup(hass, camera_entry())
    assert entry.state is ConfigEntryState.SETUP_RETRY
    api.add_camera.assert_not_called()


async def test_builtin_split_keeps_passwords(
    hass: HomeAssistant, api: AsyncMock, tmp_path: Path
) -> None:
    api.cameras.return_value = {"cameras": [CAMERA, BACK]}
    saved = [{**CAMERA, "password": "one"}, {**BACK, "password": "two"}]
    (tmp_path / "config.json").write_text(json.dumps({"cameras": saved}))
    bridge = _bridge(api)
    bridge.config_dir = tmp_path
    old = MockConfigEntry(domain=DOMAIN, version=2, unique_id="builtin", data={"mode": "builtin"})
    with patch("custom_components.tvt_archive.async_local_bridge", return_value=bridge):
        await _setup(hass, old)
    entries = {e.data["camera_id"]: e for e in hass.config_entries.async_entries(DOMAIN)}
    assert entries["front"].data["password"] == "one"
    assert entries["back"].data["password"] == "two"
    assert entries["back"].data["mode"] == "builtin"

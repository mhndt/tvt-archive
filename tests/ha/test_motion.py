from __future__ import annotations

from datetime import datetime, timedelta

from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.tvt_archive.bridge.native9008 import posix_zone
from custom_components.tvt_archive.coordinator import camera_zone
from custom_components.tvt_archive.motion import _local, _ranges, auto_motion_entities


async def test_motion_sensors_follow_the_camera_address(hass: HomeAssistant) -> None:
    onvif = MockConfigEntry(domain="onvif", data={"host": "192.0.2.10"})
    other = MockConfigEntry(domain="onvif", data={"host": "192.0.2.99"})
    onvif.add_to_hass(hass)
    other.add_to_hass(hass)
    registry = er.async_get(hass)
    for config_entry, unique_id, device_class, object_id in (
        (onvif, "a", "motion", "front_cell_motion"),
        (onvif, "b", "problem", "front_problem"),
        (other, "c", "motion", "back_motion"),
    ):
        registry.async_get_or_create(
            "binary_sensor",
            "onvif",
            unique_id,
            config_entry=config_entry,
            original_device_class=device_class,
            suggested_object_id=object_id,
        )
    assert auto_motion_entities(hass, "192.0.2.10") == ["binary_sensor.front_cell_motion"]
    assert auto_motion_entities(hass, None) == []


def test_ranges_clip_to_the_day() -> None:
    start = datetime(2026, 10, 16, tzinfo=dt_util.UTC)
    stop = start + timedelta(days=1)

    def state(value: str, minutes: int) -> State:
        return State("binary_sensor.motion", value, last_changed=start + timedelta(minutes=minutes))

    states = [
        state("on", -5),
        state("off", 10),
        state("on", 60),
        state("off", 61),
        state("on", 1430),
    ]
    assert _ranges(states, start, stop) == [
        (start, start + timedelta(minutes=10)),
        (start + timedelta(minutes=60), start + timedelta(minutes=61)),
        (start + timedelta(minutes=1430), stop),
    ]


def test_motion_times_use_the_camera_zone() -> None:
    moment = datetime(2026, 11, 8, 21, 52, 12, tzinfo=dt_util.UTC)
    assert _local(moment, camera_zone({"time_zone": "EET-2"})) == "2026-11-08T23:52:12"
    assert camera_zone({}) is dt_util.get_default_time_zone()
    assert camera_zone({"time_zone": "EET-2"}) is posix_zone("EET-2")

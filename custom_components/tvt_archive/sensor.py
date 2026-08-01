from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .entity import TVTArchiveEntity


@dataclass(frozen=True, kw_only=True)
class TVTSensorDescription(SensorEntityDescription):
    value_fn: Callable[[dict[str, Any]], Any]
    timestamp: bool = False



DESCRIPTIONS = (
    TVTSensorDescription(
        key="recorded_today",
        name="Recorded today",
        native_unit_of_measurement=UnitOfTime.HOURS,
        device_class=SensorDeviceClass.DURATION,
        icon="mdi:timeline-clock",
        value_fn=lambda s: s.get("timeline_today", {}).get("recorded_hours"),
    ),
    TVTSensorDescription(
        key="available_history",
        name="Available history",
        native_unit_of_measurement=UnitOfTime.HOURS,
        device_class=SensorDeviceClass.DURATION,
        icon="mdi:history",
        value_fn=lambda s: s.get("availability", {}).get("available_history_hours"),
    ),
    TVTSensorDescription(
        key="oldest_recording",
        name="Oldest recording",
        device_class=SensorDeviceClass.TIMESTAMP,
        icon="mdi:history",
        timestamp=True,
        value_fn=lambda s: s.get("availability", {}).get("earliest"),
    ),
    TVTSensorDescription(
        key="latest_recording",
        name="Latest recording",
        device_class=SensorDeviceClass.TIMESTAMP,
        icon="mdi:clock-check",
        timestamp=True,
        value_fn=lambda s: s.get("availability", {}).get("latest"),
    ),
)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator = data["coordinator"]
    entities = []
    for camera in coordinator.data.get("cameras", []):
        for description in DESCRIPTIONS:
            entities.append(TVTArchiveSensor(coordinator, entry.entry_id, str(camera["id"]),
                                             str(camera.get("name", camera["id"])), description))
    async_add_entities(entities)


class TVTArchiveSensor(TVTArchiveEntity, SensorEntity):
    entity_description: TVTSensorDescription

    def __init__(self, coordinator, entry_id, camera_id, camera_name, description):
        super().__init__(coordinator, entry_id, camera_id, camera_name)
        self.entity_description = description
        self._attr_unique_id = f"{entry_id}:{camera_id}:{description.key}"

    @property
    def native_value(self):
        value = self.entity_description.value_fn(self.status)
        if not self.entity_description.timestamp or not value:
            return value
        parsed = datetime.fromisoformat(str(value))
        if parsed.tzinfo is None:
            zone = dt_util.get_time_zone(self.coordinator.hass.config.time_zone)
            if zone is not None:
                parsed = parsed.replace(tzinfo=zone)
        return parsed

    @property
    def available(self) -> bool:
        return super().available and bool(self.status)

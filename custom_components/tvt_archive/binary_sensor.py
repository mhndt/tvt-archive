from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorEntity, BinarySensorDeviceClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN
from .entity import TVTArchiveEntity


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator = data["coordinator"]
    async_add_entities([
        TVTRecordingSensor(coordinator, entry.entry_id, str(camera["id"]),
                           str(camera.get("name", camera["id"])))
        for camera in coordinator.data.get("cameras", [])
    ])


class TVTRecordingSensor(TVTArchiveEntity, BinarySensorEntity):
    _attr_name = "Recording"
    _attr_device_class = BinarySensorDeviceClass.RUNNING
    _attr_icon = "mdi:record-rec"

    def __init__(self, coordinator, entry_id, camera_id, camera_name):
        super().__init__(coordinator, entry_id, camera_id, camera_name)
        self._attr_unique_id = f"{entry_id}:{camera_id}:recording"

    @property
    def is_on(self) -> bool:
        return bool(self.status.get("timeline_today", {}).get("recording_now"))

    @property
    def available(self) -> bool:
        return super().available and bool(self.status.get("online"))

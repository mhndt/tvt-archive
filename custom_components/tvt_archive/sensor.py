from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfTime
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN
from .coordinator import camera_zone
from .entity import TVTArchiveEntity


@dataclass(frozen=True, kw_only=True)
class TVTSensorDescription(SensorEntityDescription):
    value_fn: Callable[[dict[str, Any]], Any]
    attrs_fn: Callable[[dict[str, Any]], dict[str, Any]] | None = None
    exists_fn: Callable[[dict[str, Any]], bool] = lambda _: True
    timestamp: bool = False


DESCRIPTIONS = (
    TVTSensorDescription(
        key="recorded_today",
        translation_key="recorded_today",
        native_unit_of_measurement=UnitOfTime.HOURS,
        device_class=SensorDeviceClass.DURATION,
        icon="mdi:timeline-clock",
        value_fn=lambda s: s.get("timeline_today", {}).get("recorded_hours"),
    ),
    TVTSensorDescription(
        key="available_history",
        translation_key="available_history",
        native_unit_of_measurement=UnitOfTime.HOURS,
        device_class=SensorDeviceClass.DURATION,
        icon="mdi:history",
        value_fn=lambda s: s.get("availability", {}).get("available_history_hours"),
    ),
    TVTSensorDescription(
        key="oldest_recording",
        translation_key="oldest_recording",
        entity_category=EntityCategory.DIAGNOSTIC,
        device_class=SensorDeviceClass.TIMESTAMP,
        icon="mdi:history",
        timestamp=True,
        value_fn=lambda s: s.get("availability", {}).get("earliest"),
    ),
    TVTSensorDescription(
        key="latest_recording",
        translation_key="latest_recording",
        entity_category=EntityCategory.DIAGNOSTIC,
        device_class=SensorDeviceClass.TIMESTAMP,
        icon="mdi:clock-check",
        timestamp=True,
        value_fn=lambda s: s.get("availability", {}).get("latest"),
    ),
    TVTSensorDescription(
        key="sd_card",
        translation_key="sd_card",
        entity_category=EntityCategory.DIAGNOSTIC,
        device_class=SensorDeviceClass.ENUM,
        options=["normal", "no_sd_card", "unformatted", "formatting", "error", "unknown"],
        icon="mdi:sd",
        value_fn=lambda s: s.get("sd_card", {}).get("status"),
        attrs_fn=lambda s: {
            k: v for k, v in s.get("sd_card", {}).items() if k in ("total_gb", "used_gb")
        },
    ),
    TVTSensorDescription(
        key="wifi_signal",
        translation_key="wifi_signal",
        entity_category=EntityCategory.DIAGNOSTIC,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:wifi",
        exists_fn=lambda s: "wifi" in s,
        value_fn=lambda s: s.get("wifi", {}).get("quality"),
        attrs_fn=lambda s: {
            k: v for k, v in s.get("wifi", {}).items() if k in ("rate_mbps", "channel")
        },
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator = data["coordinator"]
    added: set[tuple[str, str]] = set()

    @callback
    def add_entities() -> None:
        entities = []
        for camera in coordinator.data.get("cameras", []):
            camera_id = str(camera["id"])
            status = coordinator.data.get("statuses", {}).get(camera_id, {})
            for description in DESCRIPTIONS:
                if (camera_id, description.key) in added or not description.exists_fn(status):
                    continue
                added.add((camera_id, description.key))
                entities.append(
                    TVTArchiveSensor(
                        coordinator,
                        entry.entry_id,
                        camera_id,
                        str(camera.get("name", camera_id)),
                        description,
                    )
                )
        if entities:
            async_add_entities(entities)

    add_entities()
    entry.async_on_unload(coordinator.async_add_listener(add_entities))


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
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=camera_zone(self.status))

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        attrs_fn = self.entity_description.attrs_fn
        return attrs_fn(self.status) if attrs_fn else None

    @property
    def available(self) -> bool:
        return super().available and bool(self.status)

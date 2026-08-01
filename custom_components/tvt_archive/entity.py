from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from urllib.parse import urlencode

from .const import DOMAIN
from .coordinator import TVTArchiveCoordinator


class TVTArchiveEntity(CoordinatorEntity[TVTArchiveCoordinator]):
    _attr_has_entity_name = True

    def __init__(self, coordinator: TVTArchiveCoordinator, entry_id: str,
                 camera_id: str, camera_name: str) -> None:
        super().__init__(coordinator)
        self.entry_id = entry_id
        self.camera_id = camera_id
        self.camera_name = camera_name
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry_id}:{camera_id}")},
            name=camera_name,
            configuration_url=f"homeassistant://tvt-archive?{urlencode({'camera': camera_id, 'mode': 'recording'})}",
        )

    @property
    def status(self):
        return self.coordinator.data.get("statuses", {}).get(self.camera_id, {})

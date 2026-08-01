from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import TVTArchiveApi, TVTArchiveApiError
from .const import DOMAIN


class TVTArchiveCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    def __init__(self, hass: HomeAssistant, api: TVTArchiveApi) -> None:
        super().__init__(hass, logger=__import__("logging").getLogger(__name__),
                         name=DOMAIN, update_interval=timedelta(minutes=2))
        self.api = api

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            cameras_payload = await self.api.cameras()
            cameras = list(cameras_payload.get("cameras", []))
            results = await asyncio.gather(
                *(self.api.status(str(item["id"])) for item in cameras), return_exceptions=True
            )
            statuses: dict[str, Any] = {}
            for item, result in zip(cameras, results, strict=False):
                camera_id = str(item["id"])
                if isinstance(result, Exception):
                    statuses[camera_id] = {"camera": item, "online": False, "error": str(result)}
                else:
                    statuses[camera_id] = result
            return {"cameras": cameras, "statuses": statuses}
        except TVTArchiveApiError as error:
            raise UpdateFailed(str(error)) from error

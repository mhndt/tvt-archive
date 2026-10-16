from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import TVTArchiveApi, TVTArchiveApiError, TVTArchiveAuthError
from .const import DOMAIN

SD_CARD_PROBLEMS = {
    "no_sd_card": "no SD card",
    "unformatted": "an unformatted SD card",
    "error": "a failed SD card",
}


class TVTArchiveCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    def __init__(self, hass: HomeAssistant, api: TVTArchiveApi) -> None:
        super().__init__(
            hass,
            logger=logging.getLogger(__name__),
            name=DOMAIN,
            update_interval=timedelta(minutes=2),
        )
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
            for item in cameras:
                camera_id = str(item["id"])
                state = statuses[camera_id].get("sd_card", {}).get("status")
                issue = f"sd_card_{camera_id}"
                if state in SD_CARD_PROBLEMS:
                    ir.async_create_issue(
                        self.hass,
                        DOMAIN,
                        issue,
                        is_fixable=False,
                        severity=ir.IssueSeverity.WARNING,
                        translation_key="sd_card",
                        translation_placeholders={
                            "camera": str(item.get("name", camera_id)),
                            "state": SD_CARD_PROBLEMS[state],
                        },
                    )
                elif state:
                    ir.async_delete_issue(self.hass, DOMAIN, issue)
                login = f"camera_login_{camera_id}"
                if statuses[camera_id].get("auth_failed"):
                    ir.async_create_issue(
                        self.hass,
                        DOMAIN,
                        login,
                        is_fixable=False,
                        severity=ir.IssueSeverity.ERROR,
                        translation_key="camera_login",
                        translation_placeholders={"camera": str(item.get("name", camera_id))},
                    )
                elif statuses[camera_id].get("online"):
                    ir.async_delete_issue(self.hass, DOMAIN, login)
            return {"cameras": cameras, "statuses": statuses}
        except TVTArchiveAuthError as error:
            raise ConfigEntryAuthFailed from error
        except TVTArchiveApiError as error:
            raise UpdateFailed(str(error)) from error

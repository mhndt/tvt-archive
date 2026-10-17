from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import TVTArchiveApi, TVTArchiveApiError, TVTArchiveAuthError
from .const import CONF_CAMERA_ID, CONF_RECORDING_AUDIO, DEFAULT_PORT, DOMAIN

_LOGGER = logging.getLogger(__name__)

SD_CARD_PROBLEMS = {
    "no_sd_card": "no SD card",
    "unformatted": "an unformatted SD card",
    "error": "a failed SD card",
}


class TVTArchiveCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, api: TVTArchiveApi) -> None:
        super().__init__(
            hass,
            logger=_LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {entry.title}",
            update_interval=timedelta(minutes=2),
        )
        self.api = api
        self.camera_id = str(entry.data[CONF_CAMERA_ID])

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            cameras = (await self.api.cameras()).get("cameras", [])
            camera = next((c for c in cameras if str(c.get("id")) == self.camera_id), None)
            if camera is None:
                camera = await self._async_restore()
            try:
                status = await self.api.status(self.camera_id)
            except TVTArchiveAuthError:
                raise
            except TVTArchiveApiError as error:
                status = {"camera": camera, "online": False, "error": str(error)}
        except TVTArchiveAuthError as error:
            raise ConfigEntryAuthFailed("The bridge rejected the access token") from error
        except TVTArchiveApiError as error:
            raise UpdateFailed(str(error)) from error
        if status.get("auth_failed"):
            raise ConfigEntryAuthFailed("The camera rejected its login")
        issue = f"sd_card_{self.config_entry.entry_id}"
        state = status.get("sd_card", {}).get("status")
        if state in SD_CARD_PROBLEMS:
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                issue,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key="sd_card",
                translation_placeholders={
                    "camera": str(camera.get("name", self.camera_id)),
                    "state": SD_CARD_PROBLEMS[state],
                },
            )
        elif state:
            ir.async_delete_issue(self.hass, DOMAIN, issue)
        return {"cameras": [camera], "statuses": {self.camera_id: status}}

    async def _async_restore(self) -> dict[str, Any]:
        """Add the camera back from the entry when the bridge lost it."""
        data = self.config_entry.data
        if not data.get(CONF_PASSWORD):
            raise UpdateFailed("The camera is no longer configured on the bridge")
        result = await self.api.add_camera(
            {
                "id": self.camera_id,
                "name": self.config_entry.title,
                CONF_HOST: data[CONF_HOST],
                CONF_PORT: data.get(CONF_PORT, DEFAULT_PORT),
                CONF_USERNAME: data[CONF_USERNAME],
                CONF_PASSWORD: data[CONF_PASSWORD],
                CONF_RECORDING_AUDIO: data.get(CONF_RECORDING_AUDIO, "auto"),
            }
        )
        _LOGGER.warning("Restored %s on the bridge", self.config_entry.title)
        return result["camera"]

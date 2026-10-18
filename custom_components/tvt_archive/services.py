from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from pathlib import Path

import voluptuous as vol
from aiohttp import ClientError
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr

from .api import TVTArchiveApiError
from .const import DOMAIN

RECORDING_LAG = timedelta(seconds=15)

SAVE_SCHEMA = vol.Schema(
    {
        vol.Required("device_id"): cv.string,
        vol.Optional("start"): cv.datetime,
        vol.Required("duration"): vol.All(vol.Coerce(int), vol.Range(min=5, max=3600)),
        vol.Optional("filename"): cv.string,
    }
)


def _camera(hass: HomeAssistant, device_id: str) -> tuple[dict, str]:
    device = dr.async_get(hass).async_get(device_id)
    for domain, identifier in device.identifiers if device else ():
        entry_id, _, camera_id = identifier.partition(":")
        data = hass.data.get(DOMAIN, {}).get(entry_id)
        if domain == DOMAIN and isinstance(data, dict) and "api" in data:
            return data, camera_id
    raise ServiceValidationError("Choose a TVT Archive camera")


async def _async_save(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    try:
        return await _async_export(hass, call)
    except (TVTArchiveApiError, ClientError, OSError) as error:
        raise HomeAssistantError(
            f"The camera's bridge could not save the recording: {error}"
        ) from error


async def _async_export(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    data, camera_id = _camera(hass, call.data["device_id"])
    duration = call.data["duration"]
    # The bridge reads camera times as the host's local time.
    start: datetime = call.data.get("start") or (
        datetime.now() - RECORDING_LAG - timedelta(seconds=duration)
    )
    if start.tzinfo:
        start = start.astimezone().replace(tzinfo=None)
    api = data["api"]
    job = await api.create_job(
        camera_id, {"start": start.isoformat(timespec="seconds"), "duration": duration}
    )
    deadline = asyncio.get_running_loop().time() + duration * 2 + 120
    while job.get("status") not in ("ready", "error", "cancelled"):
        if asyncio.get_running_loop().time() > deadline:
            raise HomeAssistantError("The recording took too long to export")
        await asyncio.sleep(2)
        job = await api.job(job["id"])
    if job["status"] != "ready":
        raise HomeAssistantError(job.get("error") or "The recording could not be exported")
    name = Path(call.data.get("filename") or job.get("filename") or f"{job['id']}.mp4").name
    if not name.endswith(".mp4"):
        name += ".mp4"
    media = Path(hass.config.media_dirs.get("local", hass.config.path("media")))
    path = media / DOMAIN / name
    await hass.async_add_executor_job(lambda: path.parent.mkdir(parents=True, exist_ok=True))
    response = await api.open_file(job["id"], download=True)
    try:
        handle = await hass.async_add_executor_job(path.open, "wb")
        try:
            async for chunk in response.content.iter_chunked(1024 * 1024):
                await hass.async_add_executor_job(handle.write, chunk)
        finally:
            await hass.async_add_executor_job(handle.close)
    finally:
        response.release()
    return {
        "path": str(path),
        "media_content_id": f"media-source://media_source/local/{DOMAIN}/{name}",
    }


def async_register_services(hass: HomeAssistant) -> None:
    if hass.services.has_service(DOMAIN, "save_recording"):
        return

    async def save(call: ServiceCall) -> ServiceResponse:
        return await _async_save(hass, call)

    hass.services.async_register(
        DOMAIN,
        "save_recording",
        save,
        schema=SAVE_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )

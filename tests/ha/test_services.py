from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from common import CAMERA, camera_entry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import device_registry as dr

from custom_components.tvt_archive.const import DOMAIN


async def test_save_recording(hass: HomeAssistant, api: AsyncMock, tmp_path: Path) -> None:
    api.cameras.return_value = {"cameras": [CAMERA]}
    api.create_job.return_value = {"id": "j" * 32, "status": "queued"}
    api.job.return_value = {"id": "j" * 32, "status": "ready", "filename": "Front_clip.mp4"}

    async def chunks(size):
        yield b"mp4"

    response = MagicMock()
    response.content.iter_chunked = chunks
    api.open_file.return_value = response
    hass.config.media_dirs = {"local": str(tmp_path)}
    entry = camera_entry()
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    device = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, f"{entry.entry_id}:front")})

    result = await hass.services.async_call(
        DOMAIN,
        "save_recording",
        {"device_id": device.id, "start": "2026-08-23 08:10:00", "duration": 60},
        blocking=True,
        return_response=True,
    )
    assert (tmp_path / "tvt_archive" / "Front_clip.mp4").read_bytes() == b"mp4"
    assert (
        result["media_content_id"] == "media-source://media_source/local/tvt_archive/Front_clip.mp4"
    )
    api.create_job.assert_awaited_once_with(
        "front", {"start": "2026-08-23T08:10:00", "duration": 60}
    )
    response.release.assert_called_once()

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, "save_recording", {"device_id": "nope", "duration": 60}, blocking=True
        )

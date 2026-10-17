from __future__ import annotations

from unittest.mock import AsyncMock

from common import CAMERA, camera_entry
from homeassistant.components.media_source import async_browse_media, async_resolve_media
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from custom_components.tvt_archive.const import DOMAIN


async def test_browse_and_resolve(hass: HomeAssistant, api: AsyncMock) -> None:
    api.cameras.return_value = {"cameras": [CAMERA]}
    api.status.return_value["availability"]["days_with_recordings"] = ["2026-08-22", "2026-08-23"]
    api.timeline.return_value = {
        "merged_ranges": [{"start": "2026-08-23T08:00:00", "stop": "2026-08-23T08:25:00"}],
        "events": [
            {"start": "2026-08-23T08:12:00", "stop": "2026-08-23T08:12:30", "type": "motion"}
        ],
    }
    api.create_session.return_value = {"id": "a" * 32, "playlist_ready": False}
    api.playback_session.return_value = {
        "id": "a" * 32,
        "playlist_ready": True,
        "created_at_unix": 1,
    }
    assert await async_setup_component(hass, "media_source", {})
    entry = camera_entry()
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    root = await async_browse_media(hass, f"media-source://{DOMAIN}")
    assert [child.title for child in root.children] == ["Front"]
    camera = await async_browse_media(hass, root.children[0].media_content_id)
    assert [child.title for child in camera.children] == ["2026-08-23", "2026-08-22"]
    assert camera.thumbnail.endswith("/cameras/front/thumbnail/latest")
    day = await async_browse_media(hass, camera.children[0].media_content_id)
    assert [child.title for child in day.children] == [
        "Motion",
        "08:20–08:25",
        "08:10–08:20 · motion",
        "08:00–08:10",
    ]
    clip = day.children[2]
    assert clip.thumbnail.endswith("/cameras/front/thumbnail/2026-08-23T08:10:00")
    motion = await async_browse_media(hass, day.children[0].media_content_id)
    assert [child.title for child in motion.children] == ["08:10–08:20 · motion"]
    media = await async_resolve_media(hass, clip.media_content_id, None)
    assert media.mime_type == "application/x-mpegURL"
    assert media.url.endswith("/index.m3u8")
    api.create_session.assert_awaited_once_with(
        "front", {"start": "2026-08-23T08:10:00", "duration": 600, "quality": "original"}
    )

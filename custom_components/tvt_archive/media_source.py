from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from homeassistant.components.media_player import BrowseError, MediaClass, MediaType
from homeassistant.components.media_source import (
    BrowseMediaSource,
    MediaSource,
    MediaSourceItem,
    PlayMedia,
    Unresolvable,
)
from homeassistant.core import HomeAssistant

from .api import TVTArchiveApiError
from .const import DOMAIN
from .http import _add_hls_url, _ha_motion

CLIP_SECONDS = 600


async def async_get_media_source(hass: HomeAssistant) -> TVTArchiveMediaSource:
    return TVTArchiveMediaSource(hass)


def _node(
    identifier: str, title: str, *, playable: bool = False, thumbnail: str | None = None
) -> BrowseMediaSource:
    return BrowseMediaSource(
        domain=DOMAIN,
        identifier=identifier,
        media_class=MediaClass.VIDEO if playable else MediaClass.DIRECTORY,
        media_content_type=MediaType.VIDEO if playable else "",
        title=title,
        can_play=playable,
        can_expand=not playable,
        children_media_class=None if playable else MediaClass.VIDEO,
        thumbnail=thumbnail,
    )


def _thumb(entry_id: str, camera_id: str, at: str) -> str:
    return f"/api/tvt_archive/{entry_id}/cameras/{camera_id}/thumbnail/{at}"


class TVTArchiveMediaSource(MediaSource):
    name = "TVT Archive"

    def __init__(self, hass: HomeAssistant) -> None:
        super().__init__(DOMAIN)
        self.hass = hass

    def _entries(self) -> dict[str, dict]:
        return {
            k: v
            for k, v in self.hass.data.get(DOMAIN, {}).items()
            if isinstance(v, dict) and "api" in v
        }

    def _data(self, entry_id: str) -> dict:
        data = self._entries().get(entry_id)
        if data is None:
            raise Unresolvable("Unknown TVT Archive entry")
        return data

    async def async_resolve_media(self, item: MediaSourceItem) -> PlayMedia:
        try:
            entry_id, camera_id, start, seconds = item.identifier.split("/")
        except ValueError as error:
            raise Unresolvable("Not a recording") from error
        data = self._data(entry_id)
        api = data["api"]
        try:
            session = await api.create_session(
                camera_id, {"start": start, "duration": int(seconds), "quality": "original"}
            )
            for _ in range(120):
                if session.get("error"):
                    raise Unresolvable(str(session["error"]))
                if session.get("playlist_ready"):
                    url = _add_hls_url(entry_id, data, session)["playlist_url"]
                    return PlayMedia(url, "application/x-mpegURL")
                await asyncio.sleep(0.5)
                session = await api.playback_session(session["id"])
        except TVTArchiveApiError as error:
            raise Unresolvable(f"The camera could not play this recording: {error}") from error
        raise Unresolvable("The recording did not start in time")

    async def async_browse_media(self, item: MediaSourceItem) -> BrowseMediaSource:
        parts = item.identifier.split("/") if item.identifier else []
        if not parts:
            root = _node("", self.name)
            root.children = [
                _node(
                    f"{entry_id}/{camera['id']}",
                    str(camera.get("name", camera["id"])),
                    thumbnail=_thumb(entry_id, camera["id"], "latest"),
                )
                for entry_id, data in self._entries().items()
                for camera in data["coordinator"].data.get("cameras", [])
            ]
            return root
        data = self._data(parts[0])
        statuses = data["coordinator"].data.get("statuses", {})
        name = str(statuses.get(parts[1], {}).get("camera", {}).get("name", parts[1]))
        if len(parts) == 2:
            node = _node(item.identifier, name, thumbnail=_thumb(parts[0], parts[1], "latest"))
            days = statuses.get(parts[1], {}).get("availability", {}).get("days_with_recordings")
            node.children = [
                _node(f"{item.identifier}/{day}", day) for day in sorted(days or [], reverse=True)
            ]
            return node
        if len(parts) == 3 or (len(parts) == 4 and parts[3] == "motion"):
            try:
                clips = await self._clips(parts[0], data, parts[1], parts[2])
            except TVTArchiveApiError as error:
                raise BrowseError(f"The camera could not list recordings: {error}") from error
            motion_only = len(parts) == 4
            node = _node(item.identifier, f"{name} {parts[2]}" + (" motion" if motion_only else ""))
            node.children = [
                _node(
                    f"{parts[0]}/{parts[1]}/{start.isoformat()}/{seconds}",
                    f"{start:%H:%M}–{start + timedelta(seconds=seconds):%H:%M}"
                    + (" · motion" if motion else ""),
                    playable=True,
                    thumbnail=_thumb(parts[0], parts[1], start.isoformat()),
                )
                for start, seconds, motion in clips
                if motion or not motion_only
            ]
            if not motion_only and any(motion for *_, motion in clips):
                node.children.insert(0, _node(f"{item.identifier}/motion", "Motion"))
            return node
        raise Unresolvable("Unknown media")

    async def _clips(
        self, entry_id: str, data: dict, camera_id: str, day: str
    ) -> list[tuple[datetime, int, bool]]:
        timeline = await data["api"].timeline(camera_id, day)
        events = [
            (datetime.fromisoformat(e["start"]), datetime.fromisoformat(e["stop"]))
            for e in timeline.get("events")
            or await _ha_motion(self.hass, entry_id, data, camera_id, day)
        ]
        clips = []
        for span in timeline.get("merged_ranges", []):
            cursor, stop = (
                datetime.fromisoformat(span["start"]),
                datetime.fromisoformat(span["stop"]),
            )
            while cursor < stop:
                end = min(cursor + timedelta(seconds=CLIP_SECONDS), stop)
                seconds = int((end - cursor).total_seconds())
                if seconds >= 5:
                    clips.append((cursor, seconds, any(a < end and b > cursor for a, b in events)))
                cursor = end
        return clips[::-1]

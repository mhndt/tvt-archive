from __future__ import annotations

import hashlib
import hmac
import re
import time
from typing import Any

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

from .api import TVTArchiveApiError
from .const import DOMAIN

_STREAM_QUALITIES = {"original", "balanced", "data_saver"}
_HLS_ASSET_RE = re.compile(r"(?:index\.m3u8|init\.mp4|segment-\d{5}\.m4s)")
_HLS_URL_TTL_SECONDS = 2 * 60 * 60
_MEDIA_URL_TTL_SECONDS = 2 * 60 * 60
_PLAYER_URL_TTL_SECONDS = 24 * 60 * 60


def _entry_data(hass: HomeAssistant, entry_id: str):
    data = hass.data.get(DOMAIN, {}).get(entry_id)
    if not isinstance(data, dict) or "api" not in data:
        raise web.HTTPNotFound(text="Unknown TVT Archive config entry")
    return data


def _media_signature(key: bytes, job_id: str, expires: int, download: bool) -> str:
    payload = f"{job_id}:{expires}:{int(download)}".encode()
    return hmac.new(key, payload, hashlib.sha256).hexdigest()


def _hls_signature(key: bytes, entry_id: str, session_id: str, expires: int) -> str:
    payload = f"{entry_id}:{session_id}:{expires}".encode()
    return hmac.new(key, payload, hashlib.sha256).hexdigest()

def _player_signature(key: bytes, entry_id: str, expires: int) -> str:
    payload = f"player:{entry_id}:{expires}".encode()
    return hmac.new(key, payload, hashlib.sha256).hexdigest()


def _add_media_urls(
    entry_id: str, data: dict[str, Any], job: dict[str, Any]
) -> dict[str, Any]:
    result = dict(job)
    if result.get("ready"):
        expires = int(time.time()) + _MEDIA_URL_TTL_SECONDS
        base = f"/api/tvt_archive/media/{entry_id}/{result['id']}.mp4"
        result["media_url"] = (
            f"{base}?expires={expires}&sig="
            f"{_media_signature(data['media_key'], result['id'], expires, False)}"
        )
        result["download_url"] = (
            f"{base}?download=1&expires={expires}&sig="
            f"{_media_signature(data['media_key'], result['id'], expires, True)}"
        )
    return result


def _add_hls_url(
    entry_id: str, data: dict[str, Any], session: dict[str, Any]
) -> dict[str, Any]:
    result = dict(session)
    created_at = int(result.get("created_at_unix", int(time.time())))
    player_expires = created_at + _PLAYER_URL_TTL_SECONDS
    player_signature = _player_signature(data["media_key"], entry_id, player_expires)
    result["player_script_url"] = (
        f"/api/tvt_archive/player/{entry_id}/{player_expires}/{player_signature}/hls.min.js"
    )
    if result.get("playlist_ready"):
        expires = created_at + _HLS_URL_TTL_SECONDS
        signature = _hls_signature(data["media_key"], entry_id, result["id"], expires)
        result["playlist_url"] = (
            f"/api/tvt_archive/hls/{entry_id}/{result['id']}/{expires}/{signature}/index.m3u8"
        )
    return result


class EntriesView(HomeAssistantView):
    url = "/api/tvt_archive/entries"
    name = "api:tvt_archive:entries"
    requires_auth = True

    async def get(self, request):
        hass = request.app["hass"]
        entries = []
        for entry in hass.config_entries.async_entries(DOMAIN):
            if entry.entry_id in hass.data.get(DOMAIN, {}):
                entries.append({"entry_id": entry.entry_id, "title": entry.title})
        return self.json({"entries": entries})


class CamerasView(HomeAssistantView):
    url = "/api/tvt_archive/{entry_id}/cameras"
    name = "api:tvt_archive:cameras"
    requires_auth = True

    async def get(self, request, entry_id):
        return self.json(await _entry_data(request.app["hass"], entry_id)["api"].cameras())


class StatusView(HomeAssistantView):
    url = "/api/tvt_archive/{entry_id}/cameras/{camera_id}/status"
    name = "api:tvt_archive:status"
    requires_auth = True

    async def get(self, request, entry_id, camera_id):
        data = _entry_data(request.app["hass"], entry_id)
        return self.json(
            await data["api"].status(camera_id, request.query.get("refresh") == "1")
        )


class TimelineView(HomeAssistantView):
    url = "/api/tvt_archive/{entry_id}/cameras/{camera_id}/timeline"
    name = "api:tvt_archive:timeline"
    requires_auth = True

    async def get(self, request, entry_id, camera_id):
        date = request.query.get("date")
        if not date:
            raise web.HTTPBadRequest(text="date is required")
        data = _entry_data(request.app["hass"], entry_id)
        return self.json(
            await data["api"].timeline(
                camera_id, date, request.query.get("refresh") == "1"
            )
        )


class AvailabilityView(HomeAssistantView):
    url = "/api/tvt_archive/{entry_id}/cameras/{camera_id}/availability"
    name = "api:tvt_archive:availability"
    requires_auth = True

    async def get(self, request, entry_id, camera_id):
        data = _entry_data(request.app["hass"], entry_id)
        return self.json(
            await data["api"].availability(camera_id, int(request.query.get("days", "45")))
        )


class CreateSessionView(HomeAssistantView):
    url = "/api/tvt_archive/{entry_id}/cameras/{camera_id}/sessions"
    name = "api:tvt_archive:create_session"
    requires_auth = True

    async def post(self, request, entry_id, camera_id):
        data = _entry_data(request.app["hass"], entry_id)
        try:
            session = await data["api"].create_session(camera_id, await request.json())
        except TVTArchiveApiError as error:
            raise web.HTTPBadGateway(text=str(error)) from error
        return self.json(_add_hls_url(entry_id, data, session), status_code=202)


class SessionView(HomeAssistantView):
    url = "/api/tvt_archive/{entry_id}/sessions/{session_id}"
    name = "api:tvt_archive:session"
    requires_auth = True

    async def get(self, request, entry_id, session_id):
        data = _entry_data(request.app["hass"], entry_id)
        try:
            session = await data["api"].playback_session(session_id)
        except TVTArchiveApiError as error:
            raise web.HTTPBadGateway(text=str(error)) from error
        return self.json(_add_hls_url(entry_id, data, session))

    async def delete(self, request, entry_id, session_id):
        data = _entry_data(request.app["hass"], entry_id)
        try:
            session = await data["api"].stop_session(session_id)
        except TVTArchiveApiError as error:
            raise web.HTTPBadGateway(text=str(error)) from error
        return self.json(_add_hls_url(entry_id, data, session))


class HLSLibraryView(HomeAssistantView):
    url = "/api/tvt_archive/player/{entry_id}/{expires}/{signature}/hls.min.js"
    name = "api:tvt_archive:hls_library"
    requires_auth = False

    async def get(self, request, entry_id, expires, signature):
        data = _entry_data(request.app["hass"], entry_id)
        try:
            expiry = int(expires)
        except ValueError as error:
            raise web.HTTPForbidden(text="Invalid player signature") from error
        expected = _player_signature(data["media_key"], entry_id, expiry)
        if expiry < int(time.time()) or not hmac.compare_digest(signature, expected):
            raise web.HTTPForbidden(text="Expired or invalid player signature")
        try:
            upstream = await data["api"].open_player_script()
        except TVTArchiveApiError as error:
            raise web.HTTPBadGateway(text=str(error)) from error
        body = await upstream.read()
        upstream.release()
        return web.Response(
            body=body,
            content_type="application/javascript",
            charset="utf-8",
            headers={"Cache-Control": "public, max-age=31536000, immutable"},
        )

class HLSAssetView(HomeAssistantView):
    url = "/api/tvt_archive/hls/{entry_id}/{session_id}/{expires}/{signature}/{asset}"
    name = "api:tvt_archive:hls_asset"
    requires_auth = False

    async def _serve(
        self, request, entry_id, session_id, expires, signature, asset, *, head_only: bool
    ):
        if not _HLS_ASSET_RE.fullmatch(asset):
            raise web.HTTPNotFound(text="Unknown HLS asset")
        data = _entry_data(request.app["hass"], entry_id)
        try:
            expiry = int(expires)
        except ValueError as error:
            raise web.HTTPForbidden(text="Invalid HLS signature") from error
        expected = _hls_signature(data["media_key"], entry_id, session_id, expiry)
        if expiry < int(time.time()) or not hmac.compare_digest(signature, expected):
            raise web.HTTPForbidden(text="Expired or invalid HLS signature")
        try:
            upstream = await data["api"].open_hls_asset(
                session_id, asset, range_header=request.headers.get("Range")
            )
        except TVTArchiveApiError as error:
            raise web.HTTPBadGateway(text=str(error)) from error
        response = web.StreamResponse(status=upstream.status)
        for header in (
            "Content-Type",
            "Content-Length",
            "Content-Range",
            "Accept-Ranges",
            "X-TVT-Archive-Accelerator",
        ):
            if header in upstream.headers:
                response.headers[header] = upstream.headers[header]
        response.headers["Cache-Control"] = (
            "no-store" if asset.endswith(".m3u8") else "private, max-age=3600, immutable"
        )
        await response.prepare(request)
        try:
            if not head_only:
                async for chunk in upstream.content.iter_chunked(256 * 1024):
                    await response.write(chunk)
        except (ConnectionResetError, RuntimeError):
            pass
        finally:
            upstream.release()
        try:
            await response.write_eof()
        except (ConnectionResetError, RuntimeError):
            pass
        return response

    async def get(self, request, entry_id, session_id, expires, signature, asset):
        return await self._serve(
            request, entry_id, session_id, expires, signature, asset, head_only=False
        )

    async def head(self, request, entry_id, session_id, expires, signature, asset):
        return await self._serve(
            request, entry_id, session_id, expires, signature, asset, head_only=True
        )


class CreateJobView(HomeAssistantView):
    url = "/api/tvt_archive/{entry_id}/cameras/{camera_id}/jobs"
    name = "api:tvt_archive:create_job"
    requires_auth = True

    async def post(self, request, entry_id, camera_id):
        data = _entry_data(request.app["hass"], entry_id)
        try:
            job = await data["api"].create_job(camera_id, await request.json())
        except TVTArchiveApiError as error:
            raise web.HTTPBadGateway(text=str(error)) from error
        return self.json(
            _add_media_urls(entry_id, data, job),
            status_code=202 if not job.get("ready") else 200,
        )


class JobView(HomeAssistantView):
    url = "/api/tvt_archive/{entry_id}/jobs/{job_id}"
    name = "api:tvt_archive:job"
    requires_auth = True

    async def get(self, request, entry_id, job_id):
        data = _entry_data(request.app["hass"], entry_id)
        try:
            job = await data["api"].job(job_id)
        except TVTArchiveApiError as error:
            raise web.HTTPBadGateway(text=str(error)) from error
        return self.json(_add_media_urls(entry_id, data, job))


class MediaView(HomeAssistantView):
    url = "/api/tvt_archive/media/{entry_id}/{job_id}.mp4"
    name = "api:tvt_archive:media"
    requires_auth = False

    async def get(self, request, entry_id, job_id):
        data = _entry_data(request.app["hass"], entry_id)
        try:
            expires = int(request.query.get("expires", "0"))
        except ValueError as error:
            raise web.HTTPForbidden(text="Invalid media signature") from error
        download = request.query.get("download") == "1"
        supplied = request.query.get("sig", "")
        expected = _media_signature(data["media_key"], job_id, expires, download)
        if expires < int(time.time()) or not hmac.compare_digest(supplied, expected):
            raise web.HTTPForbidden(text="Expired or invalid media signature")
        try:
            upstream = await data["api"].open_file(
                job_id,
                range_header=request.headers.get("Range"),
                download=download,
            )
        except TVTArchiveApiError as error:
            raise web.HTTPBadGateway(text=str(error)) from error
        response = web.StreamResponse(status=upstream.status)
        for header in (
            "Content-Type",
            "Content-Length",
            "Content-Range",
            "Accept-Ranges",
            "Content-Disposition",
        ):
            if header in upstream.headers:
                response.headers[header] = upstream.headers[header]
        response.headers["Cache-Control"] = "private, max-age=900"
        await response.prepare(request)
        try:
            async for chunk in upstream.content.iter_chunked(256 * 1024):
                await response.write(chunk)
        except (ConnectionResetError, RuntimeError):
            pass
        finally:
            upstream.release()
        try:
            await response.write_eof()
        except (ConnectionResetError, RuntimeError):
            pass
        return response


def register_views(hass: HomeAssistant) -> None:
    for view in (
        EntriesView(),
        CamerasView(),
        StatusView(),
        TimelineView(),
        AvailabilityView(),
        CreateSessionView(),
        SessionView(),
        HLSLibraryView(),
        HLSAssetView(),
        CreateJobView(),
        JobView(),
        MediaView(),
    ):
        hass.http.register_view(view)

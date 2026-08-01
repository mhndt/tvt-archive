from __future__ import annotations

from typing import Any
from urllib.parse import urljoin

from aiohttp import ClientResponse, ClientSession, ClientTimeout


class TVTArchiveApiError(Exception):
    """Raised when the TVT Archive bridge rejects a request."""


class TVTArchiveApi:
    def __init__(self, session: ClientSession, base_url: str, token: str) -> None:
        self.session = session
        self.base_url = base_url.rstrip("/") + "/"
        self.token = token
        self.timeout = ClientTimeout(total=90)

    def _url(self, path: str) -> str:
        return urljoin(self.base_url, path.lstrip("/"))

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_data: dict[str, Any] | None = None,
    ) -> Any:
        async with self.session.request(
            method,
            self._url(path),
            headers=self.headers,
            params=params,
            json=json_data,
            timeout=self.timeout,
        ) as response:
            try:
                data = await response.json(content_type=None)
            except Exception:
                data = {"error": await response.text()}
            if response.status >= 400:
                raise TVTArchiveApiError(
                    str(data.get("error", f"Bridge returned HTTP {response.status}"))
                )
            return data

    async def health(self) -> dict[str, Any]:
        return await self.request("GET", "/api/health")

    async def cameras(self) -> dict[str, Any]:
        return await self.request("GET", "/api/cameras")

    async def add_camera(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self.request("POST", "/api/cameras", json_data=payload)

    async def update_camera(self, camera_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return await self.request("PUT", f"/api/cameras/{camera_id}", json_data=payload)

    async def delete_camera(self, camera_id: str) -> dict[str, Any]:
        return await self.request("DELETE", f"/api/cameras/{camera_id}")

    async def status(self, camera_id: str, refresh: bool = False) -> dict[str, Any]:
        return await self.request(
            "GET",
            f"/api/cameras/{camera_id}/status",
            params={"refresh": "1" if refresh else "0"},
        )

    async def timeline(self, camera_id: str, date: str, refresh: bool = False) -> dict[str, Any]:
        return await self.request(
            "GET",
            f"/api/cameras/{camera_id}/timeline",
            params={"date": date, "refresh": "1" if refresh else "0"},
        )

    async def availability(self, camera_id: str, days: int = 45) -> dict[str, Any]:
        return await self.request(
            "GET", f"/api/cameras/{camera_id}/availability", params={"days": str(days)}
        )

    async def create_session(self, camera_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return await self.request(
            "POST", f"/api/cameras/{camera_id}/sessions", json_data=payload
        )

    async def playback_session(self, session_id: str) -> dict[str, Any]:
        return await self.request("GET", f"/api/sessions/{session_id}")

    async def stop_session(self, session_id: str) -> dict[str, Any]:
        return await self.request("DELETE", f"/api/sessions/{session_id}")

    async def create_job(self, camera_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return await self.request("POST", f"/api/cameras/{camera_id}/jobs", json_data=payload)

    async def job(self, job_id: str) -> dict[str, Any]:
        return await self.request("GET", f"/api/jobs/{job_id}")

    async def open_player_script(self) -> ClientResponse:
        response = await self.session.get(
            self._url("/api/player/hls.js"),
            headers=self.headers,
            timeout=None,
        )
        if response.status >= 400:
            text = await response.text()
            response.release()
            raise TVTArchiveApiError(text or f"Bridge returned HTTP {response.status}")
        return response

    async def open_hls_asset(
        self, session_id: str, asset: str, *, range_header: str | None = None
    ) -> ClientResponse:
        headers = self.headers.copy()
        if range_header:
            headers["Range"] = range_header
        response = await self.session.get(
            self._url(f"/api/sessions/{session_id}/{asset}"),
            headers=headers,
            timeout=None,
        )
        if response.status >= 400:
            text = await response.text()
            response.release()
            raise TVTArchiveApiError(text or f"Bridge returned HTTP {response.status}")
        return response

    async def open_stream(
        self,
        camera_id: str,
        *,
        start: str,
        duration: int,
        quality: str,
        gain_db: int = 0,
    ) -> ClientResponse:
        """Legacy progressive-MP4 endpoint retained for diagnostics."""
        response = await self.session.get(
            self._url(f"/api/cameras/{camera_id}/stream"),
            headers=self.headers,
            params={
                "start": start,
                "duration": str(duration),
                "quality": quality,
                "gain_db": str(gain_db),
            },
            timeout=None,
        )
        if response.status >= 400:
            text = await response.text()
            response.release()
            raise TVTArchiveApiError(text or f"Bridge returned HTTP {response.status}")
        return response

    async def open_file(
        self,
        job_id: str,
        *,
        range_header: str | None = None,
        download: bool = False,
    ) -> ClientResponse:
        headers = self.headers.copy()
        if range_header:
            headers["Range"] = range_header
        response = await self.session.get(
            self._url(f"/api/jobs/{job_id}/file"),
            headers=headers,
            params={"download": "1" if download else "0"},
            timeout=None,
        )
        if response.status >= 400:
            text = await response.text()
            response.release()
            raise TVTArchiveApiError(text or f"Bridge returned HTTP {response.status}")
        return response

from __future__ import annotations

from typing import Any
from urllib.parse import urljoin

from aiohttp import ClientError, ClientResponse, ClientSession, ClientTimeout

STREAM_TIMEOUT = ClientTimeout(total=None, sock_connect=15, sock_read=None)


class TVTArchiveApiError(Exception):
    """Raised when the TVT Archive bridge rejects a request."""


class TVTArchiveAuthError(TVTArchiveApiError):
    """Raised when the bridge rejects the access token."""


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
        try:
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
                except ValueError:
                    data = await response.text()
        except (ClientError, TimeoutError) as error:
            raise TVTArchiveApiError(f"Bridge is unreachable: {error}") from error
        if not isinstance(data, dict):
            if response.status < 400:
                raise TVTArchiveApiError("Bridge returned an unexpected response")
            data = {"error": str(data or "")}
        if response.status >= 400:
            message = str(data.get("error") or f"Bridge returned HTTP {response.status}")
            if response.status == 401:
                raise TVTArchiveAuthError(message)
            raise TVTArchiveApiError(message)
        return data

    async def _open(
        self,
        path: str,
        *,
        range_header: str | None = None,
        params: dict[str, str] | None = None,
        timeout: ClientTimeout = STREAM_TIMEOUT,
    ) -> ClientResponse:
        """Start a streamed response; the caller releases it."""
        headers = self.headers.copy()
        if range_header:
            headers["Range"] = range_header
        try:
            response = await self.session.get(
                self._url(path), headers=headers, params=params, timeout=timeout
            )
        except (ClientError, TimeoutError) as error:
            raise TVTArchiveApiError(f"Bridge is unreachable: {error}") from error
        if response.status >= 400:
            text = await response.text()
            response.release()
            raise TVTArchiveApiError(text or f"Bridge returned HTTP {response.status}")
        return response

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

    async def thumbnail(self, camera_id: str, at: str) -> bytes:
        response = await self._open(
            f"/api/cameras/{camera_id}/thumbnail/{at}", timeout=self.timeout
        )
        try:
            return await response.read()
        finally:
            response.release()

    async def create_session(self, camera_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return await self.request("POST", f"/api/cameras/{camera_id}/sessions", json_data=payload)

    async def playback_session(self, session_id: str) -> dict[str, Any]:
        return await self.request("GET", f"/api/sessions/{session_id}")

    async def stop_session(self, session_id: str) -> dict[str, Any]:
        return await self.request("DELETE", f"/api/sessions/{session_id}")

    async def create_stream(self, camera_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return await self.request("POST", f"/api/cameras/{camera_id}/streams", json_data=payload)

    async def stream(self, session_id: str) -> dict[str, Any]:
        return await self.request("GET", f"/api/streams/{session_id}")

    async def control_stream(self, session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return await self.request("POST", f"/api/streams/{session_id}", json_data=payload)

    async def stop_stream(self, session_id: str) -> dict[str, Any]:
        return await self.request("DELETE", f"/api/streams/{session_id}")

    async def open_frames(self, session_id: str) -> ClientResponse:
        return await self._open(f"/api/streams/{session_id}/frames")

    async def create_job(self, camera_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return await self.request("POST", f"/api/cameras/{camera_id}/jobs", json_data=payload)

    async def job(self, job_id: str) -> dict[str, Any]:
        return await self.request("GET", f"/api/jobs/{job_id}")

    async def cancel_job(self, job_id: str) -> dict[str, Any]:
        return await self.request("DELETE", f"/api/jobs/{job_id}")

    async def open_player_script(self) -> ClientResponse:
        return await self._open("/api/player/hls.js")

    async def open_hls_asset(
        self, session_id: str, asset: str, *, range_header: str | None = None
    ) -> ClientResponse:
        return await self._open(f"/api/sessions/{session_id}/{asset}", range_header=range_header)

    async def open_file(
        self, job_id: str, *, range_header: str | None = None, download: bool = False
    ) -> ClientResponse:
        return await self._open(
            f"/api/jobs/{job_id}/file",
            range_header=range_header,
            params={"download": "1" if download else "0"},
        )

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import shutil
import socket
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from homeassistant.const import (
    CONF_HOST,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_USERNAME,
    EVENT_HOMEASSISTANT_STOP,
)
from homeassistant.core import Event, HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import TVTArchiveApi
from .const import (
    CONF_MODE,
    CONF_RECORDING_AUDIO,
    CONF_TOKEN,
    CONF_URL,
    DEFAULT_PORT,
    DOMAIN,
    MODE_BUILTIN,
)

_LOGGER = logging.getLogger(__name__)
_BRIDGE = Path(__file__).with_name("bridge") / "bridge.py"
_HLS_JS = Path(__file__).with_name("frontend") / "hls.min.js"
_DATA = "local_bridge"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class LocalBridge:
    """Bridge child process bound to localhost, restarted if it exits."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.port = 0
        self.token = secrets.token_urlsafe(32)
        self.api: TVTArchiveApi | None = None
        self._process: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()
        self._stopping = False
        self._unsub_stop = None

    @property
    def config_dir(self) -> Path:
        return Path(self.hass.config.path(".storage", DOMAIN))

    async def async_start(self) -> TVTArchiveApi:
        async with self._lock:
            if self._process and self._process.returncode is None and self.api:
                return self.api
            self._stopping = False
            if self._unsub_stop is None:
                self._unsub_stop = self.hass.bus.async_listen_once(
                    EVENT_HOMEASSISTANT_STOP, self._async_on_stop
                )
            self.port = self.port or _free_port()
            scratch = Path(await self.hass.async_add_executor_job(tempfile.gettempdir)) / DOMAIN
            ffmpeg = "ffmpeg"
            if "ffmpeg" in self.hass.config.components:
                from homeassistant.components.ffmpeg import get_ffmpeg_manager

                ffmpeg = get_ffmpeg_manager(self.hass).binary
            env = {k: v for k, v in os.environ.items() if k != "SUPERVISOR_TOKEN"}
            env.update(
                TVT_ARCHIVE_EMBEDDED="1",
                TVT_ARCHIVE_BIND="127.0.0.1",
                TVT_ARCHIVE_PORT=str(self.port),
                TVT_ARCHIVE_TOKEN=self.token,
                TVT_ARCHIVE_CONFIG_DIRECTORY=str(self.config_dir),
                TVT_ARCHIVE_HLS_JS=str(_HLS_JS),
                STATE_DIRECTORY=str(scratch / "state"),
                CACHE_DIRECTORY=str(scratch / "cache"),
                PYTHONUNBUFFERED="1",
            )
            if os.path.dirname(ffmpeg):
                env["PATH"] = os.path.dirname(ffmpeg) + os.pathsep + env.get("PATH", "")
            elif not await self.hass.async_add_executor_job(shutil.which, ffmpeg):
                raise RuntimeError("ffmpeg was not found")
            self._process = process = await asyncio.create_subprocess_exec(
                sys.executable,
                str(_BRIDGE),
                env=env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            self.hass.async_create_background_task(self._async_watch(process), f"{DOMAIN} bridge")
            self.api = TVTArchiveApi(
                async_get_clientsession(self.hass), f"http://127.0.0.1:{self.port}", self.token
            )
            for _ in range(150):
                if process.returncode is not None:
                    raise RuntimeError(f"bridge exited with code {process.returncode}")
                try:
                    await self.api.health()
                except Exception:  # noqa: BLE001
                    await asyncio.sleep(0.2)
                else:
                    return self.api
            raise TimeoutError("bridge did not start")

    async def _async_watch(self, process: asyncio.subprocess.Process) -> None:
        assert process.stderr is not None
        async for raw in process.stderr:
            line = raw.decode(errors="replace").rstrip()
            if " ERROR " in line or " WARNING " in line:
                _LOGGER.warning("%s", line.split(" ", 3)[-1])
            else:
                _LOGGER.debug("%s", line)
        code = await process.wait()
        if self._stopping or process is not self._process:
            return
        _LOGGER.warning("Built-in bridge exited with code %s; restarting", code)
        await asyncio.sleep(5)
        try:
            await self.async_start()
        except Exception as error:  # noqa: BLE001
            _LOGGER.error("Built-in bridge could not restart: %s", error)

    async def _async_on_stop(self, _: Event) -> None:
        self._unsub_stop = None
        await self.async_stop()

    async def async_stop(self) -> None:
        self._stopping = True
        process, self._process = self._process, None
        if process is None or process.returncode is not None:
            return
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), 5)
        except TimeoutError:
            process.kill()
            await process.wait()


def async_local_bridge(hass: HomeAssistant) -> LocalBridge:
    data = hass.data.setdefault(DOMAIN, {})
    if _DATA not in data:
        data[_DATA] = LocalBridge(hass)
    return data[_DATA]


def is_builtin(data: Mapping[str, Any]) -> bool:
    return data.get(CONF_MODE) == MODE_BUILTIN


def bridge_key(data: Mapping[str, Any]) -> str:
    return MODE_BUILTIN if is_builtin(data) else str(data.get(CONF_URL, "")).lower()


def bridge_data(data: Mapping[str, Any]) -> dict[str, Any]:
    if is_builtin(data):
        return {CONF_MODE: MODE_BUILTIN}
    return {CONF_URL: data[CONF_URL], CONF_TOKEN: data[CONF_TOKEN]}


def camera_login(camera: Mapping[str, Any], password: str = "") -> dict[str, Any]:
    """Camera login kept in the entry, so the bridge can be repopulated."""
    login = {
        CONF_HOST: camera[CONF_HOST],
        CONF_PORT: int(camera.get(CONF_PORT, DEFAULT_PORT)),
        CONF_USERNAME: camera.get(CONF_USERNAME, "admin"),
        CONF_RECORDING_AUDIO: camera.get(CONF_RECORDING_AUDIO, "auto"),
    }
    return {**login, CONF_PASSWORD: password} if password else login


def camera_uid(host: str, port: int) -> str:
    return f"{str(host).strip().lower()}:{int(port)}"


async def async_bridge_api(hass: HomeAssistant, data: Mapping[str, Any]) -> TVTArchiveApi:
    if is_builtin(data):
        return await async_local_bridge(hass).async_start()
    return TVTArchiveApi(async_get_clientsession(hass), data[CONF_URL], data[CONF_TOKEN])

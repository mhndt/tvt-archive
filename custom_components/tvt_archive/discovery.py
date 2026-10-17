from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Iterable

from homeassistant.config_entries import (
    SIGNAL_CONFIG_ENTRY_CHANGED,
    SOURCE_INTEGRATION_DISCOVERY,
    ConfigEntry,
    ConfigEntryChange,
)
from homeassistant.const import CONF_HOST, CONF_NAME, CONF_PORT, CONF_USERNAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import discovery_flow
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.start import async_at_started

from .const import CONF_CAMERA_ID, DEFAULT_PORT, DOMAIN
from .local import camera_uid

GREETING = b"head"


async def async_greets(host: str, port: int = DEFAULT_PORT, timeout: float = 3) -> bool:
    """TVT devices greet on connect, before any login."""
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
    except (OSError, TimeoutError):
        return False
    try:
        return await asyncio.wait_for(reader.readexactly(len(GREETING)), timeout) == GREETING
    except (OSError, TimeoutError, asyncio.IncompleteReadError):
        return False
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()


async def _async_scan(hass: HomeAssistant, entries: Iterable[ConfigEntry]) -> None:
    current = hass.config_entries.async_entries(DOMAIN)
    if any(CONF_CAMERA_ID not in e.data for e in current if e.source != "ignore"):
        return  # Pre-1.1 entries own their cameras until they are split.
    known = {e.unique_id for e in current}
    for entry in entries:
        host = entry.data.get(CONF_HOST)
        if not host or camera_uid(host, DEFAULT_PORT) in known:
            continue
        if await async_greets(host):
            discovery_flow.async_create_flow(
                hass,
                DOMAIN,
                context={"source": SOURCE_INTEGRATION_DISCOVERY},
                data={
                    CONF_NAME: entry.title,
                    CONF_HOST: host,
                    CONF_PORT: DEFAULT_PORT,
                    CONF_USERNAME: entry.data.get(CONF_USERNAME) or "admin",
                },
            )


@callback
def async_setup_discovery(hass: HomeAssistant) -> None:
    def scan(entries: Iterable[ConfigEntry]) -> None:
        hass.async_create_background_task(_async_scan(hass, list(entries)), f"{DOMAIN} discovery")

    @callback
    def started(_: HomeAssistant) -> None:
        scan(hass.config_entries.async_entries("onvif"))

    @callback
    def changed(change: ConfigEntryChange, entry: ConfigEntry) -> None:
        if change is ConfigEntryChange.ADDED and entry.domain == "onvif":
            scan([entry])

    async_at_started(hass, started)
    async_dispatcher_connect(hass, SIGNAL_CONFIG_ENTRY_CHANGED, changed)

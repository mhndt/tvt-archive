from __future__ import annotations

import asyncio
from unittest.mock import patch

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.tvt_archive.discovery import _async_scan, async_greets


async def _server(greeting: bytes) -> tuple[asyncio.Server, int]:
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.write(greeting)
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


async def test_greeting_check(socket_enabled: None) -> None:
    tvt, tvt_port = await _server(b"head" + bytes(60))
    other, other_port = await _server(b"HTTP/1.1 400\r\n")
    async with tvt, other:
        assert await async_greets("127.0.0.1", tvt_port)
        assert not await async_greets("127.0.0.1", other_port)
    assert not await async_greets("127.0.0.1", tvt_port, timeout=1)


async def test_scan_offers_tvt_cameras_from_onvif(hass: HomeAssistant) -> None:
    onvif = MockConfigEntry(
        domain="onvif", title="Front Door", data={"host": "192.0.2.10", "username": "viewer"}
    )
    onvif.add_to_hass(hass)
    with (
        patch("custom_components.tvt_archive.discovery.async_greets", return_value=True),
        patch("custom_components.tvt_archive.discovery.discovery_flow.async_create_flow") as create,
    ):
        await _async_scan(hass, [onvif])
    create.assert_called_once()
    assert create.call_args.kwargs["data"] == {
        "name": "Front Door",
        "host": "192.0.2.10",
        "port": 9008,
        "username": "viewer",
    }

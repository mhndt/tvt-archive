from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_TOKEN, DOMAIN

TO_REDACT = {CONF_TOKEN, "host", "username", "password"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    data = hass.data[DOMAIN][entry.entry_id]
    try:
        health = await data["api"].health()
    except Exception as error:
        health = {"error": str(error)}
    return {
        "entry": async_redact_data(dict(entry.data), TO_REDACT),
        "health": health,
        "coordinator": async_redact_data(data["coordinator"].data, TO_REDACT),
    }

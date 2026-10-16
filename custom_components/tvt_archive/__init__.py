from __future__ import annotations

import shutil
from hashlib import sha256
from pathlib import Path

from homeassistant.components.http import StaticPathConfig
from homeassistant.components.panel_custom import async_register_panel
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_URL, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType

from .api import TVTArchiveApi
from .const import (
    CONF_MODE,
    CONF_TOKEN,
    DATA_FRONTEND_REGISTERED,
    DATA_VIEWS_REGISTERED,
    DOMAIN,
    MODE_BUILTIN,
    PANEL_LOGO_PATH,
    PANEL_MODULE_PATH,
    PANEL_URL_PATH,
)
from .coordinator import TVTArchiveCoordinator
from .http import register_views
from .local import async_local_bridge
from .services import async_register_services

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    async_register_services(hass)
    return True


async def _async_register_frontend(hass: HomeAssistant) -> None:
    domain_data = hass.data.setdefault(DOMAIN, {})
    if domain_data.get(DATA_FRONTEND_REGISTERED):
        return
    base = Path(__file__).parent
    panel_path = base / "frontend" / "tvt-archive-panel.js"
    panel_bytes = await hass.async_add_executor_job(panel_path.read_bytes)
    panel_digest = sha256(panel_bytes).hexdigest()[:12]
    panel_module_url = f"{PANEL_MODULE_PATH}?v={panel_digest}"
    await hass.http.async_register_static_paths(
        [
            StaticPathConfig(
                PANEL_MODULE_PATH,
                str(panel_path),
                cache_headers=False,
            ),
            StaticPathConfig(
                PANEL_LOGO_PATH,
                str(base / "brand" / "icon.png"),
                cache_headers=True,
            ),
        ]
    )
    await async_register_panel(
        hass,
        frontend_url_path=PANEL_URL_PATH,
        webcomponent_name="tvt-archive-panel",
        sidebar_title="Recordings",
        sidebar_icon="mdi:cctv",
        module_url=panel_module_url,
        config={},
        require_admin=False,
    )
    domain_data[DATA_FRONTEND_REGISTERED] = True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    if entry.data.get(CONF_MODE) == MODE_BUILTIN:
        bridge = async_local_bridge(hass)
        try:
            api = await bridge.async_start()
        except Exception as error:
            if "ffmpeg" in str(error):
                ir.async_create_issue(
                    hass,
                    DOMAIN,
                    "ffmpeg_missing",
                    is_fixable=False,
                    severity=ir.IssueSeverity.ERROR,
                    translation_key="ffmpeg_missing",
                )
            raise ConfigEntryNotReady(f"Built-in bridge failed to start: {error}") from error
        ir.async_delete_issue(hass, DOMAIN, "ffmpeg_missing")
        key = bridge.token
    else:
        key = entry.data[CONF_TOKEN]
        api = TVTArchiveApi(async_get_clientsession(hass), entry.data[CONF_URL], key)
    coordinator = TVTArchiveCoordinator(hass, api)
    await coordinator.async_config_entry_first_refresh()
    domain_data = hass.data.setdefault(DOMAIN, {})
    domain_data[entry.entry_id] = {
        "api": api,
        "coordinator": coordinator,
        "media_key": f"{entry.entry_id}:{key}".encode(),
    }

    registry = er.async_get(hass)
    device_registry = dr.async_get(hass)
    cameras = list(coordinator.data.get("cameras", []))
    current_camera_ids = {str(camera["id"]) for camera in cameras}

    for entity_entry in er.async_entries_for_config_entry(registry, entry.entry_id):
        parts = str(entity_entry.unique_id).split(":", 2)
        if len(parts) == 3 and parts[0] == entry.entry_id and parts[1] not in current_camera_ids:
            registry.async_remove(entity_entry.entity_id)

    for device in dr.async_entries_for_config_entry(device_registry, entry.entry_id):
        matching_ids = {
            identifier.split(":", 1)[1]
            for domain, identifier in device.identifiers
            if domain == DOMAIN and identifier.startswith(f"{entry.entry_id}:")
        }
        if matching_ids and matching_ids.isdisjoint(current_camera_ids):
            device_registry.async_remove_device(device.id)

    if not domain_data.get(DATA_VIEWS_REGISTERED):
        register_views(hass)
        domain_data[DATA_VIEWS_REGISTERED] = True
    await _async_register_frontend(hass)

    await hass.config_entries.async_forward_entry_setups(
        entry, [Platform.SENSOR, Platform.BINARY_SENSOR]
    )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(
        entry, [Platform.SENSOR, Platform.BINARY_SENSOR]
    )
    if not unloaded:
        return False
    hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    # Frontend stays registered across reloads.
    if entry.data.get(CONF_MODE) == MODE_BUILTIN:
        await async_local_bridge(hass).async_stop()
    return True


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    if entry.data.get(CONF_MODE) == MODE_BUILTIN:
        bridge = async_local_bridge(hass)
        await bridge.async_stop()
        await hass.async_add_executor_job(shutil.rmtree, bridge.config_dir, True)

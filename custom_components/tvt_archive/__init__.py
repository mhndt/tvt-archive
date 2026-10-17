from __future__ import annotations

import json
import logging
import shutil
from hashlib import sha256
from pathlib import Path

from homeassistant.components.http import StaticPathConfig
from homeassistant.components.panel_custom import async_register_panel
from homeassistant.config_entries import SOURCE_IMPORT, ConfigEntry, ConfigEntryState
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryError,
    ConfigEntryNotReady,
)
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.typing import ConfigType

from .api import TVTArchiveApi, TVTArchiveApiError, TVTArchiveAuthError
from .const import (
    CONF_CAMERA_ID,
    CONF_MIGRATED_FROM,
    CONF_MOTION,
    DATA_FRONTEND_REGISTERED,
    DATA_VIEWS_REGISTERED,
    DEFAULT_PORT,
    DOMAIN,
    PANEL_LOGO_PATH,
    PANEL_MODULE_PATH,
    PANEL_URL_PATH,
)
from .coordinator import TVTArchiveCoordinator
from .discovery import async_setup_discovery
from .http import register_views
from .local import (
    async_bridge_api,
    async_local_bridge,
    bridge_data,
    bridge_key,
    camera_login,
    camera_uid,
    is_builtin,
)
from .services import async_register_services

_LOGGER = logging.getLogger(__name__)
PLATFORMS = [Platform.SENSOR, Platform.BINARY_SENSOR]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    async_register_services(hass)
    async_setup_discovery(hass)
    return True


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    if entry.version > 3:
        return False
    # Version 2 entries held a whole bridge; setup splits them per camera.
    hass.config_entries.async_update_entry(entry, version=3)
    return True


async def _async_register_frontend(hass: HomeAssistant) -> None:
    domain_data = hass.data.setdefault(DOMAIN, {})
    if domain_data.get(DATA_FRONTEND_REGISTERED):
        return
    # Claimed before awaiting; camera entries set up concurrently.
    domain_data[DATA_FRONTEND_REGISTERED] = True
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


async def _async_api(hass: HomeAssistant, entry: ConfigEntry) -> tuple[TVTArchiveApi, str]:
    if not is_builtin(entry.data):
        return await async_bridge_api(hass, entry.data), entry.data["token"]
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
    return api, bridge.token


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


async def _async_split(hass: HomeAssistant, entry: ConfigEntry, api: TVTArchiveApi) -> bool:
    """Turn a pre-1.1 bridge entry into one entry per camera."""
    try:
        cameras = (await api.cameras()).get("cameras", [])
    except TVTArchiveAuthError as error:
        raise ConfigEntryAuthFailed("The bridge rejected the access token") from error
    except TVTArchiveApiError as error:
        raise ConfigEntryNotReady(str(error)) from error
    if not cameras:
        raise ConfigEntryError("The bridge has no cameras; remove this entry and add a camera")
    passwords: dict[str, str] = {}
    if is_builtin(entry.data):
        path = async_local_bridge(hass).config_dir / "config.json"
        saved = await hass.async_add_executor_job(_read_json, path)
        passwords = {str(c.get("id")): str(c.get("password", "")) for c in saved.get("cameras", [])}
    motion = entry.options.get(CONF_MOTION, {})
    taken = {e.unique_id for e in hass.config_entries.async_entries(DOMAIN)}
    uids: list[str | None] = []
    for camera in cameras:
        uid = camera_uid(camera["host"], camera.get("port", DEFAULT_PORT))
        uids.append(None if uid in taken else uid)
        taken.add(uid)
    for camera in cameras:
        camera_id = str(camera["id"])
        ir.async_delete_issue(hass, DOMAIN, f"camera_login_{camera_id}")
        ir.async_delete_issue(hass, DOMAIN, f"sd_card_{camera_id}")
    first, *rest = cameras
    options = {CONF_MOTION: motion[str(first["id"])]} if str(first["id"]) in motion else {}
    hass.config_entries.async_update_entry(
        entry,
        title=str(first.get("name", first["id"])),
        unique_id=uids[0] or entry.unique_id,
        data={
            **bridge_data(entry.data),
            CONF_CAMERA_ID: str(first["id"]),
            **camera_login(first, passwords.get(str(first["id"]), "")),
        },
        options=options,
    )
    for camera, uid in zip(rest, uids[1:], strict=True):
        camera_id = str(camera["id"])
        hass.async_create_task(
            hass.config_entries.flow.async_init(
                DOMAIN,
                context={"source": SOURCE_IMPORT},
                data={
                    **bridge_data(entry.data),
                    CONF_CAMERA_ID: camera_id,
                    **camera_login(camera, passwords.get(camera_id, "")),
                    CONF_MIGRATED_FROM: entry.entry_id,
                    "title": str(camera.get("name", camera_id)),
                    "unique_id": uid,
                    CONF_MOTION: motion.get(camera_id),
                },
            )
        )
    return True


def _adopt(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Move a split camera's entities and device over from the old bridge entry."""
    old, camera_id = entry.data[CONF_MIGRATED_FROM], entry.data[CONF_CAMERA_ID]
    prefix = f"{old}:{camera_id}:"
    entities = er.async_get(hass)
    for item in er.async_entries_for_config_entry(entities, old):
        if item.unique_id.startswith(prefix):
            entities.async_update_entity(
                item.entity_id,
                config_entry_id=entry.entry_id,
                new_unique_id=f"{entry.entry_id}:{camera_id}:{item.unique_id[len(prefix) :]}",
            )
    devices = dr.async_get(hass)
    device = devices.async_get_device(identifiers={(DOMAIN, f"{old}:{camera_id}")})
    if device:
        devices.async_update_device(
            device.id,
            add_config_entry_id=entry.entry_id,
            new_identifiers={(DOMAIN, f"{entry.entry_id}:{camera_id}")},
        )
        devices.async_update_device(device.id, remove_config_entry_id=old)
    data = dict(entry.data)
    data.pop(CONF_MIGRATED_FROM)
    hass.config_entries.async_update_entry(entry, data=data)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    api, key = await _async_api(hass, entry)
    if CONF_CAMERA_ID not in entry.data and not await _async_split(hass, entry, api):
        return False
    if CONF_MIGRATED_FROM in entry.data:
        _adopt(hass, entry)
    coordinator = TVTArchiveCoordinator(hass, entry, api)
    await coordinator.async_config_entry_first_refresh()
    domain_data = hass.data.setdefault(DOMAIN, {})
    domain_data[entry.entry_id] = {
        "api": api,
        "coordinator": coordinator,
        "camera_id": coordinator.camera_id,
        "builtin": is_builtin(entry.data),
        "media_key": f"{entry.entry_id}:{key}".encode(),
    }
    if not domain_data.get(DATA_VIEWS_REGISTERED):
        register_views(hass)
        domain_data[DATA_VIEWS_REGISTERED] = True
    await _async_register_frontend(hass)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


def _builtin_in_use(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    return any(
        e.entry_id != entry.entry_id
        and is_builtin(e.data)
        and e.state in (ConfigEntryState.LOADED, ConfigEntryState.SETUP_IN_PROGRESS)
        for e in hass.config_entries.async_entries(DOMAIN)
    )


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    # Frontend stays registered across reloads.
    if is_builtin(entry.data) and not _builtin_in_use(hass, entry):
        await async_local_bridge(hass).async_stop()
    return True


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    camera_id = entry.data.get(CONF_CAMERA_ID)
    shared = any(
        e.entry_id != entry.entry_id and bridge_key(e.data) == bridge_key(entry.data)
        for e in hass.config_entries.async_entries(DOMAIN)
    )
    if is_builtin(entry.data) and not shared:
        bridge = async_local_bridge(hass)
        await bridge.async_stop()
        await hass.async_add_executor_job(shutil.rmtree, bridge.config_dir, True)
        return
    if not camera_id:
        return
    try:
        api = await async_bridge_api(hass, entry.data)
        await api.delete_camera(str(camera_id))
    except Exception as error:  # noqa: BLE001
        _LOGGER.warning("Could not remove %s from the bridge: %s", entry.title, error)
    if is_builtin(entry.data) and not _builtin_in_use(hass, entry):
        await async_local_bridge(hass).async_stop()

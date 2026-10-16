from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN

_DEVICE_CLASSES = {"motion", "occupancy", "presence"}


def auto_motion_entities(hass: HomeAssistant, host: str | None) -> list[str]:
    """Motion sensors from other integrations configured for the same camera address."""
    if not host:
        return []
    registry = er.async_get(hass)
    found: list[str] = []
    for entry in hass.config_entries.async_entries():
        if entry.domain == DOMAIN or str(entry.data.get("host", "")).lower() != host.lower():
            continue
        for item in er.async_entries_for_config_entry(registry, entry.entry_id):
            device_class = item.device_class or item.original_device_class
            if item.domain == "binary_sensor" and device_class in _DEVICE_CLASSES:
                if not item.disabled_by:
                    found.append(item.entity_id)
    return found


def _ranges(states: list[Any], start: datetime, stop: datetime) -> list[tuple[datetime, datetime]]:
    ranges: list[tuple[datetime, datetime]] = []
    since: datetime | None = None
    for state in states:
        changed = max(state.last_changed, start)
        if state.state == "on" and since is None:
            since = changed
        elif state.state != "on" and since is not None:
            ranges.append((since, changed))
            since = None
    if since is not None:
        ranges.append((since, stop))
    return ranges


async def async_motion_events(
    hass: HomeAssistant, entity_ids: list[str], day: date
) -> list[dict[str, str]]:
    if not entity_ids or "recorder" not in hass.config.components:
        return []
    from homeassistant.components.recorder import get_instance, history

    start = datetime.combine(day, time.min).astimezone()
    stop = min(start + timedelta(days=1), datetime.now().astimezone())
    if stop <= start:
        return []
    states = await get_instance(hass).async_add_executor_job(
        lambda: history.get_significant_states(
            hass,
            start,
            stop,
            entity_ids,
            include_start_time_state=True,
            significant_changes_only=False,
            no_attributes=True,
        )
    )
    spans = sorted(span for items in states.values() for span in _ranges(items, start, stop))
    merged: list[list[datetime]] = []
    for begin, end in spans:
        if merged and begin <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        elif end > begin:
            merged.append([begin, end])
    return [{"start": _local(b), "stop": _local(e), "type": "motion"} for b, e in merged]


def _local(value: datetime) -> str:
    return value.astimezone().replace(tzinfo=None).isoformat(timespec="seconds")

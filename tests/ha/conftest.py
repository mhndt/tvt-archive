from __future__ import annotations

from collections.abc import Generator
from unittest.mock import AsyncMock, patch

import pytest
from common import CAMERA


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    return


@pytest.fixture
def api() -> Generator[AsyncMock]:
    client = AsyncMock()
    client.health.return_value = {"ok": True, "version": "0.9.5"}
    client.cameras.return_value = {"cameras": []}
    client.add_camera.return_value = {"camera": CAMERA, "test": {"online": True}}
    client.update_camera.return_value = {"camera": CAMERA, "test": {"online": True}}
    client.delete_camera.return_value = {"removed": CAMERA}
    client.status.return_value = {
        "camera": CAMERA,
        "online": True,
        "timeline_today": {"recorded_hours": 2.5, "recording_now": True},
        "availability": {
            "available_history_hours": 76.9,
            "earliest": "2026-08-20T13:59:06",
            "latest": "2026-08-23T18:53:00",
        },
    }
    with (
        patch("custom_components.tvt_archive.config_flow.TVTArchiveApi", return_value=client),
        patch("custom_components.tvt_archive.TVTArchiveApi", return_value=client),
    ):
        yield client

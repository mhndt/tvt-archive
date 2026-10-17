from pytest_homeassistant_custom_component.common import MockConfigEntry

URL = "http://bridge.local:8099"
TOKEN = "test-token"
CAMERA = {"id": "front", "name": "Front", "host": "192.0.2.10", "port": 9008, "username": "admin"}
BACK = {"id": "back", "name": "Back", "host": "192.0.2.11", "port": 9008, "username": "admin"}


def camera_entry(camera: dict = CAMERA, **data) -> MockConfigEntry:
    return MockConfigEntry(
        domain="tvt_archive",
        version=3,
        title=camera["name"],
        unique_id=f"{camera['host']}:{camera['port']}",
        data={"url": URL, "token": TOKEN, "camera_id": camera["id"], **data},
    )

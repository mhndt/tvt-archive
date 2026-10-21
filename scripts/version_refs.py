"""Exact application version lines shared by release validation and version bumps."""

VERSION_LINES = {
    "VERSION": ("@VERSION@",),
    "custom_components/tvt_archive/manifest.json": ('  "version": "@VERSION@"',),
    "addon/config.yaml": ('version: "@VERSION@"',),
    "Dockerfile": ("ARG APP_VERSION=@VERSION@",),
    ".env.example": ("TVT_ARCHIVE_IMAGE=ghcr.io/mhndt/tvt-archive:@VERSION@",),
    "compose/compose.yaml": (
        "    image: ${TVT_ARCHIVE_IMAGE:-ghcr.io/mhndt/tvt-archive:@VERSION@}",
    ),
    "compose/build-local.yaml": (
        "    image: ${TVT_ARCHIVE_LOCAL_IMAGE:-tvt-archive:@VERSION@-local}",
        '        APP_VERSION: "@VERSION@"',
    ),
    "setup.sh": (
        "TVT_ARCHIVE_IMAGE=${TVT_ARCHIVE_IMAGE:-ghcr.io/mhndt/tvt-archive:@VERSION@}",
        "TVT_ARCHIVE_LOCAL_IMAGE=${TVT_ARCHIVE_LOCAL_IMAGE:-tvt-archive:@VERSION@-local}",
    ),
    "custom_components/tvt_archive/bridge/bridge.py": (
        'APP_VERSION = "@VERSION@"',
        '    server_version = "TVTArchiveBridge/@VERSION@"',
    ),
}


def version_lines(version: str) -> dict[str, tuple[str, ...]]:
    return {
        path: tuple(line.replace("@VERSION@", version) for line in lines)
        for path, lines in VERSION_LINES.items()
    }

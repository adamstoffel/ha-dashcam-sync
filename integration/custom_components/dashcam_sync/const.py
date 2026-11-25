"""Constants for the Dashcam Sync integration."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

DOMAIN: Final = "dashcam_sync"
API_VERSION: Final = 1  # the local API this integration speaks
MIN_API_VERSION: Final = 1

CONF_HOST: Final = "host"
CONF_PORT: Final = "port"
CONF_TOKEN: Final = "token"
DEFAULT_PORT: Final = 8100

CONF_PRESENCE_ENTITY: Final = "presence_entity"
CONF_LIVE_CAMERA: Final = "live_camera"

POLL_INTERVAL: Final = timedelta(seconds=30)
WS_STALE_AFTER: Final = 60.0
WS_RECONNECT_MAX: Final = 60.0
REQUEST_TIMEOUT: Final = 15

EVENT_TYPES: Final = ["event_clip", "clip", "sync_completed", "sync_failed"]
SYNC_MODES: Final = ["events_only", "events_first", "everything"]
SYNC_STATUSES: Final = [
    "idle",
    "detecting",
    "preparing",
    "listing",
    "downloading",
    "restoring",
    "error",
]

MEDIA_PREFIX: Final = "media-source://dashcam_sync/"
FILE_URL: Final = "/api/dashcam_sync/clip/{clip_id}/file"
THUMB_URL: Final = "/api/dashcam_sync/clip/{clip_id}/thumb"

# Roots Core can read; clip paths from the app must resolve under one of them.
CLIP_ROOTS: Final = ("/media", "/share")

# Issue ids the integration raises itself (the app's issues come through its status).
ISSUE_API_MISMATCH: Final = "api_mismatch"
ISSUE_ANNOUNCED_ADAPTER: Final = "announced_wifi_adapter"
ISSUE_APP_UNREACHABLE: Final = "app_unreachable"

ATTR_CLIP_ID: Final = "clip_id"
ATTR_MEDIA_CONTENT_ID: Final = "media_content_id"
ATTR_DRY_RUN: Final = "dry_run"

SERVICE_SYNC: Final = "sync"
SERVICE_KEEP_CLIP: Final = "keep_clip"
SERVICE_UNKEEP_CLIP: Final = "unkeep_clip"
SERVICE_DELETE_CLIP: Final = "delete_clip"
SERVICE_PURGE: Final = "purge"

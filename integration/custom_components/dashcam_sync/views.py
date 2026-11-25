"""HTTP views that stream clip files and thumbnails to the media browser.

Core reads the same ``/media`` and ``/share`` folders as the app. Paths are
looked up through the app's catalogue (never taken from the URL) and must
resolve under an allowed root.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from aiohttp import web
from homeassistant.helpers.http import KEY_HASS, HomeAssistantView

from .api import DashcamSyncError
from .const import DOMAIN, FILE_URL, THUMB_URL
from .paths import mime_for, resolve_clip_path

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .api import DashcamSyncApi


def get_api(hass: HomeAssistant) -> DashcamSyncApi | None:
    for entry in hass.config_entries.async_loaded_entries(DOMAIN):
        return entry.runtime_data.api  # type: ignore[no-any-return]
    return None


class _ClipView(HomeAssistantView):
    requires_auth = True  # signed paths from the media browser are accepted
    thumbnail = False

    async def get(self, request: web.Request, clip_id: str) -> web.StreamResponse:
        hass = request.app[KEY_HASS]
        api = get_api(hass)
        if api is None or not clip_id.isdigit():
            raise web.HTTPNotFound
        try:
            clip = await api.async_clip(int(clip_id))
        except DashcamSyncError as err:
            raise web.HTTPNotFound from err
        raw = clip.get("thumbnail") if self.thumbnail else clip.get("path")
        path = await hass.async_add_executor_job(resolve_clip_path, raw)
        if path is None:
            raise web.HTTPNotFound
        return web.FileResponse(path, headers={"Content-Type": mime_for(str(path))})


class ClipFileView(_ClipView):
    url = FILE_URL
    name = "api:dashcam_sync:clip_file"


class ClipThumbView(_ClipView):
    url = THUMB_URL
    name = "api:dashcam_sync:clip_thumb"
    thumbnail = True

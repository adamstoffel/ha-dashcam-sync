"""Media source: Dashcam Sync, camera, day, Event or All."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from homeassistant.components.http.auth import async_sign_path
from homeassistant.components.media_player import BrowseError, MediaClass, MediaType
from homeassistant.components.media_source import (
    BrowseMediaSource,
    MediaSource,
    MediaSourceItem,
    PlayMedia,
    Unresolvable,
)
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .api import DashcamSyncApi, DashcamSyncError
from .const import DOMAIN, FILE_URL, THUMB_URL
from .paths import mime_for
from .views import get_api

THUMB_EXPIRY = timedelta(hours=1)


async def async_get_media_source(hass: HomeAssistant) -> DashcamSyncMediaSource:
    return DashcamSyncMediaSource(hass)


class DashcamSyncMediaSource(MediaSource):
    """Browses the app's catalogue, so a NAS target browses as fast as local disk."""

    name = "Dashcam Sync"

    def __init__(self, hass: HomeAssistant) -> None:
        super().__init__(DOMAIN)
        self.hass = hass

    def _api(self) -> DashcamSyncApi:
        api = get_api(self.hass)
        if api is None:
            raise BrowseError("Dashcam Sync is not loaded")
        return api

    async def async_resolve_media(self, item: MediaSourceItem) -> PlayMedia:
        parts = (item.identifier or "").split("/")
        if len(parts) != 2 or parts[0] != "clip" or not parts[1].isdigit():
            raise Unresolvable(f"Unknown Dashcam Sync item: {item.identifier}")
        try:
            clip = await self._api().async_clip(int(parts[1]))
        except (DashcamSyncError, BrowseError) as err:
            raise Unresolvable(str(err)) from err
        if not clip.get("path"):
            raise Unresolvable("This clip has no file yet")
        return PlayMedia(FILE_URL.format(clip_id=clip["id"]), mime_for(clip["path"]))

    async def async_browse_media(self, item: MediaSourceItem) -> BrowseMediaSource:
        parts = [p for p in (item.identifier or "").split("/") if p]
        api = self._api()
        try:
            if not parts:
                return await self._root(api)
            if len(parts) == 1:
                return await self._days(api, parts[0])
            if len(parts) == 2:
                return self._filters(parts[0], parts[1])
            if len(parts) == 3 and parts[2] in ("event", "all"):
                return await self._clips(api, parts[0], parts[1], parts[2])
        except DashcamSyncError as err:
            raise BrowseError(str(err)) from err
        raise BrowseError(f"Unknown Dashcam Sync item: {item.identifier}")

    # ----------------------------------------------------------------- levels

    def _folder(
        self, identifier: str | None, title: str, children: list[BrowseMediaSource] | None = None
    ) -> BrowseMediaSource:
        return BrowseMediaSource(
            domain=DOMAIN,
            identifier=identifier,
            media_class=MediaClass.DIRECTORY,
            media_content_type=MediaType.VIDEO,
            title=title,
            can_play=False,
            can_expand=True,
            children=children,
            children_media_class=MediaClass.DIRECTORY,
        )

    async def _root(self, api: DashcamSyncApi) -> BrowseMediaSource:
        status = await api.async_status()
        children = [
            self._folder(camera_id, cam.get("name") or camera_id)
            for camera_id, cam in sorted(status.get("cameras", {}).items())
        ]
        return self._folder(None, "Dashcam Sync", children)

    async def _days(self, api: DashcamSyncApi, camera: str) -> BrowseMediaSource:
        days = await api.async_clip_days(camera)  # newest first
        return self._folder(camera, camera, [self._folder(f"{camera}/{day}", day) for day in days])

    def _filters(self, camera: str, day: str) -> BrowseMediaSource:
        return self._folder(
            f"{camera}/{day}",
            day,
            [
                self._folder(f"{camera}/{day}/event", "Event"),
                self._folder(f"{camera}/{day}/all", "All"),
            ],
        )

    async def _clips(
        self, api: DashcamSyncApi, camera: str, day: str, scope: str
    ) -> BrowseMediaSource:
        clips = await api.async_clips(
            camera=camera, day=day, kind="event" if scope == "event" else None, limit=500
        )
        children = [self._clip_item(clip) for clip in clips]  # newest first from the app
        title = "Event" if scope == "event" else "All"
        return BrowseMediaSource(
            domain=DOMAIN,
            identifier=f"{camera}/{day}/{scope}",
            media_class=MediaClass.DIRECTORY,
            media_content_type=MediaType.VIDEO,
            title=f"{day} · {title}",
            can_play=False,
            can_expand=True,
            children=children,
            children_media_class=MediaClass.VIDEO,
        )

    def _clip_item(self, clip: dict[str, Any]) -> BrowseMediaSource:
        start = dt_util.parse_datetime(clip["start"])
        stamp = dt_util.as_local(start).strftime("%H:%M:%S") if start else clip["start"]
        is_photo = clip["kind"] == "photo"
        thumbnail = None
        if clip.get("thumbnail"):
            thumbnail = async_sign_path(
                self.hass, THUMB_URL.format(clip_id=clip["id"]), THUMB_EXPIRY
            )
        return BrowseMediaSource(
            domain=DOMAIN,
            identifier=f"clip/{clip['id']}",
            media_class=MediaClass.IMAGE if is_photo else MediaClass.VIDEO,
            media_content_type=MediaType.IMAGE if is_photo else MediaType.VIDEO,
            title=f"{stamp} · {clip['kind']} · {clip['channel']}",
            can_play=True,
            can_expand=False,
            thumbnail=thumbnail,
        )

"""Client for the Dashcam Sync app's local API (``/api/v1``)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import aiohttp

from .const import REQUEST_TIMEOUT


class DashcamSyncError(Exception):
    """Base class for API errors."""


class DashcamSyncAuthError(DashcamSyncError):
    """The token was rejected."""


class DashcamSyncConnectionError(DashcamSyncError):
    """The app cannot be reached."""


class DashcamSyncRequestError(DashcamSyncError):
    """The app answered with an error."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"{status}: {message}")
        self.status = status
        self.message = message


class DashcamSyncApi:
    """Thin async wrapper; every call raises one of the errors above."""

    def __init__(self, session: aiohttp.ClientSession, host: str, port: int, token: str) -> None:
        self._session = session
        self.host = host
        self.port = port
        self._token = token
        self._base = f"http://{host}:{port}/api/v1"

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}

    async def _request(
        self, method: str, path: str, *, json: Any = None, params: dict[str, Any] | None = None
    ) -> Any:
        try:
            async with self._session.request(
                method,
                f"{self._base}/{path}",
                headers=self._headers,
                json=json,
                params=params,
                timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
            ) as resp:
                try:
                    body = await resp.json(content_type=None)
                except ValueError:
                    body = None
                if resp.status in (401, 403):
                    raise DashcamSyncAuthError("invalid token")
                if resp.status >= 400:
                    message = body.get("error") if isinstance(body, dict) else None
                    raise DashcamSyncRequestError(resp.status, message or resp.reason or "error")
                return body
        except (aiohttp.ClientError, TimeoutError) as err:
            raise DashcamSyncConnectionError(str(err)) from err

    # ------------------------------------------------------------------ reads

    async def async_info(self) -> dict[str, Any]:
        return dict(await self._request("GET", "info"))

    async def async_status(self) -> dict[str, Any]:
        return dict(await self._request("GET", "status"))

    async def async_clips(self, **filters: Any) -> list[dict[str, Any]]:
        params = {k: v for k, v in filters.items() if v is not None}
        return list(await self._request("GET", "clips", params=params))

    async def async_clip(self, clip_id: int) -> dict[str, Any]:
        return dict(await self._request("GET", f"clips/{clip_id}"))

    async def async_clip_days(self, camera: str) -> list[str]:
        data = await self._request("GET", "clip-days", params={"camera": camera})
        return list(data["days"])

    async def async_sessions(self, limit: int = 50) -> list[dict[str, Any]]:
        return list(await self._request("GET", "sessions", params={"limit": limit}))

    async def async_options(self) -> dict[str, Any]:
        return dict(await self._request("GET", "options"))

    async def async_wifi(self, camera: str) -> dict[str, Any]:
        """Hotspot credentials and the reserved address block for a camera."""
        return dict(await self._request("GET", f"cameras/{camera}/wifi"))

    # ---------------------------------------------------------------- actions

    async def async_sync(self, camera: str) -> None:
        await self._request("POST", f"cameras/{camera}/sync")

    async def async_restore(self, camera: str) -> None:
        await self._request("POST", f"cameras/{camera}/restore")

    async def async_patch_camera(self, camera: str, **changes: Any) -> None:
        await self._request("PATCH", f"cameras/{camera}", json=changes)

    async def async_overlap(
        self, camera: str, known_device_ips: list[str], probe: bool = True
    ) -> dict[str, Any]:
        return dict(
            await self._request(
                "POST",
                f"cameras/{camera}/overlap",
                json={"known_device_ips": known_device_ips, "probe": probe},
            )
        )

    async def async_hint(self, camera: str | None = None) -> None:
        await self._request("POST", "hint", json={"camera": camera} if camera else {})

    async def async_keep(self, clip_id: int, keep: bool = True) -> None:
        await self._request("POST", f"clips/{clip_id}/{'keep' if keep else 'unkeep'}")

    async def async_delete(self, clip_id: int) -> dict[str, Any]:
        return dict(await self._request("DELETE", f"clips/{clip_id}"))

    async def async_purge(self, dry_run: bool = False) -> dict[str, Any]:
        return dict(await self._request("POST", "purge", json={"dry_run": dry_run}))

    # -------------------------------------------------------------- websocket

    async def async_messages(self) -> AsyncIterator[dict[str, Any]]:
        """Yield JSON messages from the push socket until it closes."""
        try:
            async with self._session.ws_connect(
                f"{self._base}/ws", headers=self._headers, heartbeat=20
            ) as ws:
                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        try:
                            data = msg.json()
                        except ValueError:
                            continue  # a malformed frame must not drop the socket
                        if isinstance(data, dict):
                            yield data
                    elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                        break
        except aiohttp.WSServerHandshakeError as err:
            if err.status in (401, 403):
                raise DashcamSyncAuthError("invalid token") from err
            raise DashcamSyncConnectionError(str(err)) from err
        except (aiohttp.ClientError, TimeoutError) as err:
            raise DashcamSyncConnectionError(str(err)) from err

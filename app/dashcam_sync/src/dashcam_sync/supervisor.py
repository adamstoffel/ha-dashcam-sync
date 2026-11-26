"""Thin client for the parts of the Supervisor API an app may call.

Apps with the default role may read ``/network/info``, post to ``/discovery``
and use their own ``/addons/self/...`` endpoints. Nothing here needs the
manager or admin role, which keeps the security rating at its maximum.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Protocol

import aiohttp

from .errors import DashcamError

SUPERVISOR_URL = "http://supervisor"


class SupervisorError(DashcamError):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class SupervisorClient:
    def __init__(
        self,
        session: aiohttp.ClientSession,
        token: str | None = None,
        base_url: str = SUPERVISOR_URL,
    ) -> None:
        self._session = session
        self._token = token or os.environ.get("SUPERVISOR_TOKEN", "")
        self._base = base_url.rstrip("/")

    @property
    def available(self) -> bool:
        return bool(self._token)

    async def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
        if not self._token:
            raise SupervisorError("no Supervisor token (not running as an app)")
        try:
            async with self._session.request(
                method,
                f"{self._base}{path}",
                json=payload,
                headers={"Authorization": f"Bearer {self._token}"},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                try:
                    body = await resp.json(content_type=None)
                except ValueError:
                    body = {}
                if resp.status >= 400 or (isinstance(body, dict) and body.get("result") == "error"):
                    message = body.get("message") if isinstance(body, dict) else None
                    raise SupervisorError(message or f"HTTP {resp.status}", resp.status)
                return body.get("data") if isinstance(body, dict) else body
        except (aiohttp.ClientError, TimeoutError) as err:
            raise SupervisorError(f"{path}: {err}") from err

    async def network_info(self) -> dict[str, Any]:
        data = await self._request("GET", "/network/info")
        return data if isinstance(data, dict) else {}

    async def access_points(self, interface: str) -> list[dict[str, Any]]:
        """Wi-Fi scan; may be refused for the default role, callers must cope."""
        data = await self._request("GET", f"/network/interface/{interface}/accesspoints")
        aps = data.get("accesspoints") if isinstance(data, dict) else None
        return aps if isinstance(aps, list) else []

    async def post_discovery(self, service: str, config: dict[str, Any]) -> str | None:
        data = await self._request("POST", "/discovery", {"service": service, "config": config})
        return data.get("uuid") if isinstance(data, dict) else None

    async def delete_discovery(self, uuid: str) -> None:
        await self._request("DELETE", f"/discovery/{uuid}")

    async def self_info(self) -> dict[str, Any]:
        data = await self._request("GET", "/addons/self/info")
        return data if isinstance(data, dict) else {}

    async def set_options(self, options: dict[str, Any]) -> None:
        await self._request("POST", "/addons/self/options", {"options": options})

    async def restart_self(self) -> None:
        await self._request("POST", "/addons/self/restart")


class OptionsStore(Protocol):
    """Where option edits are written; the daemon hot-reloads on change."""

    async def read(self) -> dict[str, Any]: ...
    async def write(self, options: dict[str, Any]) -> None: ...


class FileOptionsStore:
    """Development and test store: edits ``options.json`` directly."""

    def __init__(self, path: Path) -> None:
        self.path = path

    async def read(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text("utf-8"))
        except FileNotFoundError:
            return {}
        return data if isinstance(data, dict) else {}

    async def write(self, options: dict[str, Any]) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(options, indent=2), "utf-8")
        os.replace(tmp, self.path)


class SupervisorOptionsStore:
    """Options stored by the Supervisor, mirrored in ``/data/options.json``.

    ``POST /addons/self/options`` only stores the options in the Supervisor; it writes
    ``/data/options.json`` when the app *starts*. So a write here also updates the file
    (what the Supervisor would write on the next start), and :meth:`sync` copies edits
    made elsewhere, such as the Configuration tab, into the file while the app runs.
    """

    def __init__(self, client: SupervisorClient, path: Path) -> None:
        self._client = client
        self._file = FileOptionsStore(path)

    async def read(self) -> dict[str, Any]:
        return await self._file.read()

    async def write(self, options: dict[str, Any]) -> None:
        await self._client.set_options(options)  # validated and stored, or raises
        await self._file.write(options)

    async def sync(self) -> bool:
        """Copy the Supervisor's stored options into the file; True when they differed."""
        info = await self._client.self_info()
        options = info.get("options")
        if not isinstance(options, dict) or options == await self._file.read():
            return False
        await self._file.write(options)
        return True

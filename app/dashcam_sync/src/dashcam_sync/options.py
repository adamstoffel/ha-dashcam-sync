"""App options (``/data/options.json``) and the rules layered on the schema."""

from __future__ import annotations

import ipaddress
import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

_ID_RE = re.compile(r"^[a-z0-9_]{1,32}$")
_STATIC_RE = re.compile(r"^\d+\.\d+\.\d+\.\d+/\d+$")

SyncMode = Literal["events_only", "events_first", "everything"]


class CameraOptions(BaseModel):
    """One entry of the ``cameras`` list (flat, to respect the schema depth)."""

    model_config = ConfigDict(extra="ignore")

    id: str
    name: str = ""
    connection: Literal["hotspot", "lan"] = "hotspot"
    interface: str | None = None
    ssid: str | None = None
    wifi_password: str | None = None
    ipv4: Literal["auto", "static"] | None = None
    static_address: str | None = None
    # How Home Assistant takes an address on a hotspot camera's network:
    # auto (small block, else point-to-point), block, or point_to_point.
    addressing: Literal["auto", "block", "point_to_point"] | None = None
    host: str | None = None
    driver: Literal["auto", "novatek", "viidure_app", "http_index"] = "auto"
    enabled: bool = True
    sync_mode: SyncMode | None = None

    @field_validator("id")
    @classmethod
    def _check_id(cls, value: str) -> str:
        if not _ID_RE.match(value):
            raise ValueError("id must match ^[a-z0-9_]{1,32}$")
        return value

    @field_validator("interface", "ssid", "wifi_password", "static_address", "host")
    @classmethod
    def _empty_to_none(cls, value: str | None) -> str | None:
        if value is None or value.strip() == "":
            return None
        return value.strip()

    @property
    def display_name(self) -> str:
        return self.name or self.id

    @property
    def effective_addressing(self) -> Literal["auto", "block", "point_to_point"]:
        return self.addressing or "auto"

    @property
    def effective_ipv4(self) -> Literal["auto", "static"]:
        return self.ipv4 or "static"


class StorageOptions(BaseModel):
    model_config = ConfigDict(extra="ignore")

    target: Literal["local", "network", "both"] = "local"
    local_folder: str = "dashcam_sync"
    network_folder: str = ""
    spool_when_network_down: bool = True
    keep_original: bool = False

    @field_validator("local_folder", "network_folder")
    @classmethod
    def _plain_folder(cls, value: str) -> str:
        value = value.strip().strip("/")
        if value and (".." in value.split("/") or value.startswith(".")):
            raise ValueError("folder must be a plain relative name")
        return value


class RetentionOptions(BaseModel):
    model_config = ConfigDict(extra="ignore")

    normal_days: int = Field(default=14, ge=0)
    event_days: int = Field(default=0, ge=0)
    parking_days: int = Field(default=30, ge=0)
    photo_days: int = Field(default=90, ge=0)
    max_total_gb: int = Field(default=200, ge=0)
    min_free_percent: int = Field(default=10, ge=0, le=90)
    network_normal_days: int = Field(default=90, ge=0)
    network_event_days: int = Field(default=0, ge=0)
    network_parking_days: int = Field(default=30, ge=0)
    network_photo_days: int = Field(default=90, ge=0)
    network_max_total_gb: int = Field(default=0, ge=0)
    network_min_free_percent: int = Field(default=5, ge=0, le=90)
    purge_time: str = "03:30"

    @field_validator("purge_time")
    @classmethod
    def _check_time(cls, value: str) -> str:
        if not re.match(r"^([01]\d|2[0-3]):[0-5]\d(:[0-5]\d)?$", value):
            raise ValueError("purge_time must be HH:MM")
        return value[:5]


class SyncOptions(BaseModel):
    model_config = ConfigDict(extra="ignore")

    mode: SyncMode = "events_first"
    recent_minutes: int = Field(default=10, ge=0)
    delete_from_camera: bool = False
    delete_events_from_camera: bool = False
    remux_to_mp4: bool = True
    thumbnails: bool = True


class AppOptions(BaseModel):
    model_config = ConfigDict(extra="ignore")

    log_level: Literal["debug", "info", "warning", "error"] = "info"
    cameras: list[CameraOptions] = Field(default_factory=list)
    storage: StorageOptions = Field(default_factory=StorageOptions)
    retention: RetentionOptions = Field(default_factory=RetentionOptions)
    sync: SyncOptions = Field(default_factory=SyncOptions)

    def camera(self, camera_id: str) -> CameraOptions | None:
        for cam in self.cameras:
            if cam.id == camera_id:
                return cam
        return None


def parse_options(raw: dict[str, Any]) -> AppOptions:
    """Parse options, dropping individually broken camera entries.

    A single bad entry must never stop the app, so entries that fail pydantic
    validation are reported by :func:`split_cameras` instead of raising.
    """
    cams_raw = raw.get("cameras") or []
    if not isinstance(cams_raw, list):
        raise ValueError("cameras must be a list")
    good: list[dict[str, Any]] = []
    for entry in cams_raw:
        try:
            CameraOptions.model_validate(entry)
        except ValueError:
            continue
        good.append(entry)
    return AppOptions.model_validate({**raw, "cameras": good})


def invalid_camera_entries(raw: dict[str, Any]) -> dict[str, str]:
    """Map ``id or #index`` to the validation error for entries that failed."""
    problems: dict[str, str] = {}
    cams = raw.get("cameras") or []
    if not isinstance(cams, list):
        return {"cameras": "cameras must be a list"}
    for index, entry in enumerate(cams):
        try:
            CameraOptions.model_validate(entry)
        except ValueError as err:
            key = str(entry.get("id") or f"#{index}") if isinstance(entry, dict) else f"#{index}"
            problems[key] = str(err).splitlines()[0]
    return problems


def load_options(path: Path) -> tuple[AppOptions, dict[str, str]]:
    """Read ``options.json``; returns options and per-entry parse problems."""
    if not path.exists():
        return AppOptions(), {}
    raw = json.loads(path.read_text("utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("options.json must hold an object")
    return parse_options(raw), invalid_camera_entries(raw)


def validate_cameras(
    options: AppOptions,
    *,
    network_mount_ok: Callable[[str], bool] | None = None,
) -> dict[str, list[str]]:
    """Rules the daemon adds on top of the schema.

    Returns ``camera id -> list of reasons``; a camera with reasons is disabled
    and gets a repair issue, the rest of the app keeps running.
    """
    problems: dict[str, list[str]] = {}

    def add(camera_id: str, reason: str) -> None:
        problems.setdefault(camera_id, []).append(reason)

    seen_ids: set[str] = set()
    hotspot_by_interface: dict[str, str] = {}
    storage = options.storage

    for cam in options.cameras:
        if cam.id in seen_ids:
            add(cam.id, "duplicate_id")
        seen_ids.add(cam.id)

        if cam.connection == "hotspot":
            if not cam.interface:
                add(cam.id, "hotspot_needs_interface")
            if not cam.ssid:
                add(cam.id, "hotspot_needs_ssid")
            if cam.interface and cam.enabled:
                other = hotspot_by_interface.get(cam.interface)
                if other and other != cam.id:
                    add(cam.id, "interface_in_use")
                else:
                    hotspot_by_interface[cam.interface] = cam.id
            if cam.effective_ipv4 == "static":
                if cam.static_address:
                    if not _STATIC_RE.match(cam.static_address):
                        add(cam.id, "static_address_invalid")
                    else:
                        try:
                            ipaddress.ip_interface(cam.static_address)
                        except ValueError:
                            add(cam.id, "static_address_invalid")
        elif not cam.host:
            add(cam.id, "lan_needs_host")

        if storage.target in ("network", "both"):
            if not storage.network_folder:
                add(cam.id, "network_folder_missing")
            elif network_mount_ok is not None and not network_mount_ok(storage.network_folder):
                add(cam.id, "network_folder_not_mounted")

    return problems

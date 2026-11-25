from __future__ import annotations

import asyncio
from pathlib import Path

import aiohttp
import pytest

from dashcam_sync.daemon import Daemon, DaemonConfig
from dashcam_sync.errors import ConfigError
from dashcam_sync.hostcheck import validate_camera_host

TOKEN = "t" * 40


@pytest.mark.parametrize(
    "host",
    [
        "192.168.1.254",
        "192.168.169.1:80",
        "10.99.77.1",
        "169.254.10.1",
        "172.16.0.9",
        "[fe80::1]",
        "fd00::5",
    ],
)
async def test_camera_addresses_are_accepted(host: str) -> None:
    assert await validate_camera_host(host) == host


@pytest.mark.parametrize(
    "host",
    [
        "8.8.8.8", "1.1.1.1:80", "93.184.216.34",  # the internet
        "127.0.0.1", "127.0.0.1:8100", "::1",  # loopback
        "172.30.32.2", "172.30.32.1:80", "172.30.33.9",  # Supervisor, Core and apps
        "0.0.0.0", "224.0.0.1", "255.255.255.255",  # unusable
        "supervisor", "homeassistant", "localhost", "SUPERVISOR.",  # internal names
        "", "  ",
    ],
)  # fmt: skip
async def test_everything_else_is_refused(host: str) -> None:
    with pytest.raises(ConfigError):
        await validate_camera_host(host)


async def test_loopback_is_allowed_only_for_tests_and_dev() -> None:
    assert await validate_camera_host("127.0.0.1:9", allow_loopback=True) == "127.0.0.1:9"
    with pytest.raises(ConfigError):
        await validate_camera_host("172.30.32.2", allow_loopback=True)  # never, even then


async def test_hostnames_are_resolved_and_checked() -> None:
    with pytest.raises(ConfigError, match="cannot resolve"):
        await validate_camera_host("no-such-host.invalid")


async def test_wizard_endpoints_refuse_foreign_hosts(tmp_path: Path) -> None:
    cfg = DaemonConfig(
        data_dir=tmp_path / "d", media_root=tmp_path, share_root=tmp_path, api_port=0, ui_port=0,
        bind_host="127.0.0.1", use_supervisor=False, api_token=TOKEN,
    )  # fmt: skip
    daemon = Daemon(cfg)  # default: loopback and public addresses are refused
    await daemon.start()
    try:
        base = f"http://127.0.0.1:{daemon.bound_port(0)}/api/v1"
        headers = {"Authorization": f"Bearer {TOKEN}"}
        async with aiohttp.ClientSession(headers=headers) as http:
            for path, body in (
                ("wizard/probe", {"host": "8.8.8.8"}),
                ("wizard/probe", {"host": "127.0.0.1:8100"}),
                ("wizard/probe", {"host": "supervisor"}),
                ("wizard/capture-profile", {"host": "172.30.32.2"}),
                ("wizard/station", {"host": "93.184.216.34", "ssid": "a", "password": "b"}),
                ("wizard/camera", {"id": "x", "connection": "lan", "host": "8.8.8.8"}),
            ):
                async with http.post(f"{base}/{path}", json=body) as resp:
                    assert resp.status == 400, (path, body, resp.status)
                    assert "error" in await resp.json()
    finally:
        await daemon.stop()


async def test_concurrent_option_edits_do_not_overwrite_each_other(tmp_path: Path) -> None:
    (tmp_path / "d").mkdir()
    cfg = DaemonConfig(
        data_dir=tmp_path / "d", media_root=tmp_path, share_root=tmp_path, api_port=0, ui_port=0,
        bind_host="127.0.0.1", use_supervisor=False, api_token=TOKEN,
    )  # fmt: skip
    daemon = Daemon(cfg)
    await daemon.start()
    try:

        def add(name: str):  # type: ignore[no-untyped-def]
            return lambda raw: raw.setdefault("cameras", []).append(
                {"id": name, "connection": "lan", "host": "192.168.1.5", "enabled": False}
            )

        await asyncio.gather(*(daemon.update_raw_options(add(f"cam{i}")) for i in range(8)))
        assert sorted(c.id for c in daemon.options.cameras) == [f"cam{i}" for i in range(8)]
    finally:
        await daemon.stop()

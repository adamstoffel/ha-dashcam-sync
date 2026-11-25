"""End-to-end stack: fake camera -> real app daemon -> real integration in Home Assistant.

Nothing is mocked between the camera and the Home Assistant entities, so these
tests also pin the contract between the app's local API and the integration.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from fakecam import FakeCamera
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.dashcam_sync.const import (
    CONF_HOST,
    CONF_PORT,
    CONF_TOKEN,
    DOMAIN,
)
from dashcam_sync.daemon import Daemon, DaemonConfig
from dashcam_sync.engine import Timing

pytest_plugins = "pytest_homeassistant_custom_component"
TOKEN = "e2e-token-" + "x" * 30


@pytest.fixture(autouse=True)
def _enable(enable_custom_integrations: None, socket_enabled: None) -> None:
    """Custom integration on, real localhost sockets allowed."""


@dataclass
class Stack:
    daemon: Daemon
    camera: FakeCamera
    media: Path
    entry: MockConfigEntry


@pytest.fixture
async def camera() -> AsyncIterator[FakeCamera]:
    cam = FakeCamera("novatek")
    await cam.start()
    cam.add_novatek("20261122_101500_F.MP4", 60_000, time="2026/11/22 10:15:00")
    cam.add_novatek(
        "20261122_101600_F.MP4",
        30_000,
        folder="MOVIE\\RO",
        time="2026/11/22 10:16:00",
        attr=33,
    )
    yield cam
    await cam.stop()


@pytest.fixture
async def daemon(tmp_path: Path, camera: FakeCamera) -> AsyncIterator[Daemon]:
    data = tmp_path / "data"
    data.mkdir()
    (tmp_path / "media").mkdir()
    (data / "options.json").write_text(
        json.dumps(
            {
                "cameras": [{"id": "polestar_2", "name": "Polestar 2", "connection": "lan",
                             "host": camera.host, "driver": "auto", "enabled": True}],
                "sync": {"mode": "everything", "remux_to_mp4": False, "thumbnails": False},
                "retention": {"min_free_percent": 0, "max_total_gb": 0},
            }
        )
    )  # fmt: skip
    d = Daemon(
        DaemonConfig(
            data_dir=data, media_root=tmp_path / "media", share_root=tmp_path / "share",
            api_port=0, ui_port=0, bind_host="127.0.0.1", ingress_allow=("127.0.0.1",),
            timing=Timing(poll=0.05, hint_poll=0.02, backoff=(0.05,), restore_timeout=3),
            network_poll=0.5, hub_push_interval=0.2, use_supervisor=False, allow_loopback_cameras=True, api_token=TOKEN,
        )
    )  # fmt: skip
    await d.start()
    yield d
    await d.stop()


@pytest.fixture
async def stack(
    hass: HomeAssistant, daemon: Daemon, camera: FakeCamera, tmp_path: Path
) -> Stack:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Dashcam Sync",
        unique_id="e2e",
        data={
            CONF_HOST: "127.0.0.1",
            CONF_PORT: daemon.bound_port(0),
            CONF_TOKEN: TOKEN,
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return Stack(daemon, camera, tmp_path / "media", entry)

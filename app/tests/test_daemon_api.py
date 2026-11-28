from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiohttp
import pytest
import pytest_asyncio
from aiohttp import web

import dashcam_sync.daemon as daemon_mod
from dashcam_sync.daemon import Daemon, DaemonConfig
from dashcam_sync.engine import Timing
from dashcam_sync.network import Interface, parse_interfaces
from dashcam_sync.supervisor import (
    FileOptionsStore,
    SupervisorClient,
    SupervisorError,
    SupervisorOptionsStore,
)
from fakecam import FakeCamera, make_bytes

TOKEN = "t" * 40


@dataclass
class Stack:
    daemon: Daemon
    cam: FakeCamera
    api: str
    ui: str
    http: aiohttp.ClientSession
    options_path: Path

    def h(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {TOKEN}"}

    async def get(self, path: str, **kw: Any) -> Any:
        async with self.http.get(f"{self.api}{path}", headers=self.h(), **kw) as r:
            return r.status, await r.json()

    async def post(self, path: str, body: Any = None) -> Any:
        async with self.http.post(f"{self.api}{path}", headers=self.h(), json=body) as r:
            return r.status, await r.json()


async def wait_for(predicate: Any, timeout: float = 10.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("timed out")


@pytest_asyncio.fixture
async def stack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, novatek_cam: FakeCamera
) -> AsyncIterator[Stack]:
    monkeypatch.setattr(daemon_mod, "OPTIONS_POLL", 0.05)
    novatek_cam.add_novatek("20261122_101500_F.MP4", 50_000, time="2026/11/22 10:15:00")
    novatek_cam.add_novatek(
        "20261122_101600_F.MP4", 20_000, folder="MOVIE\\RO", time="2026/11/22 10:16:00", attr=33
    )
    data = tmp_path / "data"
    data.mkdir()
    options_path = data / "options.json"
    options_path.write_text(
        json.dumps(
            {
                "cameras": [
                    {
                        "id": "car",
                        "name": "Car",
                        "connection": "lan",
                        "host": novatek_cam.host,
                        "driver": "auto",
                        "enabled": True,
                    }
                ],
                "sync": {"mode": "everything", "remux_to_mp4": False, "thumbnails": False},
                "retention": {"min_free_percent": 0, "max_total_gb": 0},
            }
        )
    )
    cfg = DaemonConfig(
        data_dir=data,
        media_root=tmp_path / "media",
        share_root=tmp_path / "share",
        api_port=0,
        ui_port=0,
        bind_host="127.0.0.1",
        ingress_allow=("127.0.0.1",),
        timing=Timing(poll=0.05, hint_poll=0.02, backoff=(0.05,), restore_timeout=3),
        network_poll=0.2,
        hub_push_interval=0.05,
        use_supervisor=False,
        allow_loopback_cameras=True,
        api_token=TOKEN,
    )
    (tmp_path / "media").mkdir()
    daemon = Daemon(cfg)
    await daemon.start()
    async with aiohttp.ClientSession() as http:
        yield Stack(
            daemon,
            novatek_cam,
            f"http://127.0.0.1:{daemon.bound_port(0)}",
            f"http://127.0.0.1:{daemon.bound_port(1)}",
            http,
            options_path,
        )
    await daemon.stop()


async def synced(stack: Stack) -> None:
    await wait_for(lambda: stack.daemon.workers["car"].status.last_sync is not None)
    await stack.daemon.post.drain()  # type: ignore[union-attr]


# ------------------------------------------------------------------ security


async def test_api_requires_a_token_but_healthz_does_not(stack: Stack) -> None:
    async with stack.http.get(f"{stack.api}/api/v1/info") as r:
        assert r.status == 401
    async with stack.http.get(
        f"{stack.api}/api/v1/info", headers={"Authorization": "Bearer wrong"}
    ) as r:
        assert r.status == 401
    async with stack.http.get(f"{stack.api}/healthz") as r:
        assert r.status == 200
    status, body = await stack.get("/api/v1/info")
    assert status == 200 and body["api_version"] == 1 and body["name"] == "Dashcam Sync"
    assert len(body["instance"]) == 16 and TOKEN not in body["instance"]


async def test_ui_only_accepts_the_ingress_proxy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = DaemonConfig(
        data_dir=tmp_path / "d",
        media_root=tmp_path,
        share_root=tmp_path,
        api_port=0,
        ui_port=0,
        bind_host="127.0.0.1",
        use_supervisor=False,
        allow_loopback_cameras=True,
        api_token=TOKEN,
    )  # default allow-list is the Supervisor's address only
    daemon = Daemon(cfg)
    await daemon.start()
    try:
        async with aiohttp.ClientSession() as http:
            ui = f"http://127.0.0.1:{daemon.bound_port(1)}"
            async with http.get(f"{ui}/api/v1/status") as r:
                assert r.status == 403
            async with http.get(f"{ui}/") as r:
                assert r.status == 403
            async with http.get(f"{ui}/healthz") as r:  # the watchdog must still work
                assert r.status == 200
    finally:
        await daemon.stop()


async def test_ui_serves_the_panel_and_hides_secrets(stack: Stack) -> None:
    async with stack.http.get(f"{stack.ui}/") as r:
        html = await r.text()
        assert r.status == 200 and "Dashcam Sync" in html and 'href="static/style.css?v=' in html
    async with stack.http.get(f"{stack.ui}/static/app.js") as r:
        js = await r.text()
        assert r.status == 200 and 'fetch("api/v1/' in js  # relative: works under ingress
    await stack.daemon.update_raw_options(
        lambda raw: raw["cameras"][0].update({"wifi_password": "secret-pass", "interface": "wlan0"})
    )
    async with stack.http.get(f"{stack.ui}/api/v1/options") as r:
        text = await r.text()
        assert "secret-pass" not in text and "********" in text
    async with stack.http.get(f"{stack.ui}/api/v1/cameras/car/wifi") as r:
        assert r.status == 403
    async with stack.http.get(f"{stack.ui}/api/v1/cameras") as r:
        assert "secret-pass" not in await r.text()


# --------------------------------------------------------------- sync + data


async def test_sync_exposes_status_clips_and_files(stack: Stack) -> None:
    await synced(stack)
    status, snap = await stack.get("/api/v1/status")
    assert status == 200 and snap["cameras"]["car"]["last_sync"]
    assert snap["cameras"]["car"]["connected"] and snap["storage"]["targets"]["local"]["online"]

    status, clips = await stack.get("/api/v1/clips?camera=car")
    assert status == 200 and len(clips) == 2
    event = next(c for c in clips if c["kind"] == "event")
    assert event["path"] and event["copies"][0]["target"] == "local"
    status, one = await stack.get(f"/api/v1/clips/{event['id']}")
    assert one["id"] == event["id"]

    async with stack.http.get(
        f"{stack.api}/api/v1/clips/{event['id']}/file", headers=stack.h()
    ) as r:
        assert r.status == 200
        assert await r.read() == make_bytes("20261122_101600_F.MP4", 20_000)
    async with stack.http.get(
        f"{stack.api}/api/v1/clips/{event['id']}/thumb", headers=stack.h()
    ) as r:
        assert r.status == 404  # thumbnails are off in this test

    status, days = await stack.get("/api/v1/clip-days?camera=car")
    assert days["days"] == ["2026-11-22"]
    assert (await stack.get("/api/v1/clips?camera=car&kind=event"))[1][0]["id"] == event["id"]
    assert (await stack.get("/api/v1/clips?day=2026-11-22"))[0] == 200


async def test_clip_actions_keep_delete_purge(stack: Stack) -> None:
    await synced(stack)
    _, clips = await stack.get("/api/v1/clips")
    normal = next(c for c in clips if c["kind"] == "normal")
    assert (await stack.post(f"/api/v1/clips/{normal['id']}/keep"))[1]["keep"] is True
    assert (await stack.get(f"/api/v1/clips/{normal['id']}"))[1]["keep"] is True
    status, dry = await stack.post("/api/v1/purge", {"dry_run": True})
    assert status == 200 and dry["dry_run"] is True
    assert (await stack.post(f"/api/v1/clips/{normal['id']}/unkeep"))[1]["keep"] is False
    async with stack.http.delete(
        f"{stack.api}/api/v1/clips/{normal['id']}", headers=stack.h()
    ) as r:
        body = await r.json()
        assert r.status == 200 and body["bytes_freed"] >= 50_000
    assert not Path(normal["path"]).exists()
    assert (await stack.get(f"/api/v1/clips/{normal['id']}"))[0] == 404
    assert (await stack.post("/api/v1/clips/999/keep"))[0] == 404
    assert (await stack.get("/api/v1/clips/abc"))[0] == 404
    status, report = await stack.post("/api/v1/purge", {})
    assert status == 200 and report["dry_run"] is False
    assert stack.daemon.last_purge is not None


async def test_bad_requests(stack: Stack) -> None:
    assert (await stack.get("/api/v1/clips?kind=nope"))[0] == 400
    assert (await stack.get("/api/v1/clips?limit=x"))[0] == 400
    assert (await stack.get("/api/v1/clips?day=garbage"))[0] == 400
    assert (await stack.get("/api/v1/clip-days"))[0] == 400
    assert (await stack.get("/api/v1/cameras/nope"))[0] == 404
    async with stack.http.post(
        f"{stack.api}/api/v1/hint", headers=stack.h(), data="{not json"
    ) as r:
        assert r.status == 400
    async with stack.http.post(f"{stack.api}/api/v1/hint", headers=stack.h(), json=[1]) as r:
        assert r.status == 400
    async with stack.http.patch(
        f"{stack.api}/api/v1/cameras/car", headers=stack.h(), json={"host": "x"}
    ) as r:
        assert r.status == 400


async def test_camera_controls_and_option_writes(stack: Stack) -> None:
    await synced(stack)
    assert (await stack.post("/api/v1/cameras/car/sync"))[0] == 200
    assert (await stack.post("/api/v1/cameras/car/restore"))[1]["ok"] is True
    assert (await stack.post("/api/v1/hint", {"camera": "car"}))[1]["cameras"] == 1
    assert (await stack.post("/api/v1/hint"))[1]["cameras"] == 1

    async with stack.http.patch(
        f"{stack.api}/api/v1/cameras/car",
        headers=stack.h(),
        json={"enabled": False, "sync_mode": "events_only"},
    ) as r:
        assert r.status == 200
    saved = json.loads(stack.options_path.read_text())
    assert saved["cameras"][0]["enabled"] is False
    assert saved["cameras"][0]["sync_mode"] == "events_only"
    assert stack.daemon.workers["car"].status.enabled is False  # hot reloaded
    assert (await stack.post("/api/v1/cameras/car/sync"))[0] == 409  # paused: not connected


async def test_hot_reload_adds_and_removes_cameras_without_restart(stack: Stack) -> None:
    raw = json.loads(stack.options_path.read_text())
    raw["cameras"].append(
        {"id": "second", "name": "Second", "connection": "lan", "host": stack.cam.host,
         "driver": "auto", "enabled": False}
    )  # fmt: skip
    stack.options_path.write_text(json.dumps(raw))
    await wait_for(lambda: "second" in stack.daemon.workers)
    raw["cameras"] = raw["cameras"][1:]
    stack.options_path.write_text(json.dumps(raw))
    await wait_for(lambda: "car" not in stack.daemon.workers)
    # A broken file keeps the last good options.
    stack.options_path.write_text("{ broken")
    await asyncio.sleep(0.2)
    assert "second" in stack.daemon.workers


async def test_websocket_snapshot_and_events(stack: Stack) -> None:
    await synced(stack)
    async with stack.http.ws_connect(f"{stack.api}/api/v1/ws", headers=stack.h()) as ws:
        first = await ws.receive_json()
        assert first["type"] == "snapshot" and "car" in first["cameras"]
        assert isinstance(first["rev"], int)
        await ws.send_str("snapshot")
        for _ in range(60):  # other pushes may be interleaved before the requested snapshot
            reply = await asyncio.wait_for(ws.receive_json(), 5)
            if reply["type"] == "snapshot":
                break
        assert reply["type"] == "snapshot"
        status_rev = (await stack.get("/api/v1/status"))[1]["rev"]
        assert status_rev > first["rev"]
        # Every read is its own sample: even with no event between them, revs differ.
        again = (await stack.get("/api/v1/status"))[1]["rev"]
        assert again > status_rev
        status_rev = again
        await stack.post("/api/v1/cameras/car/sync")
        seen: set[str] = set()
        camera_revs: list[int] = []
        for _ in range(60):
            msg = await asyncio.wait_for(ws.receive_json(), 5)
            seen.add(msg["type"])
            if msg["type"] == "camera":
                camera_revs.append(msg["rev"])
            if "session" in seen:
                break
        assert {"camera", "session"} <= seen
        assert camera_revs == sorted(camera_revs)  # one order for every push
        assert max(camera_revs) > status_rev  # the sync's pushes come after the read


async def test_ws_rejects_missing_token(stack: Stack) -> None:
    with pytest.raises(aiohttp.WSServerHandshakeError):
        async with stack.http.ws_connect(f"{stack.api}/api/v1/ws"):
            pass


async def test_clip_event_is_published_after_post_processing(stack: Stack) -> None:
    queue = stack.daemon.bus.subscribe()
    await stack.post("/api/v1/cameras/car/sync")
    await synced(stack)
    types = []
    while not queue.empty():
        types.append(queue.get_nowait())
    clip_events = [e for e in types if e["type"] == "clip"]
    assert len(clip_events) == 2 and any(e["event_clip"] for e in clip_events)
    assert stack.daemon.workers["car"].status.last_event_clip


# ---------------------------------------------------------- wizard + network


async def test_wizard_probe_block_storage_and_save(stack: Stack) -> None:
    _, probe = await stack.post("/api/v1/wizard/probe", {"host": stack.cam.host})
    assert probe["supported"] and probe["driver"] == "novatek" and probe["capabilities"]["delete"]
    _, none = await stack.post("/api/v1/wizard/probe", {"host": "127.0.0.1:1"})
    assert none["supported"] is False

    _, block = await stack.post("/api/v1/wizard/block", {"camera_ip": "192.168.1.254"})
    assert block["block"] == "192.168.1.252/30" and block["home_address"] == "192.168.1.253"
    assert (await stack.post("/api/v1/wizard/block", {"camera_ip": "nonsense"}))[0] == 400

    _, store = await stack.get("/api/v1/wizard/storage-test")
    assert store["targets"]["local"]["online"]
    _, scan = await stack.get("/api/v1/wizard/wifi-scan?interface=wlan0")
    assert scan["available"] is False  # no Supervisor in tests: the wizard asks for the SSID

    status, saved = await stack.post(
        "/api/v1/wizard/camera",
        {"id": "hot", "name": "Hot", "connection": "hotspot", "interface": "wlan0",
         "ssid": "CAM", "wifi_password": "pw", "host": "192.168.1.254"},
    )  # fmt: skip
    assert status == 200 and saved["camera"] == "hot"
    raw = json.loads(stack.options_path.read_text())
    entry = next(c for c in raw["cameras"] if c["id"] == "hot")
    # Automatic shape: nothing is pinned at save; the network check picks and remembers it.
    assert "static_address" not in entry
    status, _ = await stack.post(
        "/api/v1/wizard/camera",
        {"id": "hot", "name": "Hot", "connection": "hotspot", "interface": "wlan0",
         "ssid": "CAM", "wifi_password": "pw", "host": "192.168.1.254",
         "addressing": "block"},
    )  # fmt: skip
    assert status == 200
    raw = json.loads(stack.options_path.read_text())
    entry = next(c for c in raw["cameras"] if c["id"] == "hot")
    assert entry["static_address"] == "192.168.1.253/30"  # block: computed and pinned
    assert (await stack.post("/api/v1/wizard/camera", {"id": "BAD ID"}))[0] == 400
    # Saving again replaces rather than duplicates.
    await stack.post("/api/v1/wizard/camera", {**entry, "name": "Renamed"})
    raw = json.loads(stack.options_path.read_text())
    assert [c["name"] for c in raw["cameras"] if c["id"] == "hot"] == ["Renamed"]

    async with stack.http.delete(f"{stack.api}/api/v1/cameras/hot", headers=stack.h()) as r:
        assert r.status == 200
    assert "hot" not in stack.daemon.workers


async def test_wizard_capture_profile_and_station(stack: Stack) -> None:
    _, bundle = await stack.post("/api/v1/wizard/capture-profile", {"host": stack.cam.host})
    assert bundle["profile_version"] == 1 and "GET /" in bundle["responses"]
    assert (await stack.post("/api/v1/wizard/capture-profile", {}))[0] == 400
    assert (await stack.post("/api/v1/wizard/station", {"host": stack.cam.host}))[0] == 400
    status, _ = await stack.post(
        "/api/v1/wizard/station", {"host": stack.cam.host, "ssid": "Home", "password": "pw"}
    )
    assert status == 200
    assert any("cmd=3033" in r for r in stack.cam.requests)


def hotspot_options(raw: dict[str, Any]) -> None:
    raw["cameras"].append(
        {"id": "hot", "name": "Hot", "connection": "hotspot", "interface": "wlan0",
         "ssid": "CAM", "host": "192.168.1.254", "static_address": "192.168.1.253/30",
         "driver": "novatek", "enabled": True}
    )  # fmt: skip


async def test_overlap_check_blocks_and_reports(stack: Stack) -> None:
    await stack.daemon.update_raw_options(
        lambda raw: (hotspot_options(raw), raw["cameras"][-1].update({"addressing": "block"}))
    )
    stack.daemon.network.set_interfaces(
        [
            Interface(
                "eth0", "ethernet", True, True, True, ["192.168.1.10/24"], "192.168.1.1", True
            ),
            Interface("wlan0", "wireless", True, False, False),
        ]
    )
    status, report = await stack.post(
        "/api/v1/cameras/hot/overlap",
        {"known_device_ips": ["192.168.1.255"], "probe": False},
    )
    assert status == 200 and report["result"] == "device_in_block" and report["blocked"]
    assert report["static_address"] == "192.168.1.253/30" and report["home_networks"]
    assert "overlap_device_in_block" in stack.daemon.blocked_reasons("hot")
    snap = (await stack.get("/api/v1/status"))[1]
    issue = next(i for i in snap["issues"] if i["key"] == "subnet_overlap")
    assert issue["camera"] == "hot" and issue["placeholders"]["conflicts"] == ["192.168.1.255"]
    assert snap["cameras"]["hot"]["blocked_reasons"] or True

    _, again = await stack.post(
        "/api/v1/cameras/hot/overlap", {"known_device_ips": [], "probe": False}
    )
    assert again["result"] == "overlap_block_free" and not again["blocked"]
    assert stack.daemon.blocked_reasons("hot") == []
    assert (await stack.get("/api/v1/cameras/hot/overlap"))[1]["result"] == "overlap_block_free"
    assert (await stack.post("/api/v1/cameras/hot/overlap", {"known_device_ips": "x"}))[0] == 400


async def test_automatic_addressing_falls_back_to_point_to_point(stack: Stack) -> None:
    """The user's real case: an address in the small block is taken at home."""
    await stack.daemon.update_raw_options(hotspot_options)  # remembered 192.168.1.253/30
    stack.daemon.network.set_interfaces(
        [
            Interface(
                "eth0", "ethernet", True, True, True, ["192.168.0.20/22"], "192.168.2.1", True
            ),
            Interface("wlan0", "wireless", True, False, False),
        ]
    )
    status, report = await stack.post(
        "/api/v1/cameras/hot/overlap", {"known_device_ips": ["192.168.1.255"], "probe": False}
    )
    assert status == 200 and not report["blocked"]
    assert report["mode"] == "point_to_point"
    assert report["static_address"] == "192.168.1.253/32"
    assert report["claimed"] == ["192.168.1.253", "192.168.1.254"]
    assert report["gateway"] == "192.168.1.254"
    # Remembered, so the join applies exactly this address.
    raw = json.loads(stack.options_path.read_text())
    assert raw["cameras"][-1]["static_address"] == "192.168.1.253/32"
    _, wifi = await stack.get("/api/v1/cameras/hot/wifi")
    assert wifi["static_address"] == "192.168.1.253/32" and wifi["gateway"] == "192.168.1.254"
    assert wifi["mode"] == "point_to_point"

    # The pick itself becomes taken: the next free one is chosen.
    _, moved = await stack.post(
        "/api/v1/cameras/hot/overlap",
        {"known_device_ips": ["192.168.1.255", "192.168.1.253"], "probe": False},
    )
    assert moved["static_address"] == "192.168.1.252/32" and not moved["blocked"]

    # Nothing helps when the camera's own address is taken at home.
    _, collides = await stack.post(
        "/api/v1/cameras/hot/overlap", {"known_device_ips": ["192.168.1.254"], "probe": False}
    )
    assert collides["blocked"] and collides["result"] == "device_in_block"
    raw = json.loads(stack.options_path.read_text())
    assert raw["cameras"][-1]["static_address"] == "192.168.1.252/32"  # not rewritten


async def test_a_fixed_address_is_never_rewritten(stack: Stack) -> None:
    await stack.daemon.update_raw_options(
        lambda raw: (
            hotspot_options(raw),
            raw["cameras"][-1].update(
                {"addressing": "point_to_point", "static_address": "192.168.1.200/32"}
            ),
        )
    )
    _, report = await stack.post(
        "/api/v1/cameras/hot/overlap", {"known_device_ips": ["192.168.1.200"], "probe": False}
    )
    assert report["blocked"] and report["static_address"] == "192.168.1.200/32"
    raw = json.loads(stack.options_path.read_text())
    assert raw["cameras"][-1]["static_address"] == "192.168.1.200/32"


async def test_wifi_credentials_only_on_the_token_api(stack: Stack) -> None:
    await stack.daemon.update_raw_options(
        lambda raw: (
            hotspot_options(raw),
            raw["cameras"][-1].update({"wifi_password": "pw1234567"}),
        )
    )
    status, wifi = await stack.get("/api/v1/cameras/hot/wifi")
    assert status == 200
    assert wifi["ssid"] == "CAM" and wifi["password"] == "pw1234567"
    assert wifi["static_address"] == "192.168.1.253/30" and wifi["interface"] == "wlan0"
    assert (await stack.get("/api/v1/cameras/car/wifi"))[0] == 400  # a LAN camera


async def test_issues_cover_missing_adapter_and_invalid_cameras(stack: Stack) -> None:
    await stack.daemon.update_raw_options(
        lambda raw: raw["cameras"].append(
            {"id": "broken", "connection": "hotspot", "enabled": True}
        )
    )
    stack.daemon.network.set_interfaces(
        [Interface("eth0", "ethernet", True, True, True, ["10.0.0.2/24"], "10.0.0.1", True)]
    )
    keys = {(i["key"], i["camera"]) for i in stack.daemon.issues()}
    assert ("no_wifi_adapter", None) in keys and ("camera_invalid", "broken") in keys


async def test_route_safety_kill_switch_state(stack: Stack) -> None:
    await stack.daemon.update_raw_options(hotspot_options)
    cam = stack.daemon.options.camera("hot")
    assert cam is not None
    # Joined to the camera, and the primary route has moved onto the hotspot interface.
    stack.daemon.network.set_interfaces(
        [
            Interface("eth0", "ethernet", True, False, False),
            Interface(
                "wlan0", "wireless", True, True, True, ["192.168.1.253/30"], None, True, "CAM"
            ),
        ]
    )
    queue = stack.daemon.bus.subscribe()
    stack.daemon._check_route_safety()
    assert stack.daemon.route_unsafe == {"hot": "default_route_moved"}
    assert "default_route_changed" in stack.daemon.blocked_reasons("hot")
    assert any(e["type"] == "safety" for e in [queue.get_nowait() for _ in range(queue.qsize())])
    assert any(i["key"] == "default_route_changed" for i in stack.daemon.issues())
    # Wired again: cleared.
    stack.daemon.network.set_interfaces(
        [
            Interface(
                "eth0", "ethernet", True, True, True, ["192.168.1.10/24"], "192.168.1.1", True
            ),
            Interface(
                "wlan0", "wireless", True, True, False, ["192.168.1.253/30"], None, True, "CAM"
            ),
        ]
    )
    stack.daemon._check_route_safety()
    assert stack.daemon.route_unsafe == {}


async def test_storage_endpoint_and_logs(stack: Stack) -> None:
    status, storage = await stack.get("/api/v1/storage")
    assert status == 200 and storage["mode"] == "local"
    _, logs = await stack.get("/api/v1/logs?lines=5")
    assert isinstance(logs["lines"], list)
    assert (await stack.get("/api/v1/logs?lines=zzz"))[0] == 200
    _, sessions = await stack.get("/api/v1/sessions?limit=5&camera=car")
    assert isinstance(sessions, list)
    assert (await stack.get("/api/v1/sessions?limit=x"))[0] == 200
    _, opts = await stack.get("/api/v1/options")
    assert opts["storage"]["target"] == "local"


async def test_log_level_change_requests_restart(stack: Stack) -> None:
    await stack.daemon.update_raw_options(lambda raw: raw.update({"log_level": "debug"}))
    assert stack.daemon._stop.is_set()


# ------------------------------------------------------- supervisor + network


@pytest_asyncio.fixture
async def fake_supervisor() -> AsyncIterator[tuple[str, list[tuple[str, str, Any]]]]:
    """Behaves like the real Supervisor where it matters for options: a POST to
    /addons/self/options stores them (shown in /addons/self/info) and does NOT touch
    the app's /data/options.json, which the real one only writes when the app starts."""
    calls: list[tuple[str, str, Any]] = []
    stored: dict[str, Any] = {}

    async def handler(request: web.Request) -> web.Response:
        body = await request.json() if request.can_read_body else None
        calls.append((request.method, request.path, body))
        assert request.headers["Authorization"] == "Bearer sup-token"
        if request.path == "/network/info":
            return web.json_response(
                {"result": "ok", "data": {"interfaces": [
                    {"interface": "eth0", "type": "ethernet", "enabled": True, "connected": True,
                     "primary": True, "ipv4": {"address": ["192.168.1.10/24"], "gateway": "192.168.1.1",
                                                "ready": True}, "wifi": None},
                    {"interface": "wlan0", "type": "wireless", "enabled": True, "connected": True,
                     "primary": False, "ipv4": {"address": ["192.168.1.253/30"], "gateway": None,
                                                 "ready": True},
                     "wifi": {"mode": "infrastructure", "ssid": "CAM", "signal": 70}},
                ]}}
            )  # fmt: skip
        if request.path == "/network/interface/wlan0/accesspoints":
            return web.json_response({"result": "ok", "data": {"accesspoints": [{"ssid": "CAM"}]}})
        if request.path == "/network/interface/wlan1/accesspoints":
            return web.json_response({"result": "error", "message": "forbidden"}, status=403)
        if request.path == "/discovery":
            return web.json_response({"result": "ok", "data": {"uuid": "abc123"}})
        if request.path == "/addons/self/options" and request.method == "POST":
            stored["options"] = body["options"]
            return web.json_response({"result": "ok", "data": {}})
        if request.path == "/addons/self/info":
            info = {"hostname": "local-dashcam-sync"}
            if "options" in stored:
                info["options"] = stored["options"]
            return web.json_response({"result": "ok", "data": info})
        if request.path == "/test/configuration-tab":  # what a Configuration-tab edit does
            stored["options"] = body
            return web.json_response({"result": "ok", "data": {}})
        return web.json_response({"result": "ok", "data": {}})

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    yield f"http://127.0.0.1:{port}", calls
    await runner.cleanup()


async def test_supervisor_client(
    fake_supervisor: tuple[str, list[tuple[str, str, Any]]], session: aiohttp.ClientSession
) -> None:
    url, calls = fake_supervisor
    client = SupervisorClient(session, "sup-token", url)
    assert client.available
    info = await client.network_info()
    assert info["interfaces"][0]["interface"] == "eth0"
    assert [a["ssid"] for a in await client.access_points("wlan0")] == ["CAM"]
    with pytest.raises(SupervisorError) as err:
        await client.access_points("wlan1")
    assert err.value.status == 403
    assert await client.post_discovery("dashcam_sync", {"host": "h", "port": 8100}) == "abc123"
    await client.delete_discovery("abc123")
    await client.set_options({"a": 1})
    await client.restart_self()
    assert ("POST", "/addons/self/options", {"options": {"a": 1}}) in calls
    assert (
        "POST",
        "/discovery",
        {"service": "dashcam_sync", "config": {"host": "h", "port": 8100}},
    ) in calls

    nobody = SupervisorClient(session, "", url)
    nobody._token = ""
    assert not nobody.available
    with pytest.raises(SupervisorError):
        await nobody.network_info()
    dead = SupervisorClient(session, "x", "http://127.0.0.1:1")
    with pytest.raises(SupervisorError):
        await dead.network_info()


async def test_network_monitor_and_options_stores(
    fake_supervisor: tuple[str, list[tuple[str, str, Any]]],
    session: aiohttp.ClientSession,
    tmp_path: Path,
) -> None:
    from dashcam_sync.network import NetworkMonitor
    from dashcam_sync.options import CameraOptions

    url, calls = fake_supervisor
    client = SupervisorClient(session, "sup-token", url)
    monitor = NetworkMonitor(client)
    assert not monitor.known and monitor.link_matches(CameraOptions(id="c")) is None
    await monitor.refresh()
    assert monitor.known and monitor.has_wifi_adapter
    cam = CameraOptions(id="c", connection="hotspot", interface="wlan0", ssid="CAM")
    assert monitor.link_matches(cam) is True
    assert monitor.link_matches(cam.model_copy(update={"ssid": "OTHER"})) is False
    assert monitor.link_matches(cam.model_copy(update={"interface": "wlan9"})) is False
    assert [str(n) for n in monitor.home_networks({"wlan0"})] == ["192.168.1.0/24"]
    assert monitor.default_route_ok({"wlan0"}) is True
    assert monitor.default_route_ok({"eth0"}) is False
    assert monitor.primary_gateway() == "192.168.1.1"
    assert monitor.interface("eth0").as_dict()["primary"] is True  # type: ignore[union-attr]

    assert parse_interfaces({"interfaces": ["junk", {"interface": "x"}]})[0].name == "x"
    broken = NetworkMonitor(SupervisorClient(session, "sup-token", "http://127.0.0.1:1"))
    await broken.refresh()
    assert broken.error and not broken.known

    path = tmp_path / "options.json"
    store = FileOptionsStore(path)
    assert await store.read() == {}
    await store.write({"x": 1})
    assert await store.read() == {"x": 1}
    sup = SupervisorOptionsStore(client, path)
    assert await sup.read() == {"x": 1}
    await sup.write({"y": 2})
    assert ("POST", "/addons/self/options", {"options": {"y": 2}}) in calls
    # The Supervisor does not write the file until the app restarts: the store must.
    assert await sup.read() == {"y": 2}
    assert await sup.sync() is False  # already the same
    async with session.post(
        f"{url}/test/configuration-tab",
        json={"z": 3},
        headers={"Authorization": "Bearer sup-token"},
    ):
        pass
    assert await sup.sync() is True and await sup.read() == {"z": 3}
    path.write_text("[1]")
    assert await store.read() == {}


async def test_daemon_announces_through_discovery(
    fake_supervisor: tuple[str, list[tuple[str, str, Any]]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("INFO")
    url, calls = fake_supervisor
    from dashcam_sync import supervisor as sup_mod

    monkeypatch.setenv("SUPERVISOR_TOKEN", "sup-token")
    monkeypatch.setattr(sup_mod, "SUPERVISOR_URL", url)
    real_init = SupervisorClient.__init__

    def init(self: SupervisorClient, session: Any, token: Any = None, base_url: str = url) -> None:
        real_init(self, session, token, url)

    monkeypatch.setattr(SupervisorClient, "__init__", init)
    cfg = DaemonConfig(
        data_dir=tmp_path / "d",
        media_root=tmp_path,
        share_root=tmp_path,
        api_port=0,
        ui_port=0,
        bind_host="127.0.0.1",
        api_token=TOKEN,
    )
    daemon = Daemon(cfg)
    await daemon.start()
    try:
        discovery = next(c for c in calls if c[1] == "/discovery")
        assert discovery[2]["service"] == "dashcam_sync"
        assert discovery[2]["config"]["token"] == TOKEN and discovery[2]["config"]["port"] == 0
        assert daemon.network.known and daemon.discovery_uuid == "abc123"
        assert any("Offered to Home Assistant" in m for m in caplog.messages)
        assert TOKEN not in caplog.text  # the token is never logged
    finally:
        await daemon.stop()
    assert ("DELETE", "/discovery/abc123", None) in calls


async def test_token_is_generated_once_and_kept(tmp_path: Path) -> None:
    cfg = DaemonConfig(
        data_dir=tmp_path, media_root=tmp_path, share_root=tmp_path, use_supervisor=False
    )
    first = Daemon(cfg)._load_token()
    assert len(first) >= 32 and (tmp_path / "api_token").stat().st_mode & 0o777 == 0o600
    assert Daemon(cfg)._load_token() == first


def _daemon_with_options(tmp_path: Path, options: object) -> Daemon:
    (tmp_path / "options.json").write_text(json.dumps(options), "utf-8")
    return Daemon(
        DaemonConfig(
            data_dir=tmp_path, media_root=tmp_path, share_root=tmp_path, use_supervisor=False
        )
    )


async def test_a_token_typed_into_the_app_configuration_is_used(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    chosen = "a-token-the-owner-typed-0123456789abcdef"
    daemon = _daemon_with_options(tmp_path, {"api_token": f"  {chosen}  "})
    with caplog.at_level("INFO"):
        assert daemon._load_token() == chosen
    assert not (tmp_path / "api_token").exists()  # nothing generated or stored
    assert any("API token from the app configuration" in m for m in caplog.messages)
    assert chosen not in caplog.text


@pytest.mark.parametrize(
    "options", [{}, {"api_token": ""}, {"api_token": "   "}, {"api_token": 7}, [1]]
)
async def test_without_a_configured_token_one_is_generated(tmp_path: Path, options: object) -> None:
    token = _daemon_with_options(tmp_path, options)._load_token()
    assert len(token) >= 32 and (tmp_path / "api_token").read_text("utf-8") == token


async def test_a_too_short_configured_token_is_ignored_with_a_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    daemon = _daemon_with_options(tmp_path, {"api_token": "short"})
    token = daemon._load_token()
    assert token != "short" and len(token) >= 32
    assert any("needs at least 32 characters" in m for m in caplog.messages)
    assert "short" not in [m.split()[-1] for m in caplog.messages]


async def test_a_broken_options_file_does_not_stop_the_token(tmp_path: Path) -> None:
    (tmp_path / "options.json").write_text("{not json", "utf-8")
    daemon = Daemon(
        DaemonConfig(
            data_dir=tmp_path, media_root=tmp_path, share_root=tmp_path, use_supervisor=False
        )
    )
    assert len(daemon._load_token()) >= 32


async def test_hub_state_is_pushed_when_it_changes(stack: Stack) -> None:
    queue = stack.daemon.bus.subscribe()
    await wait_for(
        lambda: not queue.empty() and any(e["type"] == "hub" for e in list(queue._queue))
    )
    first = next(e for e in list(queue._queue) if e["type"] == "hub")
    assert {"storage", "issues", "network", "overlap"} <= set(first)
    while not queue.empty():
        queue.get_nowait()
    # An issue appears and is pushed.
    stack.daemon.workers["car"].status.unsupported = True
    await wait_for(lambda: any(e["type"] == "hub" for e in list(queue._queue)))
    hub = next(e for e in list(queue._queue) if e["type"] == "hub")
    assert any(i["key"] == "unsupported_camera" for i in hub["issues"])


async def test_config_changes_reach_subscribers_immediately(stack: Stack) -> None:
    await synced(stack)
    queue = stack.daemon.bus.subscribe()
    await stack.daemon.update_raw_options(
        lambda raw: raw["cameras"][0].update({"sync_mode": "events_only"})
    )
    events = [queue.get_nowait() for _ in range(queue.qsize())]
    assert any(e["type"] == "camera" and e["status"]["sync_mode"] == "events_only" for e in events)


async def test_connected_websockets_do_not_delay_shutdown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import time

    cfg = DaemonConfig(
        data_dir=tmp_path / "d", media_root=tmp_path, share_root=tmp_path, api_port=0, ui_port=0,
        bind_host="127.0.0.1", use_supervisor=False, allow_loopback_cameras=True, api_token=TOKEN,
    )  # fmt: skip
    daemon = Daemon(cfg)
    await daemon.start()
    async with aiohttp.ClientSession() as http:
        ws = await http.ws_connect(
            f"http://127.0.0.1:{daemon.bound_port(0)}/api/v1/ws",
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        await ws.receive_json()
        started = time.monotonic()
        await daemon.stop()
        assert time.monotonic() - started < 3  # used to wait for the socket to time out
        closing = await ws.receive()
        while closing.type == aiohttp.WSMsgType.TEXT:  # pushes queued before the close
            closing = await ws.receive()
        assert closing.type in (
            aiohttp.WSMsgType.CLOSE,
            aiohttp.WSMsgType.CLOSING,
            aiohttp.WSMsgType.CLOSED,
        )


async def test_overlap_check_is_refused_for_lan_cameras(stack: Stack) -> None:
    status, body = await stack.post("/api/v1/cameras/car/overlap", {})
    assert status == 400 and "hotspot" in body["error"]
    assert (await stack.get("/api/v1/cameras/car/overlap"))[0] == 400


async def test_the_instance_id_is_stable_and_independent_of_the_token(tmp_path: Path) -> None:
    def config(token: str) -> DaemonConfig:
        return DaemonConfig(
            data_dir=tmp_path / "d", media_root=tmp_path, share_root=tmp_path, api_port=0, ui_port=0,
            bind_host="127.0.0.1", use_supervisor=False, api_token=token,
        )  # fmt: skip

    ids = []
    for token in ("a" * 40, "b" * 40):  # the token is rotated between runs
        daemon = Daemon(config(token))
        await daemon.start()
        try:
            async with aiohttp.ClientSession() as http:
                async with http.get(
                    f"http://127.0.0.1:{daemon.bound_port(0)}/api/v1/info",
                    headers={"Authorization": f"Bearer {token}"},
                ) as r:
                    ids.append((await r.json())["instance"])
        finally:
            await daemon.stop()
    assert ids[0] == ids[1] and len(ids[0]) == 16 and not set(ids[0]) & set("xyz")


# ------------------------------------------------------- storage location and moving clips


async def _send(stack: Stack, method: str, path: str, body: Any = None) -> Any:
    async with stack.http.request(method, f"{stack.api}{path}", headers=stack.h(), json=body) as r:
        return r.status, await r.json()


def _attach_disk(stack: Stack, name: str = "usb") -> Path:
    """Pretend Home Assistant attached a local disk at /media/<name> (exFAT)."""
    disk = stack.daemon.config.media_root / name
    disk.mkdir(parents=True, exist_ok=True)
    stack.daemon.config.mountinfo = f"1 2 179:1 / {disk} rw - exfat /dev/sdb1 rw\n"
    stack.daemon._build_storage()
    return disk


async def test_changing_the_location_then_moving_the_clips(stack: Stack) -> None:
    await synced(stack)
    disk = _attach_disk(stack)
    events = stack.daemon.bus.subscribe()

    status, loc = await stack.get("/api/v1/storage/location")
    assert status == 200 and loc["current"]["target"] == "local"
    assert [a["name"] for a in loc["attached"]] == ["usb"]
    assert loc["attached"][0]["kind"] == "disk" and loc["attached"][0]["usage"] == "media"

    status, after = await _send(
        stack, "PATCH", "/api/v1/storage/location", {"target": "network", "network_folder": "usb"}
    )
    assert status == 200, after
    assert after["plan"]["needed"] and after["plan"]["clips"] == 2
    assert after["plan"]["blocked"] is None
    assert stack.daemon.options.storage.network_folder == "usb"

    status, started = await stack.post("/api/v1/storage/migration")
    assert status == 202 and started["job"]["status"] == "running"
    await wait_for(lambda: stack.daemon.migration.state.status == "done")

    status, now = await stack.get("/api/v1/storage/migration")
    assert now["plan"]["needed"] is False and now["job"]["clips_done"] == 2
    for clip in stack.daemon.cat.list_clips(camera="car"):
        copies = stack.daemon.cat.copies(clip.id)
        assert [c.target for c in copies] == ["network"]
        assert Path(copies[0].path).is_relative_to(disk)
    assert not list((stack.daemon.config.media_root / "dashcam_sync").rglob("*.MP4"))
    # Clips are still served from their new place.
    clip = stack.daemon.cat.list_clips(camera="car")[0]
    async with stack.http.get(f"{stack.api}/api/v1/clips/{clip.id}/file", headers=stack.h()) as r:
        assert r.status == 200 and len(await r.read()) == clip.size
    kinds = set()
    while not events.empty():
        kinds.add(events.get_nowait()["type"])
    assert "migration" in kinds


async def test_location_changes_are_validated(stack: Stack) -> None:
    for body, message in (
        ({}, "nothing to change"),
        ({"target": "network"}, "choose a network share or disk"),
        ({"target": "both", "network_folder": ""}, "choose a network share or disk"),
        ({"local_folder": "../etc"}, "folder must be a plain relative name"),
        ({"target": "cloud"}, "target"),
    ):
        status, err = await _send(stack, "PATCH", "/api/v1/storage/location", body)
        assert status == 400 and message in err["error"], (body, err)


async def test_moving_refuses_when_nothing_to_do_or_the_disk_is_missing(stack: Stack) -> None:
    await synced(stack)
    status, err = await stack.post("/api/v1/storage/migration")
    assert status == 409 and err["error"] == "nothing_to_move"

    # A disk that is configured but not attached: the plan says why, the move refuses.
    status, after = await _send(
        stack, "PATCH", "/api/v1/storage/location", {"target": "network", "network_folder": "gone"}
    )
    assert after["plan"]["blocked"] == "network_not_mounted"
    status, err = await stack.post("/api/v1/storage/migration")
    assert status == 409 and err["error"] == "network_not_mounted"


async def test_the_location_cannot_change_while_clips_are_moving(stack: Stack) -> None:
    stack.daemon.migration.state.status = "running"
    try:
        status, err = await _send(stack, "PATCH", "/api/v1/storage/location", {"local_folder": "x"})
        assert status == 409
        status, err = await stack.post("/api/v1/storage/migration")
        assert status == 409 and err["error"] == "already_running"
        status, now = await _send(stack, "DELETE", "/api/v1/storage/migration")
        assert status == 200 and now["plan"] is None
        assert stack.daemon.migration._cancel.is_set()
    finally:
        stack.daemon.migration.state.status = "idle"


async def test_an_options_change_cancels_a_running_move(stack: Stack) -> None:
    stack.daemon.migration.state.status = "running"
    try:
        stack.daemon._build_storage()
        assert stack.daemon.migration._cancel.is_set()
    finally:
        stack.daemon.migration.state.status = "idle"


async def test_the_storage_payload_reports_the_move(stack: Stack) -> None:
    _, storage = await stack.get("/api/v1/storage")
    assert storage["migration"]["status"] == "idle"


# -------------------------------------------------------------- hotspot join from the panel


async def _hotspot(stack: Stack) -> None:
    body = {"id": "hot", "name": "Hot", "connection": "hotspot", "interface": "wlan0",
            "ssid": "CAM", "wifi_password": "pw-1", "host": "127.0.0.1:9", "driver": "auto",
            "enabled": True}  # fmt: skip
    status, _ = await stack.post("/api/v1/wizard/camera", body)
    assert status == 200


async def test_joining_without_the_integration_falls_back_to_manual_steps(stack: Stack) -> None:
    await _hotspot(stack)
    status, join = await stack.post("/api/v1/cameras/hot/join")
    assert status == 200 and join["state"] == "manual"
    assert join["reason"] == "integration_not_connected"
    profile = join["profile"]
    assert profile["ssid"] == "CAM" and profile["interface"] == "wlan0"
    assert profile["address"].endswith("/30") and profile["gateway"] is None
    assert "pw-1" not in json.dumps(join)  # the password is never part of the profile


async def test_a_join_request_reaches_the_integration_and_its_answer_is_shown(
    stack: Stack,
) -> None:
    await _hotspot(stack)
    events = stack.daemon.bus.subscribe()  # stands in for the integration's socket
    stack.daemon.integration_links[0] = None  # an integration is on the token port
    _, join = await stack.post("/api/v1/cameras/hot/join")
    assert join["state"] == "requested"
    sent = [events.get_nowait() for _ in range(events.qsize())]
    assert {"type": "join_wifi", "camera": "hot"}.items() <= next(
        e for e in sent if e["type"] == "join_wifi"
    ).items()
    status, _ = await stack.post(
        "/api/v1/cameras/hot/join-result", {"state": "failed", "reason": "no_adapter: wlan0"}
    )
    assert status == 200
    _, now = await stack.get("/api/v1/cameras/hot/join")
    assert now["state"] == "failed" and now["reason"] == "no_adapter: wlan0"
    status, err = await stack.post("/api/v1/cameras/hot/join-result", {"state": "bogus"})
    assert status == 400


async def test_an_unanswered_join_request_turns_manual(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _hotspot(stack)
    stack.daemon.bus.subscribe()
    stack.daemon.integration_links[0] = None  # an integration is on the token port
    await stack.post("/api/v1/cameras/hot/join")
    monkeypatch.setattr(daemon_mod, "JOIN_TIMEOUT", -1)
    _, now = await stack.get("/api/v1/cameras/hot/join")
    assert now["state"] == "manual" and now["reason"] == "no_answer"


async def test_a_join_that_stays_connecting_turns_manual(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The integration says it is connecting, then never gives a final answer."""
    await _hotspot(stack)
    stack.daemon.bus.subscribe()
    stack.daemon.integration_links[0] = None  # an integration is on the token port
    await stack.post("/api/v1/cameras/hot/join")
    status, _ = await stack.post("/api/v1/cameras/hot/join-result", {"state": "connecting"})
    assert status == 200
    _, now = await stack.get("/api/v1/cameras/hot/join")
    assert now["state"] == "connecting"
    monkeypatch.setattr(daemon_mod, "JOIN_CONNECT_TIMEOUT", -1)
    _, now = await stack.get("/api/v1/cameras/hot/join")
    assert now["state"] == "manual" and now["reason"] == "no_answer"


async def test_the_panel_can_tell_whether_the_integration_is_paired(stack: Stack) -> None:
    """Connected or not, and the same release or not: the panel warns on either."""
    from dashcam_sync import __version__

    _, info = await stack.get("/api/v1/info")
    assert info["release"] == __version__ and isinstance(info["uptime"], int)
    assert info["integration"] == {
        "connected": False, "version": None, "app_release": __version__, "matches": False,
    }  # fmt: skip
    header = "X-Dashcam-Sync-Integration"
    for sent, matches in ((__version__, True), ("2026.1.0", False), (None, False)):
        headers = {**stack.h(), **({header: sent} if sent else {})}
        async with stack.http.ws_connect(f"{stack.api}/api/v1/ws", headers=headers) as ws:
            await ws.receive_json()  # the snapshot: the socket is registered by now
            _, info = await stack.get("/api/v1/info")
            assert info["integration"]["connected"] is True
            assert info["integration"]["version"] == sent
            assert info["integration"]["matches"] is matches
            _, status = await stack.get("/api/v1/status")
            assert status["integration"] == info["integration"]
        for _ in range(100):  # the server notices the close a moment later
            _, info = await stack.get("/api/v1/info")
            if not info["integration"]["connected"]:
                break
            await asyncio.sleep(0.01)
        assert info["integration"]["connected"] is False


async def test_the_panels_own_feed_is_not_an_integration(stack: Stack) -> None:
    """The ingress port serves the same socket to the browser; it cannot join anything."""
    async with stack.http.ws_connect(f"{stack.ui}/api/v1/ws") as ws:
        await ws.receive_json()
        _, info = await stack.get("/api/v1/info")
        assert info["integration"]["connected"] is False
        await _hotspot(stack)
        _, join = await stack.post("/api/v1/cameras/hot/join")
        assert join["state"] == "manual" and join["reason"] == "integration_not_connected"


async def test_a_socket_that_fails_its_first_snapshot_is_not_left_paired(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Otherwise the panel would keep showing an integration that is gone."""

    async def broken() -> dict[str, Any]:
        raise RuntimeError("status failed")

    monkeypatch.setattr(stack.daemon, "status_with_rev", broken)
    headers = {**stack.h(), "X-Dashcam-Sync-Integration": "2026.11.0b2"}
    async with stack.http.ws_connect(f"{stack.api}/api/v1/ws", headers=headers) as ws:
        await ws.receive()  # the server closes it
    for _ in range(100):
        if not stack.daemon.integration_links:
            break
        await asyncio.sleep(0.01)
    assert stack.daemon.integration_links == {}
    assert stack.daemon.bus.subscribers == 0


async def test_join_results_are_refused_on_the_ingress_port(stack: Stack) -> None:
    await _hotspot(stack)
    async with stack.http.post(
        f"{stack.ui}/api/v1/cameras/hot/join-result", json={"state": "joined"}
    ) as r:
        assert r.status == 403


async def test_a_lan_camera_cannot_be_joined(stack: Stack) -> None:
    status, _ = await stack.post("/api/v1/cameras/car/join")
    assert status == 400


async def test_editing_a_camera_without_the_password_keeps_it(stack: Stack) -> None:
    await _hotspot(stack)
    body = {"id": "hot", "name": "Hot 2", "connection": "hotspot", "interface": "wlan0",
            "ssid": "CAM2", "wifi_password": "", "driver": "auto", "enabled": True}  # fmt: skip
    status, _ = await stack.post("/api/v1/wizard/camera", body)
    assert status == 200
    cam = stack.daemon.options.camera("hot")
    assert cam is not None and cam.ssid == "CAM2" and cam.wifi_password == "pw-1"


async def test_wifi_interfaces_are_listed_for_the_wizard(stack: Stack) -> None:
    from dashcam_sync.network import Interface

    stack.daemon.network.interfaces = [
        Interface("enp2s0", "ethernet", True, True, True),
        Interface("wlp4s0", "wireless", True, False, False),
    ]
    await _hotspot(stack)
    stack.daemon.network.interfaces.append(
        Interface("wlan0", "wireless", True, True, False, ssid="CAM")
    )
    _, r = await stack.get("/api/v1/wizard/interfaces")
    names = {i["name"]: i for i in r["wireless"]}
    assert set(names) == {"wlp4s0", "wlan0"}  # the wired interface is not offered
    assert names["wlan0"]["in_use_by"] == "hot" and names["wlp4s0"]["in_use_by"] is None


# --------------------------------------------------------------------------- settings


async def test_settings_change_only_the_keys_given(stack: Stack) -> None:
    _, before = await stack.get("/api/v1/settings")
    status, after = await _send(
        stack,
        "PATCH",
        "/api/v1/settings",
        {"sync": {"mode": "events_only"}, "retention": {"normal_days": 3, "purge_time": "04:15"}},
    )
    assert status == 200, after
    assert after["sync"]["mode"] == "events_only" and after["retention"]["normal_days"] == 3
    assert after["retention"]["purge_time"] == "04:15"
    assert after["sync"]["recent_minutes"] == before["sync"]["recent_minutes"]
    assert "migration" not in after  # the location did not change
    raw = json.loads(stack.options_path.read_text())
    assert raw["sync"]["mode"] == "events_only" and raw["retention"]["normal_days"] == 3
    assert raw["cameras"][0]["id"] == "car"  # nothing else touched


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ({"retention": {"min_free_percent": 95}}, "retention.min_free_percent"),
        ({"retention": {"normal_days": -1}}, "retention.normal_days"),
        ({"retention": {"purge_time": "25:00"}}, "retention.purge_time"),
        ({"sync": {"mode": "sometimes"}}, "sync.mode"),
        ({"sync": {"bogus": 1}}, "unknown sync settings"),
        ({"cameras": []}, "cannot change ['cameras']"),
        ({"storage": {"target": "network"}}, "storage.network_folder"),
        ({"sync": "fast"}, "sync must be an object"),
    ],
)
async def test_bad_settings_are_refused_with_the_field_named(
    stack: Stack, body: dict, message: str
) -> None:
    status, err = await _send(stack, "PATCH", "/api/v1/settings", body)
    assert status == 400 and message in err["error"], err


async def test_a_location_change_in_settings_returns_the_move_plan(stack: Stack) -> None:
    await synced(stack)
    status, after = await _send(
        stack, "PATCH", "/api/v1/settings", {"storage": {"local_folder": "elsewhere"}}
    )
    assert status == 200 and after["migration"]["plan"]["clips"] == 2


async def _supervised_daemon(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, url: str) -> Daemon:
    from dashcam_sync import supervisor as sup_mod

    monkeypatch.setenv("SUPERVISOR_TOKEN", "sup-token")
    monkeypatch.setattr(sup_mod, "SUPERVISOR_URL", url)
    monkeypatch.setattr(daemon_mod, "OPTIONS_POLL", 0.05)
    monkeypatch.setattr(daemon_mod, "OPTIONS_SYNC", 0.05)
    real_init = SupervisorClient.__init__

    def init(self: SupervisorClient, session: Any, token: Any = None, base_url: str = url) -> None:
        real_init(self, session, token, url)

    monkeypatch.setattr(SupervisorClient, "__init__", init)
    data = tmp_path / "d"
    data.mkdir()
    (data / "options.json").write_text(json.dumps({"cameras": []}))
    daemon = Daemon(
        DaemonConfig(
            data_dir=data,
            media_root=tmp_path / "media",
            share_root=tmp_path / "share",
            api_port=0,
            ui_port=0,
            bind_host="127.0.0.1",
            api_token=TOKEN,
        )
    )
    await daemon.start()
    return daemon


async def test_panel_edits_apply_at_once_under_the_real_supervisor(
    fake_supervisor: tuple[str, list[tuple[str, str, Any]]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The wizard saved a camera, then answered 'unknown camera' on a real system."""
    url, _ = fake_supervisor
    daemon = await _supervised_daemon(tmp_path, monkeypatch, url)
    try:
        api = f"http://127.0.0.1:{daemon.bound_port(0)}"
        headers = {"Authorization": f"Bearer {TOKEN}"}
        body = {"id": "hot", "name": "Hot", "connection": "hotspot", "interface": "wlan0",
                "ssid": "CAM", "wifi_password": "pw", "driver": "auto", "enabled": True}  # fmt: skip
        async with aiohttp.ClientSession(headers=headers) as http:
            async with http.post(f"{api}/api/v1/wizard/camera", json=body) as r:
                assert r.status == 200
            async with http.post(f"{api}/api/v1/cameras/hot/join") as r:
                assert r.status == 200, await r.text()  # used to be 404 "unknown camera"
            async with http.patch(
                f"{api}/api/v1/settings", json={"sync": {"mode": "events_only"}}
            ) as r:
                assert r.status == 200
        assert daemon.options.camera("hot") is not None
        assert daemon.options.sync.mode == "events_only"
    finally:
        await daemon.stop()


async def test_configuration_tab_edits_apply_without_a_restart(
    fake_supervisor: tuple[str, list[tuple[str, str, Any]]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url, _ = fake_supervisor
    daemon = await _supervised_daemon(tmp_path, monkeypatch, url)
    try:
        async with aiohttp.ClientSession(headers={"Authorization": "Bearer sup-token"}) as http:
            async with http.post(
                f"{url}/test/configuration-tab",
                json={"cameras": [], "sync": {"mode": "everything"}},
            ):
                pass
        await wait_for(lambda: daemon.options.sync.mode == "everything")
    finally:
        await daemon.stop()


# ------------------------------------------------------ Wi-Fi scan through Home Assistant


async def test_a_scan_without_the_integration_says_why(stack: Stack) -> None:
    _, r = await stack.get("/api/v1/wizard/wifi-scan?interface=wlp4s0")
    assert r == {"available": False, "accesspoints": [], "reason": "integration_not_connected"}


async def test_a_scan_is_answered_through_home_assistant(stack: Stack) -> None:
    events = stack.daemon.bus.subscribe()  # stands in for the integration's socket
    stack.daemon.integration_links[0] = None  # an integration is on the token port

    async def integration() -> None:
        while True:
            event = await events.get()
            if event["type"] == "scan_wifi":
                break
        assert event["interface"] == "wlp4s0"
        status, _ = await stack.post(
            "/api/v1/wizard/wifi-scan-result",
            {
                "request": event["request"],
                "accesspoints": [
                    {"ssid": "Weak", "signal": 20},
                    {"ssid": "CARDV-1234", "signal": 62, "mode": "infrastructure"},
                    {"ssid": "CARDV-1234", "signal": 40},  # the same network on another band
                    {"ssid": ""},
                    "junk",
                ],
            },
        )
        assert status == 200

    task = asyncio.create_task(integration())
    _, r = await stack.get("/api/v1/wizard/wifi-scan?interface=wlp4s0")
    await task
    assert r["available"] and r["via"] == "home_assistant"
    assert [a["ssid"] for a in r["accesspoints"]] == ["CARDV-1234", "Weak"]  # strongest first
    assert r["accesspoints"][0]["signal"] == 62


async def test_an_unanswered_scan_times_out_cleanly(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    stack.daemon.bus.subscribe()
    stack.daemon.integration_links[0] = None  # an integration is on the token port
    monkeypatch.setattr(daemon_mod, "SCAN_TIMEOUT", 0.1)
    _, r = await stack.get("/api/v1/wizard/wifi-scan?interface=wlp4s0")
    assert r["available"] is False and r["reason"] == "no_answer"
    assert stack.daemon.scans == {}  # nothing left waiting


async def test_scan_results_are_only_accepted_from_home_assistant(stack: Stack) -> None:
    async with stack.http.post(
        f"{stack.ui}/api/v1/wizard/wifi-scan-result", json={"request": "x"}
    ) as r:
        assert r.status == 403
    status, _ = await stack.post("/api/v1/wizard/wifi-scan-result", {"request": "nobody"})
    assert status == 404
    status, _ = await stack.post("/api/v1/wizard/wifi-scan-result", {})
    assert status == 400


async def test_a_refused_scan_from_home_assistant_is_reported(stack: Stack) -> None:
    events = stack.daemon.bus.subscribe()
    stack.daemon.integration_links[0] = None  # an integration is on the token port

    async def integration() -> None:
        while (event := await events.get())["type"] != "scan_wifi":
            pass
        await stack.post(
            "/api/v1/wizard/wifi-scan-result",
            {"request": event["request"], "error": "scan failed: busy"},
        )

    task = asyncio.create_task(integration())
    _, r = await stack.get("/api/v1/wizard/wifi-scan?interface=wlp4s0")
    await task
    assert r == {"available": False, "accesspoints": [], "reason": "scan failed: busy"}


async def test_join_status_shows_the_interface_state(stack: Stack) -> None:
    from dashcam_sync.network import Interface

    await _hotspot(stack)
    stack.daemon.network.interfaces = [
        Interface("wlan0", "wireless", True, True, False, ssid="CAM")
    ]
    _, j = await stack.get("/api/v1/cameras/hot/join")
    assert j["interface"] == {"enabled": True, "connected": True, "ssid": "CAM"}


async def test_the_panel_can_never_run_a_stale_script(stack: Stack) -> None:
    """The panel lives in an iframe whose files browsers kept cached across updates."""
    import re

    async with stack.http.get(f"{stack.ui}/") as r:
        assert r.status == 200 and r.headers["Cache-Control"] == "no-cache"
        page = await r.text()
    script = re.search(r'src="(static/app\.js\?v=[0-9a-f]{12})"', page)
    style = re.search(r'href="(static/style\.css\?v=[0-9a-f]{12})"', page)
    assert script and style, page
    async with stack.http.get(f"{stack.ui}/{script[1]}") as r:
        assert r.status == 200 and "immutable" in r.headers["Cache-Control"]
    async with stack.http.get(f"{stack.ui}/static/app.js") as r:
        assert r.headers["Cache-Control"] == "no-cache"  # an old unversioned URL revalidates

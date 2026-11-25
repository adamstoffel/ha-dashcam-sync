from __future__ import annotations

import aiohttp
import pytest
import pytest_asyncio

from dashcam_sync.detect import candidate_hosts, detect, fingerprint, split_host
from dashcam_sync.drivers import DRIVERS, HttpIndexDriver, NovatekDriver, ViidureDriver
from dashcam_sync.drivers.base import classify, timestamp_from_name
from dashcam_sync.drivers.http import CameraHttp
from dashcam_sync.errors import LinkLost, ProtocolError, UnsupportedCamera
from dashcam_sync.models import Channel, Kind, safe_extension
from fakecam import FakeCamera, make_bytes


async def read_all(stream) -> bytes:  # type: ignore[no-untyped-def]
    out = b""
    async for chunk in stream:
        out += chunk
    return out


def test_classify_maps_vendor_names_onto_kinds() -> None:
    assert classify("A:\\CARDV\\MOVIE\\RO\\x_F.MP4")[0] is Kind.EVENT
    assert classify("/DCIM/emr/a.ts")[2] is True
    assert classify("/DCIM/loop/a.ts") == (Kind.NORMAL, Channel.FRONT, False)
    assert classify("/DCIM/park/a_R.ts")[:2] == (Kind.PARKING, Channel.REAR)
    assert classify("/x/PHOTO/p.jpg")[0] is Kind.PHOTO
    assert classify("/x/20261122_101500_I.mp4")[1] is Channel.INTERIOR
    assert classify("/x/20261122_101500_R.mp4")[1] is Channel.REAR


def test_timestamp_from_name_and_extension() -> None:
    assert timestamp_from_name("20261122_101500_F.MP4") is not None
    assert timestamp_from_name("2026_1122_101500_001.MP4") is not None
    assert timestamp_from_name("nothing") is None
    assert timestamp_from_name("20269999_999999") is None
    assert safe_extension("a/b/C.MP4") == "mp4"
    assert safe_extension("x.exe") == "mp4" and safe_extension("noext") == "mp4"
    assert safe_extension("A:\\x\\y.TS") == "ts"


@pytest_asyncio.fixture
async def nova(session: aiohttp.ClientSession, novatek_cam: FakeCamera):
    novatek_cam.add_novatek("20261122_101500_F.MP4", 70_000)
    novatek_cam.add_novatek("20261122_101600_F.MP4", 40_000, folder="MOVIE\\RO", attr=33)
    drv = NovatekDriver(CameraHttp(session))
    probe = await drv.probe(novatek_cam.host)
    assert probe is not None
    return drv, novatek_cam


async def test_novatek_probe_and_info(nova) -> None:  # type: ignore[no-untyped-def]
    drv, cam = nova
    probe = await drv.probe(cam.host)
    assert probe is not None and probe.driver == "novatek" and probe.firmware == "FW-1.2.3"
    info = await drv.info()
    assert info.free_bytes == 8_000_000_000 and info.firmware == "FW-1.2.3"
    assert drv.live_url() == "rtsp://127.0.0.1/xxx.mov"


async def test_novatek_listing_requires_playback(nova) -> None:  # type: ignore[no-untyped-def]
    drv, cam = nova
    with pytest.raises(ProtocolError):
        await drv.list_recordings()
    await drv.prepare()
    assert cam.mode == "playback" and not cam.recording
    recs = await drv.list_recordings()
    assert len(recs) == 2
    normal = next(r for r in recs if r.kind is Kind.NORMAL)
    assert normal.size == 70_000 and normal.channel is Channel.FRONT and not normal.locked
    event = next(r for r in recs if r.kind is not Kind.NORMAL)
    assert event.locked and event.size == 40_000


async def test_novatek_prepare_refused(nova) -> None:  # type: ignore[no-untyped-def]
    drv, cam = nova
    cam.faults.refuse_playback = True
    with pytest.raises(ProtocolError):
        await drv.prepare()


async def test_novatek_download_full_and_resume(nova) -> None:  # type: ignore[no-untyped-def]
    drv, cam = nova
    await drv.prepare()
    rec = next(r for r in await drv.list_recordings() if r.size == 70_000)
    expected = make_bytes("20261122_101500_F.MP4", 70_000)
    stream = await drv.open(rec, 0)
    assert stream.start == 0 and not stream.range_honored
    assert await read_all(stream) == expected
    stream = await drv.open(rec, 30_000)
    assert stream.start == 30_000 and stream.range_honored and stream.content_length == 40_000
    assert await read_all(stream) == expected[30_000:]
    cam.faults.ignore_range = True
    stream = await drv.open(rec, 30_000)
    assert stream.start == 0 and await read_all(stream) == expected
    await drv.close()


async def test_requests_are_serialised(nova) -> None:  # type: ignore[no-untyped-def]
    drv, cam = nova
    await drv.prepare()
    await drv.list_recordings()
    rec = (await drv.list_recordings())[0]
    stream = await drv.open(rec, 0)
    await read_all(stream)
    assert cam.max_concurrent == 1
    await drv.close()


async def test_novatek_drop_raises_link_lost(nova) -> None:  # type: ignore[no-untyped-def]
    drv, cam = nova
    await drv.prepare()
    rec = next(r for r in await drv.list_recordings() if r.size == 70_000)
    cam.faults.drop_after = 20_000
    stream = await drv.open(rec, 0)
    with pytest.raises(LinkLost):
        await read_all(stream)
    # The request lock was released with the dead stream.
    assert (await drv.probe(cam.host)) is not None
    await drv.close()


async def test_novatek_restore_is_idempotent_and_delete(nova) -> None:  # type: ignore[no-untyped-def]
    drv, cam = nova
    await drv.prepare()
    await drv.restore()
    await drv.restore()
    assert cam.mode == "movie" and cam.recording and cam.restore_calls == 2
    rec = await drv.list_recordings() if False else []  # noqa: F841
    await drv.prepare()
    first = (await drv.list_recordings())[0]
    await drv.delete(first)
    assert cam.deleted == [first.path]


def test_novatek_url_path() -> None:
    from conftest import at
    from dashcam_sync.models import Recording

    rec = Recording("A:\\CARDV\\MOVIE\\a b.MP4", 1, at(1), Kind.NORMAL)
    assert NovatekDriver.url_path(rec) == "/CARDV/MOVIE/a%20b.MP4"
    assert (
        ViidureDriver.url_path(Recording("/mnt/mmc/DCIM/loop/a.ts", 1, at(1), Kind.NORMAL))
        == "/DCIM/loop/a.ts"
    )


async def test_novatek_heartbeat_runs_during_download(
    session: aiohttp.ClientSession, novatek_cam: FakeCamera, monkeypatch: pytest.MonkeyPatch
) -> None:
    import dashcam_sync.drivers.novatek as mod

    monkeypatch.setattr(mod, "HEARTBEAT_INTERVAL", 0.01)
    novatek_cam.add_novatek("a.MP4", 1000)
    drv = NovatekDriver(CameraHttp(session))
    drv.attach(novatek_cam.host)
    await drv.prepare()
    rec = (await drv.list_recordings())[0]
    stream = await drv.open(rec, 0)
    import asyncio

    await asyncio.sleep(0.1)
    assert novatek_cam.heartbeats >= 2
    await read_all(stream)
    await drv.close()
    beats = novatek_cam.heartbeats
    await asyncio.sleep(0.05)
    assert novatek_cam.heartbeats == beats


async def test_viidure_driver(session: aiohttp.ClientSession) -> None:
    cam = FakeCamera("viidure")
    await cam.start()
    try:
        cam.add_viidure("loop", "20261122_101500_F.ts", 5000, 1_795_000_000)
        cam.add_viidure("emr", "20261122_101700_F.ts", 3000, 1_795_000_100)
        cam.add_viidure("photo", "p.jpg", 100, 1_795_000_200)
        drv = ViidureDriver(CameraHttp(session))
        probe = await drv.probe(cam.host)
        assert probe is not None and probe.model == "V-100"
        info = await drv.info()
        assert info.firmware == "1.0.9" and info.free_bytes == 5_000_000
        await drv.prepare()
        assert cam.mode == "playback"
        recs = await drv.list_recordings()
        assert sorted(r.kind.value for r in recs) == ["event", "normal", "photo"]
        assert next(r for r in recs if r.kind is Kind.EVENT).locked
        rec = next(r for r in recs if r.kind is Kind.NORMAL)
        assert await read_all(await drv.open(rec, 0)) == make_bytes("20261122_101500_F.ts", 5000)
        await drv.restore()
        assert cam.mode == "movie" and cam.recording
        assert drv.live_url() == "rtsp://127.0.0.1:554/live"
    finally:
        await cam.stop()


async def test_viidure_pagination(session: aiohttp.ClientSession) -> None:
    cam = FakeCamera("viidure")
    await cam.start()
    try:
        for i in range(130):
            cam.add_viidure(
                "loop", f"20261122_10{i // 60:02d}{i % 60:02d}_F.ts", 10, 1_795_000_000 + i
            )
        drv = ViidureDriver(CameraHttp(session))
        drv.attach(cam.host)
        assert len(await drv.list_recordings()) == 130
    finally:
        await cam.stop()


async def test_http_index_driver(session: aiohttp.ClientSession) -> None:
    cam = FakeCamera("index")
    await cam.start()
    try:
        cam.add_index("record", "20261122_101500_F.mp4", 2000)
        cam.add_index("record/event", "20261122_101600_F.mp4", 1000)
        drv = HttpIndexDriver(CameraHttp(session))
        probe = await drv.probe(cam.host)
        assert probe is not None and probe.driver == "http_index"
        recs = await drv.list_recordings()
        assert len(recs) == 2
        kinds = {r.path: r.kind for r in recs}
        assert kinds["/record/event/20261122_101600_F.mp4"] is Kind.EVENT
        assert all(r.size > 0 for r in recs)
        rec = recs[0]
        data = await read_all(await drv.open(rec, 0))
        assert len(data) in (2000, 1000)
        await drv.prepare()
        await drv.restore()
        assert (await drv.info()).sd_state == "ready"
    finally:
        await cam.stop()


async def test_detection_order_and_cache(
    session: aiohttp.ClientSession, novatek_cam: FakeCamera
) -> None:
    det = await detect(session, [novatek_cam.host])
    assert det.probe.driver == "novatek"
    assert det.probe.fingerprint["ports"]
    novatek_cam.requests.clear()
    cached = await detect(
        session, [novatek_cam.host], cached={"driver": "novatek", "host": novatek_cam.host}
    )
    assert cached.probe.driver == "novatek"
    # A cached driver that stops matching falls back to full detection.
    novatek_cam.family = "index"
    novatek_cam.add_index("rec", "a.mp4", 10)
    redetected = await detect(
        session, [novatek_cam.host], cached={"driver": "novatek", "host": novatek_cam.host}
    )
    assert redetected.probe.driver == "http_index"


async def test_forced_driver_and_unsupported(
    session: aiohttp.ClientSession, novatek_cam: FakeCamera
) -> None:
    with pytest.raises(UnsupportedCamera):
        await detect(session, [novatek_cam.host], forced="viidure_app")
    with pytest.raises(UnsupportedCamera):
        await detect(session, ["127.0.0.1:1"])  # nothing listens
    det = await detect(session, [novatek_cam.host], forced="novatek")
    assert det.driver.name == "novatek"


async def test_fingerprint_and_hosts() -> None:
    assert split_host("1.2.3.4:80") == ("1.2.3.4", 80)
    assert split_host("cam.local") == ("cam.local", None)
    assert candidate_hosts("10.1.1.1") == ["10.1.1.1"]
    hosts = candidate_hosts(None, "192.168.1.1")
    assert hosts[0] == "192.168.1.1" and "192.168.1.254" in hosts and "192.168.169.1" in hosts
    assert candidate_hosts(None, None, ["viidure_app"]) == ["192.168.169.1"]
    assert await fingerprint("127.0.0.1:1") == {1: False}
    assert [c.name for c in DRIVERS] == ["novatek", "viidure_app", "http_index"]


async def test_body_cap_and_bad_xml(
    session: aiohttp.ClientSession, novatek_cam: FakeCamera
) -> None:
    from aiohttp import web

    async def big(request: web.Request) -> web.StreamResponse:
        return web.Response(body=b"x" * (5 * 1024 * 1024))

    async def xxe(request: web.Request) -> web.StreamResponse:
        return web.Response(
            text='<?xml version="1.0"?><!DOCTYPE a [<!ENTITY e SYSTEM "file:///etc/passwd">]><a>&e;</a>'
        )

    app = web.Application()
    app.router.add_get("/big", big)
    app.router.add_get("/xxe", xxe)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    try:
        http = CameraHttp(session, f"127.0.0.1:{port}")
        with pytest.raises(ProtocolError):
            await http.get("/big")
        with pytest.raises(ProtocolError):
            await http.get_xml("/xxe")
        with pytest.raises(ProtocolError):
            await http.get_json("/xxe")
        with pytest.raises(ProtocolError):
            await http.get("/missing")
    finally:
        await runner.cleanup()

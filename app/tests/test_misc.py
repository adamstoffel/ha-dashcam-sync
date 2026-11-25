from __future__ import annotations

import asyncio
from pathlib import Path

from dashcam_sync.events import EventBus
from dashcam_sync.models import Caps, DeviceInfo, Recording, utc
from dashcam_sync.netblock import tcp_probe
from dashcam_sync.restoreflag import RestoreFlags


async def test_tcp_probe_treats_accept_and_refuse_as_answering() -> None:
    server = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        assert await tcp_probe("127.0.0.1", ports=(port,), timeout=0.5)
    finally:
        server.close()
        await server.wait_closed()
    # Closed port: the kernel refuses, which still proves a host is there.
    assert await tcp_probe("127.0.0.1", ports=(port,), timeout=0.5)


def test_event_bus_drops_oldest_when_a_subscriber_is_slow() -> None:
    bus = EventBus(maxsize=2)
    queue = bus.subscribe()
    assert bus.subscribers == 1
    for i in range(5):
        bus.publish("x", n=i)
    assert [queue.get_nowait()["n"] for _ in range(queue.qsize())] == [3, 4]
    bus.unsubscribe(queue)
    assert bus.subscribers == 0
    bus.publish("x")  # no subscribers: fine


def test_restore_flags_roundtrip(tmp_path: Path) -> None:
    flags = RestoreFlags(tmp_path / "r")
    assert flags.get("car") is None and flags.pending() == []
    flags.set("car", "novatek", "192.168.1.254")
    got = flags.get("car")
    assert got is not None and got["driver"] == "novatek"
    assert flags.pending() == ["car"]
    (tmp_path / "r" / "bad.json").write_text("{nope")
    assert flags.get("bad") is None
    flags.clear("car")
    flags.clear("car")  # idempotent
    assert flags.get("car") is None


def test_small_models() -> None:
    assert Caps(delete=True).as_dict()["delete"] is True
    assert DeviceInfo(free_bytes=5).as_dict()["free_bytes"] == 5
    rec = Recording(
        "/a.ts", 1, utc(0), __import__("dashcam_sync.models", fromlist=["Kind"]).Kind.PHOTO
    )
    assert rec.is_photo and rec.extension == "ts"

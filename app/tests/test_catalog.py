from __future__ import annotations

from pathlib import Path

from conftest import at
from dashcam_sync.catalog import TARGET_LOCAL, Catalog
from dashcam_sync.models import Channel, Kind, Recording


def rec(name: str = "a", size: int = 10, hour: int = 10, kind: Kind = Kind.NORMAL) -> Recording:
    return Recording(f"/{name}.mp4", size, at(hour), kind, Channel.FRONT, locked=kind is Kind.EVENT)


def test_upsert_is_idempotent(catalog: Catalog) -> None:
    row, new = catalog.upsert_recording("car", rec())
    row2, new2 = catalog.upsert_recording("car", rec())
    assert new and not new2 and row.id == row2.id
    other, new3 = catalog.upsert_recording("car", rec(size=11))
    assert new3 and other.id != row.id  # size is part of the key


def test_progress_and_completion(catalog: Catalog) -> None:
    row, _ = catalog.upsert_recording("car", rec())
    assert not catalog.is_known_done("car", rec())
    catalog.set_offset(row.id, 4096, "/m/.partial/1.part")
    got = catalog.get_clip(row.id)
    assert got is not None and got.offset == 4096 and got.status == "downloading"
    assert got.part_path == "/m/.partial/1.part"
    catalog.set_offset(row.id, 8192)
    assert [c.id for c in catalog.unfinished("car")] == [row.id]
    catalog.complete_clip(row.id, sha256="abc", target=TARGET_LOCAL, path="/m/a.mp4", size=10)
    assert catalog.is_known_done("car", rec())
    done = catalog.get_clip(row.id)
    assert done is not None and done.sha256 == "abc" and done.part_path is None
    assert catalog.target_bytes(TARGET_LOCAL) == 10
    assert catalog.copy(row.id, TARGET_LOCAL) is not None
    assert catalog.unfinished("car") == []


def test_fail_and_reset(catalog: Catalog) -> None:
    row, _ = catalog.upsert_recording("car", rec())
    catalog.set_offset(row.id, 5, "/x")
    catalog.mark_failed(row.id, "boom")
    failed = catalog.get_clip(row.id)
    assert failed is not None and failed.status == "failed" and failed.error == "boom"
    catalog.reset_clip(row.id)
    reset = catalog.get_clip(row.id)
    assert reset is not None and reset.status == "queued" and reset.offset == 0


def test_listing_filters_and_days(catalog: Catalog) -> None:
    for i, (kind, hour) in enumerate([(Kind.EVENT, 9), (Kind.NORMAL, 10), (Kind.NORMAL, 11)]):
        row, _ = catalog.upsert_recording("car", rec(f"c{i}", 10 + i, hour, kind))
        catalog.complete_clip(row.id, sha256="s", target=TARGET_LOCAL, path=f"/p{i}", size=10 + i)
    catalog.upsert_recording("other", rec("o"))  # queued, not done
    assert [c.start.hour for c in catalog.list_clips(camera="car")] == [11, 10, 9]
    assert len(catalog.list_clips(camera="car", kind=Kind.EVENT)) == 1
    assert len(catalog.list_clips(camera="car", day="2026-11-22")) == 3
    assert catalog.list_clips(camera="car", day="2026-11-21") == []
    assert catalog.clip_days("car") == ["2026-11-22"]
    latest = catalog.latest_clip("car", Kind.EVENT)
    assert latest is not None and latest.kind is Kind.EVENT
    assert len(catalog.list_clips(status=None)) == 4
    assert catalog.list_clips(camera="car", limit=1, offset=1)[0].start.hour == 10


def test_keep_and_delete(catalog: Catalog) -> None:
    row, _ = catalog.upsert_recording("car", rec())
    assert catalog.set_keep(row.id, True)
    clip = catalog.get_clip(row.id)
    assert clip is not None and clip.keep and clip.as_dict()["keep"] is True
    assert not catalog.set_keep(999, True)
    catalog.add_copy(row.id, "local", "/a", 1)
    catalog.delete_clip(row.id)  # forgets the files, keeps a tombstone
    assert catalog.copies(row.id) == []
    tomb = catalog.get_clip(row.id)
    assert tomb is not None and tomb.status == "deleted" and not tomb.keep
    assert catalog.tombstones("car") == 1 and catalog.tombstones() == 1
    assert catalog.tombstones("other") == 0


def test_sessions_and_failure_streak(catalog: Catalog) -> None:
    for result in ("ok", "list_failed", "list_failed", "list_failed"):
        sid = catalog.start_session("car", "novatek")
        catalog.end_session(sid, result, 0, 0, "e" if result != "ok" else None)
    open_sid = catalog.start_session("car")  # unfinished sessions are ignored
    assert open_sid > 0
    assert catalog.consecutive_sessions_with("car", "list_failed") == 3
    assert catalog.consecutive_sessions_with("car", "ok") == 0
    assert len(catalog.recent_sessions(2)) == 2
    assert catalog.recent_sessions(10, "none") == []


def test_kv(catalog: Catalog) -> None:
    assert catalog.kv_get("x", 5) == 5
    catalog.kv_set("x", {"a": 1})
    assert catalog.kv_get("x") == {"a": 1}


def test_file_backed_catalog_survives_reopen(tmp_path: Path) -> None:
    path = tmp_path / "d" / "catalog.db"
    cat = Catalog(path)
    row, _ = cat.upsert_recording("car", rec())
    cat.close()
    again = Catalog(path)
    assert again.get_clip(row.id) is not None
    again.close()


def test_a_downloaded_clip_gets_its_type_corrected_and_its_verified_size() -> None:
    import datetime as dt

    from dashcam_sync.catalog import Catalog
    from dashcam_sync.models import Kind, Recording

    cat = Catalog(":memory:")
    start = dt.datetime(2026, 11, 25, 10, 23, 15, tzinfo=dt.UTC)
    listed = Recording("EMMC/Photo/F/20261125182315.jpg", 0, start, Kind.EVENT, approx_size=73400)
    row, _ = cat.upsert_recording("car", listed)
    cat.complete_clip(row.id, sha256="0" * 64, target="local", path="/m/x.jpg", size=325210)
    assert cat.get_clip(row.id).size == 325210  # the verified size, not the listed one
    assert cat.is_known_done("car", listed)
    later = start + dt.timedelta(hours=16)  # the time read from the file name
    fixed = Recording("EMMC/Photo/F/20261125182315.jpg", 0, later, Kind.PHOTO)
    assert cat.is_known_done("car", fixed)  # a corrected time does not make it new
    cat.refresh_listing("car", fixed)
    clip = cat.get_clip(row.id)
    assert clip.kind == "photo" and not clip.locked
    assert clip.start == later
    cat.close()

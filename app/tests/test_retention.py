from __future__ import annotations

import datetime as dt
from pathlib import Path

from conftest import at
from dashcam_sync.catalog import TARGET_LOCAL, Catalog, ClipRow, CopyRow
from dashcam_sync.models import Channel, Kind, Recording
from dashcam_sync.options import RetentionOptions, StorageOptions
from dashcam_sync.retention import (
    RetentionService,
    Rules,
    plan_purge,
    rules_for,
    seconds_until,
)
from dashcam_sync.storage import StorageManager

NOW = dt.datetime(2026, 12, 25, 12, 0, tzinfo=dt.UTC)
GB = 1024**3


def item(i: int, kind: Kind, age_days: float, size: int = 100, keep: bool = False):
    start = NOW - dt.timedelta(days=age_days)
    clip = ClipRow(
        i,
        "car",
        f"/p{i}",
        size,
        start,
        kind,
        Channel.FRONT,
        False,
        None,
        "done",
        size,
        None,
        "x",
        keep,
        "ok",
        None,
        0.0,
        0.0,
    )
    return clip, CopyRow(i, TARGET_LOCAL, f"/f{i}", size, "present", 0.0)


RULES = Rules(
    normal_days=14, parking_days=30, event_days=0, photo_days=90, max_total_gb=0, min_free_percent=0
)


def ids(plan):
    return sorted(c.clip.id for c in plan)


def test_age_rules_per_kind() -> None:
    items = [
        item(1, Kind.NORMAL, 15),
        item(2, Kind.NORMAL, 13),
        item(3, Kind.PARKING, 31),
        item(4, Kind.PARKING, 29),
        item(5, Kind.EVENT, 1000),  # 0 = never expires
        item(6, Kind.PHOTO, 91),
        item(7, Kind.PHOTO, 89),
    ]
    plan = plan_purge(items, RULES, now=NOW, free_bytes=10**12, total_bytes=10**12)
    assert ids(plan) == [1, 3, 6]
    assert all(c.reason.startswith("older_than") for c in plan)


def test_keep_is_never_deleted_by_any_rule() -> None:
    items = [item(1, Kind.NORMAL, 99, keep=True), item(2, Kind.NORMAL, 99)]
    rules = Rules(14, 30, 0, 90, 0, 90)
    plan = plan_purge(items, rules, now=NOW, free_bytes=0, total_bytes=1000)
    assert ids(plan) == [2]


def test_size_cap_deletes_normal_then_parking_then_photo_then_event_oldest_first() -> None:
    items = [
        item(1, Kind.EVENT, 5, GB),
        item(2, Kind.NORMAL, 3, GB),
        item(3, Kind.NORMAL, 4, GB),
        item(4, Kind.PARKING, 2, GB),
        item(5, Kind.PHOTO, 1, GB),
    ]
    rules = Rules(0, 0, 0, 0, max_total_gb=3, min_free_percent=0)
    plan = plan_purge(items, rules, now=NOW, free_bytes=10**15, total_bytes=10**15)
    assert ids(plan) == [2, 3]  # 5 GB -> 3 GB: the two normal clips, oldest (id 3) first
    rules = Rules(0, 0, 0, 0, max_total_gb=1, min_free_percent=0)
    plan = plan_purge(items, rules, now=NOW, free_bytes=10**15, total_bytes=10**15)
    assert ids(plan) == [2, 3, 4, 5]  # events are the last resort, and 1 GB is still allowed


def test_event_is_deleted_only_when_still_needed() -> None:
    items = [item(1, Kind.EVENT, 5, GB), item(2, Kind.NORMAL, 3, GB)]
    rules = Rules(0, 0, 0, 0, max_total_gb=0, min_free_percent=50)
    plan = plan_purge(items, rules, now=NOW, free_bytes=0, total_bytes=int(3.5 * GB))
    assert ids(plan) == [1, 2]
    plan = plan_purge(items, rules, now=NOW, free_bytes=0, total_bytes=int(1.5 * GB))
    assert ids(plan) == [2]


def test_free_space_floor_accounts_for_age_deletions() -> None:
    items = [item(1, Kind.NORMAL, 20, 400), item(2, Kind.NORMAL, 1, 400)]
    rules = Rules(14, 0, 0, 0, 0, min_free_percent=40)
    # 1000 total, 100 free; deleting the old clip frees 400 => 50% free, enough.
    plan = plan_purge(items, rules, now=NOW, free_bytes=100, total_bytes=1000)
    assert ids(plan) == [1]


def test_rules_for_picks_network_twins() -> None:
    opts = RetentionOptions()
    assert rules_for(opts, "local").normal_days == 14
    assert rules_for(opts, "network").normal_days == 90
    assert rules_for(opts, "network").min_free_percent == 5
    assert rules_for(opts, "network").max_total_gb == 0


def make_service(tmp_path: Path, catalog: Catalog, **ret):
    media = tmp_path / "media"
    media.mkdir(exist_ok=True)
    storage = StorageManager(
        catalog,
        StorageOptions(),
        media_root=media,
        share_root=tmp_path / "share",
        min_free_percent=0,
    )
    opts = RetentionOptions(**{"min_free_percent": 0, "max_total_gb": 0, **ret})
    return RetentionService(catalog, storage, opts), storage


def add_clip(catalog: Catalog, storage: StorageManager, name: str, kind: Kind, start: dt.datetime):
    rec = Recording(f"/{name}.mp4", 10, start, kind)
    row, _ = catalog.upsert_recording("car", rec)
    path = storage.local.root / "car" / f"{name}.mp4"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * 10)
    path.with_suffix(".jpg").write_bytes(b"j")
    catalog.complete_clip(row.id, sha256="s", target=TARGET_LOCAL, path=str(path), size=10)
    return row, path


def test_service_dry_run_then_real_purge(tmp_path: Path, catalog: Catalog) -> None:
    svc, storage = make_service(tmp_path, catalog, normal_days=14)
    old_row, old_path = add_clip(catalog, storage, "old", Kind.NORMAL, at(1, day=1))
    new_row, new_path = add_clip(catalog, storage, "new", Kind.NORMAL, NOW)
    now = NOW + dt.timedelta(days=1)

    report = svc.run(dry_run=True, now=now)
    assert report.dry_run and len(report.deleted) == 1
    assert old_path.exists()

    report = svc.run(now=now)
    assert [c.clip.id for c in report.deleted] == [old_row.id]
    assert not old_path.exists()
    assert not old_path.with_suffix(".jpg").exists()
    assert catalog.get_clip(old_row.id).status == "deleted"  # type: ignore[union-attr]
    assert new_path.exists() and catalog.get_clip(new_row.id) is not None
    assert report.as_dict()["count"] == 1


def test_delete_clip_everywhere(tmp_path: Path, catalog: Catalog) -> None:
    svc, storage = make_service(tmp_path, catalog)
    row, path = add_clip(catalog, storage, "c", Kind.EVENT, NOW)
    assert svc.delete_clip_everywhere(row.id) == 11
    assert not path.exists()
    gone = catalog.get_clip(row.id)
    assert gone is not None and gone.status == "deleted"  # a tombstone, not a hole


def test_seconds_until() -> None:
    now = dt.datetime(2026, 1, 1, 3, 0, tzinfo=dt.UTC)
    assert seconds_until("03:30", now) == 1800
    assert seconds_until("02:30", now) == 23.5 * 3600

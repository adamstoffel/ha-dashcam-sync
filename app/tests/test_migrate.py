"""Moving clips after the storage location changes."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

import dashcam_sync.migrate as migrate
from conftest import at
from dashcam_sync.catalog import COPY_PENDING, TARGET_LOCAL, TARGET_NETWORK, Catalog
from dashcam_sync.models import Kind, Recording
from dashcam_sync.options import StorageOptions
from dashcam_sync.storage import StorageManager, clip_relative_path


def manager(tmp_path: Path, catalog: Catalog, *, disk: bool = True, free: int = 0, **kw):
    media, share = tmp_path / "media", tmp_path / "share"
    (media / "usb").mkdir(parents=True, exist_ok=True)
    share.mkdir(exist_ok=True)
    # The added disk is mounted at /media/usb (exFAT) when ``disk`` is true.
    mountinfo = f"1 2 179:1 / {media / 'usb'} rw - exfat /dev/sdb1 rw\n" if disk else ""
    return StorageManager(
        catalog,
        StorageOptions(**kw),
        media_root=media,
        share_root=share,
        min_free_percent=free,
        mountinfo=mountinfo,
    )


def make_clip(
    catalog: Catalog,
    root: Path,
    target: str,
    hour: int,
    *,
    kind: Kind = Kind.NORMAL,
    keep: bool = False,
    post: str = "done",
    data: bytes = b"video-bytes",
) -> tuple[int, Path]:
    rec = Recording(f"/CARDV/{hour}.MP4", len(data), at(hour), kind)
    row, _ = catalog.upsert_recording("car", rec)
    path = root / clip_relative_path("car", rec, "mp4")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    path.with_suffix(".jpg").write_bytes(b"thumb")
    path.with_suffix(".json").write_bytes(b"{}")
    catalog.complete_clip(row.id, sha256="s", target=target, path=str(path), size=len(data))
    catalog.set_post_status(row.id, post)
    if keep:
        catalog.set_keep(row.id, True)
    return row.id, path


@pytest.fixture
def separate_disk(monkeypatch: pytest.MonkeyPatch) -> None:
    """In tests every folder is on one file system; make the added disk a different one."""
    monkeypatch.setattr(migrate, "_same_filesystem", lambda src, root: False)


def run(catalog: Catalog, mgr: StorageManager) -> migrate.MigrationState:
    job = migrate.Migration()
    plan = migrate.plan_migration(catalog, mgr)
    job.begin(plan)
    return job.run(catalog, mgr, threading.Lock())


def test_renaming_the_local_folder_moves_clips_and_their_files(
    tmp_path: Path, catalog: Catalog
) -> None:
    old = tmp_path / "media" / "dashcam_sync"
    ids = [make_clip(catalog, old, TARGET_LOCAL, h, keep=h == 9)[0] for h in (8, 9)]
    mgr = manager(tmp_path, catalog, local_folder="dashcam")

    plan = migrate.plan_migration(catalog, mgr)
    assert plan.as_dict()["clips"] == 2 and plan.blocked is None
    assert plan.destinations == {TARGET_LOCAL: str(tmp_path / "media" / "dashcam")}

    state = run(catalog, mgr)
    assert state.status == "done" and state.clips_done == 2 and state.errors == 0
    for clip_id in ids:
        copy = catalog.copy(clip_id, TARGET_LOCAL)
        assert copy is not None and copy.path.startswith(str(tmp_path / "media" / "dashcam"))
        new = Path(copy.path)
        assert new.read_bytes() == b"video-bytes"
        assert new.with_suffix(".jpg").exists() and new.with_suffix(".json").exists()
    assert catalog.get_clip(ids[1]).keep  # type: ignore[union-attr]
    assert not old.exists() or not any(old.rglob("*.mp4"))  # emptied and tidied
    assert migrate.plan_migration(catalog, mgr).moves == []  # nothing left to do


def test_switching_to_an_added_disk_moves_clips_off_the_system_disk(
    tmp_path: Path, catalog: Catalog, separate_disk: None
) -> None:
    local = tmp_path / "media" / "dashcam_sync"
    clip_id, path = make_clip(catalog, local, TARGET_LOCAL, 8, kind=Kind.EVENT)
    mgr = manager(tmp_path, catalog, target="network", network_folder="usb")

    assert run(catalog, mgr).status == "done"
    assert catalog.copy(clip_id, TARGET_LOCAL) is None
    copy = catalog.copy(clip_id, TARGET_NETWORK)
    assert copy is not None and Path(copy.path).is_relative_to(tmp_path / "media" / "usb")
    rel = Path(copy.path).relative_to(tmp_path / "media" / "usb")
    assert rel == path.relative_to(local)  # same folder layout on the new disk
    assert not path.exists() and not path.with_suffix(".jpg").exists()


def test_switching_to_both_adds_a_copy_and_keeps_the_local_one(
    tmp_path: Path, catalog: Catalog
) -> None:
    clip_id, path = make_clip(catalog, tmp_path / "media" / "dashcam_sync", TARGET_LOCAL, 8)
    mgr = manager(tmp_path, catalog, target="both", network_folder="usb")

    plan = migrate.plan_migration(catalog, mgr)
    assert plan.moves[0].create == [TARGET_NETWORK] and plan.moves[0].drop == []
    assert run(catalog, mgr).status == "done"
    assert path.exists() and catalog.copy(clip_id, TARGET_LOCAL) is not None
    assert Path(catalog.copy(clip_id, TARGET_NETWORK).path).exists()  # type: ignore[union-attr]


def test_going_back_to_local_brings_clips_home_from_the_disk(
    tmp_path: Path, catalog: Catalog
) -> None:
    disk = tmp_path / "media" / "usb"
    clip_id, path = make_clip(catalog, disk, TARGET_NETWORK, 8)
    mgr = manager(tmp_path, catalog, target="local")

    assert run(catalog, mgr).status == "done"
    assert catalog.copy(clip_id, TARGET_NETWORK) is None
    local = catalog.copy(clip_id, TARGET_LOCAL)
    assert local is not None and Path(local.path).read_bytes() == b"video-bytes"
    assert not path.exists()


def test_an_unmounted_destination_blocks_the_move(tmp_path: Path, catalog: Catalog) -> None:
    make_clip(catalog, tmp_path / "media" / "dashcam_sync", TARGET_LOCAL, 8)
    mgr = manager(tmp_path, catalog, disk=False, target="network", network_folder="usb")
    plan = migrate.plan_migration(catalog, mgr)
    assert plan.moves and plan.blocked == "network_not_mounted"


def test_a_clip_whose_files_are_gone_is_left_alone(tmp_path: Path, catalog: Catalog) -> None:
    clip_id, path = make_clip(catalog, tmp_path / "media" / "dashcam_sync", TARGET_LOCAL, 8)
    path.unlink()
    mgr = manager(tmp_path, catalog, local_folder="dashcam")
    plan = migrate.plan_migration(catalog, mgr)
    assert plan.moves == [] and plan.unavailable == 1
    assert catalog.copy(clip_id, TARGET_LOCAL) is not None  # still tracked, never guessed away


def test_a_copy_on_an_unplugged_old_disk_stays_tracked(tmp_path: Path, catalog: Catalog) -> None:
    local = tmp_path / "media" / "dashcam_sync"
    clip_id, _ = make_clip(catalog, local, TARGET_LOCAL, 8)
    gone = tmp_path / "media" / "old_disk" / "car" / "x.mp4"
    catalog.add_copy(clip_id, TARGET_NETWORK, str(gone), 11)  # that disk is not attached
    mgr = manager(tmp_path, catalog, target="local")
    plan = migrate.plan_migration(catalog, mgr)
    assert plan.moves == [] and plan.stranded == 1
    assert catalog.copy(clip_id, TARGET_NETWORK) is not None


def test_clips_still_being_processed_wait_for_a_later_run(tmp_path: Path, catalog: Catalog) -> None:
    make_clip(catalog, tmp_path / "media" / "dashcam_sync", TARGET_LOCAL, 8, post="pending")
    plan = migrate.plan_migration(catalog, manager(tmp_path, catalog, local_folder="dashcam"))
    assert plan.moves == [] and plan.processing == 1


def test_a_queued_copy_for_a_target_no_longer_used_is_dropped(
    tmp_path: Path, catalog: Catalog
) -> None:
    clip_id, path = make_clip(catalog, tmp_path / "media" / "dashcam_sync", TARGET_LOCAL, 8)
    catalog.add_copy(clip_id, TARGET_NETWORK, "", 0, COPY_PENDING)  # left from "both"
    mgr = manager(tmp_path, catalog, target="local")
    assert run(catalog, mgr).status == "done"
    assert catalog.copy(clip_id, TARGET_NETWORK) is None and path.exists()


def test_an_interrupted_move_resumes_without_losing_or_duplicating_clips(
    tmp_path: Path, catalog: Catalog, monkeypatch: pytest.MonkeyPatch, separate_disk: None
) -> None:
    local = tmp_path / "media" / "dashcam_sync"
    clip_id, path = make_clip(catalog, local, TARGET_LOCAL, 8)
    mgr = manager(tmp_path, catalog, target="network", network_folder="usb")
    real = migrate._copy_verified

    def fail_on_the_clip(src: Path, dst: Path) -> None:
        if src.suffix == ".mp4":
            raise OSError("disk pulled out")
        real(src, dst)

    monkeypatch.setattr(migrate, "_copy_verified", fail_on_the_clip)
    state = run(catalog, mgr)
    assert state.errors == 1
    assert catalog.copy(clip_id, TARGET_LOCAL).path == str(path) and path.exists()  # type: ignore[union-attr]
    assert catalog.copy(clip_id, TARGET_NETWORK) is None

    monkeypatch.setattr(migrate, "_copy_verified", real)
    assert run(catalog, mgr).status == "done"
    copy = catalog.copy(clip_id, TARGET_NETWORK)
    assert copy is not None and not path.exists()
    assert not list((tmp_path / "media" / "usb").rglob("*_2.*"))  # no duplicate names


def test_a_copy_left_by_a_crash_after_writing_is_reused(tmp_path: Path, catalog: Catalog) -> None:
    local = tmp_path / "media" / "dashcam_sync"
    clip_id, path = make_clip(catalog, local, TARGET_LOCAL, 8)
    already = tmp_path / "media" / "usb" / path.relative_to(local)
    already.parent.mkdir(parents=True)
    already.write_bytes(path.read_bytes())  # written, but the catalogue was never updated
    mgr = manager(tmp_path, catalog, target="network", network_folder="usb")
    assert run(catalog, mgr).status == "done"
    assert catalog.copy(clip_id, TARGET_NETWORK).path == str(already)  # type: ignore[union-attr]


def test_a_full_destination_stops_the_move_and_keeps_the_originals(
    tmp_path: Path, catalog: Catalog, separate_disk: None
) -> None:
    clip_id, path = make_clip(catalog, tmp_path / "media" / "dashcam_sync", TARGET_LOCAL, 8)
    mgr = manager(tmp_path, catalog, free=100, target="network", network_folder="usb")
    state = run(catalog, mgr)
    assert state.status == "failed" and "need 100%" in (state.error or "")
    assert path.exists() and catalog.copy(clip_id, TARGET_LOCAL) is not None


def test_cancelling_stops_before_the_next_clip(tmp_path: Path, catalog: Catalog) -> None:
    make_clip(catalog, tmp_path / "media" / "dashcam_sync", TARGET_LOCAL, 8)
    mgr = manager(tmp_path, catalog, local_folder="dashcam")
    job = migrate.Migration()
    job.begin(migrate.plan_migration(catalog, mgr))
    job.cancel()
    state = job.run(catalog, mgr, threading.Lock())
    assert state.status == "cancelled" and state.clips_done == 0


def test_progress_is_reported_per_clip_and_at_the_end(tmp_path: Path, catalog: Catalog) -> None:
    for hour in (8, 9, 10):
        make_clip(catalog, tmp_path / "media" / "dashcam_sync", TARGET_LOCAL, hour)
    mgr = manager(tmp_path, catalog, local_folder="dashcam")
    job = migrate.Migration()
    job.begin(migrate.plan_migration(catalog, mgr))
    seen: list[tuple[str, int]] = []
    job.run(catalog, mgr, threading.Lock(), lambda s: seen.append((s.status, s.clips_done)))
    assert seen == [("running", 1), ("running", 2), ("running", 3), ("done", 3)]


def test_on_one_file_system_clips_are_renamed_not_copied(
    tmp_path: Path, catalog: Catalog, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_clip(catalog, tmp_path / "media" / "dashcam_sync", TARGET_LOCAL, 8)
    copies: list[Path] = []
    monkeypatch.setattr(migrate, "_copy_verified", lambda s, d: copies.append(s))
    # Renames need no free space, so even a "full" disk does not stop a folder rename.
    mgr = manager(tmp_path, catalog, free=100, local_folder="dashcam")
    assert run(catalog, mgr).status == "done" and copies == []


def test_layout_relative_recovers_the_folder_layout() -> None:
    deep = Path("/media/old/car/2026/11/22/event/20261122_100000_front.mp4")
    assert migrate.layout_relative(deep, "car") == Path(*deep.parts[-6:])
    assert migrate.layout_relative(Path("/x/odd.mp4"), "car") == Path("car/odd.mp4")

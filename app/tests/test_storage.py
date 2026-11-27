from __future__ import annotations

from pathlib import Path

import pytest

from conftest import at
from dashcam_sync.catalog import (
    COPY_PENDING,
    TARGET_LOCAL,
    TARGET_NETWORK,
    TARGET_SPOOL,
    Catalog,
)
from dashcam_sync.errors import StorageFull, TargetOffline
from dashcam_sync.models import Channel, Kind, Recording
from dashcam_sync.options import StorageOptions
from dashcam_sync.storage import (
    StorageManager,
    clip_relative_path,
    is_storage_mount,
    mount_fstype,
    move_file,
    remove_clip_files,
    unique_path,
)

MOUNTINFO = (
    "36 35 98:0 / /media rw - ext4 /dev/sda1 rw\n"
    "40 36 0:41 / /media/nas\\040box rw,relatime - cifs //nas/share rw\n"
    "41 36 0:42 / /media/nfsshare rw - nfs4 nas:/vol rw\n"
    "42 36 0:43 / /media/tmp rw - tmpfs tmpfs rw\n"
    "43 36 179:1 / /media/sdcard rw - exfat /dev/mmcblk0p1 rw\n"
    "44 36 8:17 / /share/usb_disk rw - ext4 /dev/sdb1 rw\n"
    "45 36 8:33 / /media/old_drive rw - fuseblk /dev/sdc1 rw\n"
)


def test_mount_detection() -> None:
    assert mount_fstype("/media/nas box", MOUNTINFO) == "cifs"
    assert is_storage_mount("/media/nfsshare/", MOUNTINFO)
    assert not is_storage_mount("/media/tmp", MOUNTINFO)
    assert not is_storage_mount("/media/missing", MOUNTINFO)
    assert mount_fstype("/media", "garbage line") is None


def test_local_disks_added_in_home_assistant_count_as_storage() -> None:
    assert is_storage_mount("/media/sdcard", MOUNTINFO)  # SD card, exFAT
    assert is_storage_mount("/share/usb_disk", MOUNTINFO)  # USB disk, ext4
    assert is_storage_mount("/media/old_drive", MOUNTINFO)  # NTFS through ntfs-3g
    # The data disk is ext4 too, but a missing disk's folder is not a mount point of its
    # own: it must never be taken for the disk.
    assert not is_storage_mount("/media/unplugged_sdcard", MOUNTINFO)
    assert not is_storage_mount("/media/sdcard/dashcam", MOUNTINFO)


def test_clip_relative_path_never_uses_camera_names() -> None:
    rec = Recording("A:\\CARDV\\..\\evil\\x.mp4", 1, at(10, 16), Kind.EVENT, Channel.REAR)
    rel = clip_relative_path("polestar_2", rec)
    assert str(rel) == "polestar_2/2026/11/22/event/20261122_101600_rear.mp4"
    weird = Recording("/x/y.exe", 1, at(1), Kind.NORMAL)
    assert clip_relative_path("c", weird).suffix == ".mp4"


def test_unique_path(tmp_path: Path) -> None:
    p = tmp_path / "a.mp4"
    assert unique_path(p) == p
    p.write_bytes(b"1")
    assert unique_path(p).name == "a_2.mp4"


def test_move_file_and_remove(tmp_path: Path) -> None:
    src = tmp_path / "s.part"
    src.write_bytes(b"abc")
    dst = tmp_path / "x" / "y" / "z.mp4"
    move_file(src, dst)
    assert dst.read_bytes() == b"abc" and not src.exists()
    dst.with_suffix(".jpg").write_bytes(b"j")
    assert remove_clip_files(dst) == 4
    assert not (tmp_path / "x").exists()  # empty parents are pruned


def manager(tmp_path: Path, catalog: Catalog, mountinfo: str | None = None, **kw):
    media, share = tmp_path / "media", tmp_path / "share"
    media.mkdir(exist_ok=True)
    share.mkdir(exist_ok=True)
    opts = StorageOptions(**kw)
    return StorageManager(
        catalog, opts, media_root=media, share_root=share, min_free_percent=0, mountinfo=mountinfo
    )


def test_local_destination(tmp_path: Path, catalog: Catalog) -> None:
    mgr = manager(tmp_path, catalog)
    dest = mgr.primary_destination(1000)
    assert dest.kind == TARGET_LOCAL and not dest.spooled
    assert mgr.health()[TARGET_LOCAL].online
    assert mgr.network is None and mgr.ship_pending() == 0


def test_storage_full_refuses_write(tmp_path: Path, catalog: Catalog) -> None:
    media = tmp_path / "media"
    media.mkdir()
    mgr = StorageManager(
        catalog, StorageOptions(), media_root=media, share_root=tmp_path / "s", min_free_percent=90
    )
    with pytest.raises(StorageFull):
        mgr.primary_destination(10**15)


def test_network_down_spools_then_ships(tmp_path: Path, catalog: Catalog) -> None:
    nas = tmp_path / "media" / "nas"
    # Folder exists but is NOT a mount: the Supervisor's empty-folder trap.
    nas.mkdir(parents=True)
    mgr = manager(tmp_path, catalog, mountinfo="", target="network", network_folder="nas")
    dest = mgr.primary_destination(10)
    assert dest.spooled and dest.kind == TARGET_SPOOL
    assert mgr.network_offline_since is not None
    assert not mgr.network_mount_ok("nas")

    rec = Recording("/c.mp4", 4, at(9), Kind.EVENT)
    row, _ = catalog.upsert_recording("car", rec)
    part = dest.target.partial_dir() / "1.part"
    part.write_bytes(b"data")
    final = mgr.place("car", rec, part, dest)
    final.with_suffix(".jpg").write_bytes(b"j")
    catalog.complete_clip(row.id, sha256="s", target=TARGET_SPOOL, path=str(final), size=4)
    catalog.set_post_status(row.id, "done")
    assert mgr.ship_pending() == 0  # still offline, nothing written to the empty folder
    assert not any(nas.iterdir())

    # The share comes back.
    mountinfo = f"1 2 0:1 / {nas} rw - cifs //n/s rw\n"
    mgr2 = manager(tmp_path, catalog, mountinfo=mountinfo, target="network", network_folder="nas")
    assert mgr2.primary_destination(1).kind == TARGET_NETWORK
    assert mgr2.ship_pending() == 1
    copy = catalog.copy(row.id, TARGET_NETWORK)
    assert copy is not None and Path(copy.path).read_bytes() == b"data"
    assert Path(copy.path).with_suffix(".jpg").exists()
    assert catalog.copy(row.id, TARGET_SPOOL) is None and not final.exists()


def test_network_down_without_spool_raises(tmp_path: Path, catalog: Catalog) -> None:
    mgr = manager(
        tmp_path,
        catalog,
        mountinfo="",
        target="network",
        network_folder="nas",
        spool_when_network_down=False,
    )
    with pytest.raises(TargetOffline):
        mgr.primary_destination(1)


def test_both_writes_local_first_then_ships_copy(tmp_path: Path, catalog: Catalog) -> None:
    nas = tmp_path / "share" / "nas"  # a Share-usage mount lives under /share
    nas.mkdir(parents=True)
    mountinfo = f"1 2 0:1 / {nas} rw - nfs4 n:/v rw\n"
    mgr = manager(tmp_path, catalog, mountinfo=mountinfo, target="both", network_folder="nas")
    assert mgr.network is not None and mgr.network.root == nas
    dest = mgr.primary_destination(1)
    assert dest.kind == TARGET_LOCAL
    rec = Recording("/c.mp4", 4, at(9), Kind.NORMAL)
    row, _ = catalog.upsert_recording("car", rec)
    part = dest.target.partial_dir() / "1.part"
    part.write_bytes(b"data")
    final = mgr.place("car", rec, part, dest)
    catalog.complete_clip(row.id, sha256="s", target=TARGET_LOCAL, path=str(final), size=4)
    catalog.set_post_status(row.id, "done")
    mgr.queue_network_copy(row.id)
    assert catalog.copy(row.id, TARGET_NETWORK).state == COPY_PENDING  # type: ignore[union-attr]
    assert mgr.ship_pending() == 1
    assert final.exists()  # the local copy stays
    net = catalog.copy(row.id, TARGET_NETWORK)
    assert net is not None and Path(net.path).read_bytes() == b"data"
    assert mgr.health()[TARGET_NETWORK].online


def test_ship_cleans_up_when_local_copy_vanished(tmp_path: Path, catalog: Catalog) -> None:
    nas = tmp_path / "media" / "nas"
    nas.mkdir(parents=True)
    mgr = manager(
        tmp_path,
        catalog,
        mountinfo=f"1 2 0:1 / {nas} rw - cifs a rw\n",
        target="both",
        network_folder="nas",
    )
    row, _ = catalog.upsert_recording("car", Recording("/c.mp4", 4, at(9), Kind.NORMAL))
    catalog.set_post_status(row.id, "done")
    mgr.queue_network_copy(row.id)
    assert mgr.ship_pending() == 0
    assert catalog.copy(row.id, TARGET_NETWORK) is None

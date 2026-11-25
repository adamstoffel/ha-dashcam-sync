"""Storage targets: local ``/media``, a Supervisor network mount, or both.

The app never mounts anything. Users add a share under Settings, System,
Storage and the Supervisor mounts it. Because the Supervisor can leave an
empty folder at a mount point when the share is offline (writing there
silently fills local disk), every write is preceded by a mount health check.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import shutil
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from .catalog import (
    COPY_PENDING,
    COPY_PRESENT,
    TARGET_LOCAL,
    TARGET_NETWORK,
    TARGET_SPOOL,
    Catalog,
)
from .errors import StorageFull, TargetOffline
from .models import Recording
from .options import StorageOptions

_LOGGER = logging.getLogger(__name__)

NETWORK_FSTYPES = frozenset({"cifs", "smb3", "nfs", "nfs4"})
PARTIAL_DIR = ".partial"
SPOOL_DIR = ".spool"
SIDECAR_SUFFIXES = (".jpg", ".json")


def _unescape_mount(path: str) -> str:
    """mountinfo escapes space, tab, newline and backslash as octal."""
    out = path
    for octal, char in (("\\040", " "), ("\\011", "\t"), ("\\012", "\n"), ("\\134", "\\")):
        out = out.replace(octal, char)
    return out


def mount_fstype(path: Path | str, mountinfo: str | None = None) -> str | None:
    """Filesystem type mounted at exactly ``path``, or ``None``."""
    target = str(path).rstrip("/") or "/"
    if mountinfo is None:
        try:
            mountinfo = Path("/proc/self/mountinfo").read_text("utf-8")
        except OSError:
            return None
    for line in mountinfo.splitlines():
        left, sep, right = line.partition(" - ")
        if not sep:
            continue
        fields = left.split()
        if len(fields) < 5:
            continue
        if _unescape_mount(fields[4]).rstrip("/") == target:
            rfields = right.split()
            if rfields:
                return rfields[0]
    return None


def is_network_mount(path: Path | str, mountinfo: str | None = None) -> bool:
    fstype = mount_fstype(path, mountinfo)
    return fstype is not None and fstype.lower() in NETWORK_FSTYPES


@dataclass(slots=True)
class Usage:
    total: int
    free: int

    @property
    def free_percent(self) -> float:
        return 100.0 * self.free / self.total if self.total else 0.0


def disk_usage(path: Path | str) -> Usage:
    st = os.statvfs(path)
    return Usage(total=st.f_blocks * st.f_frsize, free=st.f_bavail * st.f_frsize)


@dataclass(slots=True)
class TargetHealth:
    name: str
    root: str
    online: bool
    writable: bool
    mounted: bool
    total_bytes: int
    free_bytes: int
    clip_bytes: int
    reason: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "root": self.root,
            "online": self.online,
            "writable": self.writable,
            "mounted": self.mounted,
            "total_bytes": self.total_bytes,
            "free_bytes": self.free_bytes,
            "clip_bytes": self.clip_bytes,
            "reason": self.reason,
        }


class Target:
    """A directory tree clips are written under."""

    def __init__(
        self,
        name: str,
        root: Path,
        *,
        needs_mount: bool,
        mountinfo: str | None = None,
    ) -> None:
        self.name = name
        self.root = root
        self.needs_mount = needs_mount
        self._mountinfo = mountinfo

    def mount_ok(self) -> bool:
        if not self.needs_mount:
            return True
        return is_network_mount(self.root, self._mountinfo)

    def check(self, catalog: Catalog | None = None) -> TargetHealth:
        """Mount check, write+delete probe, and free space."""
        clip_bytes = catalog.target_bytes(self.name) if catalog else 0
        mounted = self.mount_ok()
        if not mounted:
            return TargetHealth(
                self.name, str(self.root), False, False, False, 0, 0, clip_bytes, "not_mounted"
            )
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            probe = self.root / f".probe-{uuid.uuid4().hex}"
            probe.write_bytes(b"ok")
            probe.unlink()
        except OSError as err:
            return TargetHealth(
                self.name,
                str(self.root),
                False,
                False,
                True,
                0,
                0,
                clip_bytes,
                f"not_writable: {err.strerror or err}",
            )
        usage = disk_usage(self.root)
        return TargetHealth(
            self.name, str(self.root), True, True, True, usage.total, usage.free, clip_bytes
        )

    def usage(self) -> Usage:
        base = self.root if self.root.exists() else self.root.parent
        while not base.exists() and base != base.parent:
            base = base.parent
        return disk_usage(base)

    def partial_dir(self) -> Path:
        path = self.root / PARTIAL_DIR
        path.mkdir(parents=True, exist_ok=True)
        return path


def clip_relative_path(camera_id: str, rec: Recording, ext: str | None = None) -> Path:
    """Folder layout shared by every target; names never come from the camera."""
    start = rec.start.astimezone(dt.UTC)
    ext = ext or rec.extension
    name = f"{start:%Y%m%d_%H%M%S}_{rec.channel.value}.{ext}"
    return Path(camera_id) / f"{start:%Y}" / f"{start:%m}" / f"{start:%d}" / rec.kind.value / name


def unique_path(path: Path) -> Path:
    """Avoid clobbering a different clip that landed on the same second."""
    if not path.exists():
        return path
    for n in range(2, 1000):
        candidate = path.with_name(f"{path.stem}_{n}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise OSError(f"too many files named like {path.name}")


def sidecars(path: Path) -> list[Path]:
    """Companion files of a clip: thumbnail and metadata."""
    return [path.with_suffix(s) for s in SIDECAR_SUFFIXES]


def remove_clip_files(path: Path) -> int:
    """Delete a clip and its sidecars, then empty parent folders. Returns bytes freed."""
    freed = 0
    for candidate in (path, *sidecars(path), path.with_suffix(".ts")):
        try:
            size = candidate.stat().st_size
            candidate.unlink()
            freed += size
        except FileNotFoundError:
            continue
        except OSError as err:
            _LOGGER.warning("Could not delete %s: %s", candidate, err)
    parent = path.parent
    for _ in range(5):
        try:
            parent.rmdir()
        except OSError:
            break
        parent = parent.parent
    return freed


def move_file(src: Path, dst: Path) -> None:
    """Atomic rename when on one filesystem, otherwise copy then replace."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.replace(src, dst)
        return
    except OSError:
        pass
    tmp = dst.with_name(f".{dst.name}.{uuid.uuid4().hex}.tmp")
    shutil.copyfile(src, tmp)
    os.replace(tmp, dst)
    src.unlink()


@dataclass(slots=True)
class Destination:
    """Where the next clip is written."""

    target: Target
    kind: str  # catalog target name the copy is recorded as
    spooled: bool = False


class StorageManager:
    """Chooses destinations per ``storage.target`` and ships spooled copies."""

    def __init__(
        self,
        catalog: Catalog,
        storage: StorageOptions,
        *,
        media_root: Path,
        share_root: Path,
        min_free_percent: int,
        mountinfo: str | None = None,
    ) -> None:
        self.catalog = catalog
        self.opts = storage
        self.media_root = media_root
        self.share_root = share_root
        self.min_free_percent = min_free_percent
        self._mountinfo = mountinfo
        self.local = Target(
            TARGET_LOCAL,
            media_root / (storage.local_folder or "dashcam_sync"),
            needs_mount=False,
        )
        self.network: Target | None = None
        if storage.target in ("network", "both") and storage.network_folder:
            self.network = Target(
                TARGET_NETWORK,
                self.resolve_network_root(storage.network_folder),
                needs_mount=True,
                mountinfo=mountinfo,
            )
        self.spool = Target(TARGET_SPOOL, self.local.root / SPOOL_DIR, needs_mount=False)
        self.network_offline_since: float | None = None

    # ------------------------------------------------------------------ roots

    def resolve_network_root(self, folder: str) -> Path:
        """``/media/<name>`` for Media shares, ``/share/<name>`` for Share ones."""
        candidates = [self.media_root / folder, self.share_root / folder]
        for candidate in candidates:
            if is_network_mount(candidate, self._mountinfo):
                return candidate
        return candidates[0]

    def network_mount_ok(self, folder: str) -> bool:
        return any(
            is_network_mount(root / folder, self._mountinfo)
            for root in (self.media_root, self.share_root)
        )

    # ---------------------------------------------------------------- routing

    def _ensure_space(self, target: Target, needed: int) -> None:
        usage = target.usage()
        floor = usage.total * self.min_free_percent / 100.0
        if usage.free - needed < floor:
            raise StorageFull(
                f"{target.name}: {usage.free_percent:.1f}% free, need {self.min_free_percent}%"
                f" after writing {needed} bytes"
            )

    def primary_destination(self, needed: int = 0) -> Destination:
        """Pick where a new clip is written, running the mount check first."""
        mode = self.opts.target
        if mode == "local" or self.network is None:
            self._ensure_space(self.local, needed)
            return Destination(self.local, TARGET_LOCAL)
        health = self.network.check(self.catalog)
        if health.online:
            self.network_offline_since = None
        elif self.network_offline_since is None:
            self.network_offline_since = time.time()
        if mode == "network":
            if health.online:
                self._ensure_space(self.network, needed)
                return Destination(self.network, TARGET_NETWORK)
            if not self.opts.spool_when_network_down:
                raise TargetOffline(f"network target offline: {health.reason}")
            self._ensure_space(self.spool, needed)
            return Destination(self.spool, TARGET_SPOOL, spooled=True)
        # both: always local first, the copy to the share follows.
        self._ensure_space(self.local, needed)
        return Destination(self.local, TARGET_LOCAL)

    def needs_network_copy(self) -> bool:
        return self.opts.target == "both" and self.network is not None

    # ----------------------------------------------------------------- placing

    def place(self, camera_id: str, rec: Recording, part: Path, dest: Destination) -> Path:
        """Atomically move a verified ``.part`` file to its final place."""
        final = unique_path(dest.target.root / clip_relative_path(camera_id, rec))
        move_file(part, final)
        return final

    # ---------------------------------------------------------------- shipping

    def ship_pending(self) -> int:
        """Copy spooled or pending clips to the share once it is back."""
        if self.network is None:
            return 0
        if not self.network.check(self.catalog).online:
            return 0
        shipped = 0
        # Spooled copies move; ``both`` copies are duplicated and the local one stays.
        for clip, copy in self.catalog.copies_for_target(TARGET_SPOOL, COPY_PRESENT):
            src = Path(copy.path)
            if not src.exists():
                self.catalog.remove_copy(clip.id, TARGET_SPOOL)
                continue
            rel = src.relative_to(self.spool.root)
            dst = unique_path(self.network.root / rel)
            self._ship_files(src, dst)
            self.catalog.remove_copy(clip.id, TARGET_SPOOL)
            self.catalog.add_copy(clip.id, TARGET_NETWORK, str(dst), dst.stat().st_size)
            shipped += 1
        for clip, copy in self.catalog.copies_for_target(TARGET_NETWORK, COPY_PENDING):
            local = self.catalog.copy(clip.id, TARGET_LOCAL)
            if local is None or not Path(local.path).exists():
                self.catalog.remove_copy(clip.id, TARGET_NETWORK)
                continue
            src = Path(local.path)
            rel = src.relative_to(self.local.root)
            dst = unique_path(self.network.root / rel)
            self._ship_files(src, dst, move=False)
            self.catalog.add_copy(clip.id, TARGET_NETWORK, str(dst), dst.stat().st_size)
            shipped += 1
        return shipped

    def _ship_files(self, src: Path, dst: Path, *, move: bool = True) -> None:
        dst.parent.mkdir(parents=True, exist_ok=True)
        pairs = [(src, dst)]
        for ext in (".jpg", ".json"):
            side = src.with_suffix(ext)
            if side.exists():
                pairs.append((side, dst.with_suffix(ext)))
        for s, d in pairs:
            tmp = d.with_name(f".{d.name}.{uuid.uuid4().hex}.tmp")
            shutil.copyfile(s, tmp)
            if tmp.stat().st_size != s.stat().st_size:
                tmp.unlink()
                raise OSError(f"short copy of {s.name}")
            os.replace(tmp, d)
            if move:
                s.unlink()

    def queue_network_copy(self, clip_id: int) -> None:
        if self.needs_network_copy():
            self.catalog.add_copy(clip_id, TARGET_NETWORK, "", 0, COPY_PENDING)

    # ------------------------------------------------------------------ health

    def health(self) -> dict[str, TargetHealth]:
        out = {TARGET_LOCAL: self.local.check(self.catalog)}
        if self.network is not None:
            net = self.network.check(self.catalog)
            if net.online:
                self.network_offline_since = None
            elif self.network_offline_since is None:
                self.network_offline_since = time.time()
            out[TARGET_NETWORK] = net
        return out

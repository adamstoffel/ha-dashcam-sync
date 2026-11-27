"""Move clips to where the current storage settings put them.

Changing ``local_folder``, ``target`` or ``network_folder`` only affects new clips: the
catalogue keeps each copy's full path, so existing clips stay where they were. This
module plans and runs the move of those clips:

* every clip ends up with one copy in each place the settings want (local, the network
  share or disk, or both);
* a copy is written next to the new root with the same folder layout, its size is
  checked, the catalogue is updated, and only then is the old copy deleted. A clip is
  never left with fewer verified copies than it had;
* the work is done clip by clip under the lock that shipping and retention also take, so
  an interrupted run (cancel, restart, a full disk) can simply be started again.
"""

from __future__ import annotations

import logging
import os
import shutil
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .catalog import (
    COPY_PENDING,
    COPY_PRESENT,
    TARGET_LOCAL,
    TARGET_NETWORK,
    Catalog,
    ClipRow,
    CopyRow,
)
from .errors import StorageError, StorageFull
from .storage import (
    StorageManager,
    Target,
    layout_relative,
    remove_clip_files,
    reuse_or_unique,
    sidecars,
)

_LOGGER = logging.getLogger(__name__)


def _under(path: Path, root: Path) -> bool:
    return path.is_relative_to(root)


def _companions(path: Path) -> list[Path]:
    """The clip, its thumbnail and metadata, and a kept original ``.ts``."""
    out = [path, *sidecars(path)]
    original = path.with_suffix(".ts")
    if original != path:
        out.append(original)
    return out


def wanted_targets(storage: StorageManager) -> list[str]:
    """Where the current settings put a clip; mirrors ``primary_destination``."""
    mode = storage.opts.target
    if mode == "local" or storage.network is None:
        return [TARGET_LOCAL]
    if mode == "network":
        return [TARGET_NETWORK]
    return [TARGET_LOCAL, TARGET_NETWORK]


def target_objects(storage: StorageManager) -> dict[str, Target]:
    out = {TARGET_LOCAL: storage.local}
    if storage.network is not None:
        out[TARGET_NETWORK] = storage.network
    return out


@dataclass(slots=True)
class ClipMove:
    """What has to happen to one clip."""

    clip: ClipRow
    create: list[str]  # targets that need a copy under their current root
    drop: list[CopyRow]  # copies to remove once the new ones exist
    source: CopyRow | None  # where the new copies are copied from
    bytes: int  # bytes that will be written


@dataclass(slots=True)
class MigrationPlan:
    moves: list[ClipMove] = field(default_factory=list)
    destinations: dict[str, str] = field(default_factory=dict)
    unavailable: int = 0  # clips that need a move but whose files cannot be reached
    stranded: int = 0  # copies left in an unwanted place that cannot be reached
    processing: int = 0  # clips still being post-processed; moved on a later run
    blocked: str | None = None  # why the move cannot start now

    @property
    def bytes(self) -> int:
        return sum(m.bytes for m in self.moves)

    def as_dict(self) -> dict[str, Any]:
        return {
            "needed": bool(self.moves),
            "clips": len(self.moves),
            "bytes": self.bytes,
            "copies_to_remove": sum(len(m.drop) for m in self.moves),
            "destinations": self.destinations,
            "unavailable": self.unavailable,
            "stranded": self.stranded,
            "processing": self.processing,
            "blocked": self.blocked,
        }


def _plan_clip(
    clip: ClipRow, copies: list[CopyRow], wanted: list[str], roots: dict[str, Path]
) -> tuple[ClipMove | None, str | None]:
    """The move for one clip, or why it cannot be moved (``unavailable``/``stranded``...)."""
    if clip.status != "done":
        return None, None
    if clip.post_status == "pending":
        return None, "processing"
    present = [
        c
        for c in copies
        if c.state == COPY_PRESENT and c.path and c.target in (TARGET_LOCAL, TARGET_NETWORK)
    ]
    pending = [c for c in copies if c.state == COPY_PENDING]
    by_target = {c.target: c for c in present}
    reachable = [c for c in present if Path(c.path).is_file()]

    create: list[str] = []
    for target in wanted:
        row = by_target.get(target)
        if row is not None and _under(Path(row.path), roots[target]) and Path(row.path).is_file():
            continue
        if target == TARGET_NETWORK and any(p.target == TARGET_NETWORK for p in pending):
            continue  # shipping writes it
        create.append(target)

    drop: list[CopyRow] = []
    stranded = False
    for row in present:
        misplaced_same = row.target in create
        unwanted = row.target not in wanted
        if not (misplaced_same or unwanted):
            continue
        if Path(row.path).is_file():
            drop.append(row)
        elif unwanted:
            stranded = True  # an unplugged disk, say: leave it tracked
    drop.extend(p for p in pending if p.target not in wanted)

    if create and not reachable:
        return None, "unavailable"
    if not create and not drop:
        return None, "stranded" if stranded else None
    source = None
    if create:
        # Prefer a copy that goes away afterwards: on one file system it is just renamed.
        source = next((c for c in drop if c in reachable), reachable[0])
    size = Path(source.path).stat().st_size if source else 0
    move = ClipMove(clip, create, drop, source, size * len(create))
    return move, "stranded" if stranded else None


def _blocked(storage: StorageManager, wanted: list[str]) -> str | None:
    targets = target_objects(storage)
    for name in wanted:
        health = targets[name].check(None)
        if not health.online:
            return f"{name}_{health.reason or 'offline'}".split(":")[0]
    return None


def plan_migration(catalog: Catalog, storage: StorageManager) -> MigrationPlan:
    wanted = wanted_targets(storage)
    targets = target_objects(storage)
    roots = {name: t.root for name, t in targets.items()}
    plan = MigrationPlan(destinations={name: str(roots[name]) for name in wanted})
    clips: dict[int, tuple[ClipRow, list[CopyRow]]] = {}
    for target in (TARGET_LOCAL, TARGET_NETWORK):
        for clip, copy in catalog.copies_for_target(target, state=None):
            clips.setdefault(clip.id, (clip, []))[1].append(copy)
    for clip, copies in clips.values():
        move, note = _plan_clip(clip, copies, wanted, roots)
        if move is not None:
            plan.moves.append(move)
        if note == "processing":
            plan.processing += 1
        elif note == "unavailable":
            plan.unavailable += 1
        elif note == "stranded":
            plan.stranded += 1
    if plan.moves:
        plan.blocked = _blocked(storage, wanted)
    return plan


def _same_filesystem(src: Path, root: Path) -> bool:
    try:
        return src.stat().st_dev == root.stat().st_dev
    except OSError:
        return False


def _copy_verified(src: Path, dst: Path) -> None:
    tmp = dst.with_name(f".{dst.name}.{uuid.uuid4().hex}.tmp")
    try:
        shutil.copyfile(src, tmp)
        if tmp.stat().st_size != src.stat().st_size:
            raise OSError(f"short copy of {src.name}")
        os.replace(tmp, dst)
    finally:
        tmp.unlink(missing_ok=True)


def _destination(root: Path, rel: Path, src: Path) -> tuple[Path, bool]:
    """Final path for a copy, and whether an earlier, interrupted run already wrote it."""
    dst = reuse_or_unique(root / rel, src)
    return dst, dst == root / rel and dst.is_file()


def _transfer(src: Path, dst: Path, *, rename: bool) -> None:
    """Copy (or rename) a clip and its companions. The clip itself goes last.

    If anything fails part-way, the clip is still at ``src``, where the catalogue points,
    so nothing drops out of tracking and the next run picks up the rest.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    pairs = [(s, dst.with_name(dst.stem + s.suffix)) for s in _companions(src)[1:]]
    pairs.append((src, dst))
    for s, d in pairs:
        if not s.is_file():
            continue
        if rename:
            os.replace(s, d)
        else:
            _copy_verified(s, d)


def execute_move(
    move: ClipMove, catalog: Catalog, storage: StorageManager, *, check_space: bool = True
) -> int:
    """Carry out one clip's move. Returns the bytes written."""
    targets = target_objects(storage)
    clip = move.clip
    written: dict[str, Path] = {}
    if move.source is not None:
        src = Path(move.source.path)
        rel = layout_relative(src, clip.camera)
        size = src.stat().st_size
        for i, name in enumerate(move.create):
            target = targets[name]
            target.root.mkdir(parents=True, exist_ok=True)
            dst, done_before = _destination(target.root, rel, src)
            if dst == src:
                written[name] = dst
                continue
            last = i == len(move.create) - 1
            rename = (
                last
                and not done_before
                and move.source in move.drop
                and _same_filesystem(src, target.root)
            )
            if not done_before:
                if not rename and check_space:
                    storage._ensure_space(target, size)
                _transfer(src, dst, rename=rename)
            written[name] = dst
    for name, dst in written.items():
        catalog.add_copy(clip.id, name, str(dst), dst.stat().st_size)
    new_paths = set(written.values())
    for row in move.drop:
        if row.target not in written:
            catalog.remove_copy(clip.id, row.target)
        if row.state == COPY_PRESENT and Path(row.path) not in new_paths:
            remove_clip_files(Path(row.path))
    return move.bytes


@dataclass(slots=True)
class MigrationState:
    status: str = "idle"  # idle, running, done, cancelled, failed
    clips_total: int = 0
    clips_done: int = 0
    bytes_total: int = 0
    bytes_done: int = 0
    errors: int = 0
    current: str | None = None
    error: str | None = None
    started: float | None = None
    finished: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "clips_total": self.clips_total,
            "clips_done": self.clips_done,
            "bytes_total": self.bytes_total,
            "bytes_done": self.bytes_done,
            "errors": self.errors,
            "current": self.current,
            "error": self.error,
            "started": self.started,
            "finished": self.finished,
        }


class Migration:
    """One move job at a time; the daemon runs :meth:`run` in a worker thread."""

    def __init__(self) -> None:
        self.state = MigrationState()
        self._cancel = threading.Event()

    @property
    def running(self) -> bool:
        return self.state.status == "running"

    def cancel(self) -> None:
        self._cancel.set()

    def begin(self, plan: MigrationPlan) -> None:
        self._cancel.clear()
        self.state = MigrationState(
            status="running",
            clips_total=len(plan.moves),
            bytes_total=plan.bytes,
            started=time.time(),
        )

    def run(
        self,
        catalog: Catalog,
        storage: StorageManager,
        lock: threading.Lock,
        on_progress: Callable[[MigrationState], None] | None = None,
    ) -> MigrationState:
        """Move every clip of a fresh plan; the plan is re-checked clip by clip."""
        state = self.state
        wanted = wanted_targets(storage)
        roots = {name: t.root for name, t in target_objects(storage).items()}
        try:
            for move in plan_migration(catalog, storage).moves:
                if self._cancel.is_set():
                    state.status = "cancelled"
                    break
                state.current = f"{move.clip.camera} {move.clip.start.isoformat()}"
                with lock:
                    # Retention, shipping or a delete may have changed it since planning.
                    clip = catalog.get_clip(move.clip.id)
                    fresh, _ = (
                        _plan_clip(clip, catalog.copies(clip.id), wanted, roots)
                        if clip is not None
                        else (None, None)
                    )
                    if fresh is not None:
                        try:
                            state.bytes_done += execute_move(fresh, catalog, storage)
                        except StorageFull as err:
                            state.status, state.error = "failed", str(err)
                            break
                        except (OSError, StorageError) as err:
                            state.errors += 1
                            _LOGGER.warning("Moving clip %s failed: %s", move.clip.id, err)
                state.clips_done += 1
                if on_progress is not None:
                    on_progress(state)
            else:
                state.status = "done"
        except Exception as err:  # never leave the job looking like it is still running
            _LOGGER.exception("Moving clips failed")
            state.status, state.error = "failed", str(err)
        state.current = None
        state.finished = time.time()
        _LOGGER.info(
            "Moving clips %s: %d of %d clips, %.1f MB, %d errors",
            state.status,
            state.clips_done,
            state.clips_total,
            state.bytes_done / 1e6,
            state.errors,
        )
        if on_progress is not None:
            on_progress(state)
        return state

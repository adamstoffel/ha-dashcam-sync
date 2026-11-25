"""Rolling expiration, per target, with a dry-run mode."""

from __future__ import annotations

import datetime as dt
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .catalog import TARGET_LOCAL, TARGET_NETWORK, Catalog, ClipRow, CopyRow
from .models import Kind
from .options import RetentionOptions
from .storage import StorageManager, Target, remove_clip_files

_LOGGER = logging.getLogger(__name__)

# Size-based deletion order: normal first, events only when nothing else helps.
SIZE_ORDER = (Kind.NORMAL, Kind.PARKING, Kind.PHOTO, Kind.EVENT)


@dataclass(frozen=True, slots=True)
class Rules:
    """Retention rules for one target. ``0`` always means unlimited."""

    normal_days: int
    parking_days: int
    event_days: int
    photo_days: int
    max_total_gb: int
    min_free_percent: int

    def days_for(self, kind: Kind) -> int:
        return {
            Kind.NORMAL: self.normal_days,
            Kind.PARKING: self.parking_days,
            Kind.EVENT: self.event_days,
            Kind.PHOTO: self.photo_days,
        }[kind]


def rules_for(opts: RetentionOptions, target: str) -> Rules:
    if target == TARGET_NETWORK:
        return Rules(
            opts.network_normal_days,
            opts.network_parking_days,
            opts.network_event_days,
            opts.network_photo_days,
            opts.network_max_total_gb,
            opts.network_min_free_percent,
        )
    return Rules(
        opts.normal_days,
        opts.parking_days,
        opts.event_days,
        opts.photo_days,
        opts.max_total_gb,
        opts.min_free_percent,
    )


@dataclass(slots=True)
class Candidate:
    clip: ClipRow
    copy: CopyRow
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "clip_id": self.clip.id,
            "camera": self.clip.camera,
            "kind": self.clip.kind.value,
            "start": self.clip.start.isoformat(),
            "size": self.copy.size,
            "target": self.copy.target,
            "reason": self.reason,
        }


def reference_time(clip: ClipRow) -> dt.datetime:
    """When a clip counts as having been made, for expiry.

    A dashcam without GPS often resets its clock to 1970 or 2014 after a power loss.
    Ageing such a clip by its own timestamp would delete it right after download, so
    it never counts as older than the moment it was downloaded.
    """
    if clip.done_ts:
        return max(clip.start, dt.datetime.fromtimestamp(clip.done_ts, tz=dt.UTC))
    return clip.start


def plan_purge(
    items: list[tuple[ClipRow, CopyRow]],
    rules: Rules,
    *,
    now: dt.datetime,
    free_bytes: int,
    total_bytes: int,
) -> list[Candidate]:
    """Decide what to delete from one target. Pure, so it is easy to test.

    Clips marked Keep are never touched by any rule.
    """
    chosen: dict[int, Candidate] = {}
    eligible = [(c, p) for c, p in items if not c.keep]

    # Age rules.
    for clip, copy in eligible:
        days = rules.days_for(clip.kind)
        if days > 0 and reference_time(clip) <= now - dt.timedelta(days=days):
            chosen[clip.id] = Candidate(clip, copy, f"older_than_{days}_days")

    def remaining(skip: dict[int, Candidate]) -> list[tuple[ClipRow, CopyRow]]:
        return [(c, p) for c, p in eligible if c.id not in skip]

    # Size cap.
    if rules.max_total_gb > 0:
        cap = rules.max_total_gb * 1024**3
        total = sum(p.size for c, p in items if c.id not in chosen)
        if total > cap:
            _evict(chosen, remaining(chosen), total - cap, "over_max_total")

    # Free space floor.
    if rules.min_free_percent > 0 and total_bytes > 0:
        want = total_bytes * rules.min_free_percent / 100.0
        freed = sum(p.size for p in (cand.copy for cand in chosen.values()))
        deficit = want - (free_bytes + freed)
        if deficit > 0:
            _evict(chosen, remaining(chosen), int(deficit), "low_free_space")

    return sorted(chosen.values(), key=lambda c: reference_time(c.clip))


def _evict(
    chosen: dict[int, Candidate],
    pool: list[tuple[ClipRow, CopyRow]],
    need: int,
    reason: str,
) -> None:
    """Oldest-first within each kind, kinds in SIZE_ORDER, until ``need`` bytes."""
    freed = 0
    for kind in SIZE_ORDER:
        ordered = sorted((x for x in pool if x[0].kind is kind), key=lambda x: reference_time(x[0]))
        for clip, copy in ordered:
            if freed >= need:
                return
            chosen[clip.id] = Candidate(clip, copy, reason)
            freed += copy.size


@dataclass(slots=True)
class PurgeReport:
    dry_run: bool
    deleted: list[Candidate] = field(default_factory=list)
    bytes_freed: int = 0
    skipped_targets: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "count": len(self.deleted),
            "bytes_freed": self.bytes_freed,
            "skipped_targets": self.skipped_targets,
            "clips": [c.as_dict() for c in self.deleted],
        }


class RetentionService:
    def __init__(
        self,
        catalog: Catalog,
        storage: StorageManager,
        opts: RetentionOptions,
    ) -> None:
        self.catalog = catalog
        self.storage = storage
        self.opts = opts

    def _targets(self) -> list[Target]:
        targets = [self.storage.local]
        if self.storage.network is not None:
            targets.append(self.storage.network)
        return targets

    def run(self, *, dry_run: bool = False, now: dt.datetime | None = None) -> PurgeReport:
        now = now or dt.datetime.now(dt.UTC)
        report = PurgeReport(dry_run=dry_run)
        for target in self._targets():
            if target.needs_mount and not target.mount_ok():
                # Never delete (or judge free space) through an unmounted folder.
                report.skipped_targets.append(target.name)
                continue
            usage = target.usage()
            items = self.catalog.copies_for_target(target.name)
            plan = plan_purge(
                items,
                rules_for(self.opts, target.name),
                now=now,
                free_bytes=usage.free,
                total_bytes=usage.total,
            )
            for cand in plan:
                report.deleted.append(cand)
                if dry_run:
                    report.bytes_freed += cand.copy.size
                    continue
                report.bytes_freed += self._delete_copy(cand)
        if not dry_run:
            _LOGGER.info(
                "Purge removed %d copies, %.1f MB freed",
                len(report.deleted),
                report.bytes_freed / 1e6,
            )
        return report

    def _delete_copy(self, cand: Candidate) -> int:
        freed = remove_clip_files(Path(cand.copy.path))
        self.catalog.remove_copy(cand.clip.id, cand.copy.target)
        if not self.catalog.copies(cand.clip.id):
            self.catalog.delete_clip(cand.clip.id)
        return freed or cand.copy.size

    def delete_clip_everywhere(self, clip_id: int) -> int:
        """Remove a clip from all targets (the delete_clip action)."""
        freed = 0
        for copy in self.catalog.copies(clip_id):
            if copy.path:
                if copy.target == TARGET_NETWORK and self.storage.network is not None:
                    if not self.storage.network.mount_ok():
                        continue
                freed += remove_clip_files(Path(copy.path))
        self.catalog.delete_clip(clip_id)
        return freed


def seconds_until(purge_time: str, now: dt.datetime | None = None) -> float:
    """Seconds until the next local ``HH:MM``."""
    now = now or dt.datetime.now().astimezone()
    hour, minute = (int(x) for x in purge_time.split(":")[:2])
    nxt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if nxt <= now:
        nxt += dt.timedelta(days=1)
    return (nxt - now).total_seconds()


__all__ = [
    "TARGET_LOCAL",
    "Candidate",
    "PurgeReport",
    "RetentionService",
    "Rules",
    "plan_purge",
    "rules_for",
    "seconds_until",
    "time",
]

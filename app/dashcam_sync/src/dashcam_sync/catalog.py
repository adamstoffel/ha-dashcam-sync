"""SQLite catalogue in ``/data/catalog.db``.

Keyed by camera, source path, size and start time. Each stored copy of a clip
(local, network, spool) is tracked separately so the two targets can expire
clips on their own schedules.
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models import Channel, Kind, Recording, utc

SCHEMA = """
CREATE TABLE IF NOT EXISTS clips (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    camera TEXT NOT NULL,
    src_path TEXT NOT NULL,
    size INTEGER NOT NULL,
    start_ts REAL NOT NULL,
    kind TEXT NOT NULL,
    channel TEXT NOT NULL,
    locked INTEGER NOT NULL DEFAULT 0,
    duration REAL,
    status TEXT NOT NULL DEFAULT 'queued',
    offset INTEGER NOT NULL DEFAULT 0,
    part_path TEXT,
    sha256 TEXT,
    keep INTEGER NOT NULL DEFAULT 0,
    post_status TEXT NOT NULL DEFAULT 'pending',
    error TEXT,
    created_ts REAL NOT NULL,
    done_ts REAL,
    UNIQUE (camera, src_path, size, start_ts)
);
CREATE INDEX IF NOT EXISTS clips_camera_start ON clips (camera, start_ts);
CREATE TABLE IF NOT EXISTS copies (
    clip_id INTEGER NOT NULL REFERENCES clips(id) ON DELETE CASCADE,
    target TEXT NOT NULL,
    path TEXT NOT NULL,
    size INTEGER NOT NULL DEFAULT 0,
    state TEXT NOT NULL DEFAULT 'present',
    created_ts REAL NOT NULL,
    PRIMARY KEY (clip_id, target)
);
CREATE INDEX IF NOT EXISTS copies_target ON copies (target, state);
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    camera TEXT NOT NULL,
    started_ts REAL NOT NULL,
    ended_ts REAL,
    result TEXT,
    clips INTEGER NOT NULL DEFAULT 0,
    bytes INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    driver TEXT
);
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

# The state a copy can be in. ``present`` is a finished file at ``path``;
# ``pending`` is a copy that still has to be written (the share was down).
COPY_PRESENT = "present"
COPY_PENDING = "pending"

TARGET_LOCAL = "local"
TARGET_NETWORK = "network"
TARGET_SPOOL = "spool"


@dataclass(slots=True)
class ClipRow:
    id: int
    camera: str
    src_path: str
    size: int
    start: dt.datetime
    kind: Kind
    channel: Channel
    locked: bool
    duration: float | None
    status: str
    offset: int
    part_path: str | None
    sha256: str | None
    keep: bool
    post_status: str
    error: str | None
    created_ts: float
    done_ts: float | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "camera": self.camera,
            "size": self.size,
            "start": self.start.isoformat(),
            "kind": self.kind.value,
            "channel": self.channel.value,
            "locked": self.locked,
            "duration": self.duration,
            "status": self.status,
            "keep": self.keep,
            "sha256": self.sha256,
            "post_status": self.post_status,
        }


@dataclass(slots=True)
class CopyRow:
    clip_id: int
    target: str
    path: str
    size: int
    state: str
    created_ts: float


def _clip(row: sqlite3.Row) -> ClipRow:
    return ClipRow(
        id=row["id"],
        camera=row["camera"],
        src_path=row["src_path"],
        size=row["size"],
        start=utc(row["start_ts"]),
        kind=Kind(row["kind"]),
        channel=Channel(row["channel"]),
        locked=bool(row["locked"]),
        duration=row["duration"],
        status=row["status"],
        offset=row["offset"],
        part_path=row["part_path"],
        sha256=row["sha256"],
        keep=bool(row["keep"]),
        post_status=row["post_status"],
        error=row["error"],
        created_ts=row["created_ts"],
        done_ts=row["done_ts"],
    )


def _copy(row: sqlite3.Row) -> CopyRow:
    return CopyRow(
        clip_id=row["clip_id"],
        target=row["target"],
        path=row["path"],
        size=row["size"],
        state=row["state"],
        created_ts=row["created_ts"],
    )


class Catalog:
    """Thread-safe wrapper around one SQLite connection."""

    def __init__(self, path: Path | str) -> None:
        self._path = str(path)
        if self._path != ":memory:":
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            if self._path != ":memory:":
                self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    # ------------------------------------------------------------------ clips

    def upsert_recording(self, camera: str, rec: Recording) -> tuple[ClipRow, bool]:
        """Insert a listed recording; returns the row and whether it was new."""
        with self._tx() as conn:
            row = conn.execute(
                "SELECT * FROM clips WHERE camera=? AND src_path=? AND size=? AND start_ts=?",
                (camera, rec.path, rec.size, rec.start.timestamp()),
            ).fetchone()
            if row is not None:
                return _clip(row), False
            cur = conn.execute(
                "INSERT INTO clips (camera, src_path, size, start_ts, kind, channel, locked,"
                " duration, created_ts) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    camera,
                    rec.path,
                    rec.size,
                    rec.start.timestamp(),
                    rec.kind.value,
                    rec.channel.value,
                    int(rec.locked),
                    rec.duration,
                    time.time(),
                ),
            )
            row = conn.execute("SELECT * FROM clips WHERE id=?", (cur.lastrowid,)).fetchone()
            return _clip(row), True

    def is_known_done(self, camera: str, rec: Recording) -> bool:
        """Already downloaded, or downloaded once and deleted since (a tombstone).

        Deleting a clip keeps its row so the camera's copy, still on the card, is
        never fetched again.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT status FROM clips WHERE camera=? AND src_path=? AND size=? AND start_ts=?",
                (camera, rec.path, rec.size, rec.start.timestamp()),
            ).fetchone()
        return row is not None and row["status"] in ("done", "deleted")

    def get_clip(self, clip_id: int) -> ClipRow | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM clips WHERE id=?", (clip_id,)).fetchone()
        return _clip(row) if row else None

    def set_offset(self, clip_id: int, offset: int, part_path: str | None = None) -> None:
        with self._lock:
            if part_path is None:
                self._conn.execute(
                    "UPDATE clips SET offset=?, status='downloading' WHERE id=?",
                    (offset, clip_id),
                )
            else:
                self._conn.execute(
                    "UPDATE clips SET offset=?, part_path=?, status='downloading' WHERE id=?",
                    (offset, part_path, clip_id),
                )

    def mark_failed(self, clip_id: int, error: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE clips SET status='failed', error=? WHERE id=?", (error[:500], clip_id)
            )

    def reset_clip(self, clip_id: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE clips SET status='queued', offset=0, part_path=NULL, error=NULL WHERE id=?",
                (clip_id,),
            )

    def complete_clip(
        self,
        clip_id: int,
        *,
        sha256: str,
        target: str,
        path: str,
        size: int,
    ) -> None:
        now = time.time()
        with self._tx() as conn:
            conn.execute(
                "UPDATE clips SET status='done', sha256=?, offset=size, part_path=NULL,"
                " done_ts=?, error=NULL WHERE id=?",
                (sha256, now, clip_id),
            )
            conn.execute(
                "INSERT OR REPLACE INTO copies (clip_id, target, path, size, state, created_ts)"
                " VALUES (?,?,?,?,?,?)",
                (clip_id, target, path, size, COPY_PRESENT, now),
            )

    def set_post_status(self, clip_id: int, status: str, duration: float | None = None) -> None:
        with self._lock:
            if duration is not None:
                self._conn.execute(
                    "UPDATE clips SET post_status=?, duration=? WHERE id=?",
                    (status, duration, clip_id),
                )
            else:
                self._conn.execute("UPDATE clips SET post_status=? WHERE id=?", (status, clip_id))

    def set_keep(self, clip_id: int, keep: bool) -> bool:
        with self._lock:
            cur = self._conn.execute("UPDATE clips SET keep=? WHERE id=?", (int(keep), clip_id))
            return cur.rowcount > 0

    def delete_clip(self, clip_id: int) -> None:
        """Forget a clip's files but keep a tombstone row (status ``deleted``)."""
        with self._tx() as conn:
            conn.execute("DELETE FROM copies WHERE clip_id=?", (clip_id,))
            conn.execute(
                "UPDATE clips SET status='deleted', keep=0, part_path=NULL WHERE id=?", (clip_id,)
            )

    def list_clips(
        self,
        *,
        camera: str | None = None,
        kind: Kind | None = None,
        day: str | None = None,
        status: str | None = "done",
        limit: int = 200,
        offset: int = 0,
    ) -> list[ClipRow]:
        clauses: list[str] = []
        args: list[Any] = []
        if camera:
            clauses.append("camera=?")
            args.append(camera)
        if kind:
            clauses.append("kind=?")
            args.append(kind.value)
        if status:
            clauses.append("status=?")
            args.append(status)
        if day:
            start = dt.datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=dt.UTC)
            clauses.append("start_ts>=? AND start_ts<?")
            args += [start.timestamp(), (start + dt.timedelta(days=1)).timestamp()]
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM clips {where} ORDER BY start_ts DESC, id DESC LIMIT ? OFFSET ?",
                (*args, limit, offset),
            ).fetchall()
        return [_clip(r) for r in rows]

    def clip_days(self, camera: str) -> list[str]:
        """Distinct UTC days with finished clips, newest first."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT date(start_ts,'unixepoch') AS d FROM clips"
                " WHERE camera=? AND status='done' ORDER BY d DESC",
                (camera,),
            ).fetchall()
        return [r["d"] for r in rows]

    def latest_clip(self, camera: str, kind: Kind | None = None) -> ClipRow | None:
        rows = self.list_clips(camera=camera, kind=kind, limit=1)
        return rows[0] if rows else None

    def tombstones(self, camera: str | None = None) -> int:
        with self._lock:
            if camera:
                row = self._conn.execute(
                    "SELECT COUNT(*) AS n FROM clips WHERE status='deleted' AND camera=?", (camera,)
                ).fetchone()
            else:
                row = self._conn.execute(
                    "SELECT COUNT(*) AS n FROM clips WHERE status='deleted'"
                ).fetchone()
        return int(row["n"])

    def unfinished(self, camera: str) -> list[ClipRow]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM clips WHERE camera=? AND status IN ('queued','downloading','failed')",
                (camera,),
            ).fetchall()
        return [_clip(r) for r in rows]

    # ----------------------------------------------------------------- copies

    def add_copy(
        self, clip_id: int, target: str, path: str, size: int, state: str = COPY_PRESENT
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO copies (clip_id, target, path, size, state, created_ts)"
                " VALUES (?,?,?,?,?,?)",
                (clip_id, target, path, size, state, time.time()),
            )

    def remove_copy(self, clip_id: int, target: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM copies WHERE clip_id=? AND target=?", (clip_id, target))

    def copies(self, clip_id: int) -> list[CopyRow]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM copies WHERE clip_id=?", (clip_id,)).fetchall()
        return [_copy(r) for r in rows]

    def copy(self, clip_id: int, target: str) -> CopyRow | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM copies WHERE clip_id=? AND target=?", (clip_id, target)
            ).fetchone()
        return _copy(row) if row else None

    def copies_for_target(
        self, target: str, state: str | None = COPY_PRESENT
    ) -> list[tuple[ClipRow, CopyRow]]:
        query = (
            "SELECT c.*, p.target AS p_target, p.path AS p_path, p.size AS p_size,"
            " p.state AS p_state, p.created_ts AS p_created FROM copies p"
            " JOIN clips c ON c.id=p.clip_id WHERE p.target=?"
        )
        args: list[Any] = [target]
        if state:
            query += " AND p.state=?"
            args.append(state)
        query += " ORDER BY c.start_ts ASC"
        with self._lock:
            rows = self._conn.execute(query, args).fetchall()
        out: list[tuple[ClipRow, CopyRow]] = []
        for r in rows:
            out.append(
                (
                    _clip(r),
                    CopyRow(
                        clip_id=r["id"],
                        target=r["p_target"],
                        path=r["p_path"],
                        size=r["p_size"],
                        state=r["p_state"],
                        created_ts=r["p_created"],
                    ),
                )
            )
        return out

    def target_bytes(self, target: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(SUM(size),0) AS s FROM copies WHERE target=? AND state=?",
                (target, COPY_PRESENT),
            ).fetchone()
        return int(row["s"])

    # --------------------------------------------------------------- sessions

    def start_session(self, camera: str, driver: str | None = None) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO sessions (camera, started_ts, driver) VALUES (?,?,?)",
                (camera, time.time(), driver),
            )
            session_id = cur.lastrowid
        assert session_id is not None
        return session_id

    def end_session(
        self, session_id: int, result: str, clips: int, nbytes: int, error: str | None = None
    ) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE sessions SET ended_ts=?, result=?, clips=?, bytes=?, error=? WHERE id=?",
                (time.time(), result, clips, nbytes, (error or "")[:500] or None, session_id),
            )
            self._conn.execute(
                "DELETE FROM sessions WHERE id NOT IN"
                " (SELECT id FROM sessions ORDER BY id DESC LIMIT 200)"
            )

    def recent_sessions(self, limit: int = 50, camera: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            if camera:
                rows = self._conn.execute(
                    "SELECT * FROM sessions WHERE camera=? ORDER BY id DESC LIMIT ?",
                    (camera, limit),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM sessions ORDER BY id DESC LIMIT ?", (limit,)
                ).fetchall()
        return [dict(r) for r in rows]

    def consecutive_sessions_with(self, camera: str, result: str) -> int:
        """How many of the newest finished sessions in a row ended with ``result``."""
        count = 0
        for sess in self.recent_sessions(10, camera):
            if sess["result"] is None:
                continue
            if sess["result"] != result:
                break
            count += 1
        return count

    # --------------------------------------------------------------------- kv

    def kv_get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            row = self._conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def kv_set(self, key: str, value: Any) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO kv (key, value) VALUES (?,?)", (key, json.dumps(value))
            )

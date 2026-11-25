"""Novatek NT96xxx Wi-Fi cameras (FitcamX-style clones).

Protocol: HTTP commands of the form ``/?custom=1&cmd=<n>[&par=<p>][&str=<s>]``
answered with ``<Function><Cmd/><Status/></Function>`` XML. Listing only works
in playback mode, which stops recording, so ``prepare()`` enters playback and
``restore()`` returns to movie mode and starts recording again.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import logging
from typing import Any, ClassVar
from urllib.parse import quote

from ..errors import ProtocolError
from ..models import Caps, DeviceInfo, Probe, Recording, naive_local_to_utc
from .base import EPOCH, BaseDriver, classify, timestamp_from_name
from .http import ByteStream

_LOGGER = logging.getLogger(__name__)

CMD_MODE = 3001
CMD_FREE_SPACE = 3017
CMD_FILE_LIST = 3015
CMD_HEARTBEAT = 3016
CMD_FW_VERSION = 3012
CMD_RECORD = 2001
CMD_DELETE_ONE = 4003
CMD_STATION_CREDENTIALS = 3032
CMD_STATION_MODE = 3033
CMD_RECONNECT = 3018

MODE_MOVIE = 1
MODE_PLAYBACK = 2

HEARTBEAT_INTERVAL = 10.0


class NovatekDriver(BaseDriver):
    name: ClassVar[str] = "novatek"
    default_hosts: ClassVar[list[str]] = ["192.168.1.254"]
    capabilities: ClassVar[Caps] = Caps(
        list=True, range=True, delete=True, live=True, station_mode=True, gps=False
    )

    def __init__(self, http: Any) -> None:
        super().__init__(http)
        self._heartbeat: asyncio.Task[None] | None = None

    # ---------------------------------------------------------------- commands

    def _cmd_path(self, cmd: int, par: int | str | None = None, text: str | None = None) -> str:
        path = f"/?custom=1&cmd={cmd}"
        if par is not None:
            path += f"&par={par}"
        if text is not None:
            path += f"&str={quote(text, safe='')}"
        return path

    async def _cmd(
        self,
        cmd: int,
        par: int | str | None = None,
        text: str | None = None,
        *,
        serialise: bool = True,
    ) -> Any:
        root = await self.http.get_xml(self._cmd_path(cmd, par, text), serialise=serialise)
        if root.tag != "Function":
            raise ProtocolError(f"cmd {cmd}: unexpected root <{root.tag}>")
        return root

    @staticmethod
    def _status_ok(root: Any) -> bool:
        status = root.findtext("Status")
        return status is not None and status.strip() == "0"

    # ------------------------------------------------------------------- probe

    async def probe(self, host: str) -> Probe | None:
        self.attach(host)
        try:
            root = await self._cmd(CMD_HEARTBEAT)
        except Exception:
            return None
        cmd = (root.findtext("Cmd") or "").strip()
        if cmd != str(CMD_HEARTBEAT) or root.findtext("Status") is None:
            return None
        firmware = None
        try:
            fw = await self._cmd(CMD_FW_VERSION)
            firmware = (fw.findtext("String") or fw.findtext("Value") or "").strip() or None
        except Exception:  # firmware lookup is optional
            pass
        return Probe(
            driver=self.name,
            confidence=0.95,
            host=host,
            firmware=firmware,
            fingerprint={"heartbeat": "xml"},
        )

    async def info(self) -> DeviceInfo:
        free: int | None = None
        try:
            root = await self._cmd(CMD_FREE_SPACE)
            value = (root.findtext("Value") or "").strip()
            if value.isdigit():
                free = int(value)
        except Exception:
            pass
        firmware = None
        try:
            fw = await self._cmd(CMD_FW_VERSION)
            firmware = (fw.findtext("String") or fw.findtext("Value") or "").strip() or None
        except Exception:
            pass
        return DeviceInfo(firmware=firmware, model=None, sd_state="ready", free_bytes=free)

    # --------------------------------------------------------- mode switching

    async def prepare(self) -> None:
        """Stop recording and enter playback. Listing only works there."""
        await self._cmd(CMD_RECORD, 0)  # stop; a non-zero status just means "not recording"
        root = await self._cmd(CMD_MODE, MODE_PLAYBACK)
        if not self._status_ok(root):
            raise ProtocolError("camera refused playback mode")

    async def restore(self) -> None:
        """Back to movie mode and recording. Safe to call repeatedly."""
        await self.stop_heartbeat()
        await self._cmd(CMD_MODE, MODE_MOVIE)
        await self._cmd(CMD_RECORD, 1)

    # ----------------------------------------------------------------- listing

    async def list_recordings(self) -> list[Recording]:
        root = await self.http.get_xml(self._cmd_path(CMD_FILE_LIST))
        if root.tag == "Function" and not self._status_ok(root):
            raise ProtocolError("camera refused the file list (not in playback mode?)")
        recs: list[Recording] = []
        for node in root.iter("File"):
            rec = self._parse_file(node)
            if rec is not None:
                recs.append(rec)
        return recs

    def _parse_file(self, node: Any) -> Recording | None:
        fpath = (node.findtext("FPATH") or "").strip()
        name = (node.findtext("NAME") or "").strip()
        if not fpath and not name:
            return None
        path = fpath or name
        try:
            size = int((node.findtext("SIZE") or "0").strip())
        except ValueError:
            return None
        kind, channel, locked = classify(path)
        attr = (node.findtext("ATTR") or "").strip()
        if attr.isdigit() and int(attr) & 0x01:  # FAT read-only bit: user or impact locked
            locked = True
        start = self._parse_time((node.findtext("TIME") or "").strip(), name or path)
        return Recording(
            path=path, size=size, start=start, kind=kind, channel=channel, locked=locked
        )

    @staticmethod
    def _parse_time(text: str, name: str) -> dt.datetime:
        for fmt in ("%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
            try:
                return naive_local_to_utc(dt.datetime.strptime(text, fmt))
            except ValueError:
                continue
        return timestamp_from_name(name) or EPOCH

    @staticmethod
    def url_path(rec: Recording) -> str:
        """``A:\\CARDV\\MOVIE\\x.MP4`` becomes ``/CARDV/MOVIE/x.MP4``."""
        path = rec.path.replace("\\", "/")
        if len(path) > 2 and path[1] == ":":
            path = path[2:]
        if not path.startswith("/"):
            path = "/" + path
        return quote(path, safe="/")

    # --------------------------------------------------------------- download

    async def open(self, rec: Recording, offset: int) -> ByteStream:
        await self._start_heartbeat()
        try:
            return await self.http.stream(self.url_path(rec), offset)
        except BaseException:
            await self.stop_heartbeat()
            raise

    async def delete(self, rec: Recording) -> None:
        await self._cmd(CMD_DELETE_ONE, text=rec.path)

    # --------------------------------------------------------------- heartbeat

    async def _start_heartbeat(self) -> None:
        """A heartbeat every 10 s stops the camera's Wi-Fi idle timeout.

        It deliberately bypasses the request lock: the lock is held by the
        running download, and these cameras accept a tiny command alongside it.
        """
        if self._heartbeat is not None and not self._heartbeat.done():
            return

        async def beat() -> None:
            while True:
                await asyncio.sleep(HEARTBEAT_INTERVAL)
                try:
                    await self._cmd(CMD_HEARTBEAT, serialise=False)
                except Exception as err:
                    _LOGGER.debug("Heartbeat failed: %s", err)

        self._heartbeat = asyncio.create_task(beat(), name="novatek-heartbeat")

    async def stop_heartbeat(self) -> None:
        task, self._heartbeat = self._heartbeat, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def close(self) -> None:
        await self.stop_heartbeat()

    # -------------------------------------------------------------------- misc

    def live_url(self) -> str | None:
        return f"rtsp://{self.hostname}/xxx.mov" if self.host else None

    async def move_to_station(self, ssid: str, password: str) -> None:
        """Send home Wi-Fi credentials, switch to station mode and reconnect."""
        await self._cmd(CMD_STATION_CREDENTIALS, text=f"{ssid}\n{password}")
        await self._cmd(CMD_STATION_MODE)
        await self._cmd(CMD_RECONNECT)

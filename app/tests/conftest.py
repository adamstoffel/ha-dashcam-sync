from __future__ import annotations

import datetime as dt
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import aiohttp
import pytest
import pytest_asyncio

from dashcam_sync.catalog import Catalog
from fakecam import FakeCamera


@pytest.fixture
def catalog() -> Iterator[Catalog]:
    cat = Catalog(":memory:")
    yield cat
    cat.close()


@pytest_asyncio.fixture
async def session() -> AsyncIterator[aiohttp.ClientSession]:
    async with aiohttp.ClientSession() as sess:
        yield sess


@pytest_asyncio.fixture
async def novatek_cam() -> AsyncIterator[FakeCamera]:
    cam = FakeCamera("novatek")
    await cam.start()
    yield cam
    await cam.stop()


@pytest.fixture
def tmp_media(tmp_path: Path) -> Path:
    media = tmp_path / "media"
    media.mkdir()
    return media


def at(hour: int, minute: int = 0, second: int = 0, day: int = 22) -> dt.datetime:
    return dt.datetime(2026, 11, day, hour, minute, second, tzinfo=dt.UTC)

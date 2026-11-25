"""Camera drivers. Each family is one class behind the interface in ``base``."""

from __future__ import annotations

from .base import BaseDriver, Driver
from .http_index import HttpIndexDriver
from .novatek import NovatekDriver
from .viidure import ViidureDriver

# Probe order: most specific first, the generic HTTP index last.
DRIVERS: tuple[type[BaseDriver], ...] = (NovatekDriver, ViidureDriver, HttpIndexDriver)
DRIVER_BY_NAME: dict[str, type[BaseDriver]] = {cls.name: cls for cls in DRIVERS}

__all__ = [
    "DRIVERS",
    "DRIVER_BY_NAME",
    "BaseDriver",
    "Driver",
    "HttpIndexDriver",
    "NovatekDriver",
    "ViidureDriver",
]

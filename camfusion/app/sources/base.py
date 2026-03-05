from __future__ import annotations

import abc
import logging
from typing import Optional


class BaseCameraSource(abc.ABC):
    """Interface for any camera-like source that can produce frames."""

    def __init__(self, name: str, config: dict, logger: logging.Logger) -> None:
        self.name = name
        self.config = config
        self.logger = logger
        self.last_error: Optional[str] = None
        self.ok: bool = True

    async def start(self) -> None:
        """Optional source startup hook."""

    async def stop(self) -> None:
        """Optional source shutdown hook."""

    @abc.abstractmethod
    async def snapshot_frame(self) -> bytes:
        """Return a JPEG frame for this source."""

    def stream_url(self) -> Optional[str]:
        """Optional stream URL for ffmpeg live compositing."""
        return None

    def mark_ok(self) -> None:
        self.ok = True
        self.last_error = None

    def mark_failed(self, err: Exception | str) -> None:
        self.ok = False
        self.last_error = str(err)

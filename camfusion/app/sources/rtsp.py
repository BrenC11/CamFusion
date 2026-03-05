from __future__ import annotations

import asyncio
import logging
import subprocess
from typing import Optional

from .base import BaseCameraSource


class RTSPCameraSource(BaseCameraSource):
    def __init__(self, name: str, config: dict, logger: logging.Logger) -> None:
        super().__init__(name=name, config=config, logger=logger)
        self.url = config.get("url", "")

    def _snapshot_with_ffmpeg(self) -> bytes:
        if not self.url:
            raise ValueError(f"{self.name}: missing RTSP url")

        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-rtsp_transport",
            "tcp",
            "-fflags",
            "nobuffer",
            "-flags",
            "low_delay",
            "-i",
            self.url,
            "-frames:v",
            "1",
            "-f",
            "image2pipe",
            "-vcodec",
            "mjpeg",
            "pipe:1",
        ]

        proc = subprocess.run(cmd, capture_output=True, timeout=8, check=False)
        if proc.returncode != 0 or not proc.stdout:
            raise RuntimeError(f"{self.name}: ffmpeg failed to capture RTSP snapshot")
        return proc.stdout

    async def snapshot_frame(self) -> bytes:
        try:
            frame = await asyncio.to_thread(self._snapshot_with_ffmpeg)
            self.mark_ok()
            return frame
        except Exception as err:  # pragma: no cover
            self.mark_failed(err)
            raise

    def stream_url(self) -> Optional[str]:
        return self.url or None

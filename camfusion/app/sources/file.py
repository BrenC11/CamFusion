from __future__ import annotations

import asyncio
import logging
import subprocess
from pathlib import Path
from typing import Optional

from .base import BaseCameraSource


class FileCameraSource(BaseCameraSource):
    def __init__(self, name: str, config: dict, logger: logging.Logger) -> None:
        super().__init__(name=name, config=config, logger=logger)
        self.path = config.get("path", "")
        self._seek_offset = 0.0
        self._seek_step = float(config.get("seek_step", 0.4) or 0.4)

    def _snapshot_with_ffmpeg(self) -> bytes:
        path = Path(self.path)
        if not self.path:
            raise ValueError(f"{self.name}: missing file path")
        if not path.exists():
            raise FileNotFoundError(f"{self.name}: file not found: {self.path}")

        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            f"{self._seek_offset:.3f}",
            "-i",
            str(path),
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
            self._seek_offset = 0.0
            cmd[5] = "0.000"
            proc = subprocess.run(cmd, capture_output=True, timeout=8, check=False)

        if proc.returncode != 0 or not proc.stdout:
            raise RuntimeError(f"{self.name}: ffmpeg failed to capture file snapshot")

        self._seek_offset += self._seek_step
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
        return self.path or None

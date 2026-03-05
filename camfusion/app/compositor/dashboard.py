from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.sources.base import BaseCameraSource
from app.util.images import compose_panorama, decode_image, encode_jpeg


class DashboardCompositor:
    """Low-CPU snapshot compositor for dashboard-friendly operation."""

    def __init__(
        self,
        *,
        sources: list[BaseCameraSource],
        input_cfgs: list[dict],
        settings: dict,
        frame_store: Any,
        logger: logging.Logger,
    ) -> None:
        self.sources = sources
        self.input_cfgs = input_cfgs
        self.settings = settings
        self.frame_store = frame_store
        self.logger = logger
        self._task: asyncio.Task | None = None
        self._stopping = False

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run_loop(), name="dashboard-compositor")

    async def stop(self) -> None:
        self._stopping = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _run_loop(self) -> None:
        fps = max(1, int(self.settings.get("fps", 3)))
        interval = 1.0 / fps

        while not self._stopping:
            tick_start = asyncio.get_running_loop().time()
            await self._render_once()

            elapsed = asyncio.get_running_loop().time() - tick_start
            await asyncio.sleep(max(0.0, interval - elapsed))

    async def _render_once(self) -> None:
        tasks = [asyncio.create_task(source.snapshot_frame()) for source in self.sources]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        decoded_images = []
        ok_count = 0
        failed_count = 0

        for idx, result in enumerate(results):
            if isinstance(result, Exception):
                failed_count += 1
                self.logger.warning("Source %s failed: %s", self.sources[idx].name, result)
                decoded_images.append(None)
                continue

            try:
                decoded_images.append(decode_image(result))
                ok_count += 1
            except Exception as err:  # pragma: no cover
                failed_count += 1
                self.logger.warning("Failed to decode source %s frame: %s", self.sources[idx].name, err)
                decoded_images.append(None)

        canvas = compose_panorama(
            decoded_images,
            source_cfgs=self.input_cfgs,
            layout=self.settings.get("layout", "hstack"),
            output_width=int(self.settings.get("output_width", 1280)),
            output_height=int(self.settings.get("output_height", 0)),
        )
        jpeg = encode_jpeg(canvas, quality=82)

        self.frame_store.update(
            jpeg=jpeg,
            source_ok=ok_count,
            source_failed=failed_count,
            mode=self.settings.get("mode", "dashboard"),
            fps=max(1, int(self.settings.get("fps", 3))),
        )

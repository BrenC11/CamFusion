from __future__ import annotations

import asyncio
import logging
from typing import Any

import numpy as np
from PIL import Image, ImageFilter

from app.sources.base import BaseCameraSource
from app.util.images import compose_panorama, decode_image, encode_jpeg

try:
    import cv2
except Exception:  # pragma: no cover
    cv2 = None


class ExperimentalCompositor:
    """Experimental compositor with optional OpenCV warp transforms and seam feathering."""

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
        if cv2 is None:
            self.logger.warning("OpenCV not available; experimental mode running without warp transforms")
        if self._task is None:
            self._task = asyncio.create_task(self._run_loop(), name="experimental-compositor")

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
        fps = max(1, int(self.settings.get("fps", 8)))
        interval = 1.0 / fps

        while not self._stopping:
            tick_start = asyncio.get_running_loop().time()
            await self._render_once()
            elapsed = asyncio.get_running_loop().time() - tick_start
            await asyncio.sleep(max(0.0, interval - elapsed))

    async def _render_once(self) -> None:
        tasks = [asyncio.create_task(source.snapshot_frame()) for source in self.sources]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        decoded_images: list[Image.Image | None] = []
        ok_count = 0
        failed_count = 0

        for idx, result in enumerate(results):
            if isinstance(result, Exception):
                failed_count += 1
                self.logger.warning("Source %s failed in experimental mode: %s", self.sources[idx].name, result)
                decoded_images.append(None)
                continue

            try:
                image = decode_image(result)
                image = self._apply_optional_warp(image, self.input_cfgs[idx] if idx < len(self.input_cfgs) else {})
                decoded_images.append(image)
                ok_count += 1
            except Exception as err:  # pragma: no cover
                failed_count += 1
                self.logger.warning("Experimental decode/warp failed for %s: %s", self.sources[idx].name, err)
                decoded_images.append(None)

        canvas = compose_panorama(
            decoded_images,
            source_cfgs=self.input_cfgs,
            layout=self.settings.get("layout", "hstack"),
            output_width=int(self.settings.get("output_width", 1280)),
            output_height=int(self.settings.get("output_height", 0)),
        )
        canvas = self._apply_optional_feather(canvas)

        jpeg = encode_jpeg(canvas, quality=80)
        self.frame_store.update(
            jpeg=jpeg,
            source_ok=ok_count,
            source_failed=failed_count,
            mode="experimental",
            fps=max(1, int(self.settings.get("fps", 8))),
        )

    def _apply_optional_warp(self, image: Image.Image, source_cfg: dict) -> Image.Image:
        if cv2 is None:
            return image

        warp_cfg = source_cfg.get("warp") or {}
        affine = warp_cfg.get("affine")
        perspective = warp_cfg.get("perspective")
        if not affine and not perspective:
            return image

        arr = np.array(image)
        h, w = arr.shape[:2]

        try:
            if perspective:
                vals = [float(x.strip()) for x in str(perspective).split(",")]
                if len(vals) != 9:
                    raise ValueError("perspective warp expects 9 comma-separated values")
                matrix = np.array(vals, dtype=np.float32).reshape((3, 3))
                warped = cv2.warpPerspective(arr, matrix, (w, h), borderMode=cv2.BORDER_REFLECT)
                return Image.fromarray(warped)

            vals = [float(x.strip()) for x in str(affine).split(",")]
            if len(vals) != 6:
                raise ValueError("affine warp expects 6 comma-separated values")
            matrix = np.array(vals, dtype=np.float32).reshape((2, 3))
            warped = cv2.warpAffine(arr, matrix, (w, h), borderMode=cv2.BORDER_REFLECT)
            return Image.fromarray(warped)
        except Exception as err:
            self.logger.warning("Invalid warp config for source; using original frame: %s", err)
            return image

    def _apply_optional_feather(self, image: Image.Image) -> Image.Image:
        feather_px = int(self.settings.get("blend_feather_px", 0) or 0)
        layout = str(self.settings.get("layout", "hstack"))
        if feather_px <= 0:
            return image
        if layout not in {"hstack", "3x1"}:
            return image

        source_count = max(1, len(self.sources))
        panel_width = max(1, image.width // source_count)

        canvas = image.copy()
        for idx in range(1, source_count):
            seam_x = idx * panel_width
            left = max(0, seam_x - feather_px)
            right = min(image.width, seam_x + feather_px)
            strip = canvas.crop((left, 0, right, image.height)).filter(
                ImageFilter.GaussianBlur(radius=max(1, feather_px // 2))
            )
            canvas.paste(strip, (left, 0))

        return canvas

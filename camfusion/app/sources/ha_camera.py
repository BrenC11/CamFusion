from __future__ import annotations

import io
import logging

import httpx
from PIL import Image

from .base import BaseCameraSource


class HACameraSource(BaseCameraSource):
    CORE_API_BASE = "http://supervisor/core/api"

    def __init__(self, name: str, config: dict, logger: logging.Logger, supervisor_token: str) -> None:
        super().__init__(name=name, config=config, logger=logger)
        self.entity_id = config.get("entity_id", "")
        self._token = supervisor_token
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=3.0, read=8.0, write=5.0, pool=5.0),
            headers={"Authorization": f"Bearer {self._token}"},
        )

    async def stop(self) -> None:
        await self._client.aclose()

    def snapshot_url(self) -> str:
        return f"{self.CORE_API_BASE}/camera_proxy/{self.entity_id}"

    def stream_url(self) -> str:
        return f"{self.CORE_API_BASE}/camera_proxy_stream/{self.entity_id}"

    async def snapshot_frame(self) -> bytes:
        if not self.entity_id:
            raise ValueError(f"{self.name}: missing entity_id")

        try:
            response = await self._client.get(self.snapshot_url())
            response.raise_for_status()
            content = response.content
            content_type = response.headers.get("content-type", "")

            if "jpeg" in content_type or "jpg" in content_type:
                self.mark_ok()
                return content

            image = Image.open(io.BytesIO(content)).convert("RGB")
            out = io.BytesIO()
            image.save(out, format="JPEG", quality=82)
            self.mark_ok()
            return out.getvalue()
        except Exception as err:  # pragma: no cover
            self.mark_failed(err)
            raise

    async def stream_available(self) -> bool:
        """Probe stream endpoint quickly before using it in ffmpeg LIVE mode."""
        if not self.entity_id:
            return False

        try:
            async with self._client.stream("GET", self.stream_url()) as response:
                if response.status_code >= 400:
                    return False
                chunk = await response.aiter_bytes().__anext__()
                return bool(chunk)
        except Exception:
            return False

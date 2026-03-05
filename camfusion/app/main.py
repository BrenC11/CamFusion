from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse, Response, StreamingResponse

from app.compositor.dashboard import DashboardCompositor
from app.compositor.experimental_opencv import ExperimentalCompositor
from app.compositor.live_ffmpeg import LiveFFmpegCompositor
from app.sources.base import BaseCameraSource
from app.sources.file import FileCameraSource
from app.sources.ha_camera import HACameraSource
from app.sources.rtsp import RTSPCameraSource
from app.util.images import encode_jpeg, placeholder
from app.util.mjpeg import multipart_chunk

LOGGER = logging.getLogger("camfusion")


@dataclass
class FrameStore:
    latest_frame: bytes | None = None
    last_frame_ts: float = 0.0
    frame_count: int = 0
    source_ok: int = 0
    source_failed: int = 0
    mode: str = "dashboard"
    fps: int = 3

    def update(self, *, jpeg: bytes, source_ok: int, source_failed: int, mode: str, fps: int) -> None:
        self.latest_frame = jpeg
        self.last_frame_ts = time.monotonic()
        self.frame_count += 1
        self.source_ok = source_ok
        self.source_failed = source_failed
        self.mode = mode
        self.fps = fps

    def last_frame_age_ms(self) -> int:
        if not self.last_frame_ts:
            return -1
        return int((time.monotonic() - self.last_frame_ts) * 1000)


@dataclass
class AppState:
    settings: dict = field(default_factory=dict)
    sources: list[BaseCameraSource] = field(default_factory=list)
    compositor: Any = None
    frame_store: FrameStore = field(default_factory=FrameStore)
    startup_error: str | None = None
    started_monotonic: float = field(default_factory=time.monotonic)
    placeholder_jpeg: bytes = field(default_factory=lambda: encode_jpeg(placeholder((1280, 720), "STARTING")))


state = AppState()
app = FastAPI(title="CamFusion", version="0.1.0")


def load_options() -> dict:
    options_file = os.environ.get("PANORAMA_OPTIONS_FILE", "/data/options.json")
    defaults = {
        "mode": "dashboard",
        "fps": 3,
        "layout": "hstack",
        "output_width": 1280,
        "output_height": 0,
        "inputs": [],
        "blend_feather_px": 0,
    }

    if not os.path.exists(options_file):
        LOGGER.warning("Options file %s not found; using defaults", options_file)
        return defaults

    with open(options_file, "r", encoding="utf-8") as f:
        loaded = json.load(f)

    merged = {**defaults, **loaded}
    merged["inputs"] = loaded.get("inputs", defaults["inputs"])

    if not isinstance(merged["inputs"], list):
        raise ValueError("inputs must be a list")
    if not (2 <= len(merged["inputs"]) <= 4):
        raise ValueError("inputs must include between 2 and 4 sources")

    merged["fps"] = int(merged.get("fps", 3))
    merged["output_width"] = int(merged.get("output_width", 1280))
    merged["output_height"] = int(merged.get("output_height", 0))
    merged["blend_feather_px"] = int(merged.get("blend_feather_px", 0) or 0)

    mode = str(merged.get("mode", "dashboard")).lower()
    if mode not in {"dashboard", "live", "experimental"}:
        raise ValueError(f"Unsupported mode: {mode}")
    merged["mode"] = mode

    layout = str(merged.get("layout", "hstack")).lower()
    if layout not in {"hstack", "2x2", "3x1"}:
        raise ValueError(f"Unsupported layout: {layout}")
    merged["layout"] = layout

    return merged


def build_sources(settings: dict, supervisor_token: str) -> list[BaseCameraSource]:
    sources: list[BaseCameraSource] = []

    for idx, raw_input in enumerate(settings.get("inputs", [])):
        if not isinstance(raw_input, dict):
            raise ValueError(f"inputs[{idx}] must be an object")

        source_type = str(raw_input.get("type", "")).strip()
        name = f"source_{idx + 1}"

        if source_type == "rtsp":
            sources.append(RTSPCameraSource(name=name, config=raw_input, logger=LOGGER))
            continue

        if source_type == "file":
            sources.append(FileCameraSource(name=name, config=raw_input, logger=LOGGER))
            continue

        if source_type == "ha_camera":
            if not supervisor_token:
                raise ValueError("SUPERVISOR_TOKEN (or HASSIO_TOKEN) is required for ha_camera sources")
            sources.append(
                HACameraSource(
                    name=name,
                    config=raw_input,
                    logger=LOGGER,
                    supervisor_token=supervisor_token,
                )
            )
            continue

        raise ValueError(f"Unsupported source type for inputs[{idx}]: {source_type}")

    return sources


async def build_compositor(settings: dict, supervisor_token: str) -> Any:
    mode = settings.get("mode", "dashboard")

    if mode == "dashboard":
        return DashboardCompositor(
            sources=state.sources,
            input_cfgs=settings.get("inputs", []),
            settings=settings,
            frame_store=state.frame_store,
            logger=LOGGER,
        )

    if mode == "live":
        return LiveFFmpegCompositor(
            sources=state.sources,
            input_cfgs=settings.get("inputs", []),
            settings=settings,
            frame_store=state.frame_store,
            logger=LOGGER,
            supervisor_token=supervisor_token,
        )

    return ExperimentalCompositor(
        sources=state.sources,
        input_cfgs=settings.get("inputs", []),
        settings=settings,
        frame_store=state.frame_store,
        logger=LOGGER,
    )


@app.on_event("startup")
async def on_startup() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )

    try:
        settings = load_options()
        supervisor_token = os.environ.get("SUPERVISOR_TOKEN") or os.environ.get("HASSIO_TOKEN", "")

        state.settings = settings
        state.sources = build_sources(settings, supervisor_token)

        for source in state.sources:
            await source.start()

        state.compositor = await build_compositor(settings, supervisor_token)
        await state.compositor.start()
        LOGGER.info("camfusion started in %s mode with %d source(s)", settings["mode"], len(state.sources))
    except Exception as err:
        state.startup_error = str(err)
        LOGGER.exception("camfusion startup failed: %s", err)


@app.on_event("shutdown")
async def on_shutdown() -> None:
    if state.compositor is not None:
        await state.compositor.stop()

    for source in state.sources:
        try:
            await source.stop()
        except Exception as err:  # pragma: no cover
            LOGGER.warning("Source shutdown warning (%s): %s", source.name, err)


@app.get("/snapshot.jpg")
async def snapshot() -> Response:
    frame = state.frame_store.latest_frame or state.placeholder_jpeg
    return Response(content=frame, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@app.get("/stream.mjpeg")
async def stream() -> StreamingResponse:
    fps = int(state.settings.get("fps", 3) or 3)
    interval = 1.0 / max(1, fps)

    async def generate() -> Any:
        while True:
            frame = state.frame_store.latest_frame or state.placeholder_jpeg
            yield multipart_chunk(frame)
            await asyncio.sleep(interval)

    return StreamingResponse(
        generate(),
        media_type="multipart/x-mixed-replace; boundary=frame",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/healthz")
async def healthz() -> JSONResponse:
    stats = {
        "status": "OK" if state.startup_error is None else "ERROR",
        "mode": state.frame_store.mode or state.settings.get("mode", "dashboard"),
        "fps": state.frame_store.fps or int(state.settings.get("fps", 3)),
        "sources_ok": state.frame_store.source_ok,
        "sources_failed": state.frame_store.source_failed,
        "last_frame_age_ms": state.frame_store.last_frame_age_ms(),
        "frames_emitted": state.frame_store.frame_count,
        "uptime_s": int(time.monotonic() - state.started_monotonic),
    }
    if state.startup_error:
        stats["error"] = state.startup_error
    return JSONResponse(stats)


if __name__ == "__main__":
    import uvicorn

    host = os.environ.get("PANORAMA_BIND_HOST", "0.0.0.0")
    port = int(os.environ.get("PANORAMA_PORT", "8099"))
    uvicorn.run("app.main:app", host=host, port=port, log_level="info")

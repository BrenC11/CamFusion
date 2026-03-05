from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.compositor.dashboard import DashboardCompositor
from app.sources.base import BaseCameraSource
from app.sources.ha_camera import HACameraSource
from app.util.mjpeg import MJPEGByteParser


def _normalize_pct(value: float | int | None) -> float:
    if value is None:
        return 0.0
    pct = float(value)
    if pct > 1.0:
        pct /= 100.0
    return min(max(pct, 0.0), 0.9)


def _layout_dimensions(layout: str, source_count: int) -> tuple[int, int]:
    if layout == "2x2" and source_count >= 3:
        return (2, 2)
    if layout == "3x1":
        return (3, 1)
    return (max(source_count, 1), 1)


class LiveFFmpegCompositor:
    """LIVE mode compositor using ffmpeg xstack when all inputs are streamable."""

    def __init__(
        self,
        *,
        sources: list[BaseCameraSource],
        input_cfgs: list[dict],
        settings: dict,
        frame_store: Any,
        logger: logging.Logger,
        supervisor_token: str,
    ) -> None:
        self.sources = sources
        self.input_cfgs = input_cfgs
        self.settings = settings
        self.frame_store = frame_store
        self.logger = logger
        self.supervisor_token = supervisor_token

        self._process: asyncio.subprocess.Process | None = None
        self._stdout_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._fallback: DashboardCompositor | None = None
        self._stopping = False

    async def start(self) -> None:
        command = await self._build_ffmpeg_command()
        if command is None:
            await self._start_snapshot_fallback("some sources are not ffmpeg-streamable")
            return

        try:
            self._process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except Exception as err:
            self.logger.warning("ffmpeg process failed to start; switching to snapshot fallback: %s", err)
            await self._start_snapshot_fallback("ffmpeg launch error")
            return

        self.logger.info("LIVE mode: ffmpeg compositor started")
        self._stdout_task = asyncio.create_task(self._read_stdout(), name="live-ffmpeg-stdout")
        self._stderr_task = asyncio.create_task(self._read_stderr(), name="live-ffmpeg-stderr")

    async def stop(self) -> None:
        self._stopping = True

        if self._fallback is not None:
            await self._fallback.stop()
            self._fallback = None

        if self._stdout_task is not None:
            self._stdout_task.cancel()
            try:
                await self._stdout_task
            except asyncio.CancelledError:
                pass
            self._stdout_task = None

        if self._stderr_task is not None:
            self._stderr_task.cancel()
            try:
                await self._stderr_task
            except asyncio.CancelledError:
                pass
            self._stderr_task = None

        await self._terminate_process()

    async def _terminate_process(self) -> None:
        process = self._process
        self._process = None
        if process is None:
            return

        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=3.0)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()

    async def _start_snapshot_fallback(self, reason: str) -> None:
        if self._fallback is not None:
            return

        self.logger.warning("LIVE mode fallback active (%s)", reason)
        fallback_settings = dict(self.settings)
        fallback_settings["mode"] = "live_snapshot_fallback"
        fallback_settings["fps"] = max(10, min(15, int(self.settings.get("fps", 12))))

        self._fallback = DashboardCompositor(
            sources=self.sources,
            input_cfgs=self.input_cfgs,
            settings=fallback_settings,
            frame_store=self.frame_store,
            logger=self.logger,
        )
        await self._fallback.start()

    async def _read_stderr(self) -> None:
        if self._process is None or self._process.stderr is None:
            return

        while not self._stopping:
            line = await self._process.stderr.readline()
            if not line:
                return
            msg = line.decode("utf-8", errors="replace").strip()
            if msg:
                self.logger.debug("ffmpeg: %s", msg)

    async def _read_stdout(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            return

        parser = MJPEGByteParser()
        received_any = False

        while not self._stopping:
            chunk = await process.stdout.read(32768)
            if not chunk:
                break

            for frame in parser.feed(chunk):
                received_any = True
                source_ok = sum(1 for src in self.sources if src.ok)
                source_failed = len(self.sources) - source_ok
                self.frame_store.update(
                    jpeg=frame,
                    source_ok=source_ok,
                    source_failed=source_failed,
                    mode="live",
                    fps=max(10, min(15, int(self.settings.get("fps", 12)))),
                )

        if self._stopping:
            return

        await self._terminate_process()
        if not received_any:
            await self._start_snapshot_fallback("no frames from ffmpeg stream")
        else:
            await self._start_snapshot_fallback("ffmpeg stream ended unexpectedly")

    async def _build_ffmpeg_command(self) -> list[str] | None:
        if len(self.sources) == 0:
            return None

        input_specs: list[tuple[str, dict, BaseCameraSource]] = []
        for idx, source in enumerate(self.sources):
            cfg = self.input_cfgs[idx] if idx < len(self.input_cfgs) else {}
            source_type = str(cfg.get("type", "")).strip()

            if source_type == "ha_camera":
                if not isinstance(source, HACameraSource):
                    return None
                if not await source.stream_available():
                    return None
                input_specs.append(("ha_camera", cfg, source))
                continue

            if source_type == "rtsp":
                if not source.stream_url():
                    return None
                input_specs.append(("rtsp", cfg, source))
                continue

            if source_type == "file":
                if not source.stream_url():
                    return None
                input_specs.append(("file", cfg, source))
                continue

            return None

        ffmpeg_cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "warning",
            "-nostdin",
        ]

        for input_type, _cfg, source in input_specs:
            if input_type == "rtsp":
                ffmpeg_cmd.extend(
                    [
                        "-rtsp_transport",
                        "tcp",
                        "-fflags",
                        "nobuffer",
                        "-flags",
                        "low_delay",
                        "-probesize",
                        "32768",
                        "-analyzeduration",
                        "1000000",
                        "-i",
                        source.stream_url() or "",
                    ]
                )
            elif input_type == "file":
                ffmpeg_cmd.extend(["-stream_loop", "-1", "-re", "-i", source.stream_url() or ""])
            else:  # ha_camera
                ffmpeg_cmd.extend(
                    [
                        "-fflags",
                        "nobuffer",
                        "-flags",
                        "low_delay",
                        "-headers",
                        f"Authorization: Bearer {self.supervisor_token}\r\n",
                        "-i",
                        source.stream_url() or "",
                    ]
                )

        filter_complex = self._build_filter_complex(len(input_specs))
        fps = max(10, min(15, int(self.settings.get("fps", 12))))

        ffmpeg_cmd.extend(
            [
                "-filter_complex",
                filter_complex,
                "-map",
                "[vout]",
                "-r",
                str(fps),
                "-q:v",
                "6",
                "-f",
                "mjpeg",
                "pipe:1",
            ]
        )

        return ffmpeg_cmd

    def _build_filter_complex(self, source_count: int) -> str:
        layout = str(self.settings.get("layout", "hstack"))
        output_width = int(self.settings.get("output_width", 1280))
        output_height = int(self.settings.get("output_height", 0))

        cols, rows = _layout_dimensions(layout, source_count)
        panel_width = max(1, output_width // cols)
        if output_height > 0:
            panel_height = max(1, output_height // rows)
        else:
            panel_height = max(1, int(panel_width * 9 / 16))

        chains: list[str] = []
        for idx in range(source_count):
            cfg = self.input_cfgs[idx] if idx < len(self.input_cfgs) else {}
            crop_cfg = cfg.get("crop") or {}

            left = _normalize_pct(crop_cfg.get("left", 0))
            right = _normalize_pct(crop_cfg.get("right", 0))
            top = _normalize_pct(crop_cfg.get("top", 0))
            bottom = _normalize_pct(crop_cfg.get("bottom", 0))
            crop_w = max(0.05, 1.0 - left - right)
            crop_h = max(0.05, 1.0 - top - bottom)

            scale = max(0.1, float(cfg.get("scale", 1.0) or 1.0))

            chain = (
                f"[{idx}:v]"
                f"crop=iw*{crop_w:.6f}:ih*{crop_h:.6f}:iw*{left:.6f}:ih*{top:.6f},"
                f"scale=iw*{scale:.6f}:ih*{scale:.6f},"
                f"scale={panel_width}:{panel_height}:force_original_aspect_ratio=decrease,"
                f"pad={panel_width}:{panel_height}:(ow-iw)/2:(oh-ih)/2:black"
                f"[v{idx}]"
            )
            chains.append(chain)

        if layout == "2x2" and source_count >= 4:
            layout_expr = "0_0|w0_0|0_h0|w0_h0"
        else:
            x_positions = []
            for idx in range(source_count):
                if idx == 0:
                    x_positions.append("0_0")
                else:
                    width_chain = "+".join(f"w{j}" for j in range(idx))
                    x_positions.append(f"{width_chain}_0")
            layout_expr = "|".join(x_positions)

        input_refs = "".join(f"[v{idx}]" for idx in range(source_count))
        chains.append(f"{input_refs}xstack=inputs={source_count}:layout={layout_expr}[vout]")
        return ";".join(chains)

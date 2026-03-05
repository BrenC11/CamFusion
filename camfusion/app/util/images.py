from __future__ import annotations

import io
from typing import Iterable

from PIL import Image, ImageDraw, ImageFont, ImageOps


def decode_image(image_bytes: bytes) -> Image.Image:
    return Image.open(io.BytesIO(image_bytes)).convert("RGB")


def encode_jpeg(image: Image.Image, quality: int = 82) -> bytes:
    out = io.BytesIO()
    image.save(out, format="JPEG", quality=quality)
    return out.getvalue()


def _normalize_pct(value: float | int | None) -> float:
    if value is None:
        return 0.0
    pct = float(value)
    if pct > 1.0:
        pct /= 100.0
    return min(max(pct, 0.0), 0.9)


def _layout_dimensions(layout: str, source_count: int) -> tuple[int, int]:
    if layout == "2x2":
        if source_count <= 2:
            return (2, 1)
        return (2, 2)
    if layout == "3x1":
        return (3, 1)
    return (max(source_count, 1), 1)


def _prepare_panel(image: Image.Image, panel_size: tuple[int, int], source_cfg: dict) -> Image.Image:
    panel_w, panel_h = panel_size
    crop = source_cfg.get("crop") or {}

    left = _normalize_pct(crop.get("left", 0))
    right = _normalize_pct(crop.get("right", 0))
    top = _normalize_pct(crop.get("top", 0))
    bottom = _normalize_pct(crop.get("bottom", 0))

    width, height = image.size
    crop_width = max(1, int(width * (1.0 - left - right)))
    crop_height = max(1, int(height * (1.0 - top - bottom)))
    crop_x = int(width * left)
    crop_y = int(height * top)
    image = image.crop((crop_x, crop_y, crop_x + crop_width, crop_y + crop_height))

    scale = float(source_cfg.get("scale", 1.0) or 1.0)
    if scale != 1.0:
        scaled_w = max(1, int(image.width * scale))
        scaled_h = max(1, int(image.height * scale))
        image = image.resize((scaled_w, scaled_h), Image.Resampling.BICUBIC)

    return ImageOps.fit(
        image,
        (panel_w, panel_h),
        method=Image.Resampling.BICUBIC,
        centering=(0.5, 0.5),
    )


def placeholder(size: tuple[int, int], text: str = "NO SIGNAL") -> Image.Image:
    image = Image.new("RGB", size, color=(25, 25, 25))
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default()

    draw.rectangle((0, 0, size[0] - 1, size[1] - 1), outline=(210, 70, 70), width=4)
    text_bbox = draw.textbbox((0, 0), text, font=font)
    text_w = text_bbox[2] - text_bbox[0]
    text_h = text_bbox[3] - text_bbox[1]
    draw.text(((size[0] - text_w) / 2, (size[1] - text_h) / 2), text, fill=(240, 240, 240), font=font)
    return image


def compose_panorama(
    images: Iterable[Image.Image | None],
    source_cfgs: list[dict],
    layout: str,
    output_width: int,
    output_height: int = 0,
) -> Image.Image:
    image_list = list(images)
    cols, rows = _layout_dimensions(layout, len(image_list))
    panel_width = max(1, output_width // cols)

    if output_height > 0:
        panel_height = max(1, output_height // rows)
        canvas_height = output_height
    else:
        panel_height = max(1, int(panel_width * 9 / 16))
        canvas_height = panel_height * rows

    canvas = Image.new("RGB", (output_width, canvas_height), color=(0, 0, 0))

    slot_count = cols * rows
    for idx in range(slot_count):
        col = idx % cols
        row = idx // cols
        x = col * panel_width
        y = row * panel_height

        cfg = source_cfgs[idx] if idx < len(source_cfgs) else {}
        panel = None

        if idx < len(image_list) and image_list[idx] is not None:
            panel = _prepare_panel(image_list[idx], (panel_width, panel_height), cfg)
        else:
            panel = placeholder((panel_width, panel_height))

        canvas.paste(panel, (x, y))

    return canvas

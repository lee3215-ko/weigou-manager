# -*- coding: utf-8 -*-
"""Upscale and sharpen low-resolution product photos with Pillow."""
from __future__ import annotations

import pathlib

try:
    from PIL import Image, ImageFilter, ImageEnhance, ImageOps
except ImportError:  # pragma: no cover
    Image = None  # type: ignore


MIN_LONG_EDGE = 1400
TARGET_LONG_EDGE = 1800
JPEG_QUALITY = 92


def needs_upscale(width: int, height: int, min_long: int = MIN_LONG_EDGE) -> bool:
    return max(width, height) < min_long


def enhance_image_file(src: pathlib.Path, dest: pathlib.Path | None = None) -> pathlib.Path:
    """
    If image is small/soft, upscale with LANCZOS and apply light sharpening.
    Returns path written (dest or src).
    """
    if Image is None:
        return src

    out = dest or src
    try:
        with Image.open(src) as im:
            im = ImageOps.exif_transpose(im)
            if im.mode not in ("RGB", "L"):
                im = im.convert("RGB")
            elif im.mode == "L":
                im = im.convert("RGB")

            w, h = im.size
            long_edge = max(w, h)

            if needs_upscale(w, h):
                scale = TARGET_LONG_EDGE / float(long_edge)
                nw = max(1, int(round(w * scale)))
                nh = max(1, int(round(h * scale)))
                im = im.resize((nw, nh), Image.Resampling.LANCZOS)
                # mild unsharp after upscale
                im = im.filter(ImageFilter.UnsharpMask(radius=1.6, percent=140, threshold=2))
                im = ImageEnhance.Sharpness(im).enhance(1.12)
                im = ImageEnhance.Contrast(im).enhance(1.05)
                im = ImageEnhance.Color(im).enhance(1.04)
            else:
                # already large — light polish only
                im = im.filter(ImageFilter.UnsharpMask(radius=0.8, percent=80, threshold=3))

            out.parent.mkdir(parents=True, exist_ok=True)
            suffix = out.suffix.lower()
            if suffix in (".png", ".webp"):
                im.save(out, optimize=True)
            else:
                if out.suffix.lower() not in (".jpg", ".jpeg"):
                    out = out.with_suffix(".jpg")
                im.save(out, format="JPEG", quality=JPEG_QUALITY, optimize=True, progressive=True)
            return out
    except Exception:
        return src

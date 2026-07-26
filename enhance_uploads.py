# -*- coding: utf-8 -*-
"""Re-enhance all images already in shoot-repl/public/uploads."""
from __future__ import annotations

import pathlib
import sys

from image_enhance import enhance_image_file, needs_upscale

try:
    from PIL import Image
except ImportError:
    print("Pillow required")
    sys.exit(1)


def uploads_root() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parents[2] / "public" / "uploads"


def main() -> None:
    root = uploads_root()
    if not root.exists():
        print(f"No uploads folder: {root}")
        return

    files = [
        p
        for p in root.rglob("*")
        if p.is_file() and p.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}
    ]
    print(f"Found {len(files)} images under {root}")
    ok = 0
    for path in files:
        try:
            with Image.open(path) as im:
                w, h = im.size
            before = f"{w}x{h}"
            did = needs_upscale(w, h)
            enhance_image_file(path)
            with Image.open(path) as im2:
                after = f"{im2.size[0]}x{im2.size[1]}"
            print(f"{'[UP]' if did else '[OK]'} {path.relative_to(root)}  {before} -> {after}")
            ok += 1
        except Exception as e:
            print(f"[ERR] {path}: {e}")
    print(f"Done: {ok}/{len(files)}")


if __name__ == "__main__":
    main()

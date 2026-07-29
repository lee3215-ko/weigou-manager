# -*- coding: utf-8 -*-
"""Fast remote thumbnail fetch for manager preview (disk cache + CDN resize)."""
from __future__ import annotations

import hashlib
import pathlib
import urllib.error
import urllib.request

from paths import get_catalog_root

# Preview size — small enough for UI, big enough to look sharp on 180px cells.
THUMB_EDGE = 360
THUMB_JPEG_QUALITY = 72
CACHE_SUBDIR = "thumb_cache"
MAX_CACHE_FILES = 4000

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://www.szwego.com/",
}


def thumb_cache_dir() -> pathlib.Path:
    d = pathlib.Path(get_catalog_root()) / CACHE_SUBDIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def _url_key(url: str) -> str:
    return hashlib.sha1(url.encode("utf-8", "replace")).hexdigest()


def cache_path_for(url: str) -> pathlib.Path:
    return thumb_cache_dir() / f"{_url_key(url)}.jpg"


def preview_fetch_url(url: str) -> str:
    """Ask CDN for a small JPEG when host supports imageMogr2 (szwego)."""
    if not url or not url.startswith("http"):
        return url
    base = url.split("?")[0]
    low = base.lower()
    if "szwego.com" in low or "xcimg." in low:
        # Qiniu-style transform used by Weigou CDNs
        return (
            f"{base}?imageMogr2/thumbnail/{THUMB_EDGE}x"
            f"/format/jpg/quality/{THUMB_JPEG_QUALITY}"
        )
    return url


def prune_thumb_cache(max_files: int = MAX_CACHE_FILES) -> None:
    """Drop oldest cached thumbs when the folder grows too large."""
    root = thumb_cache_dir()
    try:
        files = sorted(root.glob("*.jpg"), key=lambda p: p.stat().st_mtime)
    except OSError:
        return
    excess = len(files) - max_files
    if excess <= 0:
        return
    for p in files[:excess]:
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass


def fetch_thumb_file(url: str, timeout: float = 5.0) -> pathlib.Path | None:
    """Return a local JPEG path for ``url`` (cache hit or fresh download)."""
    if not url or not url.startswith("http"):
        return None
    dest = cache_path_for(url)
    if dest.exists() and dest.stat().st_size > 200:
        return dest

    fetch_url = preview_fetch_url(url)
    req = urllib.request.Request(fetch_url, headers=_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()
    except (urllib.error.URLError, TimeoutError, OSError):
        # Fallback: original URL without CDN transform
        if fetch_url == url:
            return None
        try:
            req2 = urllib.request.Request(url, headers=_HEADERS)
            with urllib.request.urlopen(req2, timeout=timeout) as resp:
                data = resp.read()
        except (urllib.error.URLError, TimeoutError, OSError):
            return None

    if not data or len(data) < 200:
        return None
    try:
        tmp = dest.with_suffix(".tmp")
        tmp.write_bytes(data)
        tmp.replace(dest)
    except OSError:
        return None
    return dest if dest.exists() else None

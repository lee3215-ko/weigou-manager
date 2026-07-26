# -*- coding: utf-8 -*-
from __future__ import annotations

import concurrent.futures
import pathlib
import re
import urllib.error
import urllib.request
from typing import Callable


ProgressCb = Callable[[str], None]


def _safe_name(url: str, index: int) -> str:
    name = url.rstrip("/").split("/")[-1]
    name = re.sub(r"[^\w.\-]+", "_", name)
    if not name.lower().endswith((".jpg", ".jpeg", ".png", ".webp", ".gif")):
        name += ".jpg"
    return f"{index:04d}_{name}"


def download_one(url: str, dest: pathlib.Path, timeout: float = 30.0) -> tuple[bool, str]:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://www.szwego.com/",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()
        if len(data) < 200:
            return False, f"too_small:{url}"
        dest.write_bytes(data)
        return True, str(dest)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return False, f"{type(e).__name__}:{e}"


def download_all(
    urls: list[str],
    out_dir: pathlib.Path,
    workers: int = 8,
    on_progress: ProgressCb | None = None,
) -> tuple[int, int]:
    out_dir.mkdir(parents=True, exist_ok=True)
    ok = 0
    fail = 0
    total = len(urls)

    def log(msg: str) -> None:
        if on_progress:
            on_progress(msg)

    log(f"저장 폴더: {out_dir}")
    log(f"다운로드 시작: {total}장")

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {}
        for i, url in enumerate(urls, start=1):
            path = out_dir / _safe_name(url, i)
            futures[pool.submit(download_one, url, path)] = (i, url)

        done = 0
        for fut in concurrent.futures.as_completed(futures):
            done += 1
            i, url = futures[fut]
            success, detail = fut.result()
            if success:
                ok += 1
            else:
                fail += 1
                log(f"실패 [{i}] {detail}")
            if done % 10 == 0 or done == total:
                log(f"진행 {done}/{total} (성공 {ok} / 실패 {fail})")

    log(f"완료: 성공 {ok} / 실패 {fail}")
    return ok, fail

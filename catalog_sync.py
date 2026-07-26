# -*- coding: utf-8 -*-
"""Multi-user catalog sync via GitHub (shared/catalog-sync.json)."""
from __future__ import annotations

import base64
import json
import pathlib
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

from paths import UPDATE_VERSION_URL, data_path
from product_store import ProductStore

ProgressCb = Callable[[str], None]

DEFAULT_SETTINGS = {
    "enabled": True,
    "github_owner": "lee3215-ko",
    "github_repo": "weigou-manager",
    "github_token": "",
    "branch": "main",
    "path": "shared/catalog-sync.json",
    "interval_sec": 12,
    "device_name": "",
}


def load_sync_settings() -> dict[str, Any]:
    path = pathlib.Path(data_path("sync_settings.json"))
    cfg = dict(DEFAULT_SETTINGS)
    # Infer owner/repo from UPDATE_VERSION_URL when possible
    try:
        # .../lee3215-ko/weigou-manager/main/version.json
        parts = UPDATE_VERSION_URL.strip("/").split("/")
        if "githubusercontent.com" in UPDATE_VERSION_URL and len(parts) >= 6:
            cfg["github_owner"] = parts[3]
            cfg["github_repo"] = parts[4]
    except Exception:
        pass
    if path.is_file():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                cfg.update({k: raw[k] for k in raw if k in DEFAULT_SETTINGS or k == "github_token"})
        except Exception:
            pass
    return cfg


def save_sync_settings(cfg: dict[str, Any]) -> None:
    path = pathlib.Path(data_path("sync_settings.json"))
    path.parent.mkdir(parents=True, exist_ok=True)
    out = dict(DEFAULT_SETTINGS)
    out.update(cfg)
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


class CatalogSyncService:
    """Background pull/push of the shared catalog."""

    def __init__(
        self,
        store: ProductStore,
        *,
        on_log: ProgressCb | None = None,
        on_pulled: Callable[[], None] | None = None,
    ) -> None:
        self.store = store
        self.on_log = on_log or (lambda _m: None)
        self.on_pulled = on_pulled
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._push_needed = False
        self._remote_sha: str | None = None
        self._last_remote_rev = -1

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="catalog-sync", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def mark_dirty(self) -> None:
        """Call after local catalog changes so the next cycle pushes."""
        self._push_needed = True

    def sync_now(self) -> None:
        threading.Thread(target=self._cycle, daemon=True).start()

    def _loop(self) -> None:
        # First pull quickly
        self._cycle()
        while not self._stop.wait(self._interval()):
            self._cycle()

    def _interval(self) -> float:
        cfg = load_sync_settings()
        try:
            return max(5.0, float(cfg.get("interval_sec") or 12))
        except Exception:
            return 12.0

    def _cycle(self) -> None:
        cfg = load_sync_settings()
        if not cfg.get("enabled", True):
            return
        with self._lock:
            try:
                self._pull(cfg)
                if self._push_needed:
                    self._push(cfg)
                    self._push_needed = False
            except Exception as e:
                self.on_log(f"[동기화] 오류: {e}")

    def _api_headers(self, cfg: dict[str, Any], *, for_raw_blob: bool = False) -> dict[str, str]:
        headers = {
            "User-Agent": "WeigouManager-Sync/1.0",
            "Accept": "application/vnd.github+json",
        }
        token = (cfg.get("github_token") or "").strip()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if for_raw_blob:
            headers["Accept"] = "application/vnd.github.raw"
        return headers

    def _contents_url(self, cfg: dict[str, Any]) -> str:
        owner = cfg["github_owner"]
        repo = cfg["github_repo"]
        path = urllib.parse.quote(str(cfg.get("path") or "shared/catalog-sync.json"))
        branch = urllib.parse.quote(str(cfg.get("branch") or "main"))
        return f"https://api.github.com/repos/{owner}/{repo}/contents/{path}?ref={branch}"

    def _pull(self, cfg: dict[str, Any]) -> bool:
        """Return True if local DB changed from remote."""
        url = self._contents_url(cfg)
        req = urllib.request.Request(url, headers=self._api_headers(cfg))
        try:
            with urllib.request.urlopen(req, timeout=25) as resp:
                meta = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                self.on_log("[동기화] 원격 카탈로그 없음 — 첫 업로드 대기")
                return False
            raise
        self._remote_sha = meta.get("sha")
        content_b64 = meta.get("content") or ""
        raw = base64.b64decode(content_b64).decode("utf-8")
        bundle = json.loads(raw)
        remote_rev = int(bundle.get("rev") or 0)
        local_rev = int(self.store.get_setting("sync_rev", "0") or "0")
        if remote_rev <= local_rev and remote_rev == self._last_remote_rev:
            return False
        if remote_rev < local_rev:
            # Local ahead — push instead
            self._push_needed = True
            return False
        stats = self.store.apply_sync_bundle(bundle)
        self._last_remote_rev = remote_rev
        n = stats["products"] + stats["excluded"] + stats["published"]
        if n:
            self.on_log(
                f"[동기화] 원격 반영 rev={remote_rev} "
                f"(상품+{stats['products']} 제외+{stats['excluded']} 등록+{stats['published']})"
            )
            if self.on_pulled:
                self.on_pulled()
            return True
        self.store.set_setting("sync_rev", str(remote_rev))
        return False

    def _push(self, cfg: dict[str, Any]) -> None:
        token = (cfg.get("github_token") or "").strip()
        if not token:
            self.on_log("[동기화] github_token 없음 — 받기만 가능 (data/sync_settings.json)")
            return
        rev = self.store.bump_sync_rev()
        bundle = self.store.export_sync_bundle()
        bundle["rev"] = rev
        bundle["device"] = (cfg.get("device_name") or "").strip()
        body_bytes = (json.dumps(bundle, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        # refresh sha
        try:
            req = urllib.request.Request(
                self._contents_url(cfg), headers=self._api_headers(cfg)
            )
            with urllib.request.urlopen(req, timeout=20) as resp:
                meta = json.loads(resp.read().decode("utf-8"))
                self._remote_sha = meta.get("sha")
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise
            self._remote_sha = None

        owner = cfg["github_owner"]
        repo = cfg["github_repo"]
        path = str(cfg.get("path") or "shared/catalog-sync.json")
        api = f"https://api.github.com/repos/{owner}/{repo}/contents/{urllib.parse.quote(path)}"
        payload: dict[str, Any] = {
            "message": f"catalog sync rev {rev}",
            "content": base64.b64encode(body_bytes).decode("ascii"),
            "branch": cfg.get("branch") or "main",
        }
        if self._remote_sha:
            payload["sha"] = self._remote_sha
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            api,
            data=data,
            method="PUT",
            headers={
                **self._api_headers(cfg),
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=40) as resp:
            result = json.loads(resp.read().decode("utf-8"))
        self._remote_sha = (result.get("content") or {}).get("sha") or self._remote_sha
        self._last_remote_rev = rev
        self.on_log(f"[동기화] 업로드 완료 rev={rev}")

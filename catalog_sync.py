# -*- coding: utf-8 -*-
"""Multi-user catalog sync via Supabase Storage (near real-time).

Uses the same mall_cloud.json credentials as homepage publish.
No GitHub token required.
"""
from __future__ import annotations

import json
import hashlib
import pathlib
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

from mall_cloud import cloud_config_issue, cloud_enabled, ensure_cloud_settings, load_cloud_settings
from paths import data_path
from product_store import ProductStore

ProgressCb = Callable[[str], None]

SYNC_BUCKET = "manager-sync"
SYNC_OBJECT = "catalog-sync.json"

DEFAULT_SETTINGS = {
    "enabled": True,
    "backend": "supabase",
    # How often to pull others' changes (seconds). Push is immediate on local edit.
    "interval_sec": 2,
    "device_name": "",
    # full = A (collect+debug) · manager = B (no collect/debug, on-demand images)
    "role": "full",
    # Prefer table sync when manager_* tables exist; else storage JSON/delta
    "prefer_table_sync": True,
}

_ALLOWED_KEYS = set(DEFAULT_SETTINGS) | {"enabled", "interval_sec", "device_name", "backend", "role"}


def load_sync_settings() -> dict[str, Any]:
    path = pathlib.Path(data_path("sync_settings.json"))
    cfg = dict(DEFAULT_SETTINGS)
    if path.is_file():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                for k, v in raw.items():
                    if k in _ALLOWED_KEYS:
                        cfg[k] = v
        except Exception:
            pass
    cfg["backend"] = "supabase"
    role = str(cfg.get("role") or "full").strip().lower()
    cfg["role"] = "manager" if role == "manager" else "full"
    return cfg


def save_sync_settings(cfg: dict[str, Any]) -> None:
    path = pathlib.Path(data_path("sync_settings.json"))
    path.parent.mkdir(parents=True, exist_ok=True)
    out = dict(DEFAULT_SETTINGS)
    for k in _ALLOWED_KEYS:
        if k in cfg:
            out[k] = cfg[k]
    out["backend"] = "supabase"
    role = str(out.get("role") or "full").strip().lower()
    out["role"] = "manager" if role == "manager" else "full"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def ensure_sync_defaults() -> dict[str, Any]:
    """Auto-enable sync and fill device name — no manual setup required."""
    import socket

    ensure_cloud_settings(repair_invalid=True)
    cfg = load_sync_settings()
    changed = False
    if not cfg.get("enabled", True):
        cfg["enabled"] = True
        changed = True
    name = str(cfg.get("device_name") or "").strip()
    if not name:
        try:
            name = socket.gethostname().strip() or "PC"
        except Exception:
            name = "PC"
        cfg["device_name"] = name
        changed = True
    try:
        interval = float(cfg.get("interval_sec") or 2)
    except Exception:
        interval = 2
    if interval > 5:
        cfg["interval_sec"] = 2
        changed = True
    if changed or not pathlib.Path(data_path("sync_settings.json")).is_file():
        save_sync_settings(cfg)
    return cfg


class CatalogSyncService:
    """Background pull/push of the shared catalog via Supabase.

    Cloud is the shared catalog. Local SQLite is a cache.
    - Local change → push almost immediately (debounced ~0.4s)
    - Remote change → pull every few seconds (default 2s)
    - Startup → full reconcile (pull then push) without user action
    """

    def __init__(
        self,
        store: ProductStore,
        *,
        on_log: ProgressCb | None = None,
        on_pulled: Callable[[], None] | None = None,
        on_status: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.store = store
        self.on_log = on_log or (lambda _m: None)
        self.on_pulled = on_pulled
        self.on_status = on_status
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._push_needed = False
        self._force_full = False
        self._cycle_n = 0
        self._last_remote_rev = -1
        self._last_etag = ""
        self._bucket_ready = False
        self._last_err_log = 0.0
        self._warned_cloud = False
        self._status = "idle"
        self._status_detail = "시작 대기"
        self._last_ok_at = 0.0
        self._last_error = ""
        self._ever_ok = False
        self._status_lock = threading.Lock()

    def get_status(self) -> dict[str, Any]:
        with self._status_lock:
            age = (time.time() - self._last_ok_at) if self._last_ok_at else None
            return {
                "state": self._status,
                "detail": self._status_detail,
                "last_ok_at": self._last_ok_at,
                "last_ok_age_sec": age,
                "last_error": self._last_error,
                "ever_ok": self._ever_ok,
                "remote_rev": self._last_remote_rev,
            }

    def _set_status(self, state: str, detail: str = "", *, error: str = "") -> None:
        with self._status_lock:
            self._status = state
            self._status_detail = detail or state
            if error:
                self._last_error = error
            if state == "ok":
                self._last_ok_at = time.time()
                self._ever_ok = True
                self._last_error = ""
        if self.on_status:
            try:
                self.on_status(self.get_status())
            except Exception:
                pass

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        ensure_sync_defaults()
        self._stop.clear()
        self._wake.clear()
        self._set_status("starting", "클라우드 목록 연결 중…")
        self._thread = threading.Thread(target=self._loop, name="catalog-sync", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def mark_dirty(self) -> None:
        """Local catalog changed → wake sync loop and push ASAP."""
        self._push_needed = True
        self._wake.set()

    def sync_now(self, *, full: bool = False) -> None:
        if full:
            self.force_full_sync()
            return
        self._wake.set()
        threading.Thread(target=self._cycle, daemon=True).start()

    def force_full_sync(self) -> None:
        """Full pull+push so diverged PCs converge to the union of both catalogs."""
        self._force_full = True
        self._push_needed = True
        self._last_remote_rev = -1
        # Clear delta cursors so next push/pull is complete.
        try:
            self.store.set_setting("sync_table_since", "")
            self.store.set_setting("sync_table_pull_since", "")
        except Exception:
            pass
        self.on_log("[동기화] 전체 목록 맞추기 시작 (양쪽 합집합)")
        self._wake.set()
        threading.Thread(target=self._cycle, daemon=True).start()

    def _loop(self) -> None:
        # First cycle: always full reconcile once after launch (pull then push).
        self._force_full = True
        self._push_needed = True
        self._set_status("syncing", "시작 시 전체 목록 맞추는 중…")
        self._cycle()
        while not self._stop.is_set():
            woken = self._wake.wait(timeout=self._interval())
            self._wake.clear()
            if self._stop.is_set():
                break
            if woken and self._push_needed:
                time.sleep(0.4)
            self._cycle()

    def _interval(self) -> float:
        cfg = load_sync_settings()
        try:
            return max(1.0, float(cfg.get("interval_sec") or 2))
        except Exception:
            return 2.0

    def _log_err_throttled(self, msg: str, *, every_sec: float = 120.0) -> None:
        now = time.time()
        if now - self._last_err_log < every_sec:
            return
        self._last_err_log = now
        self.on_log(msg)

    def _cycle(self) -> None:
        cfg = load_sync_settings()
        if not cfg.get("enabled", True):
            self._set_status("disabled", "동기화 꺼짐 — 설정에서 켜 주세요")
            return
        # Re-try bootstrap each cycle until cloud works (bundled may appear after update)
        if not cloud_enabled():
            ensure_cloud_settings(repair_invalid=True)
        issue = cloud_config_issue()
        if not cloud_enabled():
            if not self._warned_cloud:
                self._warned_cloud = True
                self.on_log(
                    f"[동기화] 클라우드 설정 필요: {issue or 'mall_cloud.json'}"
                )
            self._set_status("no_cloud", issue or "클라우드 설정 없음", error=issue)
            return
        if issue:
            # Key present but looks wrong — still try, surface warning
            self._set_status("syncing", f"연결 시도 중… ({issue})")
        else:
            self._set_status("syncing", "목록 동기화 중…")
        self._cycle_n += 1
        # Periodic full reconcile (~5 min at 2s interval) keeps A/B identical.
        if self._cycle_n > 1 and self._cycle_n % 150 == 0:
            self._force_full = True
            self._push_needed = True
        force = bool(self._force_full)
        self._force_full = False
        with self._lock:
            try:
                if cfg.get("prefer_table_sync", True) and self._table_sync_available():
                    self._pull_table(cfg, full=force)
                    if self._push_needed or force:
                        self._push_table(cfg, full=force)
                        self._push_needed = False
                    self._set_status("ok", f"목록 동기화됨 · rev {self._last_remote_rev}")
                    return
                self._pull(cfg)
                if self._push_needed or force:
                    self._push(cfg)
                    self._push_needed = False
                self._set_status("ok", f"목록 동기화됨 · rev {self._last_remote_rev}")
            except Exception as e:
                err = str(e)
                self._log_err_throttled(f"[동기화] 오류: {e}")
                self._set_status("error", f"동기화 실패: {err[:80]}", error=err)

    def _table_sync_available(self) -> bool:
        if getattr(self, "_table_sync_ok", None) is False:
            return False
        if getattr(self, "_table_sync_ok", None) is True:
            return True
        try:
            base, key = self._rest_base()
            url = (
                f"{base}/rest/v1/manager_sync_meta"
                f"?select=rev&id=eq.1&limit=1"
            )
            req = urllib.request.Request(url, headers=self._headers(key), method="GET")
            with urllib.request.urlopen(req, timeout=15) as resp:
                resp.read()
            self._table_sync_ok = True
            return True
        except urllib.error.HTTPError as e:
            if e.code == 404:
                self._table_sync_ok = False
                return False
            # 401/other — treat as unavailable this session
            self._table_sync_ok = False
            return False
        except Exception:
            self._table_sync_ok = False
            return False

    def _sync_key_product(self, row: dict[str, Any]) -> str:
        gid = (row.get("goods_id") or "").strip()
        code = (row.get("search_code") or "").strip()
        if gid:
            return gid
        if code:
            return code
        # Fallback so untitled rows without codes still sync across PCs
        title = (row.get("title") or "").strip()
        created = (row.get("created_at") or "").strip()
        if title:
            digest = hashlib.sha1(
                f"{title}|{created}".encode("utf-8", errors="ignore")
            ).hexdigest()[:20]
            return f"t:{digest}"
        return ""

    def _sync_key_other(self, row: dict[str, Any]) -> str:
        return (
            (row.get("mall_id") or "").strip()
            or (row.get("goods_id") or "").strip()
            or (row.get("search_code") or "").strip()
        )

    def _push_table(self, cfg: dict[str, Any], *, full: bool = False) -> None:
        """Upsert rows into manager_* tables + bump rev.

        full=True exports the entire local catalog (not just delta) so the other
        PC can catch up when lists have diverged.
        """
        base, key = self._rest_base()
        since = "" if full else (self.store.get_setting("sync_table_since", "") or "").strip()
        if since:
            bundle = self.store.export_sync_delta(since)
            mode = "delta"
        else:
            bundle = self.store.export_sync_bundle()
            mode = "full"
        rev = self.store.bump_sync_rev()
        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        headers = {
            **self._headers(key),
            "Content-Type": "application/json; charset=utf-8",
            "Prefer": "resolution=merge-duplicates",
        }

        def upsert(table: str, rows: list[dict[str, Any]], deleted_keys: list[str]) -> None:
            payload: list[dict[str, Any]] = []
            for row in rows:
                if table == "manager_products":
                    sk = self._sync_key_product(row)
                else:
                    sk = self._sync_key_other(row)
                if not sk:
                    continue
                payload.append(
                    {
                        "sync_key": sk,
                        "payload": row,
                        "updated_at": row.get("updated_at") or row.get("created_at") or now,
                        "deleted": False,
                    }
                )
            for dk in deleted_keys:
                k = (dk or "").strip()
                if not k or k.startswith("id:"):
                    continue
                payload.append(
                    {
                        "sync_key": k,
                        "payload": {},
                        "updated_at": now,
                        "deleted": True,
                    }
                )
            if not payload:
                return
            for i in range(0, len(payload), 400):
                chunk = payload[i : i + 400]
                body = json.dumps(chunk, ensure_ascii=False).encode("utf-8")
                url = f"{base}/rest/v1/{table}?on_conflict=sync_key"
                req = urllib.request.Request(url, data=body, method="POST", headers=headers)
                with urllib.request.urlopen(req, timeout=120) as resp:
                    resp.read()

        deleted = bundle.get("deleted") or {}
        upsert("manager_products", list(bundle.get("products") or []), list(deleted.get("products") or []))
        upsert("manager_excluded", list(bundle.get("excluded") or []), list(deleted.get("excluded") or []))
        upsert("manager_published", list(bundle.get("published") or []), list(deleted.get("published") or []))

        meta_body = json.dumps(
            {"id": 1, "rev": rev, "updated_at": now},
            ensure_ascii=False,
        ).encode("utf-8")
        meta_req = urllib.request.Request(
            f"{base}/rest/v1/manager_sync_meta?on_conflict=id",
            data=meta_body,
            method="POST",
            headers=headers,
        )
        with urllib.request.urlopen(meta_req, timeout=30) as resp:
            resp.read()

        self.store.set_setting("sync_table_since", now)
        self._last_remote_rev = rev
        n = (
            len(bundle.get("products") or [])
            + len(bundle.get("excluded") or [])
            + len(bundle.get("published") or [])
        )
        self.on_log(f"[동기화] 테이블 업로드 완료 rev={rev} ({mode}, {n}건)")

    def _pull_table(self, cfg: dict[str, Any], *, full: bool = False) -> bool:
        base, key = self._rest_base()
        url = f"{base}/rest/v1/manager_sync_meta?select=rev,updated_at&id=eq.1&limit=1"
        req = urllib.request.Request(url, headers=self._headers(key), method="GET")
        with urllib.request.urlopen(req, timeout=20) as resp:
            rows = json.loads(resp.read().decode("utf-8"))
        if not rows:
            return False
        remote_rev = int(rows[0].get("rev") or 0)
        local_rev = int(self.store.get_setting("sync_rev", "0") or "0")
        if remote_rev < local_rev:
            self._push_needed = True
            if not full:
                return False
        if (
            not full
            and remote_rev == local_rev
            and remote_rev == self._last_remote_rev
        ):
            return False

        since = (
            ""
            if full
            else (self.store.get_setting("sync_table_pull_since", "") or "").strip()
        )
        filt = f"&updated_at=gte.{urllib.parse.quote(since)}" if since else ""

        def fetch_table(table: str) -> list[dict[str, Any]]:
            out: list[dict[str, Any]] = []
            start = 0
            page = 1000
            while True:
                q = (
                    f"{base}/rest/v1/{table}?select=sync_key,payload,updated_at,deleted"
                    f"{filt}&order=updated_at.asc&offset={start}&limit={page}"
                )
                r = urllib.request.Request(q, headers=self._headers(key), method="GET")
                with urllib.request.urlopen(r, timeout=90) as resp:
                    chunk = json.loads(resp.read().decode("utf-8"))
                if not chunk:
                    break
                out.extend(chunk)
                if len(chunk) < page:
                    break
                start += page
            return out

        products: list[dict[str, Any]] = []
        excluded: list[dict[str, Any]] = []
        published: list[dict[str, Any]] = []
        deleted: dict[str, list[str]] = {"products": [], "excluded": [], "published": []}

        for row in fetch_table("manager_products"):
            if row.get("deleted"):
                deleted["products"].append(str(row.get("sync_key") or ""))
            else:
                payload = row.get("payload") or {}
                if isinstance(payload, dict):
                    products.append(payload)
        for row in fetch_table("manager_excluded"):
            if row.get("deleted"):
                deleted["excluded"].append(str(row.get("sync_key") or ""))
            else:
                payload = row.get("payload") or {}
                if isinstance(payload, dict):
                    excluded.append(payload)
        for row in fetch_table("manager_published"):
            if row.get("deleted"):
                deleted["published"].append(str(row.get("sync_key") or ""))
            else:
                payload = row.get("payload") or {}
                if isinstance(payload, dict):
                    published.append(payload)

        bundle = {
            "schema": 2,
            "type": "full" if full or not since else "delta",
            "rev": remote_rev,
            "products": products,
            "excluded": excluded,
            "published": published,
            "deleted": deleted,
        }
        stats = self.store.apply_sync_bundle(bundle)
        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        self.store.set_setting("sync_table_pull_since", now)
        if remote_rev >= local_rev:
            self.store.set_setting("sync_rev", str(remote_rev))
            self._last_remote_rev = remote_rev
        n = stats["products"] + stats["excluded"] + stats["published"]
        n += sum(len(v) for v in deleted.values())
        mode = "전체" if full or not since else "증분"
        self.on_log(
            f"[동기화] 테이블 반영 ({mode}) rev={remote_rev} "
            f"상품+{stats['products']} 제외+{stats['excluded']} 등록+{stats['published']}"
        )
        if n and self.on_pulled:
            self.on_pulled()
        return bool(n)

    def _rest_base(self) -> tuple[str, str]:
        s = load_cloud_settings()
        base = (s.get("supabaseUrl") or "").rstrip("/")
        key = (s.get("serviceRoleKey") or "").strip()
        if not base or not key:
            raise RuntimeError("Supabase 설정 없음")
        return base, key

    def _headers(self, key: str, *, upsert: bool = False) -> dict[str, str]:
        h = {
            "Authorization": f"Bearer {key}",
            "apikey": key,
            "User-Agent": "WeigouManager-Sync/2.0",
        }
        if upsert:
            h["x-upsert"] = "true"
            h["Content-Type"] = "application/json; charset=utf-8"
        return h

    def _ensure_bucket(self, base: str, key: str) -> None:
        if self._bucket_ready:
            return
        url = f"{base}/storage/v1/bucket"
        body = json.dumps(
            {
                "id": SYNC_BUCKET,
                "name": SYNC_BUCKET,
                "public": False,
                "file_size_limit": 50_000_000,
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={
                **self._headers(key),
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                resp.read()
            self._bucket_ready = True
        except urllib.error.HTTPError as e:
            raw = ""
            try:
                raw = e.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            # 409 / Duplicate / already exists → OK
            low = raw.lower()
            if e.code in (200, 201, 409) or "exist" in low or "duplicate" in low:
                self._bucket_ready = True
                return
            raise RuntimeError(f"동기화 버킷 생성 실패 ({e.code}): {raw[:200]}") from e

    def _object_url(self, base: str) -> str:
        return f"{base}/storage/v1/object/{SYNC_BUCKET}/{SYNC_OBJECT}"

    def _pull(self, cfg: dict[str, Any]) -> bool:
        """Return True if local DB changed from remote."""
        base, key = self._rest_base()
        self._ensure_bucket(base, key)
        req = urllib.request.Request(
            self._object_url(base),
            headers=self._headers(key),
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=40) as resp:
                etag = resp.headers.get("ETag") or resp.headers.get("etag") or ""
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                # Nothing uploaded yet — push local on next dirty / sync_now
                return False
            raise

        if etag and etag == self._last_etag and self._last_remote_rev >= 0:
            return False

        bundle = json.loads(raw)
        if not isinstance(bundle, dict):
            return False
        remote_rev = int(bundle.get("rev") or 0)
        local_rev = int(self.store.get_setting("sync_rev", "0") or "0")

        if remote_rev < local_rev:
            # Local ahead — upload on next push
            self._push_needed = True
            self._last_etag = etag
            return False

        if remote_rev == local_rev and remote_rev == self._last_remote_rev:
            self._last_etag = etag
            return False

        stats = self.store.apply_sync_bundle(bundle)
        self._last_remote_rev = remote_rev
        self._last_etag = etag
        n = stats["products"] + stats["excluded"] + stats["published"]
        if n:
            device = (bundle.get("device") or "").strip()
            who = f" · {device}" if device else ""
            self.on_log(
                f"[동기화] 원격 반영 rev={remote_rev}{who} "
                f"(상품+{stats['products']} 제외+{stats['excluded']} 등록+{stats['published']})"
            )
            if self.on_pulled:
                self.on_pulled()
            return True
        self.store.set_setting("sync_rev", str(remote_rev))
        return False

    def _push(self, cfg: dict[str, Any]) -> None:
        base, key = self._rest_base()
        self._ensure_bucket(base, key)
        rev = self.store.bump_sync_rev()
        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        since = (self.store.get_setting("sync_last_full_at", "") or "").strip()
        try:
            prod_n = int(self.store.count_products())
        except Exception:
            prod_n = 0
        # Large catalogs: prefer delta unless periodic full refresh
        use_delta = bool(since) and prod_n >= 400 and (rev % 25 != 0)
        if use_delta:
            bundle = self.store.export_sync_delta(since)
            if len(bundle.get("products") or []) > 2500:
                use_delta = False
        if not use_delta:
            bundle = self.store.export_sync_bundle()
            self.store.set_setting("sync_last_full_at", now)
        bundle["rev"] = rev
        bundle["device"] = (cfg.get("device_name") or "").strip()
        bundle["updated_at"] = now
        body = (json.dumps(bundle, ensure_ascii=False) + "\n").encode("utf-8")
        # Raise storage limit hint for large catalogs
        if len(body) > 45_000_000:
            raise RuntimeError(
                "동기화 파일이 너무 큽니다. Supabase에 manager_catalog.sql 테이블을 "
                "만든 뒤 prefer_table_sync 를 사용하세요."
            )
        req = urllib.request.Request(
            self._object_url(base),
            data=body,
            method="POST",
            headers=self._headers(key, upsert=True),
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                resp.read()
                etag = resp.headers.get("ETag") or resp.headers.get("etag") or ""
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", errors="replace")
            # retry with PUT
            if e.code in (400, 409):
                req2 = urllib.request.Request(
                    self._object_url(base),
                    data=body,
                    method="PUT",
                    headers=self._headers(key, upsert=True),
                )
                with urllib.request.urlopen(req2, timeout=120) as resp:
                    resp.read()
                    etag = resp.headers.get("ETag") or resp.headers.get("etag") or ""
            else:
                raise RuntimeError(f"동기화 업로드 실패 ({e.code}): {raw[:240]}") from e
        self._last_remote_rev = rev
        self._last_etag = etag
        kind = "delta" if use_delta else "full"
        self.on_log(f"[동기화] 업로드 완료 rev={rev} ({kind})")

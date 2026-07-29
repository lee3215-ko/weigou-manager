# -*- coding: utf-8 -*-
"""Supabase Storage + mall API helpers for cloud publish."""
from __future__ import annotations

import json
import mimetypes
import pathlib
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

DEFAULT_MALL_API = "https://shoot-repl.vercel.app/api/catalog"
DEFAULT_STYLES_API = "https://shoot-repl.vercel.app/api/styles"
DEFAULT_ORDERS_API = "https://shoot-repl.vercel.app/api/orders"
DEFAULT_CUSTOMERS_API = "https://shoot-repl.vercel.app/api/customers"


def _settings_path() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parent / "data" / "mall_cloud.json"


def load_cloud_settings() -> dict[str, Any]:
    path = _settings_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def cloud_enabled() -> bool:
    s = load_cloud_settings()
    return bool(s.get("supabaseUrl") and s.get("serviceRoleKey"))


def mall_catalog_api() -> str:
    s = load_cloud_settings()
    return (s.get("mallCatalogApi") or DEFAULT_MALL_API).strip()


def mall_styles_api() -> str:
    s = load_cloud_settings()
    return (s.get("mallStylesApi") or DEFAULT_STYLES_API).strip()


def mall_orders_api() -> str:
    s = load_cloud_settings()
    return (s.get("mallOrdersApi") or DEFAULT_ORDERS_API).strip()


def mall_customers_api() -> str:
    s = load_cloud_settings()
    return (s.get("mallCustomersApi") or DEFAULT_CUSTOMERS_API).strip()


def mall_site_base() -> str:
    """Homepage origin derived from catalog/orders API URL."""
    for api in (mall_orders_api(), mall_catalog_api(), DEFAULT_ORDERS_API):
        u = (api or "").strip()
        if not u:
            continue
        if "/api/" in u:
            return u.split("/api/", 1)[0].rstrip("/")
        return u.rstrip("/")
    return "https://shoot-repl.vercel.app"


def mall_product_page_url(product_id: str) -> str:
    pid = (product_id or "").strip()
    if not pid:
        return ""
    return f"{mall_site_base()}/products/{pid}"


def write_secret() -> str:
    return (load_cloud_settings().get("catalogWriteSecret") or "").strip()


def safe_object_key(object_path: str) -> str:
    """Storage keys must be ASCII — non-ASCII segments become utf-8 hex."""
    parts = [p for p in object_path.replace("\\", "/").split("/") if p]
    out: list[str] = []
    for p in parts:
        if all(ord(c) < 128 and (c.isalnum() or c in "._-") for c in p):
            out.append(p)
        else:
            out.append(p.encode("utf-8").hex())
    return "/".join(out) or "file"


def public_object_url(bucket: str, object_path: str) -> str:
    s = load_cloud_settings()
    base = (s.get("supabaseUrl") or "").rstrip("/")
    key = safe_object_key(object_path)
    return f"{base}/storage/v1/object/public/{bucket}/{key}"


def upload_file(bucket: str, object_path: str, file_path: pathlib.Path) -> str:
    """Upload local file to Supabase Storage; return public URL."""
    s = load_cloud_settings()
    base = (s.get("supabaseUrl") or "").rstrip("/")
    key = s.get("serviceRoleKey") or ""
    if not base or not key:
        raise RuntimeError(
            "mall_cloud.json 에 supabaseUrl / serviceRoleKey 를 설정해 주세요."
        )

    src = pathlib.Path(file_path)
    data = src.read_bytes()
    ctype = mimetypes.guess_type(src.name)[0] or "application/octet-stream"
    object_key = safe_object_key(object_path)
    url = f"{base}/storage/v1/object/{bucket}/{object_key}"
    headers = {
        "Authorization": f"Bearer {key}",
        "apikey": key,
        "Content-Type": ctype,
        "x-upsert": "true",
        "User-Agent": "shoot-repl-manager/1.0",
    }
    req = urllib.request.Request(url, data=data, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            resp.read()
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        if e.code in (400, 409):
            req2 = urllib.request.Request(
                url, data=data, method="PUT", headers=headers
            )
            with urllib.request.urlopen(req2, timeout=120) as resp:
                resp.read()
        else:
            raise RuntimeError(f"Storage upload failed ({e.code}): {body[:300]}") from e

    return public_object_url(bucket, object_key)


def get_json(api_url: str, timeout: int = 60) -> dict[str, Any]:
    headers = {
        "Accept": "application/json",
        "User-Agent": "shoot-repl-manager/1.0",
    }
    req = urllib.request.Request(api_url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"API 오류 ({e.code}): {body[:240] or e.reason}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"API 미연결: {e.reason or e}") from e
    data = json.loads(raw) if raw else {}
    return data if isinstance(data, dict) else {}


def _supabase_rest(
    path_qs: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | list[Any] | None = None,
    timeout: int = 60,
    prefer: str = "",
) -> Any:
    """Call Supabase PostgREST with service role from mall_cloud.json."""
    s = load_cloud_settings()
    base = (s.get("supabaseUrl") or "").rstrip("/")
    key = (s.get("serviceRoleKey") or "").strip()
    if not base or not key:
        raise RuntimeError(
            "mall_cloud.json 에 supabaseUrl / serviceRoleKey 가 없습니다."
        )
    url = f"{base}/rest/v1/{path_qs.lstrip('/')}"
    headers = {
        "Accept": "application/json",
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "User-Agent": "shoot-repl-manager/1.0",
    }
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    if prefer:
        headers["Prefer"] = prefer
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"Supabase 오류 ({e.code}): {body[:240] or e.reason}"
        ) from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Supabase 미연결: {e.reason or e}") from e
    if not raw:
        return None
    return json.loads(raw)


def _row_to_order(row: dict[str, Any]) -> dict[str, Any]:
    payload = row.get("payload")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            payload = {}
    if not isinstance(payload, dict):
        payload = {}
    order = dict(payload)
    order["id"] = row.get("id") or order.get("id") or ""
    order["status"] = row.get("status") or order.get("status") or "pending"
    order["createdAt"] = row.get("created_at") or order.get("createdAt") or ""
    if row.get("customer_name"):
        order["customerName"] = row["customer_name"]
    if row.get("phone"):
        order["phone"] = row["phone"]
    if row.get("product_id"):
        order["productId"] = row["product_id"]
    return order


def fetch_orders_from_supabase() -> list[dict[str, Any]]:
    rows = _supabase_rest(
        "orders?select=*&order=created_at.desc",
        method="GET",
    )
    if not isinstance(rows, list):
        return []
    return [_row_to_order(r) for r in rows if isinstance(r, dict)]


def request_json(
    api_url: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    timeout: int = 60,
    with_secret: bool = False,
) -> dict[str, Any]:
    """HTTP JSON helper. Raises RuntimeError on HTTP/network errors."""
    headers = {
        "Accept": "application/json",
        "User-Agent": "shoot-repl-manager/1.0",
    }
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    if with_secret:
        secret = write_secret()
        if secret:
            headers["x-catalog-secret"] = secret
    req = urllib.request.Request(api_url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"API 오류 ({e.code}): {raw[:240]}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"API 미연결: {e}") from e
    parsed = json.loads(raw) if raw else {}
    return parsed if isinstance(parsed, dict) else {"data": parsed}


def fetch_orders() -> list[dict[str, Any]]:
    """
    Prefer mall /api/orders; if not deployed (404) or failing,
    read directly from Supabase orders table (service role).
    """
    api_err: Exception | None = None
    try:
        data = request_json(mall_orders_api(), method="GET", with_secret=True)
        if isinstance(data.get("orders"), list):
            return list(data["orders"])
        if data.get("error"):
            raise RuntimeError(str(data.get("error")))
    except Exception as e:  # noqa: BLE001
        api_err = e

    try:
        return fetch_orders_from_supabase()
    except Exception as sb_err:  # noqa: BLE001
        bits = []
        if api_err:
            bits.append(f"사이트API: {api_err}")
        bits.append(f"Supabase: {sb_err}")
        raise RuntimeError(" · ".join(bits)) from sb_err


def fetch_members_from_supabase() -> list[dict[str, Any]]:
    """All member profiles (service role). Empty list if table missing."""
    try:
        rows = _supabase_rest(
            "members?select=*&order=created_at.desc",
            method="GET",
        )
    except RuntimeError as e:
        # table may not exist yet
        if "404" in str(e) or "does not exist" in str(e).lower():
            return []
        raise
    if not isinstance(rows, list):
        return []
    return [r for r in rows if isinstance(r, dict)]


def fetch_customers() -> list[dict[str, Any]]:
    """
    Prefer mall /api/customers; fallback: merge orders + members locally.
    """
    from customers_ui import build_customers

    api_err: Exception | None = None
    try:
        data = request_json(mall_customers_api(), method="GET", with_secret=True)
        if isinstance(data.get("customers"), list):
            return list(data["customers"])
        if data.get("error"):
            raise RuntimeError(str(data.get("error")))
    except Exception as e:  # noqa: BLE001
        api_err = e

    try:
        orders = fetch_orders()
        try:
            members = fetch_members_from_supabase()
        except Exception:
            members = []
        return build_customers(orders, members)
    except Exception as local_err:  # noqa: BLE001
        bits = []
        if api_err:
            bits.append(f"사이트API: {api_err}")
        bits.append(f"로컬집계: {local_err}")
        raise RuntimeError(" · ".join(bits)) from local_err


def patch_member_points(user_id: str, points: int) -> dict[str, Any]:
    """Update member mileage/points via Supabase service role."""
    uid = (user_id or "").strip()
    if not uid:
        raise RuntimeError("회원 ID가 없습니다.")
    try:
        pts = int(points)
    except (TypeError, ValueError) as e:
        raise RuntimeError("포인트는 숫자여야 합니다.") from e
    if pts < 0:
        raise RuntimeError("포인트는 0 이상이어야 합니다.")
    _supabase_rest(
        "members?id=eq." + urllib.parse.quote(uid),
        method="PATCH",
        payload={"points": pts},
        prefer="return=minimal",
    )
    return {"ok": True, "id": uid, "points": pts}


def patch_order(order_id: str, *, status: str | None = None, memo: str | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {"id": order_id}
    if status is not None:
        body["status"] = status
    if memo is not None:
        body["memo"] = memo
    try:
        return request_json(
            mall_orders_api(),
            method="PATCH",
            payload=body,
            with_secret=True,
        )
    except Exception:
        # Fallback: update Supabase row (+ payload status/memo)
        rows = _supabase_rest(
            f"orders?id=eq.{urllib.parse.quote(order_id)}&select=*",
            method="GET",
        )
        if not isinstance(rows, list) or not rows:
            raise RuntimeError("주문을 찾지 못했습니다.") from None
        order = _row_to_order(rows[0])
        if status is not None:
            order["status"] = status
        if memo is not None:
            order["memo"] = memo
        row = {
            "id": order["id"],
            "status": order.get("status") or "pending",
            "created_at": order.get("createdAt") or None,
            "customer_name": order.get("customerName"),
            "phone": order.get("phone"),
            "product_id": order.get("productId"),
            "payload": order,
        }
        _supabase_rest(
            "orders?id=eq." + urllib.parse.quote(order_id),
            method="PATCH",
            payload=row,
            prefer="return=minimal",
        )
        return {"ok": True, "order": order}


def delete_order_remote(order_id: str) -> dict[str, Any]:
    url = f"{mall_orders_api().rstrip('/')}?id={urllib.parse.quote(order_id)}"
    try:
        return request_json(url, method="DELETE", with_secret=True)
    except Exception:
        _supabase_rest(
            "orders?id=eq." + urllib.parse.quote(order_id),
            method="DELETE",
            prefer="return=minimal",
        )
        return {"ok": True}


def post_json(api_url: str, payload: dict[str, Any], timeout: int = 60) -> str:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "User-Agent": "shoot-repl-manager/1.0",
    }
    secret = write_secret()
    if secret:
        headers["x-catalog-secret"] = secret
    req = urllib.request.Request(
        api_url, data=body, headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return f"API OK ({resp.status}) {raw[:200]}"
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        return f"API 오류 ({e.code}): {raw[:240]}"
    except urllib.error.URLError as e:
        return f"API 미연결: {e}"
    except Exception as e:  # noqa: BLE001
        return f"API 오류: {e}"

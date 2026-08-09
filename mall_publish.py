# -*- coding: utf-8 -*-
"""Publish Weigou products into the Shoot Repl mall catalog."""
from __future__ import annotations

import json
import pathlib
import re
import shutil
import time
import urllib.parse
from datetime import datetime, timezone
from typing import Any, Callable

from price_codec import (
    DEFAULT_PRICE_TEXT,
    decode_price_code,
    effective_price_code,
    is_text_price_label,
)
from product_attrs import _detect_brand, extract_attrs
from product_store import Product, ProductStore
from image_enhance import enhance_image_file
from mall_cloud import (
    _supabase_rest,
    cloud_enabled,
    get_json,
    mall_catalog_api,
    post_json,
    request_json,
    upload_file,
)

ProgressCb = Callable[[str], None]

MALL_API = "http://127.0.0.1:3000/api/catalog"
PRODUCT_BUCKET = "product-images"


def project_root() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parents[2]


def catalog_path() -> pathlib.Path:
    return project_root() / "data" / "catalog.json"


def uploads_root() -> pathlib.Path:
    return project_root() / "public" / "uploads"


def _load_catalog() -> list[dict[str, Any]]:
    path = catalog_path()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return list(data.get("products") or [])
        if isinstance(data, list):
            return data
    except json.JSONDecodeError:
        return []
    return []


def _save_catalog(products: list[dict[str, Any]]) -> None:
    path = catalog_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"products": products}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _copy_images(product: Product, folder_key: str) -> list[str]:
    dest_dir = uploads_root() / folder_key
    dest_dir.mkdir(parents=True, exist_ok=True)
    urls: list[str] = []
    use_cloud = cloud_enabled()
    for i, src in enumerate(product.image_paths, start=1):
        src_path = pathlib.Path(src)
        if not src_path.exists():
            continue
        ext = src_path.suffix.lower() or ".jpg"
        if ext not in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
            ext = ".jpg"
        name = f"{i:02d}{ext}"
        dest = dest_dir / name
        shutil.copy2(src_path, dest)
        # Auto upscale / sharpen low-quality photos
        enhanced = enhance_image_file(dest)
        # If enhancer rewrote to .jpg, prefer that filename
        final_name = enhanced.name
        if use_cloud:
            object_path = f"{folder_key}/{final_name}"
            urls.append(upload_file(PRODUCT_BUCKET, object_path, enhanced))
        else:
            urls.append(f"/uploads/{folder_key}/{final_name}")
    if urls:
        return urls
    # Reconcile / manager PC: local files may be absent — reuse stored public URLs.
    for u in list(getattr(product, "image_urls", None) or []):
        s = str(u or "").strip()
        if s.startswith("http://") or s.startswith("https://"):
            urls.append(s)
    return urls


def re_safe(value: str) -> str:
    # ASCII-only — Supabase Storage rejects non-ASCII object keys
    s = re.sub(r"[^a-zA-Z0-9\-]+", "-", (value or "").strip())
    return s.strip("-") or "item"


def normalize_colors(colors: list[str] | None) -> list[str]:
    """여러 색상 토큰·쉼표를 / 로 묶어 한 색상 옵션으로 만든다."""
    if not colors:
        return []
    parts: list[str] = []
    for c in colors:
        t = re.sub(r"\s*,\s*", "/", (c or "").strip())
        t = re.sub(r"\s*/\s*", "/", t)
        if t:
            parts.append(t)
    if not parts:
        return []
    if len(parts) == 1:
        return parts
    return ["/".join(parts)]


def _clean_description(
    *,
    brand: str,
    category: str,
    colors: list[str],
    sizes: list[str],
    dimension: str,
    search_code: str,
) -> str:
    lines = [
        f"브랜드: {brand}",
        f"카테고리: {category}",
        f"색상: {' / '.join(colors) if colors else '-'}",
        f"사이즈: {', '.join(sizes) if sizes else '-'}",
    ]
    if dimension and dimension not in sizes:
        lines.insert(0, f"치수: {dimension}")
    if search_code:
        lines.append(f"NO: {search_code}")
    return "\n".join(lines)


def build_mall_product(
    product: Product,
    *,
    colors: list[str] | None = None,
    sizes: list[str] | None = None,
    category: str | None = None,
    mall_id: str | None = None,
) -> dict[str, Any]:
    price_code = effective_price_code(product.sku_no)
    search_code = (product.search_code or "").strip()
    # Homepage "NO" under product name = 搜索码
    display_no = search_code or price_code

    price_info = decode_price_code(price_code)
    price_text = ""
    if not price_info:
        if is_text_price_label(price_code) or price_code == DEFAULT_PRICE_TEXT:
            # 한글 가격 문구 → 쇼핑몰에 숫자 대신 그대로 표시
            price_text = price_code.strip() or DEFAULT_PRICE_TEXT
        else:
            raise ValueError(
                f"가격 코드를 해석할 수 없습니다: NO(가격)={price_code or '(없음)'}\n"
                "예: 8888033 → 원가 33만원 / 00008 → 원가 8만원 / 8888033.5 → 33.5만원\n"
                f"또는 한글 문구: {DEFAULT_PRICE_TEXT}\n"
                "검색코드(搜索码)는 제품명 아래 NO로 표시됩니다."
            )

    attrs = extract_attrs(
        product.title,
        product.tags,
        product.description,
        google_name=product.google_name or "",
        name_en=product.name_en or "",
    )
    raw_colors = colors if colors is not None and len(colors) > 0 else (attrs.colors or ["블랙"])
    use_colors = normalize_colors(raw_colors) or ["블랙"]
    use_sizes = sizes if sizes is not None and len(sizes) > 0 else (attrs.sizes or ["FREE"])
    use_category = category if category else attrs.category
    brand_name = (attrs.brand_name or "").strip()
    brand_id = (attrs.brand_id or "").strip()
    # Last resort: brand word inside product name only (never invent Chanel)
    if not brand_id:
        brand_id, brand_name = _detect_brand(
            " ".join(
                x
                for x in (
                    product.google_name,
                    product.name_en,
                    product.title,
                    product.tags,
                )
                if x
            )
        )

    # Unique per catalog product — same 搜索码/가격코드 색상 변형이 서로 덮어쓰지 않도록
    color_key = re_safe((use_colors[0] if use_colors else "") or "c")
    folder_key = re_safe(f"{product.id}-{display_no or price_code or 'item'}-{color_key}")
    image_urls = _copy_images(product, folder_key)
    if not image_urls:
        raise ValueError("등록할 이미지가 없습니다. 먼저 상품 이미지를 가져와 주세요.")

    name = (product.google_name or "").strip() or attrs.display_name or (
        f"{brand_name} {use_category}".strip() if brand_name else use_category
    )
    name_en = (product.name_en or "").strip()
    # If only Korean stored as google_name but EN missing, keep empty — UI still shows KO
    description = _clean_description(
        brand=brand_name or "-",
        category=use_category,
        colors=use_colors,
        sizes=use_sizes,
        dimension=attrs.dimension,
        search_code=display_no,
    )

    force_id = (mall_id or "").strip() or f"wg-{product.id}"
    item: dict[str, Any] = {
        "id": force_id,
        "skuNo": display_no,  # shown as NO under product name (= 搜索码)
        "priceCode": price_code,  # 8888… used for pricing
        "searchCode": search_code,
        "weigouId": product.goods_id or "",
        "weigouProductId": product.id,
        "name": name,
        "nameEn": name_en,
        "brand": brand_name or "미확인",
        "brandId": brand_id or "unknown",
        "price": price_info.sell if price_info else 0,
        "originalPrice": (price_info.cost * 2) if price_info else None,
        "costPrice": price_info.cost if price_info else None,
        "description": description,
        "category": use_category,
        "image": image_urls[0],
        "images": image_urls,
        "stock": 10,
        "featured": True,
        "isNew": True,
        "colors": use_colors,
        "sizes": use_sizes,
        "createdAt": product.created_at,
        "source": "weigou",
        "priceNote": price_info.label if price_info else f"가격 표시: {price_text}",
    }
    # Omit empty priceText so API upsert (price || priceText) accepts the row
    if price_text:
        item["priceText"] = price_text
    return item


def _upsert_catalog_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge by unique product id only (never by shared 搜索码/가격코드)."""
    catalog = _load_catalog()
    by_id = {str(row.get("id")): row for row in catalog if row.get("id")}
    order = [str(row.get("id")) for row in catalog if row.get("id")]
    for item in items:
        iid = str(item.get("id") or "")
        if not iid:
            continue
        if iid in by_id:
            # Keep flags like recommended when republish omits them
            by_id[iid] = {**by_id[iid], **item}
        else:
            by_id[iid] = item
            order.insert(0, iid)
    out = [by_id[i] for i in order if i in by_id]
    _save_catalog(out)
    return out


def _catalog_api_base() -> str:
    return mall_catalog_api() if cloud_enabled() else MALL_API


def _api_url(extra_query: str) -> str:
    base = _catalog_api_base().rstrip("/")
    extra = (extra_query or "").lstrip("?&")
    if not extra:
        return base
    sep = "&" if "?" in base else "?"
    return f"{base}{sep}{extra}"


def _fetch_live_catalog(*, allow_local: bool = True) -> list[dict[str, Any]]:
    """Full remote catalog. Prefer API all=1, then Supabase rows, then local mirror."""
    try:
        data = get_json(_api_url("all=1"), timeout=240)
        products = data.get("products")
        if isinstance(products, list) and products:
            return [p for p in products if isinstance(p, dict)]
    except Exception:
        pass
    # Direct Supabase (when API dump is too large / restricted)
    try:
        out: list[dict[str, Any]] = []
        start = 0
        page = 1000
        while True:
            rows = _supabase_rest(
                f"products?select=id,search_code,created_at,payload"
                f"&order=created_at.desc&offset={start}&limit={page}",
                method="GET",
                timeout=120,
            )
            if not isinstance(rows, list) or not rows:
                break
            for row in rows:
                if not isinstance(row, dict):
                    continue
                payload = row.get("payload")
                if isinstance(payload, str):
                    try:
                        payload = json.loads(payload)
                    except json.JSONDecodeError:
                        payload = None
                if not isinstance(payload, dict):
                    continue
                item = dict(payload)
                item["id"] = str(row.get("id") or item.get("id") or "").strip()
                if row.get("created_at") and not item.get("createdAt"):
                    item["createdAt"] = row["created_at"]
                if item.get("id"):
                    out.append(item)
            if len(rows) < page:
                break
            start += page
        if out:
            return out
    except Exception:
        pass
    if allow_local:
        return _load_catalog()
    return []


def _fetch_product_by_id(
    mall_id: str, *, remote_only: bool = False
) -> dict[str, Any] | None:
    """Fetch one homepage product by id (reliable; ignores pagination)."""
    mid = (mall_id or "").strip()
    if not mid:
        return None
    try:
        data = get_json(_api_url(f"id={urllib.parse.quote(mid)}"), timeout=60)
        product = data.get("product")
        if isinstance(product, dict) and product.get("id"):
            return dict(product)
    except Exception:
        if remote_only:
            return None
    if remote_only:
        return None
    for row in _load_catalog():
        if str(row.get("id") or "").strip() == mid:
            return dict(row)
    return None


def _wait_live_product(mall_id: str, *, attempts: int = 6, delay: float = 0.7) -> dict[str, Any] | None:
    mid = (mall_id or "").strip()
    if not mid:
        return None
    for i in range(max(1, attempts)):
        hit = _fetch_product_by_id(mid, remote_only=True)
        if hit:
            return hit
        if i + 1 < attempts:
            time.sleep(delay)
    return None


def resolve_mall_id(
    *,
    mall_id: str = "",
    search_code: str = "",
    goods_id: str = "",
    catalog: list[dict[str, Any]] | None = None,
) -> str:
    """Resolve homepage product id. Prefer stored mall_id, else match by 搜索码/goods."""
    mid = (mall_id or "").strip()
    if mid:
        # Verify it still exists on homepage when possible
        hit = _fetch_product_by_id(mid)
        if hit:
            return mid
        # Stale mall_id — fall through to code match
    code = (search_code or "").strip()
    gid = (goods_id or "").strip()
    if not code and not gid:
        return mid  # keep stale id for caller messaging
    # Prefer codes= lookup (cheap) over full dump
    if code:
        try:
            data = get_json(
                _api_url(f"codes={urllib.parse.quote(code)}"), timeout=60
            )
            products = data.get("products") or []
            if isinstance(products, list):
                for row in products:
                    if not isinstance(row, dict):
                        continue
                    rid = str(row.get("id") or "").strip()
                    sku = str(row.get("skuNo") or row.get("searchCode") or "").strip()
                    if rid and sku == code:
                        return rid
                    if rid and rid == code:
                        return rid
        except Exception:
            pass
    rows = catalog if catalog is not None else None
    if rows is None and (code or gid):
        # Last resort: do not pull all=1 here (slow); local mirror only
        rows = _load_catalog()
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        rid = str(row.get("id") or "").strip()
        if not rid:
            continue
        sku = str(row.get("skuNo") or row.get("searchCode") or "").strip()
        if code and sku and sku == code:
            return rid
        remote_gid = str(
            row.get("weigouId") or row.get("goodsId") or row.get("goods_id") or ""
        ).strip()
        if gid and remote_gid and remote_gid == gid:
            return rid
    return mid if mid else ""


def delete_mall_products(
    mall_ids: list[str],
    *,
    push_api: bool = True,
) -> dict[str, Any]:
    """Remove products from local catalog.json and remote homepage catalog."""
    ids = [str(x).strip() for x in mall_ids if str(x).strip()]
    ids = list(dict.fromkeys(ids))
    if not ids:
        return {"deleted": 0, "ids": [], "api": "none", "missing": []}

    # Local project catalog (dev / offline mirror)
    catalog = _load_catalog()
    before = len(catalog)
    drop = set(ids)
    kept = [p for p in catalog if str(p.get("id") or "") not in drop]
    if len(kept) != before:
        _save_catalog(kept)
    local_deleted = before - len(kept)

    api_msg = "skipped"
    remote_deleted = 0
    if push_api:
        api = mall_catalog_api() if cloud_enabled() else MALL_API
        try:
            data = request_json(
                api,
                method="DELETE",
                payload={"ids": ids},
                with_secret=True,
                timeout=90,
            )
            remote_deleted = int(data.get("deleted") or 0)
            api_msg = f"API OK deleted={remote_deleted}"
        except Exception as e:  # noqa: BLE001
            api_msg = str(e)
            if cloud_enabled():
                try:
                    # PostgREST: id=in.("a","b")
                    in_list = ",".join(
                        '"' + i.replace("\\", "\\\\").replace('"', '\\"') + '"'
                        for i in ids
                    )
                    _supabase_rest(
                        f"products?id=in.({in_list})",
                        method="DELETE",
                        prefer="return=minimal",
                    )
                    remote_deleted = len(ids)
                    api_msg = f"Supabase OK deleted≈{remote_deleted} (API: {e})"
                except Exception as sb_err:  # noqa: BLE001
                    raise RuntimeError(
                        f"홈페이지 상품 삭제 실패 — API: {api_msg} · Supabase: {sb_err}"
                    ) from sb_err
            elif local_deleted == 0:
                raise RuntimeError(f"홈페이지 상품 삭제 실패: {api_msg}") from e

    return {
        "deleted": max(local_deleted, remote_deleted),
        "localDeleted": local_deleted,
        "remoteDeleted": remote_deleted,
        "ids": ids,
        "api": api_msg,
        "missing": [],
    }


def set_products_recommended(
    mall_ids: list[str],
    *,
    recommended: bool = True,
    push_api: bool = True,
) -> dict[str, Any]:
    """Mark existing mall products as homepage recommended (or clear).

    Fetches each product by id (not the paginated list) so older items
    still get the flag on the live homepage.
    """
    ids = [str(x).strip() for x in mall_ids if str(x).strip()]
    if not ids:
        return {"updated": 0, "api": "none", "products": [], "missing": []}

    updated: list[dict[str, Any]] = []
    missing: list[str] = []
    now = datetime.now(timezone.utc).isoformat()
    for mid in ids:
        row = _fetch_product_by_id(mid)
        if not row:
            missing.append(mid)
            continue
        row = dict(row)
        row["recommended"] = bool(recommended)
        if recommended:
            row["recommendedAt"] = now
        else:
            row.pop("recommendedAt", None)
        updated.append(row)

    if updated:
        _upsert_catalog_items(updated)
    api_msg = _post_api_many(updated) if (push_api and updated) else "skipped"
    if push_api and updated and cloud_enabled() and _api_failed(api_msg):
        raise RuntimeError(f"추천 상품 API 반영 실패: {api_msg}")
    # Verify at least one remote row actually has the flag (when cloud on)
    if push_api and updated and cloud_enabled() and not _api_failed(api_msg):
        still_bad: list[str] = []
        for row in updated:
            mid = str(row.get("id") or "")
            check = _fetch_product_by_id(mid)
            if not check or bool(check.get("recommended")) != bool(recommended):
                still_bad.append(mid)
        if still_bad:
            raise RuntimeError(
                "홈페이지에 추천 상태가 반영되지 않았습니다: "
                + ", ".join(still_bad[:5])
            )
    return {
        "updated": len(updated),
        "api": api_msg,
        "products": updated,
        "missing": missing,
    }


def publish_product(
    product: Product,
    *,
    colors: list[str] | None = None,
    sizes: list[str] | None = None,
    category: str | None = None,
    push_api: bool = True,
    mall_id: str | None = None,
    recommended: bool | None = None,
    verify_live: bool = True,
) -> dict[str, Any]:
    item = build_mall_product(
        product,
        colors=colors,
        sizes=sizes,
        category=category,
        mall_id=mall_id,
    )
    now = datetime.now(timezone.utc).isoformat()
    force_id = (mall_id or "").strip()
    existing_remote = (
        _fetch_product_by_id(str(item.get("id") or ""), remote_only=True)
        if force_id or cloud_enabled()
        else None
    )
    # New publishes get "now" so they appear on the newest homepage page.
    # Re-publishes keep prior createdAt when the live row already exists.
    if existing_remote and existing_remote.get("createdAt"):
        item["createdAt"] = existing_remote.get("createdAt")
    else:
        item["createdAt"] = now
    if recommended is not None:
        item["recommended"] = bool(recommended)
        if recommended:
            item["recommendedAt"] = now
        else:
            item.pop("recommendedAt", None)
    else:
        # Preserve existing homepage recommended flag when re-publishing.
        if existing_remote and existing_remote.get("recommended"):
            item["recommended"] = True
            if existing_remote.get("recommendedAt"):
                item["recommendedAt"] = existing_remote.get("recommendedAt")
    out = _upsert_catalog_items([item])
    api_msg = _post_api(item) if push_api else "skipped"
    if push_api and cloud_enabled() and _api_failed(api_msg):
        raise RuntimeError(f"홈페이지 API 등록 실패: {api_msg}")
    mid = str(item.get("id") or "").strip()
    if push_api and verify_live and cloud_enabled():
        live = _wait_live_product(mid)
        if not live:
            raise RuntimeError(
                "홈페이지에 상품이 확인되지 않아 등록 목록으로 옮기지 않습니다 "
                f"(id={mid}). 잠시 후 다시 시도하세요."
            )
    return {
        "product": item,
        "catalogCount": len(out),
        "catalogPath": str(catalog_path()),
        "api": api_msg,
        "priceLabel": item.get("priceNote", ""),
        "verified": bool(push_api and cloud_enabled()),
    }


def publish_products(
    jobs: list[tuple[Product, list[str] | None, list[str] | None, str | None]],
    *,
    push_api: bool = True,
) -> list[dict[str, Any]]:
    """Publish many products in one catalog write so none overwrite each other.

    Each job returns its own result. Build failures are per-item (ok=False)
    and do not block the rest.
    """
    results: list[dict[str, Any]] = []
    items: list[dict[str, Any]] = []
    for product, colors, sizes, category in jobs:
        try:
            item = build_mall_product(
                product, colors=colors, sizes=sizes, category=category
            )
            items.append(item)
            results.append(
                {
                    "ok": True,
                    "productId": product.id,
                    "product": item,
                    "priceLabel": item.get("priceNote", ""),
                }
            )
        except Exception as e:  # noqa: BLE001
            results.append(
                {
                    "ok": False,
                    "productId": product.id,
                    "error": str(e),
                    "product": None,
                    "priceLabel": "",
                }
            )
    out = _upsert_catalog_items(items) if items else _load_catalog()
    api_msg = ""
    if push_api and items:
        api_msg = _post_api_many(items)
        if cloud_enabled() and _api_failed(api_msg):
            for r in results:
                if r.get("ok"):
                    r["ok"] = False
                    r["error"] = f"홈페이지 API 실패: {api_msg}"
                    r["api"] = api_msg
            return results
        if cloud_enabled():
            for r in results:
                if not r.get("ok"):
                    continue
                mid = str((r.get("product") or {}).get("id") or "").strip()
                if not mid:
                    r["ok"] = False
                    r["error"] = "mall_id 없음"
                    continue
                if not _wait_live_product(mid):
                    r["ok"] = False
                    r["error"] = f"홈페이지 미확인: {mid}"
    for r in results:
        if r.get("ok"):
            r["catalogCount"] = len(out)
            r["catalogPath"] = str(catalog_path())
            r["api"] = api_msg
    return results


def _api_failed(msg: str) -> bool:
    m = (msg or "").strip()
    if not m:
        return True
    compact = m.lower().replace(" ", "")
    if "unauthorized" in compact:
        return True
    if compact.startswith("api오류") or compact.startswith("api미연결"):
        return True
    if compact.startswith("apiok"):
        # upsertProducts can return ok:true with count:0 (all rows filtered)
        if '"count":0' in compact or "count:0" in compact:
            return True
        return False
    return True


def _post_api(item: dict[str, Any]) -> str:
    return _post_api_many([item])


def _post_api_many(items: list[dict[str, Any]]) -> str:
    api = mall_catalog_api() if cloud_enabled() else MALL_API
    return post_json(api, {"products": items}, timeout=180)


def _dup_keys(row: dict[str, Any]) -> list[str]:
    """Keys used to detect homepage duplicates.

    Prefer weigou goods id. 搜索码 alone is too weak (many distinct items can
    share a NO) — only treat as duplicate when code + name match.
    """
    keys: list[str] = []
    gid = str(row.get("weigouId") or row.get("goodsId") or "").strip()
    code = str(row.get("searchCode") or row.get("skuNo") or "").strip()
    name = str(row.get("name") or "").strip().lower()
    if gid:
        keys.append(f"gid:{gid}")
    if code and len(code) >= 3 and name:
        keys.append(f"code:{code}|name:{name}")
    return keys


def _dup_rank(row: dict[str, Any]) -> tuple:
    imgs = row.get("images") if isinstance(row.get("images"), list) else []
    return (
        1 if row.get("recommended") else 0,
        len(imgs),
        1 if row.get("image") else 0,
        str(row.get("createdAt") or ""),
        str(row.get("id") or ""),
    )


def dedupe_homepage_catalog(
    *,
    push_api: bool = True,
    on_log: ProgressCb | None = None,
) -> dict[str, Any]:
    """Remove duplicate live products (same weigouId / 搜索码). Keep best row."""
    log = on_log or (lambda _m: None)
    products = _fetch_live_catalog(allow_local=False)
    if not products:
        log("[정리] 홈페이지 상품을 불러오지 못했습니다")
        return {"scanned": 0, "deleted": 0, "groups": 0, "ids": []}

    id_to_row: dict[str, dict[str, Any]] = {}
    for row in products:
        rid = str(row.get("id") or "").strip()
        if rid:
            id_to_row[rid] = row

    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    key_owner: dict[str, str] = {}
    for rid, row in id_to_row.items():
        for key in _dup_keys(row):
            prev = key_owner.get(key)
            if prev:
                union(prev, rid)
            else:
                key_owner[key] = rid

    components: dict[str, list[str]] = {}
    for rid in id_to_row:
        components.setdefault(find(rid), []).append(rid)

    drop_ids: list[str] = []
    groups = 0
    for members in components.values():
        if len(members) <= 1:
            continue
        groups += 1
        ranked = sorted(
            (id_to_row[m] for m in members),
            key=_dup_rank,
            reverse=True,
        )
        for row in ranked[1:]:
            drop_ids.append(str(row.get("id") or ""))

    ids = [i for i in dict.fromkeys(drop_ids) if i]
    if not ids:
        log(f"[정리] 홈페이지 중복 없음 (스캔 {len(products)}개)")
        return {"scanned": len(products), "deleted": 0, "groups": 0, "ids": []}

    log(f"[정리] 홈페이지 중복 {len(ids)}개 삭제 예정 (그룹 {groups} · 스캔 {len(products)}개)")
    result = delete_mall_products(ids, push_api=push_api)
    log(f"[정리] 홈페이지 중복 삭제 완료 {result.get('deleted') or 0}개")
    return {
        "scanned": len(products),
        "deleted": int(result.get("deleted") or 0),
        "groups": groups,
        "ids": ids,
        "api": result.get("api"),
    }


def reconcile_published_to_homepage(
    store: ProductStore,
    *,
    on_log: ProgressCb | None = None,
    on_progress: Callable[[int, int, str], None] | None = None,
    dedupe_first: bool = True,
) -> dict[str, Any]:
    """Ensure every local 「등록」 row exists on the live homepage; dedupe site first."""
    log = on_log or (lambda _m: None)
    stats: dict[str, Any] = {
        "deduped": 0,
        "ok": 0,
        "fixed": 0,
        "failed": 0,
        "skipped": 0,
        "errors": [],
    }
    local_dupes = 0
    try:
        local_dupes = int(store.dedupe_published() or 0)
    except Exception:
        local_dupes = 0
    if local_dupes:
        log(f"[정리] 로컬 등록 중복 {local_dupes}개 제거")

    if dedupe_first and cloud_enabled():
        try:
            d = dedupe_homepage_catalog(push_api=True, on_log=log)
            stats["deduped"] = int(d.get("deleted") or 0)
        except Exception as e:  # noqa: BLE001
            log(f"[정리] 홈페이지 중복 정리 실패: {e}")
            stats["errors"].append(str(e))

    items = store.list_published()
    total = len(items)
    log(f"[맞추기] 로컬 등록 {total}개 → 홈페이지 확인/재등록 시작")

    live_rows = _fetch_live_catalog(allow_local=False)
    by_id: dict[str, dict[str, Any]] = {}
    by_code: dict[str, dict[str, Any]] = {}
    by_gid: dict[str, dict[str, Any]] = {}
    for row in live_rows:
        rid = str(row.get("id") or "").strip()
        if rid:
            by_id[rid] = row
        code = str(row.get("searchCode") or row.get("skuNo") or "").strip()
        if code and code not in by_code:
            by_code[code] = row
        gid = str(row.get("weigouId") or row.get("goodsId") or "").strip()
        if gid and gid not in by_gid:
            by_gid[gid] = row
    log(f"[맞추기] 홈페이지 현재 {len(by_id)}개 로드")

    for i, item in enumerate(items, start=1):
        name = (item.google_name or item.title or f"#{item.id}").strip()[:40]
        if on_progress:
            on_progress(i - 1, total, f"[{i}/{total}] {name}")
        try:
            mid = (item.mall_id or "").strip()
            code = (item.search_code or "").strip()
            gid = (item.goods_id or "").strip()
            live = None
            if mid and mid in by_id:
                live = by_id[mid]
            elif code and code in by_code:
                live = by_code[code]
            elif gid and gid in by_gid:
                live = by_gid[gid]
            if live:
                new_mid = str(live.get("id") or mid or "").strip()
                if new_mid and new_mid != (item.mall_id or "").strip():
                    store.update_published(item.id, mall_id=new_mid)
                stats["ok"] += 1
                continue

            product = store.published_to_product(item)
            if not (product.sku_no or "").strip():
                store.update_published(item.id, sku_no=DEFAULT_PRICE_TEXT)
                item = store.get_published(item.id) or item
                product = store.published_to_product(item)
            colors = [c.strip() for c in (item.colors or "").split(",") if c.strip()]
            sizes = [s.strip() for s in (item.sizes or "").split(",") if s.strip()]
            use_mid = mid or None
            result = publish_product(
                product,
                colors=colors or None,
                sizes=sizes or None,
                category=item.category or None,
                push_api=True,
                mall_id=use_mid,
                recommended=True if item.recommended else None,
                verify_live=True,
            )
            new_mall = str((result.get("product") or {}).get("id") or use_mid or "")
            store.update_published(
                item.id,
                mall_id=new_mall,
                note=result.get("priceLabel") or item.note,
            )
            # Keep indexes fresh so later local dupes match the new live row
            pub = result.get("product") or {}
            if isinstance(pub, dict) and new_mall:
                by_id[new_mall] = pub
                c2 = str(pub.get("searchCode") or pub.get("skuNo") or code).strip()
                if c2:
                    by_code[c2] = pub
                g2 = str(pub.get("weigouId") or gid).strip()
                if g2:
                    by_gid[g2] = pub
            stats["fixed"] += 1
            log(f"[맞추기] 재등록 완료 등록#{item.id} → {new_mall}")
        except Exception as e:  # noqa: BLE001
            stats["failed"] += 1
            err = f"등록#{item.id}: {e}"
            stats["errors"].append(err)
            log(f"[맞추기] 실패 {err}")
    if on_progress:
        on_progress(total, total, "완료")
    log(
        f"[맞추기] 끝 - 이미있음 {stats['ok']} / 재등록 {stats['fixed']} / "
        f"실패 {stats['failed']} / 중복삭제 {stats['deduped']}"
    )
    return stats


def preview_price(sku_no: str) -> str:
    code = effective_price_code(sku_no)
    info = decode_price_code(code)
    if info:
        return info.label
    if is_text_price_label(code) or code == DEFAULT_PRICE_TEXT:
        note = "" if (sku_no or "").strip() else " (빈칸→자동)"
        return f"가격 표시: {code}{note}"
    return f"가격코드 예: 8888033 → 33만원 / 00008 → 8만원 / 또는 한글: {DEFAULT_PRICE_TEXT}"

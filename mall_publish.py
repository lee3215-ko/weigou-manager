# -*- coding: utf-8 -*-
"""Publish Weigou products into the Shoot Repl mall catalog."""
from __future__ import annotations

import json
import pathlib
import re
import shutil
from datetime import datetime, timezone
from typing import Any

from price_codec import (
    DEFAULT_PRICE_TEXT,
    decode_price_code,
    effective_price_code,
    is_text_price_label,
)
from product_attrs import _detect_brand, extract_attrs
from product_store import Product
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


def _fetch_live_catalog() -> list[dict[str, Any]]:
    """Prefer remote catalog when cloud is on; fall back to local JSON."""
    api = mall_catalog_api() if cloud_enabled() else MALL_API
    try:
        data = get_json(api, timeout=90)
        products = data.get("products")
        if isinstance(products, list) and products:
            return [p for p in products if isinstance(p, dict)]
    except Exception:
        pass
    return _load_catalog()


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
    """Mark existing mall products as homepage recommended (or clear)."""
    ids = [str(x).strip() for x in mall_ids if str(x).strip()]
    if not ids:
        return {"updated": 0, "api": "none", "products": [], "missing": []}

    catalog = _fetch_live_catalog()
    by_id = {str(p.get("id")): dict(p) for p in catalog if p.get("id")}
    updated: list[dict[str, Any]] = []
    missing: list[str] = []
    now = datetime.now(timezone.utc).isoformat()
    for mid in ids:
        row = by_id.get(mid)
        if not row:
            missing.append(mid)
            continue
        row["recommended"] = bool(recommended)
        if recommended:
            row["recommendedAt"] = now
        else:
            row.pop("recommendedAt", None)
        by_id[mid] = row
        updated.append(row)

    if updated:
        _upsert_catalog_items(updated)
    api_msg = _post_api_many(updated) if (push_api and updated) else "skipped"
    if push_api and updated and cloud_enabled() and _api_failed(api_msg):
        raise RuntimeError(f"추천 상품 API 반영 실패: {api_msg}")
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
) -> dict[str, Any]:
    item = build_mall_product(
        product,
        colors=colors,
        sizes=sizes,
        category=category,
        mall_id=mall_id,
    )
    out = _upsert_catalog_items([item])
    api_msg = _post_api(item) if push_api else "skipped"
    if push_api and cloud_enabled() and _api_failed(api_msg):
        raise RuntimeError(f"홈페이지 API 등록 실패: {api_msg}")
    return {
        "product": item,
        "catalogCount": len(out),
        "catalogPath": str(catalog_path()),
        "api": api_msg,
        "priceLabel": item.get("priceNote", ""),
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
    low = m.lower()
    if low.startswith("api ok"):
        return False
    return (
        low.startswith("api 오류")
        or low.startswith("api 미연결")
        or "unauthorized" in low
    )


def _post_api(item: dict[str, Any]) -> str:
    return _post_api_many([item])


def _post_api_many(items: list[dict[str, Any]]) -> str:
    api = mall_catalog_api() if cloud_enabled() else MALL_API
    return post_json(api, {"products": items}, timeout=120)


def preview_price(sku_no: str) -> str:
    code = effective_price_code(sku_no)
    info = decode_price_code(code)
    if info:
        return info.label
    if is_text_price_label(code) or code == DEFAULT_PRICE_TEXT:
        note = "" if (sku_no or "").strip() else " (빈칸→자동)"
        return f"가격 표시: {code}{note}"
    return f"가격코드 예: 8888033 → 33만원 / 00008 → 8만원 / 또는 한글: {DEFAULT_PRICE_TEXT}"

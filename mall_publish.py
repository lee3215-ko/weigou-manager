# -*- coding: utf-8 -*-
"""Publish Weigou products into the Shoot Repl mall catalog."""
from __future__ import annotations

import json
import pathlib
import re
import shutil
import urllib.error
import urllib.request
from typing import Any

from price_codec import decode_price_code, is_text_price_label
from product_attrs import extract_attrs
from product_store import Product
from image_enhance import enhance_image_file

MALL_API = "http://127.0.0.1:3000/api/catalog"


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
        urls.append(f"/uploads/{folder_key}/{final_name}")
    return urls


def re_safe(value: str) -> str:
    s = re.sub(r"[^\w\-]+", "-", (value or "").strip())
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
) -> dict[str, Any]:
    price_code = (product.sku_no or "").strip()
    search_code = (product.search_code or "").strip()
    # Homepage "NO" under product name = 搜索码
    display_no = search_code or price_code

    price_info = decode_price_code(price_code)
    price_text = ""
    if not price_info:
        if is_text_price_label(price_code):
            # 한글 가격 문구 → 쇼핑몰에 숫자 대신 그대로 표시
            price_text = price_code.strip()
        else:
            raise ValueError(
                f"가격 코드를 해석할 수 없습니다: NO(가격)={price_code or '(없음)'}\n"
                "예: 8888033 → 원가 33만원 / 8888033.5 → 원가 33.5만원(335,000원)\n"
                "또는 한글 문구: 반수제품 가격문의\n"
                "검색코드(搜索码)는 제품명 아래 NO로 표시됩니다."
            )

    attrs = extract_attrs(product.title, product.tags, product.description)
    raw_colors = colors if colors is not None and len(colors) > 0 else (attrs.colors or ["블랙"])
    use_colors = normalize_colors(raw_colors) or ["블랙"]
    use_sizes = sizes if sizes is not None and len(sizes) > 0 else (attrs.sizes or ["FREE"])
    use_category = category if category else attrs.category

    # Unique per catalog product — same 搜索码/가격코드 색상 변형이 서로 덮어쓰지 않도록
    color_key = re_safe((use_colors[0] if use_colors else "") or "c")
    folder_key = re_safe(f"{product.id}-{display_no or price_code or 'item'}-{color_key}")
    image_urls = _copy_images(product, folder_key)
    if not image_urls:
        raise ValueError("등록할 이미지가 없습니다. 먼저 상품 이미지를 가져와 주세요.")

    name = (product.google_name or "").strip() or attrs.display_name or f"{attrs.brand_name} {use_category}"
    name_en = (product.name_en or "").strip()
    # If only Korean stored as google_name but EN missing, keep empty — UI still shows KO
    description = _clean_description(
        brand=attrs.brand_name,
        category=use_category,
        colors=use_colors,
        sizes=use_sizes,
        dimension=attrs.dimension,
        search_code=display_no,
    )

    return {
        "id": f"wg-{product.id}",
        "skuNo": display_no,  # shown as NO under product name (= 搜索码)
        "priceCode": price_code,  # 8888… used for pricing
        "searchCode": search_code,
        "weigouId": product.goods_id or "",
        "weigouProductId": product.id,
        "name": name,
        "nameEn": name_en,
        "brand": attrs.brand_name,
        "brandId": attrs.brand_id,
        "price": price_info.sell if price_info else 0,
        "originalPrice": (price_info.cost * 2) if price_info else None,
        "costPrice": price_info.cost if price_info else None,
        "priceText": price_text or None,
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
            by_id[iid] = item
        else:
            by_id[iid] = item
            order.insert(0, iid)
    out = [by_id[i] for i in order if i in by_id]
    _save_catalog(out)
    return out


def publish_product(
    product: Product,
    *,
    colors: list[str] | None = None,
    sizes: list[str] | None = None,
    category: str | None = None,
    push_api: bool = True,
) -> dict[str, Any]:
    item = build_mall_product(
        product, colors=colors, sizes=sizes, category=category
    )
    out = _upsert_catalog_items([item])
    api_msg = _post_api(item) if push_api else ""
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
    for r in results:
        if r.get("ok"):
            r["catalogCount"] = len(out)
            r["catalogPath"] = str(catalog_path())
            r["api"] = api_msg
    return results


def _post_api(item: dict[str, Any]) -> str:
    return _post_api_many([item])


def _post_api_many(items: list[dict[str, Any]]) -> str:
    body = json.dumps({"products": items}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        MALL_API,
        data=body,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return f"API OK ({resp.status}) {raw[:160]}"
    except urllib.error.URLError as e:
        return f"API 미연결 (파일로만 저장됨): {e}"
    except Exception as e:  # noqa: BLE001
        return f"API 오류: {e}"


def preview_price(sku_no: str) -> str:
    info = decode_price_code(sku_no)
    if info:
        return info.label
    if is_text_price_label(sku_no):
        return f"가격 표시: {sku_no.strip()}"
    return "가격코드 예: 8888033 → 33만원 / 또는 한글: 반수제품 가격문의"

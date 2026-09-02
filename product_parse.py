# -*- coding: utf-8 -*-
"""Parse Weigou album HTML / page text into product records."""
from __future__ import annotations

import html
import re
from dataclasses import dataclass, field

from collector import normalize_image_url


@dataclass
class ParsedProduct:
    goods_id: str = ""
    shop_id: str = ""
    title: str = ""
    search_code: str = ""
    sku_no: str = ""
    tags: str = ""
    description: str = ""
    image_urls: list[str] = field(default_factory=list)
    list_seq: int | None = None


_RE_GOODS = re.compile(
    r'data-search-bury-info="\{&quot;goods_id&quot;:&quot;([^&]+)&quot;,'
    r'&quot;shop_id&quot;:&quot;([^&]*)&quot;',
    re.I,
)
_RE_GOODS_ALT = re.compile(
    r'data-search-bury-info="\{&quot;goods_id&quot;:&quot;([^&]+)&quot;',
    re.I,
)
_RE_TITLE = re.compile(
    r'class="[^"]*ellipsis-two[^"]*"[^>]*>(.*?)</div>',
    re.I | re.S,
)
_RE_SOURCE_TITLE = re.compile(
    r'class="[^"]*sourceTitle[^"]*"[^>]*>[\s\S]*?<span[^>]*>\s*([^<]+?)\s*</span>',
    re.I,
)
_RE_IMG = re.compile(
    r"https://xcimg\.szwego\.com/(?:img(?:HD)?/[^\\\"'\\s<>]+|\d{8}/[^\\\"'\\s<>]+\.(?:jpe?g|png|webp|gif)(?!_\d))",
    re.I,
)
_RE_SEARCH = re.compile(r"搜索码\s*[：:]\s*(\d+)")
_RE_SEARCH_CLIP = re.compile(
    r"搜索码\s*[：:]?\s*</[^>]+>\s*<[^>]*data-clipboard-text=\"(\d+)\"",
    re.I | re.S,
)
_RE_SEARCH_CLIP_ANY = re.compile(
    r'class="[^"]*moment_copy_text[^"]*"[^>]*data-clipboard-text="(\d+)"',
    re.I,
)
_RE_SKU = re.compile(r"NO\s*[：:]\s*([0-9.]+)", re.I)
_RE_ATTR_TAG = re.compile(
    r'class="[^"]*attributeTag[^"]*"[^>]*>([^<]+)<',
    re.I,
)


def _clean_text(s: str) -> str:
    s = html.unescape(s or "")
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _uniq_urls(urls: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in urls:
        u = normalize_image_url(raw)
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    return out


def _is_detail_page(page_html: str) -> bool:
    markers = (
        "GoodsDynamicDetails",
        "GoodsMomentItem",
        "sourceItemGrid",
        "GoodsMomentAttribute",
        "moment_copy_text",
    )
    return any(m in (page_html or "") for m in markers)


def _fill_meta(p: ParsedProduct) -> ParsedProduct:
    blob = "\n".join(x for x in (p.title, p.description, p.tags) if x)
    if not p.search_code:
        m = _RE_SEARCH.search(blob)
        if m:
            p.search_code = m.group(1)
    if not p.sku_no:
        m = _RE_SKU.search(blob)
        if m:
            p.sku_no = m.group(1)
    if not p.description:
        parts = [p.title]
        if p.search_code:
            parts.append(f"搜索码：{p.search_code}")
        if p.sku_no and f"NO：{p.sku_no}" not in (p.title or ""):
            parts.append(f"NO：{p.sku_no}")
        if p.tags:
            parts.append(p.tags)
        p.description = "\n".join(parts)
    return p


def parse_list_products(page_html: str) -> list[ParsedProduct]:
    """Parse friend-album list grid cards."""
    if not page_html:
        return []
    chunks = re.split(r'(?=<div class="relative w-1-3"[^>]*data-search-bury-info=)', page_html)
    products: list[ParsedProduct] = []
    seen_ids: set[str] = set()

    for chunk in chunks:
        if "data-search-bury-info" not in chunk[:300]:
            continue
        m = _RE_GOODS.search(chunk[:500]) or _RE_GOODS_ALT.search(chunk[:500])
        if not m:
            continue
        goods_id = m.group(1)
        shop_id = m.group(2) if m.lastindex and m.lastindex >= 2 else ""
        if goods_id in seen_ids:
            continue
        body = chunk[:6000]
        title_m = _RE_TITLE.search(body)
        title = _clean_text(title_m.group(1)) if title_m else ""
        imgs = _uniq_urls(_RE_IMG.findall(body))
        if not imgs and not title:
            continue
        seen_ids.add(goods_id)
        p = ParsedProduct(
            goods_id=goods_id,
            shop_id=shop_id,
            title=title,
            image_urls=imgs[:1] if imgs else [],
        )
        if len(imgs) > 1:
            p.image_urls = imgs
        products.append(_fill_meta(p))
    return products


def parse_detail_product(page_html: str, page_text: str = "") -> ParsedProduct | None:
    """Parse a single product detail page (title + search code + many images)."""
    text = page_text or ""
    if not text and page_html:
        text = _clean_text(re.sub(r"<script[\s\S]*?</script>", " ", page_html, flags=re.I))
        text = _clean_text(re.sub(r"<style[\s\S]*?</style>", " ", text, flags=re.I))

    imgs = _uniq_urls(_RE_IMG.findall(page_html or ""))

    search = ""
    for rx in (_RE_SEARCH_CLIP, _RE_SEARCH_CLIP_ANY, _RE_SEARCH):
        m = rx.search(page_html or "") or rx.search(text)
        if m:
            search = m.group(1)
            break

    title = ""
    sm = _RE_SOURCE_TITLE.search(page_html or "")
    if sm:
        title = _clean_text(sm.group(1))
    if not title:
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        for ln in lines[:40]:
            if ln in ("详情", "全部", "上新", "小视频", "图集", "编辑", "复制", "收藏", "下载"):
                continue
            if ln.startswith("搜索码"):
                break
            if len(ln) >= 4 and ("size" in ln.lower() or "NO" in ln or len(ln) >= 8):
                title = ln
                break
    if not title:
        tm = _RE_TITLE.search(page_html or "")
        if tm:
            title = _clean_text(tm.group(1))

    goods_id = ""
    shop_id = ""
    gm = _RE_GOODS.search(page_html or "") or _RE_GOODS_ALT.search(page_html or "")
    if gm:
        goods_id = gm.group(1)
        shop_id = gm.group(2) if gm.lastindex and gm.lastindex >= 2 else ""

    tags = ""
    am = _RE_ATTR_TAG.search(page_html or "")
    if am:
        tags = _clean_text(am.group(1))
    if not tags:
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        for ln in lines:
            if re.search(r"[\uac00-\ud7a3]", ln) and len(ln) <= 40:
                tags = ln
                break

    if not imgs and not title and not search:
        return None

    desc_parts = []
    if title:
        desc_parts.append(title)
    if search:
        desc_parts.append(f"搜索码：{search}")
    if tags:
        desc_parts.append(tags)

    p = ParsedProduct(
        goods_id=goods_id,
        shop_id=shop_id,
        title=title,
        search_code=search,
        tags=tags,
        description="\n".join(desc_parts),
        image_urls=imgs,
    )
    return _fill_meta(p)


def parse_products(page_html: str, page_text: str = "") -> list[ParsedProduct]:
    """
    Auto-detect detail vs list.
    Detail if 搜索码 present, GoodsDynamicDetails, or many images with one title.
    """
    detail = parse_detail_product(page_html, page_text)
    if _is_detail_page(page_html) and detail:
        return [detail]

    list_items = parse_list_products(page_html)
    has_search = bool(detail and detail.search_code)
    many_imgs = bool(detail and len(detail.image_urls) >= 3)

    if has_search or (many_imgs and len(list_items) <= 1):
        return [detail] if detail else list_items
    if list_items:
        return list_items
    return [detail] if detail else []

# -*- coding: utf-8 -*-
"""Parse Google AI multi-image answers into per-product name/color."""
from __future__ import annotations

import re

from product_name import (
    build_product_name,
    extract_ai_clothing_fields,
    extract_ai_labeled_fields,
    normalize_ai_color,
)

_KO_ORD = [
    (1, r"첫\s*번째"),
    (2, r"두\s*번째"),
    (3, r"세\s*번째"),
    (4, r"네\s*번째"),
    (5, r"다섯\s*번째"),
    (6, r"여섯\s*번째"),
    (7, r"일곱\s*번째"),
    (8, r"여덟\s*번째"),
    (9, r"아홉\s*번째"),
    (10, r"열\s*번째"),
]

_RE_START = re.compile(
    r"^[•\-–—*·\d.．)\]]*\s*"
    r"(?:"
    r"(?:첫|두|세|네|다섯|여섯|일곱|여덟|아홉|열)\s*번째"
    r"|(\d+)\s*번(?:째)?"
    r")"
    r"(?:\s*이미지)?",
    re.I,
)

_RE_PAREN_TONE = re.compile(r"[\(（]([^\)）]+)[\)）]")


def _ord_from_line(line: str) -> int | None:
    s = re.sub(r"[*_`#]+", "", (line or "")).strip()
    if not s:
        return None
    m = _RE_START.match(s)
    if not m:
        # "이미지 1" / "Image 2"
        m_img = re.search(r"이미지\s*(\d+)|image\s*(\d+)", s, re.I)
        if m_img:
            try:
                return int(m_img.group(1) or m_img.group(2))
            except ValueError:
                return None
        # "1. …" / "1) …"
        m2 = re.match(r"^[•\-–—*·]?\s*(\d+)\s*[\.．\)]\s*", s)
        if m2:
            try:
                return int(m2.group(1))
            except ValueError:
                return None
        return None
    if m.group(1):
        try:
            return int(m.group(1))
        except ValueError:
            return None
    for num, pat in _KO_ORD:
        if re.search(pat, s):
            return num
    return None


def _keep_color_phrase(raw: str) -> str:
    """Keep AI color wording even if not in our dictionary."""
    text = re.sub(r"[*\[\]`#_~]+", "", (raw or "")).strip()
    if not text:
        return ""
    text = re.split(r"\s*[\(（]", text, maxsplit=1)[0].strip()
    # Keep "오렌지 / 블루" as one color; only strip brand suffixes after /
    text = re.split(
        r"\s*/\s*(?:Chanel|Herm[eè]s|KREAM|eBay|Louis|Dior|Gucci)\b",
        text,
        maxsplit=1,
        flags=re.I,
    )[0].strip()
    text = re.split(r"\s+또는\s+|\s+or\s+", text, maxsplit=1, flags=re.I)[0].strip()
    text = re.sub(r"\s*(계열|톤|컬러|색상)\s*$", "", text).strip()
    if not text or len(text) > 40:
        return ""
    if re.search(r"문의|입니다|보입니다|이미지|제품명|사이즈", text):
        return ""
    return text


def _color_from_chunk(chunk_lines: list[str], head: str) -> str:
    head = re.sub(r"[*_`#]+", "", head or "").strip()
    # 1) explicit 컬러: label inside chunk
    _n, c = extract_ai_labeled_fields(chunk_lines)
    if c:
        return c
    # 2) "N번째 이미지 (블랙): 느와르 / …"
    m = _RE_PAREN_TONE.search(head)
    tone = (m.group(1).strip() if m else "") or ""
    after = ""
    if ":" in head or "：" in head:
        after = re.split(r"[:：]", head, maxsplit=1)[-1].strip()
    elif m:
        # no colon — text after the parenthesis
        after = head[m.end() :].strip(" -–—|/")
    after = _keep_color_phrase(after)
    tone_main = _keep_color_phrase(tone.split("·")[0] if tone else "")

    # Prefer specific name after colon (느와르), else parenthesis tone (블랙)
    for cand in (after, tone_main, tone):
        if not cand:
            continue
        nc = normalize_ai_color(cand)
        if nc:
            return nc
        kept = _keep_color_phrase(cand)
        if kept:
            return kept
    # 3) dictionary / normalize from whole chunk
    named = build_product_name(chunk_lines, hint="")
    if named.color:
        return named.color
    blob = "\n".join(chunk_lines)
    return normalize_ai_color(blob) or _keep_color_phrase(blob) or ""


def _name_from_chunk(chunk_lines: list[str], shared_name: str) -> tuple[str, str]:
    ctype, _c = extract_ai_clothing_fields(chunk_lines)
    if ctype:
        return ctype, ""
    name, _c = extract_ai_labeled_fields(chunk_lines)
    if name:
        named = build_product_name([name], hint="")
        return name, named.name_en or ""
    # Color-guide lines ("N번째 이미지 …") — keep shared model name
    head = chunk_lines[0] if chunk_lines else ""
    if shared_name and _ord_from_line(head) is not None:
        named = build_product_name([shared_name], hint="")
        return shared_name, named.name_en or ""
    named = build_product_name(chunk_lines, hint=shared_name)
    if shared_name:
        return shared_name, named.name_en or ""
    return named.name, named.name_en


def parse_multi_image_answers(
    lines: list[str], count: int
) -> list[dict[str, str]]:
    """Split AI multi-image guide into per-index {name, name_en, color, raw}."""
    shared_name, _shared_color = extract_ai_labeled_fields(lines)
    if not shared_name:
        # Intro often states one model for all images
        for ln in lines[:12]:
            if re.search(r"에르메스|샤넬|루이|디올|구찌|Hermes|Chanel", ln, re.I):
                named = build_product_name([ln], hint="")
                if named.name and len(named.name.split()) >= 2:
                    shared_name = named.name
                    break

    buckets: dict[int, list[str]] = {}
    order: list[int] = []
    cur: int | None = None
    for ln in lines:
        idx = _ord_from_line(ln)
        if idx is not None and 1 <= idx <= max(count, 20):
            cur = idx
            if cur not in buckets:
                buckets[cur] = []
                order.append(cur)
            buckets[cur].append(ln)
            continue
        if cur is not None:
            buckets[cur].append(ln)

    out: list[dict[str, str]] = []
    for i in range(1, count + 1):
        chunk = buckets.get(i) or []
        head = chunk[0] if chunk else ""
        if not chunk and shared_name:
            out.append(
                {
                    "name": shared_name,
                    "name_en": "",
                    "color": "",
                    "raw": "",
                }
            )
            continue
        name, name_en = _name_from_chunk(chunk, shared_name)
        color = _color_from_chunk(chunk, head)
        # If name still empty, keep shared
        if not name:
            name = shared_name
        out.append(
            {
                "name": name or "",
                "name_en": name_en or "",
                "color": color or "",
                "raw": "\n".join(chunk[:8]),
            }
        )
    return out

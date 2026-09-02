# -*- coding: utf-8 -*-
"""Publish AI model coordination looks into the Shoot Repl mall."""
from __future__ import annotations

import json
import pathlib
import re
import shutil
import time
from typing import Any

from image_enhance import enhance_image_file
from mall_cloud import (
    cloud_enabled,
    mall_styles_api,
    post_json,
    upload_file,
)

MALL_STYLES_API = "http://127.0.0.1:3000/api/styles"
STYLE_BUCKET = "style-images"

# + 버튼 기본 위치 (제품 옆, 카테고리별)
PIN_BY_CATEGORY: dict[str, tuple[float, float, str]] = {
    "가방": (28.0, 56.0, "가방"),
    "신발": (70.0, 91.0, "신발"),
    "여성옷": (58.0, 38.0, "여성옷"),
    "남성옷": (55.0, 42.0, "남성옷"),
    "선글라스": (72.0, 22.0, "선글라스"),
    "벨트": (55.0, 58.0, "벨트"),
    "시계": (74.0, 30.0, "시계"),
    "악세사리": (74.0, 26.0, "악세사리"),
    "기타": (50.0, 50.0, "기타"),
}


def project_root() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parents[2]


def styles_json_path() -> pathlib.Path:
    return project_root() / "data" / "style-looks.json"


def styles_public_dir() -> pathlib.Path:
    return project_root() / "public" / "styles"


def _safe_slug(text: str) -> str:
    s = re.sub(r"[^\w\-]+", "-", (text or "").strip(), flags=re.UNICODE)
    s = re.sub(r"-+", "-", s).strip("-").lower()
    return s[:48] or "look"


def _load_looks() -> list[dict[str, Any]]:
    path = styles_json_path()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return list(data.get("looks") or [])
        if isinstance(data, list):
            return data
    except json.JSONDecodeError:
        return []
    return []


def _save_looks(looks: list[dict[str, Any]]) -> None:
    path = styles_json_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"looks": looks}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


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


def _post_api(
    look: dict[str, Any],
    *,
    replace_all: bool = False,
    looks: list[dict[str, Any]] | None = None,
) -> str:
    api = mall_styles_api() if cloud_enabled() else MALL_STYLES_API
    if replace_all:
        payload: dict[str, Any] = {
            "replaceAll": True,
            "looks": looks if looks is not None else [look],
        }
    else:
        payload = {"look": look}
    return post_json(api, payload, timeout=120)


def build_pins(items: list[dict[str, str]]) -> list[dict[str, Any]]:
    """items: {code, category, label?} → pin list with x/y beside products."""
    pins: list[dict[str, Any]] = []
    used: dict[str, int] = {}
    for item in items:
        cat = (item.get("category") or "가방").strip() or "가방"
        code = (item.get("code") or "").strip()
        if not code:
            continue
        base = PIN_BY_CATEGORY.get(cat) or PIN_BY_CATEGORY["가방"]
        n = used.get(cat, 0)
        used[cat] = n + 1
        x = min(88.0, max(10.0, base[0] + n * 5))
        y = min(94.0, max(12.0, base[1] + (n % 2) * 3))
        pins.append(
            {
                "code": code,
                "x": round(x, 1),
                "y": round(y, 1),
                "label": (item.get("label") or base[2] or cat).strip(),
            }
        )
    return pins


def _has_batchim(word: str) -> bool:
    """한글 마지막 글자에 받침이 있으면 True (와/과, 을/를 선택용)."""
    if not word:
        return False
    ch = word.strip()[-1]
    if "가" <= ch <= "힣":
        return (ord(ch) - 0xAC00) % 28 != 0
    return False


def _particle_wa(name: str) -> str:
    return f"{name}과" if _has_batchim(name) else f"{name}와"


def _particle_eul(name: str) -> str:
    return f"{name}을" if _has_batchim(name) else f"{name}를"


def _particle_ro(name: str) -> str:
    """로 / 으로 — 받침 ㄹ은 '로', 그 외 받침은 '으로'."""
    if not name:
        return "로"
    ch = name.strip()[-1]
    if "가" <= ch <= "힣":
        jong = (ord(ch) - 0xAC00) % 28
        if jong == 0 or jong == 8:  # 없음 or ㄹ
            return f"{name}로"
        return f"{name}으로"
    return f"{name}로"


def _compact_product_name(name: str, *, max_len: int = 28) -> str:
    """제품명을 자연스럽게 짧게 — 중간 … 자르기 없이 브랜드+핵심 모델만."""
    s = re.sub(r"\s+", " ", (name or "").strip())
    s = re.sub(r"[…\.]+$", "", s).strip()
    if not s:
        return ""
    if len(s) <= max_len:
        return s
    parts = s.split()
    if len(parts) >= 4:
        # 앞 브랜드 1~2어절 + 뒤 모델 1~2어절
        head = parts[:2]
        tail = parts[-2:]
        cand = " ".join(head + [t for t in tail if t not in head])
        if len(cand) <= max_len + 2:
            return cand
        cand = f"{parts[0]} {parts[-2]} {parts[-1]}"
        if len(cand) <= max_len + 2:
            return cand
        return f"{parts[0]} {parts[-1]}"
    if len(parts) == 3:
        cand = f"{parts[0]} {parts[-1]}"
        return cand if len(cand) <= max_len else parts[0]
    return parts[0]


def auto_look_copy(items: list[dict[str, str]]) -> tuple[str, str]:
    """제목·설명을 자연스러운 문장으로 자동 생성 (… 생략 없음)."""
    cats: list[str] = []
    names: list[str] = []
    for it in items:
        cat = (it.get("category") or it.get("label") or "").strip()
        if cat and cat not in cats:
            cats.append(cat)
        name = (it.get("name") or "").strip()
        if name and name not in names:
            names.append(name)

    if len(cats) >= 2:
        title = f"{_particle_wa(cats[0])} {cats[1]} 룩"
    elif len(cats) == 1:
        title = f"{cats[0]} 스타일 룩"
    else:
        title = "AI 모델 코디"

    short_names = [_compact_product_name(n) for n in names]
    short_names = [n for n in short_names if n]

    if len(short_names) >= 3:
        a, b = short_names[0], short_names[1]
        subtitle = (
            f"{_particle_wa(a)} {_particle_eul(b)} 중심으로 "
            f"{len(short_names)}가지 아이템을 매치한 코디"
        )
    elif len(short_names) == 2:
        a, b = short_names[0], short_names[1]
        # 예: 생 로랑 가비 슬라이드 뮬에 셀린느 클래식 트리옹프 백을 더한 코디
        subtitle = f"{a}에 {_particle_eul(b)} 더한 코디"
    elif len(short_names) == 1:
        subtitle = f"{_particle_ro(short_names[0])} 완성한 스타일링"
    elif len(cats) >= 2:
        subtitle = f"{_particle_wa(cats[0])} {cats[1]} 아이템으로 완성한 코디"
    elif cats:
        subtitle = f"{cats[0]} 아이템으로 완성한 코디"
    else:
        subtitle = "선택한 상품으로 완성한 코디"
    return title, subtitle


def publish_style_look(
    *,
    model_image: pathlib.Path,
    items: list[dict[str, str]],
    title: str = "",
    subtitle: str = "",
    replace_all: bool = False,
) -> dict[str, Any]:
    """
    Copy model image → public/styles, upsert look into data/style-looks.json.
    items: [{code, category, label?, name?}, ...]  code = 搜索码 / product NO
    제목·설명은 비우면 상품 정보로 자동 생성.
    """
    src = pathlib.Path(model_image)
    if not src.exists():
        raise ValueError("모델 이미지 파일이 없습니다.")

    pins = build_pins(items)
    if not pins:
        raise ValueError("상품 번호(搜索码)가 없습니다. AI 상품선택을 먼저 해 주세요.")

    look_id = f"look-{_safe_slug(pins[0]['code'])}-{int(time.time())}"
    styles_dir = styles_public_dir()
    styles_dir.mkdir(parents=True, exist_ok=True)

    ext = src.suffix.lower() if src.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"} else ".png"
    dest_name = f"{look_id}{ext}"
    dest = styles_dir / dest_name
    shutil.copy2(src, dest)
    try:
        enhance_image_file(dest, dest)
    except Exception:
        pass

    auto_title, auto_sub = auto_look_copy(items)
    look_title = (title or "").strip() or auto_title
    look_sub = (subtitle or "").strip() or auto_sub

    if cloud_enabled():
        model_url = upload_file(STYLE_BUCKET, dest_name, dest)
    else:
        model_url = f"/styles/{dest_name}"

    look: dict[str, Any] = {
        "id": look_id,
        "title": look_title,
        "subtitle": look_sub,
        "mood": "AI MODEL",
        "modelImage": model_url,
        "items": pins,
        "createdAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }

    looks = [] if replace_all else _load_looks()
    # newest first
    looks = [look, *[x for x in looks if x.get("id") != look_id]]
    _save_looks(looks)

    api_msg = _post_api(look, replace_all=replace_all, looks=looks if replace_all else None)
    if cloud_enabled() and _api_failed(api_msg):
        raise RuntimeError(f"AI 코디 API 적용 실패: {api_msg}")
    return {"look": look, "count": len(looks), "api": api_msg, "path": str(dest)}

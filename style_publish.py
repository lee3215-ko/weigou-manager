# -*- coding: utf-8 -*-
"""Publish AI model coordination looks into the Shoot Repl mall."""
from __future__ import annotations

import json
import pathlib
import re
import shutil
import time
import urllib.error
import urllib.request
from typing import Any

from image_enhance import enhance_image_file

MALL_STYLES_API = "http://127.0.0.1:3000/api/styles"

# + 버튼 기본 위치 (제품 옆, 카테고리별)
PIN_BY_CATEGORY: dict[str, tuple[float, float, str]] = {
    "가방": (28.0, 56.0, "가방"),
    "신발": (70.0, 91.0, "신발"),
    "상의": (58.0, 38.0, "상의"),
    "하의": (55.0, 68.0, "하의"),
    "자켓": (60.0, 30.0, "아우터"),
    "악세사리": (74.0, 26.0, "악세사리"),
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


def _post_api(look: dict[str, Any]) -> str:
    body = json.dumps({"look": look}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        MALL_STYLES_API,
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


def auto_look_copy(items: list[dict[str, str]]) -> tuple[str, str]:
    """제목·설명 자동 생성 (입력 없이)."""
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
        title = f"{cats[0]}·{cats[1]} 룩"
    elif len(cats) == 1:
        title = f"{cats[0]} 스타일 룩"
    else:
        title = "AI 모델 코디"

    def short(s: str, n: int = 16) -> str:
        s = re.sub(r"\s+", " ", s).strip()
        return s if len(s) <= n else s[: n - 1] + "…"

    if len(names) >= 2:
        subtitle = f"{short(names[0])}과 {short(names[1])}로 맞춘 코디"
    elif len(names) == 1:
        subtitle = f"{short(names[0], 22)} 기준으로 맞춘 코디"
    elif cats:
        subtitle = f"{'·'.join(cats[:3])} 등록 상품으로 구성"
    else:
        subtitle = "등록 상품으로 구성한 코디"
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

    look: dict[str, Any] = {
        "id": look_id,
        "title": look_title,
        "subtitle": look_sub,
        "mood": "AI MODEL",
        "modelImage": f"/styles/{dest_name}",
        "items": pins,
        "createdAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }

    looks = [] if replace_all else _load_looks()
    # newest first
    looks = [look, *[x for x in looks if x.get("id") != look_id]]
    _save_looks(looks)

    api_msg = _post_api(look)
    return {"look": look, "count": len(looks), "api": api_msg, "path": str(dest)}

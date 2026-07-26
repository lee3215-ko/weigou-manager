# -*- coding: utf-8 -*-
"""Extract category / colors / sizes / brand from Weigou product text."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path


BRAND_MAP = [
    ("LOUIS VUITTON", "louisvuitton", "루이비통"),
    ("SAINT LAURENT", "ysl", "생로랑"),
    ("BALENCIAGA", "balenciaga", "발렌시아가"),
    ("BOTTEGA", "bottega", "보테가"),
    ("HERMES", "hermes", "에르메스"),
    ("HERMÈS", "hermes", "에르메스"),
    ("CHROME HEARTS", "chromehearts", "크롬하츠"),
    ("CHROMHEARTS", "chromehearts", "크롬하츠"),
    ("THOM BROWNE", "thombrowne", "톰브라운"),
    ("CHANEL", "chanel", "샤넬"),
    ("CELINE", "celine", "셀린느"),
    ("GUCCI", "gucci", "구찌"),
    ("PRADA", "prada", "프라다"),
    ("DIOR", "dior", "디올"),
    ("YSL", "ysl", "생로랑"),
    ("LV", "louisvuitton", "루이비통"),
    ("루이비통", "louisvuitton", "루이비통"),
    ("생로랑", "ysl", "생로랑"),
    ("발렌시아가", "balenciaga", "발렌시아가"),
    ("보테가", "bottega", "보테가"),
    ("에르메스", "hermes", "에르메스"),
    ("샤넬", "chanel", "샤넬"),
    ("셀린느", "celine", "셀린느"),
    ("구찌", "gucci", "구찌"),
    ("프라다", "prada", "프라다"),
    ("디올", "dior", "디올"),
    ("迪奥", "dior", "디올"),
    ("香奈儿", "chanel", "샤넬"),
    ("古驰", "gucci", "구찌"),
]

COLOR_WORDS = [
    "블랙", "화이트", "베이지", "브라운", "네이비", "그레이", "레드", "핑크",
    "그린", "블루", "스카이 블루", "스카이블루", "라이트 블루", "골드", "실버",
    "아이보리", "카멜", "카키", "올리브", "버건디", "브릭",
    "옐로우", "오렌지", "퍼플", "보라", "노랑", "노란", "흰색", "검정",
    "BLACK", "WHITE", "BEIGE", "BROWN", "NAVY", "GREY", "GRAY", "RED",
    "PINK", "GREEN", "BLUE", "SKY BLUE", "LIGHT BLUE", "GOLD", "SILVER",
    "IVORY", "CAMEL", "KHAKI", "OLIVE", "YELLOW", "ORANGE", "PURPLE",
]

# Avoid false positives: THOM BROWNE → BROWN
_COLOR_ASCII_BLOCK = re.compile(
    r"THOM\s*BROWNE|BROWNE",
    re.I,
)

COLOR_KO = {
    "BLACK": "블랙",
    "WHITE": "화이트",
    "BEIGE": "베이지",
    "BROWN": "브라운",
    "NAVY": "네이비",
    "GREY": "그레이",
    "GRAY": "그레이",
    "RED": "레드",
    "PINK": "핑크",
    "GREEN": "그린",
    "BLUE": "블루",
    "GOLD": "골드",
    "SILVER": "실버",
    "IVORY": "아이보리",
    "CAMEL": "카멜",
    "KHAKI": "카키",
    "OLIVE": "올리브",
    "YELLOW": "옐로우",
    "ORANGE": "오렌지",
    "PURPLE": "퍼플",
    "SKY BLUE": "스카이 블루",
    "LIGHT BLUE": "스카이 블루",
    "스카이블루": "스카이 블루",
    "라이트 블루": "스카이 블루",
    "노랑": "옐로우",
    "노란": "옐로우",
    "흰색": "화이트",
    "검정": "블랙",
    "보라": "퍼플",
}

CN_COLOR = {
    "黑色": "블랙",
    "黑": "블랙",
    "白色": "화이트",
    "白": "화이트",
    "米色": "베이지",
    "棕色": "브라운",
    "褐色": "브라운",
    "蓝色": "블루",
    "蓝": "블루",
    "浅蓝": "스카이 블루",
    "浅蓝色": "스카이 블루",
    "天蓝": "스카이 블루",
    "天蓝色": "스카이 블루",
    "粉蓝": "스카이 블루",
    "藏青": "네이비",
    "深蓝": "네이비",
    "深蓝色": "네이비",
    "红色": "레드",
    "红": "레드",
    "粉色": "핑크",
    "粉": "핑크",
    "绿色": "그린",
    "绿": "그린",
    "军绿": "올리브",
    "橄榄绿": "올리브",
    "墨绿": "올리브",
    "卡其": "카키",
    "卡其色": "카키",
    "黄色": "옐로우",
    "黄": "옐로우",
    "橙色": "오렌지",
    "橘色": "오렌지",
    "紫色": "퍼플",
    "灰": "그레이",
    "灰色": "그레이",
    "金色": "골드",
    "银色": "실버",
}

# RGB centroids for dominant-color naming (fallback)
_RGB_NAMED = [
    ("블랙", (20, 20, 20)),
    ("화이트", (245, 245, 245)),
    ("그레이", (140, 140, 140)),
    ("베이지", (220, 200, 160)),
    ("브라운", (110, 70, 40)),
    ("카멜", (180, 130, 80)),
    ("네이비", (25, 40, 90)),
    ("블루", (40, 90, 180)),
    ("스카이 블루", (150, 185, 220)),
    ("레드", (180, 35, 35)),
    ("핑크", (230, 130, 160)),
    ("그린", (50, 140, 70)),
    ("카키", (120, 115, 70)),
    ("올리브", (90, 100, 55)),
    ("옐로우", (230, 200, 50)),
    ("오렌지", (230, 120, 40)),
    ("퍼플", (120, 50, 150)),
    ("골드", (200, 170, 70)),
    ("실버", (190, 190, 195)),
]

# Prefer these when voting across images (body colors over metal/packaging)
_BODY_COLORS = {
    "블랙",
    "화이트",
    "그레이",
    "베이지",
    "브라운",
    "카멜",
    "카키",
    "올리브",
    "네이비",
    "블루",
    "스카이 블루",
    "레드",
    "핑크",
    "그린",
    "옐로우",
    "오렌지",
    "퍼플",
    "아이보리",
    "버건디",
}


@dataclass
class ProductAttrs:
    category: str = "가방"
    brand_id: str = "chanel"
    brand_name: str = "샤넬"
    colors: list[str] = field(default_factory=list)
    sizes: list[str] = field(default_factory=list)
    is_shoes: bool = False
    display_name: str = ""
    dimension: str = ""


def _detect_brand(text: str) -> tuple[str, str]:
    upper = text.upper()
    for key, bid, name in BRAND_MAP:
        if key.isascii():
            if key in upper:
                return bid, name
        elif key in text:
            return bid, name
    return "chanel", "샤넬"


def _detect_colors(text: str) -> list[str]:
    found: list[str] = []
    # Strip brand names that contain color-looking English (Thom Browne → Brown)
    safe = _COLOR_ASCII_BLOCK.sub(" ", text or "")

    # Chinese first (longer keys first)
    for cn, ko in sorted(CN_COLOR.items(), key=lambda x: -len(x[0])):
        if cn in safe and ko not in found:
            found.append(ko)

    upper = safe.upper()
    for word in COLOR_WORDS:
        if word.isascii():
            if re.search(rf"\b{re.escape(word)}\b", upper, re.I):
                mapped = COLOR_KO.get(word.upper(), word)
                if mapped not in found:
                    found.append(mapped)
        elif word in safe:
            mapped = COLOR_KO.get(word, word)
            if mapped not in found:
                found.append(mapped)
    return found


def _classify_pixel(r: float, g: float, b: float) -> str:
    """Label one RGB pixel for product-color voting.

    Rules (priority):
    1) Skip studio white / dust-bag background and gold hardware.
    2) Detect green / khaki / olive BEFORE near-black — dark nylon bags
       (olive Prada etc.) were misread as 블랙 because value was low.
    3) True black needs low value AND low chroma (not green/brown cast).
    """
    import colorsys

    rf, gf, bf = r / 255.0, g / 255.0, b / 255.0
    h, s, v = colorsys.rgb_to_hsv(rf, gf, bf)
    deg = h * 360.0
    # Chroma helpers: green/olive cast vs neutral black
    green_cast = (g - r) > 8 and (g - b) > 5
    warm_cast = (r - b) > 18 and (r - g) > 5

    luma = (0.2126 * rf) + (0.7152 * gf) + (0.0722 * bf)

    # Only skip clipped studio flash — NOT white leather (that was the gray bug)
    if v >= 0.97 and s <= 0.08:
        return "bg"
    if luma >= 0.96 and s <= 0.06:
        return "bg"

    # Gold / brass hardware (skip for body color)
    if 32 <= deg <= 58 and s >= 0.28 and v >= 0.42:
        return "metal"
    if 28 <= deg <= 62 and s >= 0.45 and v >= 0.55:
        return "metal"

    # --- White / ivory FIRST (before gray) ---
    # Pebbled white leather is often luma 0.70–0.92; old code discarded it as bg.
    if s <= 0.18 and luma >= 0.72:
        return "화이트"
    if s <= 0.22 and luma >= 0.78:
        return "화이트"
    if s <= 0.14 and v >= 0.68:
        return "화이트"
    if s <= 0.28 and luma >= 0.78 and 20 <= deg <= 55:
        return "화이트"
    if s <= 0.25 and luma >= 0.82 and (deg <= 60 or deg >= 300):
        return "화이트"

    # --- Green family (olive / khaki / green nylon) ---
    # Hue 55–165 covers yellow-green → green. Dark olive often has v~0.25–0.45.
    if 55 <= deg <= 165 and s >= 0.12 and v >= 0.18:
        if 55 <= deg <= 95:
            # khaki / olive / military green
            if v <= 0.48:
                return "올리브"
            if s <= 0.45:
                return "카키"
            return "그린" if deg >= 75 else "카키"
        # cooler greens
        return "그린"

    # Explicit green channel dominance (even when HSV edge cases)
    if green_cast and s >= 0.10 and 0.18 <= v <= 0.72 and deg <= 180:
        return "올리브" if v <= 0.48 else "카키"

    # Brown / camel leather accents (before black)
    if warm_cast and 8 <= deg <= 55 and s >= 0.18 and 0.22 <= v <= 0.70:
        if v >= 0.55 and s <= 0.40:
            return "베이지"
        if v >= 0.48 and s >= 0.35:
            return "카멜"
        return "브라운"

    # True black / near-black — require low chroma (not olive/brown cast)
    if v <= 0.22 and s <= 0.35 and not green_cast:
        return "블랙"
    if v <= 0.32 and s <= 0.16 and not green_cast and not warm_cast:
        return "블랙"
    if v <= 0.38 and s <= 0.10 and not green_cast:
        return "블랙"

    # Neutral gray — mid brightness only (light = white, dark = black)
    if s <= 0.14:
        if luma >= 0.68 or v >= 0.68:
            return "화이트"
        if v <= 0.40 or luma <= 0.38:
            return "블랙"
        if 0.40 <= v <= 0.65:
            return "그레이"
        return "화이트"

    # Pink / blush / nude-pink (before beige — pale pink was misread as beige)
    if (deg >= 320 or deg <= 20) and s >= 0.08 and v >= 0.45:
        if s <= 0.35 and v >= 0.62:
            return "핑크"
        if v >= 0.45:
            return "핑크" if s >= 0.18 else "베이지"

    # Beige / ivory / camel / brown
    if 18 <= deg <= 55:
        if v >= 0.78 and s <= 0.30:
            return "화이트"  # cream / ivory bag
        if v >= 0.72 and s <= 0.28:
            return "베이지"
        if v >= 0.55 and s <= 0.40:
            return "베이지"
        if v >= 0.55 and s >= 0.45 and deg <= 48:
            return "카멜" if v < 0.75 else "골드"
        if v < 0.55:
            return "브라운"

    if 8 <= deg < 18:
        return "브라운" if v < 0.55 else "카멜"

    if deg < 8 or deg >= 345:
        if v < 0.40:
            return "버건디" if s > 0.30 else "블랙"
        return "레드"

    if 55 < deg <= 70:
        if v >= 0.55 and s >= 0.35:
            return "옐로우"
        return "카키" if s >= 0.18 else "베이지"

    if 70 < deg <= 160:
        return "그린"

    if 160 < deg <= 255:
        if v >= 0.68 and s <= 0.55:
            return "스카이 블루"
        if v >= 0.42:
            return "블루"
        return "네이비"

    if 255 < deg <= 310:
        return "퍼플"
    if 310 < deg < 345:
        return "핑크" if v > 0.45 else "버건디"

    return "other"


def detect_color_from_image(image_path: str | Path | None) -> str:
    """Dominant product body color — ignores white packaging and gold hardware."""
    if not image_path:
        return ""
    path = Path(image_path)
    if not path.exists():
        return ""
    try:
        from PIL import Image
    except ImportError:
        return ""
    try:
        im = Image.open(path).convert("RGB")
        w, h = im.size
        # Center crop: bag body usually mid-frame; packaging often edges
        box = (int(w * 0.18), int(h * 0.14), int(w * 0.82), int(h * 0.86))
        im = im.crop(box).resize((96, 96), Image.Resampling.BILINEAR)

        from collections import Counter

        votes: Counter[str] = Counter()
        for r, g, b in im.getdata():
            label = _classify_pixel(float(r), float(g), float(b))
            if label in ("bg", "metal", "other"):
                continue
            votes[label] += 1

        if not votes:
            return ""

        total = sum(votes.values())
        black_n = votes.get("블랙", 0)
        white_n = votes.get("화이트", 0) + votes.get("아이보리", 0)
        gray_n = votes.get("그레이", 0)
        beige_n = votes.get("베이지", 0)
        pink_n = votes.get("핑크", 0)
        green_n = (
            votes.get("그린", 0)
            + votes.get("카키", 0)
            + votes.get("올리브", 0)
        )
        brown_n = votes.get("브라운", 0) + votes.get("카멜", 0)

        # White bag: folds cast gray shadows — white must beat gray
        if white_n >= total * 0.18 and white_n >= gray_n * 0.55:
            return "화이트"
        if white_n >= gray_n and white_n >= total * 0.12 and gray_n <= total * 0.45:
            return "화이트"
        if gray_n >= total * 0.28 and gray_n > white_n * 1.35 and white_n < total * 0.15:
            return "그레이"

        # Green / khaki / olive body wins over black leather trim / straps
        if green_n >= total * 0.12 and green_n >= black_n * 0.55:
            g_votes = {
                k: votes.get(k, 0) for k in ("올리브", "카키", "그린")
            }
            return max(g_votes, key=g_votes.get)

        # Pale pink / nude often mislabeled beige
        if pink_n >= total * 0.20 and pink_n >= beige_n:
            return "핑크"

        # Black only when truly dominant and not outvoted by body colors
        if (
            black_n >= total * 0.28
            and black_n >= beige_n
            and black_n >= pink_n
            and black_n >= green_n * 1.4
            and black_n >= brown_n
            and black_n >= white_n
        ):
            return "블랙"

        name, count = votes.most_common(1)[0]
        if count < max(8, total * 0.08):
            return ""
        if name == "그레이" and white_n >= gray_n * 0.5:
            return "화이트"
        if name == "베이지" and white_n >= beige_n:
            return "화이트"
        if name == "블랙" and green_n >= total * 0.10:
            g_votes = {k: votes.get(k, 0) for k in ("올리브", "카키", "그린")}
            return max(g_votes, key=g_votes.get)
        return name
    except Exception:
        return ""


def detect_color_from_images(image_paths: list[str] | None) -> str:
    """Vote across several product photos; prefer body color (not packaging)."""
    if not image_paths:
        return ""
    from collections import Counter

    tallies: Counter[str] = Counter()
    for path in image_paths[:6]:
        c = detect_color_from_image(path)
        if c and c in _BODY_COLORS:
            if c in ("화이트", "아이보리"):
                tallies[c] += 2
            elif c in ("올리브", "카키", "그린"):
                tallies[c] += 2
            else:
                tallies[c] += 1
    if not tallies:
        return detect_color_from_image(image_paths[0]) if image_paths else ""

    white_n = tallies.get("화이트", 0) + tallies.get("아이보리", 0)
    gray_n = tallies.get("그레이", 0)
    if white_n >= 1 and white_n >= gray_n:
        return "화이트"

    green_n = tallies.get("그린", 0) + tallies.get("카키", 0) + tallies.get("올리브", 0)
    black_n = tallies.get("블랙", 0)
    if green_n >= 1 and green_n >= black_n:
        g_votes = {k: tallies.get(k, 0) for k in ("올리브", "카키", "그린")}
        best = max(g_votes, key=g_votes.get)
        if g_votes[best] > 0:
            return best
    return tallies.most_common(1)[0][0]

def _shoe_sizes(start: int, end: int) -> list[str]:
    if end < start:
        start, end = end, start
    start = max(200, start)
    end = min(310, end)
    return [str(n) for n in range(start, end + 1, 5)]


def _parse_bag_dimension(text: str) -> str:
    """size 21*11.5*4.5 / 21x11.5x4.5 / bare 21*11.5*4.5"""
    patterns = [
        r"(?:size|사이즈|치수)\s*[：:\s]*"
        r"(\d+(?:\.\d+)?\s*[xX*×]\s*\d+(?:\.\d+)?(?:\s*[xX*×]\s*\d+(?:\.\d+)?)?)",
        r"(?<![\d.])(\d+(?:\.\d+)?\s*[xX*×]\s*\d+(?:\.\d+)?\s*[xX*×]\s*\d+(?:\.\d+)?)(?![\d.])",
        r"(?<![\d.])(\d+(?:\.\d+)?\s*[xX*×]\s*\d+(?:\.\d+)?)(?![\d.\-])",
    ]
    for pat in patterns:
        m = re.search(pat, text, re.I)
        if not m:
            continue
        raw = m.group(1)
        # reject shoe ranges mistaken as dims (225-245 already handled elsewhere)
        if "-" in raw or "～" in raw:
            continue
        dim = re.sub(r"\s+", "", raw).replace("×", "*").replace("x", "*").replace("X", "*")
        parts = dim.split("*")
        # bag dims are usually small numbers (<100), not shoe mm
        try:
            nums = [float(p) for p in parts]
        except ValueError:
            continue
        if any(n >= 150 for n in nums):
            continue
        return dim
    return ""


def _expand_couple_range(text: str) -> list[str]:
    """Couple S-XL / S-L → list of sizes."""
    m = re.search(
        r"(?:Couple\s+)?(XXS|XS|S|M|L|XL|XXL)\s*[-~～]\s*(XXS|XS|S|M|L|XL|XXL|2XL|3XL)",
        text,
        re.I,
    )
    if not m:
        return []
    order = ["XXS", "XS", "S", "M", "L", "XL", "XXL", "2XL", "3XL"]
    a, b = m.group(1).upper(), m.group(2).upper()
    if a not in order or b not in order:
        return [a, b]
    i, j = order.index(a), order.index(b)
    if j < i:
        i, j = j, i
    return order[i : j + 1]


def _parse_sizes(text: str) -> tuple[list[str], bool, str]:
    """Return (sizes, is_shoes, dimension)."""
    dim = _parse_bag_dimension(text)

    # shoe range: size 225-245
    m = re.search(
        r"(?:size|사이즈)\s*[：:\s]*(\d{2,3})\s*[-~～]\s*(\d{2,3})",
        text,
        re.I,
    )
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        if a >= 200 or b >= 200:
            return _shoe_sizes(a, b), True, dim

    m2 = re.search(r"(?:size|사이즈)\s*[：:\s]*(\d{3})\b", text, re.I)
    if m2:
        n = int(m2.group(1))
        if 200 <= n <= 310:
            return [str(n)], True, dim

    # bag dimension takes priority over letter sizes
    if dim:
        return [dim], False, dim

    couple = _expand_couple_range(text)
    if couple:
        return couple, False, dim

    clothes = re.findall(r"\b(XXS|XS|S|M|L|XL|XXL|2XL|3XL)\b", text, re.I)
    if clothes:
        out: list[str] = []
        for c in clothes:
            u = c.upper()
            if u not in out:
                out.append(u)
        return out, False, dim

    return [], False, dim


def _has_any(text: str, lower: str, keys: tuple[str, ...] | list[str]) -> bool:
    return any(k.lower() in lower or k in text for k in keys)


def _detect_category(text: str, is_shoes: bool, dimension: str = "") -> str:
    if is_shoes:
        return "신발"
    lower = text.lower()

    has_bag = _has_any(
        text,
        lower,
        (
            "가방",
            "包包",
            "bag",
            "tote",
            "클러치",
            "크로스백",
            "백팩",
            "핸드백",
            "shoulder bag",
            "handbag",
            "버킷백",
            "버킨",
            "birkin",
            "kelly",
            "켈리",
            "背包",
            "手提包",
            "斜挎",
            "手拿包",
        ),
    )

    # 1) Scarves — before bag-dimension (90*90 etc.)
    if _has_any(
        text,
        lower,
        (
            "스카프",
            "scarf",
            "twilly",
            "트윌리",
            "マフラー",
            "围巾",
            "丝巾",
            "絲巾",
            "방도",
            "bandeau",
            "shawl",
            "숄",
            "머플러",
        ),
    ):
        return "악세사리"

    # 2) Clothing — 옷-여 / 衣服女 / WOMEN
    if _has_any(
        text,
        lower,
        (
            "하의",
            "팬츠",
            "pants",
            "skirt",
            "스커트",
            "데님",
            "슬랙스",
            "진",
            "短裤",
            "裤子",
            "半裙",
            "裙子",
        ),
    ):
        return "하의"
    if _has_any(
        text,
        lower,
        (
            "자켓",
            "jacket",
            "코트",
            "패딩",
            "아우터",
            "블레이저",
            "outer",
            "外套",
            "大衣",
        ),
    ):
        return "자켓"
    if _has_any(
        text,
        lower,
        (
            "상의",
            "shirt",
            "tee",
            "t-shirt",
            "니트",
            "후드",
            "맨투맨",
            "가디건",
            "cardigan",
            "블라우스",
            "blouse",
            "sweater",
            "스웨터",
            "knit",
            "hoodie",
            "옷-여",
            "옷-남",
            "옷여",
            "옷남",
            "衣服",
            "女装",
            "男装",
            "women",
            "men's",
            "mens",
            "woman",
            "ladies",
            "의류",
            "의상",
            "티셔츠",
            "원피스",
            "dress",
            "조끼",
            "vest",
        ),
    ):
        return "상의"

    # 3) Bags — shop tags like 가방지갑 / 包包 win over bare 지갑
    if has_bag:
        return "가방"

    # 4) Other accessories
    if _has_any(
        text,
        lower,
        (
            "지갑",
            "钱包",
            "wallet",
            "卡包",
            "belt",
            "벨트",
            "腰带",
            "watch",
            "시계",
            "목걸이",
            "귀걸이",
            "팔찌",
            "브로치",
            "선글라스",
            "모자",
            "악세사리",
            "액세서리",
            "配饰",
            "饰品",
        ),
    ):
        return "악세사리"

    # 5) Shoes by keyword
    if _has_any(
        text,
        lower,
        (
            "shoe",
            "sneaker",
            "로퍼",
            "힐",
            "부츠",
            "슬리퍼",
            "샌들",
            "구두",
            "鞋",
            "拖鞋",
        ),
    ):
        return "신발"

    # 6) Dimension → bag when no clothing/scarf cue
    if dimension:
        return "가방"
    if re.search(r"(?<![가-힣a-z])백(?![가-힣a-z])", text) or "包" in text:
        return "가방"

    return "가방"


def _korean_only(text: str) -> str:
    s = re.sub(r"[\u4e00-\u9fff]+", "", text or "")
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _build_display_name(title: str, tags: str, brand_name: str, category: str) -> str:
    ko_tag = _korean_only(tags)
    if ko_tag and len(ko_tag) >= 2:
        return ko_tag

    raw = title or ""
    raw = re.split(r"\b(?:NO|size|사이즈|搜索码)\b", raw, maxsplit=1, flags=re.I)[0]
    raw = _korean_only(raw) or raw.strip()
    raw = re.sub(r"\s+", " ", raw).strip(" -_/")

    if not raw or raw.upper() in {b[0] for b in BRAND_MAP if b[0].isascii()}:
        return f"{brand_name} {category}".strip()
    if raw.upper() == brand_name.upper() or raw == brand_name:
        return f"{brand_name} {category}".strip()
    return raw


def extract_attrs(
    title: str = "",
    tags: str = "",
    description: str = "",
    image_path: str | Path | None = None,
    image_paths: list[str] | None = None,
    default_color: bool = False,
    default_size: bool = False,
) -> ProductAttrs:
    blob = "\n".join(x for x in (title, tags, description) if x)
    brand_id, brand_name = _detect_brand(blob)
    colors = _detect_colors(blob)
    sizes, is_shoes, dimension = _parse_sizes(blob)
    category = _detect_category(blob, is_shoes, dimension)

    paths: list[str] = []
    if image_paths:
        paths.extend(str(p) for p in image_paths if p)
    if image_path:
        p0 = str(image_path)
        if p0 not in paths:
            paths.insert(0, p0)

    img_color = detect_color_from_images(paths) if paths else ""
    if img_color:
        # Image is the ground truth for product body color.
        # Text colors from tags are often missing; old beige guesses were packaging.
        weak = {"베이지", "골드", "실버", "아이보리"}
        greenish = {"그린", "카키", "올리브"}
        if not colors:
            colors = [img_color]
        elif img_color == "화이트" and colors[0] in {"그레이", "베이지", "실버", "아이보리"}:
            colors = ["화이트"]
        elif img_color not in colors:
            if colors[0] in weak and img_color not in weak:
                colors = [img_color]
            elif img_color in greenish:
                colors = [img_color]
            elif img_color == "블랙" and colors[0] in greenish:
                pass
            elif img_color == "블랙" and colors[0] not in greenish | {"블랙"}:
                colors = [img_color]
            else:
                colors = [img_color] + [c for c in colors if c != img_color]
        elif colors[0] == "블랙" and img_color in greenish:
            colors = [img_color] + [c for c in colors if c != img_color]
        elif colors[0] == "그레이" and img_color == "화이트":
            colors = ["화이트"]

    if is_shoes and not sizes:
        sizes = [str(n) for n in range(220, 281, 5)]

    # Do NOT force 블랙/FREE unless explicitly requested
    if default_color and not colors:
        colors = ["블랙"]
    if default_size and not sizes:
        sizes = ["FREE"]

    display_name = _build_display_name(title, tags, brand_name, category)

    return ProductAttrs(
        category=category,
        brand_id=brand_id,
        brand_name=brand_name,
        colors=colors,
        sizes=sizes,
        is_shoes=is_shoes or category == "신발",
        display_name=display_name,
        dimension=dimension,
    )

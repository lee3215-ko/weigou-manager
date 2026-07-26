# -*- coding: utf-8 -*-
"""Build bilingual (KO / EN) luxury product names from reverse-image results."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from product_attrs import extract_attrs


_RE_GUESS = re.compile(r"图中可能是\s*(.+)")
_RE_PRICE = re.compile(r"^[￥¥$]\s*[\d.,]+$")

# (pattern, ko, en)
_BRANDS = [
    (r"路易威登|LOUIS\s*VUITTON|\bLV\b", "루이비통", "Louis Vuitton"),
    (r"圣罗兰|SAINT\s*LAURENT|\bYSL\b", "생로랑", "Saint Laurent"),
    (r"巴黎世家|BALENCIAGA", "발렌시아가", "Balenciaga"),
    (r"葆蝶家|BOTTEGA", "보테가", "Bottega Veneta"),
    (r"香奈儿|CHANEL", "샤넬", "Chanel"),
    (r"普拉达|PRADA", "프라다", "Prada"),
    (r"爱马仕|HERM[EÈ]S", "에르메스", "Hermès"),
    (r"赛琳|思琳|CELINE", "셀린느", "Celine"),
    (r"迪奥|DIOR", "디올", "Dior"),
    (r"古驰|GUCCI", "구찌", "Gucci"),
    (r"芬迪|FENDI", "펜디", "Fendi"),
]

_MODELS = [
    (r"Lady\s*D[-\s]?Joy|LadyDJoy|D-?Joy|Djoy|迪奥?D.?joy|레이디\s*D\s*조이", "레이디 D 조이", "Lady D Joy"),
    (r"My\s*Dior|我的迪奥|DiorMyDior|마이\s*디올", "마이 디올", "My Dior"),
    (r"Coco\s*Beach|코코\s*비치|可可海滩", "코코 비치", "Coco Beach"),
    (r"Book\s*Tote|북\s*토트", "북 토트", "Book Tote"),
    (r"Lady\s*Dior|戴妃|레이디\s*디올", "레이디 디올", "Lady Dior"),
    (r"Classic\s*Flap|经典翻盖|클래식\s*플랩|Timeless", "클래식 플랩", "Classic Flap"),
    # Hermès
    (r"\bLindy\b|린디|琳迪", "린디", "Lindy"),
    (r"\bBirkin\b|버킨|铂金包", "버킨", "Birkin"),
    (r"\bKelly\b|켈리|凯莉", "켈리", "Kelly"),
    (r"\bPicotin\b|피코틴|菜篮子", "피코틴", "Picotin"),
    (r"\bEvelyne\b|에블린|伊芙琳", "에블린", "Evelyne"),
    (r"\bConstance\b|콘스탄스", "콘스탄스", "Constance"),
    (r"Garden\s*Party|가든\s*파티", "가든 파티", "Garden Party"),
    (r"\bBolide\b|볼리드", "볼리드", "Bolide"),
    (r"\bHerbag\b|에르백", "에르백", "Herbag"),
    (r"\bRoulis\b|룰리스", "룰리스", "Roulis"),
    (r"\bJypsiere\b|집시에르", "집시에르", "Jypsiere"),
    # Chanel / others
    (r"플랩|\bFlap\b", "플랩", "Flap"),
    (r"Saddle|马鞍包", "새들", "Saddle"),
    (r"\bCaro\b", "카로", "Caro"),
    (r"Tribeca", "트라이베카", "Tribeca"),
    (r"Jackie", "재키", "Jackie"),
    (r"Ophidia", "오피디아", "Ophidia"),
    (r"Dionysus", "디오니소스", "Dionysus"),
    (r"Marmont", "마몽", "Marmont"),
    (r"Horsebit|1955", "홀스빗", "Horsebit"),
    (r"Card\s*Holder|卡包|卡夹|卡片夹", "카드 홀더", "Card Holder"),
    (r"WOC|Wallet\s*on\s*Chain", "WOC", "WOC"),
    (r"Boy\s*Chanel", "보이 샤넬", "Boy Chanel"),
    (r"Galleria", "갤러리아", "Galleria"),
    (r"Re-?Edition", "리에디션", "Re-Edition"),
    (r"\bSpeedy\b|스피디", "스피디", "Speedy"),
    (r"\bNeverfull\b|네버풀", "네버풀", "Neverfull"),
    (r"\bAlma\b|알마", "알마", "Alma"),
    (r"\bCapucines\b|카퓌신", "카퓌신", "Capucines"),
]

# Hermès/Chanel style numeric model size: 린디 26, Birkin 30
_RE_MODEL_NUM = re.compile(
    r"(?:린디|버킨|켈리|피코틴|에블린|콘스탄스|가든\s*파티|볼리드|"
    r"Lindy|Birkin|Kelly|Picotin|Evelyne|Constance|Bolide|"
    r"Speedy|Neverfull|클래식\s*플랩|Classic\s*Flap)"
    r"\s*[#]?\s*(\d{2}(?:\.\d)?)\b",
    re.I,
)

_MATERIALS = [
    (r"Cannage|藤格纹|菱格|카나쥬|까나쥬", "까나쥬", "Cannage"),
    (r"Macrame|Macram[eé]|마크라메|마크라메", "마크라메", "Macrame"),
    (r"Mesh|메쉬|网纱|网眼", "메쉬", "Mesh"),
    (r"Crochet|编织|钩织|크로셰|니트\s*메시|knit\s*mesh", "크로셰", "Crochet"),
    (r"Tweed|斜纹软呢|트위드|呢子", "트위드", "Tweed"),
    (r"Lambskin|小羊皮|羊皮|램스킨", "램스킨", "Lambskin"),
    (r"Caviar|鱼子酱|캐비어", "캐비어", "Caviar"),
    (r"Denim|牛仔|데님", "데님", "Denim"),
    (r"Nylon|尼龙|나일론", "나일론", "Nylon"),
    (r"Oblique|老花|오블리크", "오블리크", "Oblique"),
    (r"Calfskin|小牛皮|카프스킨", "카프스킨", "Calfskin"),
]

_SIZES = [
    (r"迷你|미니|\bmini\b", "미니", "Mini"),
    (r"小号|스몰|\bsmall\b", "스몰", "Small"),
    (r"中号|미디엄|\bmedium\b", "미디엄", "Medium"),
    (r"大号|라지|\blarge\b", "라지", "Large"),
]

_TYPES = [
    (r"迷你手袋|迷你手提包|迷你包|mini\s*(hand)?bag", "미니 백", "Mini Bag"),
    (r"手提包|手袋|handbag", "백", "Bag"),
    (r"托特|tote", "토트백", "Tote Bag"),
    (r"斜挎|crossbody", "크로스백", "Crossbody Bag"),
    (r"单肩|shoulder", "숄더백", "Shoulder Bag"),
    (r"手拿|clutch", "클러치", "Clutch"),
    (r"双肩|backpack", "백팩", "Backpack"),
    (r"钱包|wallet", "지갑", "Wallet"),
    (r"卡包|卡夹|card\s*holder", "카드 홀더", "Card Holder"),
    (r"拖鞋|mule|slide", "뮬", "Mule"),
    (r"凉鞋|sandal", "샌들", "Sandal"),
    (r"运动鞋|sneaker", "스니커즈", "Sneaker"),
    (r"短靴|ankle", "앵클부츠", "Ankle Boot"),
    (r"长靴|boots?", "부츠", "Boot"),
    (r"高跟鞋|heel|泵", "힐", "Heel"),
    (r"乐福|loafer", "로퍼", "Loafer"),
    (r"鞋|shoes?", "신발", "Shoes"),
    (r"包|bags?", "백", "Bag"),
]

_COLORS = [
    (r"New\s*Bleu\s*Jean|뉴\s*블루\s*진|블루\s*진|Bleu\s*Jean", "뉴 블루 진", "New Bleu Jean"),
    (r"Bleu\s*Lin|블루\s*린", "블루 린", "Bleu Lin"),
    (r"Bleu\s*Agate|블루\s*아가트", "블루 아가트", "Bleu Agate"),
    (r"Bleu\s*Nuit|블루\s*뉘", "블루 뉘", "Bleu Nuit"),
    (r"拿铁色|拿铁|latte", "라떼", "Latte"),
    (r"sand|샌드|沙色", "샌드", "Sand"),
    (r"深蓝色|深蓝|dark\s*blue|다크\s*블루|네이비|navy|藏青", "네이비", "Navy"),
    (r"浅蓝色|浅蓝|sky\s*blue|스카이\s*블루|light\s*blue|라이트\s*블루|天蓝|粉蓝", "스카이 블루", "Sky Blue"),
    (r"蓝色|蓝|blue|블루", "블루", "Blue"),
    (r"黑色|黑|black|블랙", "블랙", "Black"),
    (r"白色|白|white|화이트|磨砂白", "화이트", "White"),
    (r"米色|beige|베이지|象牙", "베이지", "Beige"),
    (r"棕色|褐色|brown|브라운", "브라운", "Brown"),
    (r"粉色|粉|pink|핑크", "핑크", "Pink"),
    (r"红色|红|red|레드", "레드", "Red"),
    (r"卡其|khaki|카키|军绿|橄榄绿|olive|올리브", "카키", "Khaki"),
    (r"绿色|绿|green|그린", "그린", "Green"),
    (r"黄色|黄|yellow|옐로우", "옐로우", "Yellow"),
    (r"橙色|橘|orange|오렌지", "오렌지", "Orange"),
    (r"紫色|紫|purple|퍼플", "퍼플", "Purple"),
    (r"灰色|灰|gr[ae]y|그레이", "그레이", "Gray"),
    (r"金色|gold|골드", "골드", "Gold"),
    (r"银色|silver|실버", "실버", "Silver"),
    (r"camel|카멜", "카멜", "Camel"),
]

# AI Mode labeled answers: **제품명:** … / 컬러: …
# NOTE: never use bare "색" — it false-matches mid-sentence junk
_RE_AI_NAME = re.compile(
    r"(?:^|[\n•·\-\*📌]\s*)(?:\*\*|__)?제품명(?:\*\*|__)?\s*[:：]\s*\**\s*(.+)$",
    re.I | re.M,
)
_RE_AI_COLOR = re.compile(
    r"(?:^|[\n•·\-\*📌]\s*)(?:\*\*|__)?(?:컬러|색상)(?:\*\*|__)?\s*[:：]\s*\**\s*(.+)$",
    re.I | re.M,
)
_RE_AI_SENTENCE_JUNK = re.compile(
    r"(문의하신|질문하신|로\s*보입니다|입니다[.!]?$|제품\s*정보|사이즈\s*확인|"
    r"표준과|일치|시즌\s*제품|알려줘|제품명,\s*컬러)",
    re.I,
)

_JUNK = re.compile(
    r"(自营|可退|欧洲直邮|香港直邮|专柜价|正品|新款|上新|女士|女|男|"
    r"1h|直邮|淘宝|京东|小红书|大众点评|微博|百家号|店铺|"
    r"识图|百度|相关商品|全部|礼品箱包|相似图片|辅助|模式|文字提取|"
    r"图片来源|知道啦|解放双手|手机端|反馈|机器人|"
    r"\[[^\]]*\]|（[^）]*）|\([^)]*\)|"
    r"S\d{3,}[A-Z0-9]*|￥[\d.,]+|¥[\d.,]+|"
    r"98新|95新|99新|未使用|二手)",
    re.I,
)

_NOISE_LINE = re.compile(
    r"^(鞋靴|全部|相关商品|相似图片|礼品箱包|淘宝|京东|小红书|识图一下|文字提取|"
    r"辅\s*助|模\s*式|图片来源)$",
    re.I,
)


@dataclass
class NameParts:
    brand_ko: str = ""
    brand_en: str = ""
    size_ko: str = ""
    size_en: str = ""
    model_num: str = ""  # e.g. "26" for Lindy 26
    models: list[tuple[str, str]] = field(default_factory=list)
    type_ko: str = ""
    type_en: str = ""
    materials: list[tuple[str, str]] = field(default_factory=list)
    color_ko: str = ""
    color_en: str = ""

    def to_ko(self) -> str:
        type_ko = _strip_size_from_type(self.type_ko, self.size_ko)
        models = [
            (_strip_brand_from_model(ko, self.brand_ko), en)
            for ko, en in self.models
        ]
        parts: list[str] = []
        if self.brand_ko:
            parts.append(self.brand_ko)
        # Numeric model size (린디 26) beats generic 미니/스몰
        if self.model_num and models:
            for ko, _ in models:
                if ko and ko not in parts:
                    parts.append(f"{ko} {self.model_num}" if self.model_num not in ko else ko)
            # skip generic size_ko when model number present
        else:
            if self.size_ko:
                parts.append(self.size_ko)
            for ko, _ in models:
                if ko and ko not in parts:
                    parts.append(ko)
        if type_ko and type_ko not in " ".join(parts):
            if not any(type_ko in m or m in type_ko for m, _ in models if m):
                parts.append(type_ko)
        # 모델명만으로 끝나기 쉬워 '백'을 보강
        joined = " ".join(parts)
        bag_models = (
            "클래식 플랩", "레이디 디올", "보이 샤넬",
            "린디", "버킨", "켈리", "피코틴", "에블린", "콘스탄스",
            "가든 파티", "볼리드", "스피디", "네버풀",
        )
        if any(any(bm in (m or "") for bm in bag_models) for m, _ in models if m):
            if "백" not in joined and "지갑" not in joined:
                parts.append("백")
        for ko, _ in self.materials:
            if ko not in parts:
                parts.append(ko)
        # Specific Hermès color names: keep as-is (뉴 블루 진). Generic: "블루 컬러"
        if self.color_ko:
            specific = " " in self.color_ko or self.color_ko in (
                "뉴 블루 진", "블루 린", "블루 아가트", "블루 뉘", "라떼", "샌드",
            )
            if self.color_ko.endswith("컬러") or specific:
                parts.append(self.color_ko)
            else:
                parts.append(f"{self.color_ko} 컬러")
        return _join_unique_tokens(parts)

    def to_en(self) -> str:
        type_en = _strip_size_from_type(self.type_en, self.size_en)
        parts: list[str] = []
        if self.brand_en:
            parts.append(self.brand_en)
        if self.model_num and self.models:
            for _, en in self.models:
                if en and en not in parts:
                    parts.append(f"{en} {self.model_num}" if self.model_num not in en else en)
        else:
            if self.size_en:
                parts.append(self.size_en)
            for _, en in self.models:
                if en and en not in parts:
                    parts.append(en)
        if type_en:
            existing = " ".join(parts).lower()
            if type_en.lower() not in existing:
                if not (type_en == "Bag" and existing.endswith("bag")):
                    if not any(type_en.lower() in m.lower() for _, m in self.models if m):
                        parts.append(type_en)
        joined = " ".join(parts).lower()
        bag_ens = (
            "Classic Flap", "Lady Dior", "Boy Chanel",
            "Lindy", "Birkin", "Kelly", "Picotin", "Evelyne", "Constance",
            "Garden Party", "Bolide", "Speedy", "Neverfull",
        )
        if any(en in bag_ens for _, en in self.models if en):
            if "bag" not in joined and "wallet" not in joined:
                parts.append("Bag")
        for _, en in self.materials:
            if en not in parts:
                parts.append(en)
        if self.color_en:
            specific = " " in self.color_en or self.color_en.lower() in (
                "new bleu jean", "bleu lin", "bleu agate", "bleu nuit", "latte", "sand",
            )
            if self.color_en.lower().endswith("colored") or specific:
                parts.append(self.color_en)
            else:
                parts.append(f"{self.color_en} Colored")
        return _join_unique_tokens(parts, protect_tokens={self.brand_en} if self.brand_en else None)


def _strip_size_from_type(type_s: str, size_s: str) -> str:
    """미니 백 + size 미니 → 백 (사이즈 중복 제거)."""
    t = (type_s or "").strip()
    s = (size_s or "").strip()
    if not t or not s:
        return t
    if t.lower() == s.lower():
        return ""
    if t.lower().startswith(s.lower() + " "):
        return t[len(s) + 1 :].strip()
    return t


def _strip_brand_from_model(model: str, brand: str) -> str:
    """마이 디올 + brand 디올 → 마이 (브랜드 중복 제거)."""
    m = (model or "").strip()
    b = (brand or "").strip()
    if not m or not b:
        return m
    if m.lower() == b.lower():
        return ""
    if m.lower().endswith(" " + b.lower()):
        return m[: -(len(b) + 1)].strip()
    return m


def _join_unique_tokens(
    parts: list[str],
    protect_tokens: set[str] | None = None,
) -> str:
    """Exact phrase + token 중복 제거. protect_tokens는 모델명 안 브랜드 단어 허용(EN)."""
    protect = {t.lower() for t in (protect_tokens or set()) if t}
    out_parts: list[str] = []
    seen_phrases: set[str] = set()
    seen_tokens: set[str] = set()
    for part in parts:
        p = (part or "").strip()
        if not p or p.lower() in seen_phrases:
            continue
        tokens = p.split()
        # Multi-word model containing protected brand: keep whole phrase once
        if len(tokens) > 1 and any(t.lower() in protect for t in tokens):
            for tok in tokens:
                seen_tokens.add(tok.lower())
            seen_phrases.add(p.lower())
            out_parts.append(p)
            continue
        kept: list[str] = []
        for tok in tokens:
            key = tok.lower()
            if key in seen_tokens:
                continue
            seen_tokens.add(key)
            kept.append(tok)
        if not kept:
            continue
        cleaned = " ".join(kept)
        seen_phrases.add(cleaned.lower())
        out_parts.append(cleaned)
    return " ".join(out_parts)


@dataclass
class NamedProduct:
    name: str = ""          # Korean (primary)
    name_en: str = ""       # English
    candidates: list[str] = field(default_factory=list)
    category: str = ""
    brand_ko: str = ""
    color: str = ""


def _match_first(maps: list[tuple[str, str, str]], text: str) -> tuple[str, str]:
    for pat, ko, en in maps:
        if re.search(pat, text, re.I):
            return ko, en
    return "", ""


def _match_all(maps: list[tuple[str, str, str]], text: str) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    for pat, ko, en in maps:
        if re.search(pat, text, re.I) and ko not in seen:
            seen.add(ko)
            found.append((ko, en))
    return found


def _extract_parts(raw: str, hint: str = "") -> NameParts:
    """Parse name parts from *raw* search text.

    hint(원본 제목)은 브랜드 fallback 에만 쓰고, 사이즈·모델·컬러에는 섞지 않는다.
    (예: AI가 '린디 26'인데 hint의 '미니 백'이 덮어쓰는 문제 방지)
    """
    text = (raw or "").strip()
    brand_ko, brand_en = _match_first(_BRANDS, text)
    if not brand_ko:
        # brand only: allow hint as last resort
        brand_ko, brand_en = _match_first(_BRANDS, hint or "")
    if not brand_ko:
        attrs = extract_attrs(hint or text, "", "")
        brand_ko = attrs.brand_name
        en_map = {
            "dior": "Dior",
            "chanel": "Chanel",
            "gucci": "Gucci",
            "prada": "Prada",
            "celine": "Celine",
            "louisvuitton": "Louis Vuitton",
            "ysl": "Saint Laurent",
            "hermes": "Hermès",
            "balenciaga": "Balenciaga",
            "bottega": "Bottega Veneta",
            "fendi": "Fendi",
        }
        brand_en = en_map.get(attrs.brand_id, attrs.brand_id.title())

    size_ko, size_en = _match_first(_SIZES, text)
    models = _match_all(_MODELS, text)
    type_ko, type_en = _match_first(_TYPES, text)
    materials = _match_all(_MATERIALS, text)
    color_ko, color_en = _match_first(_COLORS, text)

    model_num = ""
    mnum = _RE_MODEL_NUM.search(text)
    if mnum:
        model_num = mnum.group(1)
        # numeric size replaces vague mini/small from the same line
        size_ko, size_en = "", ""

    # If Lady D Joy found, don't also keep generic Lady Dior unless alone
    if any(ko == "레이디 D 조이" for ko, _ in models):
        models = [(ko, en) for ko, en in models if ko != "레이디 디올"]

    if not type_ko and not models:
        attrs = extract_attrs(text or hint or "", "", "")
        cat_map = {
            "가방": ("백", "Bag"),
            "신발": ("신발", "Shoes"),
            "악세사리": ("액세서리", "Accessory"),
            "상의": ("상의", "Top"),
            "하의": ("하의", "Bottom"),
            "자켓": ("자켓", "Jacket"),
        }
        type_ko, type_en = cat_map.get(attrs.category, ("백", "Bag"))
    elif models and not type_ko:
        type_ko, type_en = "백", "Bag"

    return NameParts(
        brand_ko=brand_ko,
        brand_en=brand_en,
        size_ko=size_ko,
        size_en=size_en,
        model_num=model_num,
        models=models,
        type_ko=type_ko,
        type_en=type_en,
        materials=materials,
        color_ko=color_ko,
        color_en=color_en,
    )


def _clean_ai_name(raw: str) -> str:
    text = (raw or "").strip()
    if not text:
        return ""
    text = re.sub(r"[*\[\]`#_~]+", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    text = re.split(r"\s*[\(（]", text, maxsplit=1)[0].strip()
    text = re.split(r"\s*또는\s*", text, maxsplit=1)[0].strip()
    text = re.split(
        r"\s*[|/]\s*(?:Chanel|Hermes|KREAM|eBay)", text, maxsplit=1, flags=re.I
    )[0].strip()
    if _RE_AI_SENTENCE_JUNK.search(text):
        return ""
    if len(text) < 4 or len(text) > 80:
        return ""
    if text.endswith(("다", "요", "음", "죠")) and len(text) > 25:
        return ""
    return text


def normalize_ai_color(raw: str) -> str:
    """Keep AI color wording — multi-tone as '오렌지 / 블루' (one option, not two)."""
    text = (raw or "").strip()
    if not text:
        return ""
    text = re.sub(r"[*\[\]`#_~]+", "", text)
    text = re.split(r"\s*[\(（]", text, maxsplit=1)[0].strip()
    if _RE_AI_SENTENCE_JUNK.search(text) or re.search(
        r"문의|제품은|입니다|보입니다|알려", text
    ):
        return ""
    if len(text) > 40:
        return ""
    if not re.search(r"[&/,]|및|하고|투톤|투\s*톤", text):
        text = re.split(r"\s*또는\s*|\s*or\s*", text, maxsplit=1, flags=re.I)[0].strip()
    text = re.sub(r"\s+", " ", text).strip(" -–—|/,")
    text = re.sub(
        r"\s*(투톤\s*컬러|투\s*톤|컬러|색상)\s*$",
        "",
        text,
        flags=re.I,
    ).strip()
    if not text:
        return ""

    parts = re.split(r"\s*(?:&|/|,|및)\s*", text)
    parts = [p.strip() for p in parts if p.strip()]
    if len(parts) >= 2:
        mapped: list[str] = []
        for p in parts:
            p = re.sub(r"\s*투톤\s*$", "", p).strip()
            ko, _ = _match_first(_COLORS, p)
            mapped.append(ko or p)
        out: list[str] = []
        for m in mapped:
            if m and m not in out and not _RE_AI_SENTENCE_JUNK.search(m):
                out.append(m)
        return " / ".join(out) if out else ""

    best_ko, best_len = "", 0
    for pat, ko, _en in _COLORS:
        m = re.search(pat, text, re.I)
        if not m:
            continue
        span = m.end() - m.start()
        if span > best_len or (span == best_len and len(ko) > len(best_ko)):
            best_ko, best_len = ko, span
    if best_ko and (best_len >= len(text.replace(" ", "")) * 0.5 or best_ko in text):
        if " " in best_ko or best_ko == text or text.replace(" ", "") == best_ko.replace(" ", ""):
            return best_ko
        if text in (best_ko, f"{best_ko} 컬러") or re.fullmatch(
            rf"{re.escape(best_ko)}(\s*컬러)?", text, re.I
        ):
            return best_ko
    if re.search(r"[가-힣]", text) and len(text) <= 24:
        return text
    return best_ko or ""


def extract_ai_labeled_fields(lines: list[str]) -> tuple[str, str]:
    """Pull 제품명 / 컬러 from Google AI Mode labeled answers."""
    blob = "\n".join(lines or [])
    section = blob
    for marker in ("제품 정보", "제품정보", "Product info", "Product Info"):
        idx = blob.find(marker)
        if idx >= 0:
            section = blob[idx:]
            break

    name = ""
    color = ""
    for ln in section.splitlines():
        ln = ln.strip()
        if not ln:
            continue
        if not name:
            m = re.search(
                r"(?:\*\*|__)?제품명(?:\*\*|__)?\s*[:：]\s*\**\s*(.+)$",
                ln,
                re.I,
            )
            if m:
                cand = _clean_ai_name(m.group(1))
                if cand:
                    name = cand
        if not color:
            m = re.search(
                r"(?:\*\*|__)?(?:컬러|색상)(?:\*\*|__)?\s*[:：]\s*\**\s*(.+)$",
                ln,
                re.I,
            )
            if m:
                cand = normalize_ai_color(m.group(1))
                if cand:
                    color = cand
        if name and color:
            break

    if not name:
        m = _RE_AI_NAME.search(section) or _RE_AI_NAME.search(blob)
        if m:
            name = _clean_ai_name(m.group(1))
    if not color:
        m = _RE_AI_COLOR.search(section) or _RE_AI_COLOR.search(blob)
        if m:
            color = normalize_ai_color(m.group(1))
    return name, color


def _score_title(text: str, hint: str = "") -> int:
    if not text or _NOISE_LINE.match(text.strip()):
        return -100
    if _RE_PRICE.match(text.strip()):
        return -100
    score = 0
    if _match_first(_BRANDS, text + " " + hint)[0]:
        score += 8
    score += 14 * len(_match_all(_MODELS, text))
    score += 4 * len(_match_all(_MATERIALS, text))
    if _match_first(_TYPES, text)[0]:
        score += 4
    if _match_first(_COLORS, text)[0]:
        score += 3
    if _match_first(_SIZES, text)[0]:
        score += 2
    if _RE_GUESS.search(text) or text.startswith("图中可能是"):
        score += 10
    cleaned = _JUNK.sub(" ", text)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if 8 <= len(cleaned) <= 90:
        score += 2
    if re.fullmatch(r"(Dior|Chanel|Gucci|Prada|迪奥|香奈儿).{0,6}(包|鞋|手袋)?", text, re.I):
        score -= 6
    brand_ko, brand_en = _match_first(_BRANDS, hint)
    if brand_en and brand_en.upper() in (hint or "").upper():
        score += 3
    return score


def build_product_name(lines: list[str], hint: str = "") -> NamedProduct:
    # 1) Google AI Mode가 "제품명: …" 으로 준 답을 최우선 (원본 제목 hint로 덮지 않음)
    ai_name, ai_color = extract_ai_labeled_fields(lines)
    if ai_name and len(ai_name) >= 4:
        # AI가 준 제품명 문장을 우선 그대로 사용 (램스킨+신발로 재조립하지 않음)
        parts = _extract_parts(ai_name, hint="")
        color_out = normalize_ai_color(ai_color) if ai_color else ""
        name_ko = ai_name
        name_en = parts.to_en() if parts.models else ko_name_to_en(name_ko)
        # EN이 빈약하면 KO 직역
        if not name_en or len(name_en.split()) < 2:
            name_en = ko_name_to_en(name_ko)
        attrs = extract_attrs(name_ko, hint, "")
        return NamedProduct(
            name=name_ko,
            name_en=name_en,
            candidates=[ai_name],
            category=attrs.category,
            brand_ko=parts.brand_ko,
            color=color_out,
        )

    scored: list[tuple[int, str]] = []
    for ln in lines:
        s = ln.strip()
        if not s:
            continue
        m = _RE_GUESS.search(s)
        content = m.group(1).strip() if m else s
        if _NOISE_LINE.match(content) or len(content) < 4:
            continue
        if _RE_PRICE.match(content):
            continue
        sc = _score_title(content if not m else s, hint)
        if m:
            sc += 6
        # Boost lines that look like real model names
        if _match_all(_MODELS, content):
            sc += 20
        if _RE_MODEL_NUM.search(content):
            sc += 12
        scored.append((sc, content))

    scored.sort(key=lambda x: (-x[0], -len(x[1])))
    candidates_raw = [t for sc, t in scored if sc > 0][:15]

    best_parts: NameParts | None = None
    best_score = -10**9
    candidate_names: list[str] = []

    for raw in candidates_raw:
        parts = _extract_parts(raw, hint)
        ko = parts.to_ko()
        if not ko:
            continue
        if ko not in candidate_names:
            candidate_names.append(ko)

        sc = 0
        sc += 10 * len(parts.models)
        sc += 8 if parts.model_num else 0
        sc += 4 * len(parts.materials)
        if parts.size_ko:
            sc += 3
        if parts.color_ko:
            sc += 4
        if parts.type_ko and parts.type_ko not in ("백", "가방", "신발"):
            sc += 2
        tokens = ko.split()
        sc += min(len(tokens), 8)
        if len(tokens) < 3:
            sc -= 5
        if ko.endswith(" 가방") or ko.endswith(" 신발"):
            sc -= 4
        # Penalize names that only echo the vague original title (미니 백) when better models exist
        if parts.models:
            sc += 15
        if sc > best_score:
            best_score = sc
            best_parts = parts

    if not best_parts:
        parts = _extract_parts(hint or "bag", hint="")
        best_parts = parts

    name_ko = best_parts.to_ko()
    name_en = best_parts.to_en()
    attrs = extract_attrs(name_ko, hint, "")

    return NamedProduct(
        name=name_ko,
        name_en=name_en,
        candidates=candidate_names[:10] or candidates_raw[:8],
        category=attrs.category,
        brand_ko=best_parts.brand_ko,
        color=best_parts.color_ko or ai_color,
    )


def format_bilingual(name_ko: str, name_en: str) -> str:
    """Display block: Korean line + English line."""
    ko = (name_ko or "").strip()
    en = (name_en or "").strip()
    if ko and en:
        return f"{ko}\n{en}"
    return ko or en


def _ko_en_lexicon() -> list[tuple[str, str]]:
    """Korean phrase → English, longest-first for greedy translate."""
    pairs: list[tuple[str, str]] = []
    for maps in (_BRANDS, _MODELS, _MATERIALS, _SIZES, _TYPES, _COLORS):
        for _pat, ko, en in maps:
            if ko and en:
                pairs.append((ko, en))
    # Extra display tokens used in our KO names
    pairs.extend(
        [
            ("컬러", "Colored"),
            ("금장", "Gold Hardware"),
            ("은장", "Silver Hardware"),
            ("마이", "My"),  # after brand-stripped "마이 디올"
            ("레이디", "Lady"),
            ("코코", "Coco"),
            ("비치", "Beach"),
            ("플랩", "Flap"),
            ("메쉬", "Mesh"),
            ("마크라메", "Macrame"),
            ("백", "Bag"),
        ]
    )
    # Dedupe by ko, keep first (longest already preferred by sort)
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for ko, en in sorted(pairs, key=lambda x: -len(x[0])):
        if ko in seen:
            continue
        seen.add(ko)
        out.append((ko, en))
    return out


_KO_EN_LEX = _ko_en_lexicon()


_HANGUL_CHO = [
    "g", "kk", "n", "d", "tt", "r", "m", "b", "pp", "s", "ss", "",
    "j", "jj", "ch", "k", "t", "p", "h",
]
_HANGUL_JUNG = [
    "a", "ae", "ya", "yae", "eo", "e", "yeo", "ye", "o", "wa", "wae",
    "oe", "yo", "u", "wo", "we", "wi", "yu", "eu", "ui", "i",
]
_HANGUL_JONG = [
    "", "k", "k", "k", "n", "n", "n", "t", "l", "l", "l", "l", "l",
    "l", "l", "l", "m", "p", "p", "t", "t", "ng", "t", "t", "k", "t",
    "p", "t",
]


def _romanize_hangul_token(token: str) -> str:
    """Simple Revised Romanization for leftover Korean words (Title Case)."""
    out: list[str] = []
    for ch in token:
        code = ord(ch)
        if 0xAC00 <= code <= 0xD7A3:
            syl = code - 0xAC00
            cho = syl // 588
            jung = (syl % 588) // 28
            jong = syl % 28
            out.append(_HANGUL_CHO[cho] + _HANGUL_JUNG[jung] + _HANGUL_JONG[jong])
        elif ch.isascii() and ch.isalnum():
            out.append(ch)
    roman = "".join(out).strip()
    if not roman:
        return token
    return roman[:1].upper() + roman[1:].lower()


def ko_name_to_en(name_ko: str) -> str:
    """Translate Korean product name → English, keeping every token in order."""
    text = (name_ko or "").strip()
    if not text:
        return ""

    remaining = text
    tokens_en: list[str] = []
    while remaining:
        remaining = remaining.lstrip()
        if not remaining:
            break
        matched = False
        for ko, en in _KO_EN_LEX:
            if not remaining.startswith(ko):
                continue
            end = len(ko)
            # Space-separated KO names: match whole phrase/token only
            if end < len(remaining) and remaining[end] not in " \t/":
                continue
            tokens_en.append(en)
            remaining = remaining[end:]
            matched = True
            break
        if matched:
            continue
        # Keep unknown token — romanize hangul, keep latin as-is
        m = re.match(r"\S+", remaining)
        if not m:
            break
        raw = m.group(0)
        remaining = remaining[m.end() :]
        if re.search(r"[가-힣]", raw):
            tokens_en.append(_romanize_hangul_token(raw))
        else:
            tokens_en.append(raw)

    # Soft dedupe while keeping Korean order; allow brand word inside model (My Dior)
    out: list[str] = []
    seen_lower: set[str] = set()
    for t in tokens_en:
        if t == "My" and out and out[0] == "Dior":
            t = "My Dior"
        key = t.lower()
        if " " in t:
            if key in seen_lower:
                continue
            seen_lower.add(key)
            for w in t.split():
                seen_lower.add(w.lower())
            out.append(t)
            continue
        if key in seen_lower:
            continue
        seen_lower.add(key)
        out.append(t)
    return " ".join(out)
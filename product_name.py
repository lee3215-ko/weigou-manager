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
    (r"路易威登|LOUIS\s*VUITTON|\bLV\b|루이비통", "루이비통", "Louis Vuitton"),
    (r"圣罗兰|SAINT\s*LAURENT|\bYSL\b|생로랑", "생로랑", "Saint Laurent"),
    (r"巴黎世家|BALENCIAGA|발렌시아가", "발렌시아가", "Balenciaga"),
    (r"葆蝶家|BOTTEGA|보테가", "보테가", "Bottega Veneta"),
    (r"戈雅|GOYARD|고야드", "고야드", "Goyard"),
    (r"德尔沃|德尔福|DELVAUX|델보", "델보", "Delvaux"),
    (r"阿莱娅|阿拉亚|ALA[IÏ]A|알라이아", "알라이아", "Alaïa"),
    (r"华伦天奴|瓦伦蒂诺|VALENTINO|발렌티노", "발렌티노", "Valentino"),
    (r"\bMM6\b|엠엠식스", "엠엠식스", "MM6"),
    (r"蔻依|克洛伊|CHLO[EÉ]|CHIOE|클로에", "클로에", "Chloé"),
    (r"小鹅|金鹅|GOLDEN\s*GOOSE|\bGGDB\b|골든구스", "골든구스", "Golden Goose"),
    (r"缪缪|MIU\s*MIU|미우미우", "미우미우", "Miu Miu"),
    (r"巴宝莉|博柏利|BURBERRY|버버리", "버버리", "Burberry"),
    (r"THE\s*ROW|더\s*로우|더로우", "더로우", "The Row"),
    (r"香奈儿|CHANEL|샤넬", "샤넬", "Chanel"),
    (r"普拉达|PRADA|프라다", "프라다", "Prada"),
    (r"爱马仕|HERM[EÈ]S|에르메스", "에르메스", "Hermès"),
    (r"赛琳|思琳|CELINE|셀린느", "셀린느", "Celine"),
    (r"迪奥|DIOR|디올", "디올", "Dior"),
    (r"古驰|GUCCI|구찌", "구찌", "Gucci"),
    (r"芬迪|FENDI|펜디", "펜디", "Fendi"),
    (r"THOM\s*BROWNE|톰브라운", "톰브라운", "Thom Browne"),
    (r"CHROME\s*HEARTS|크롬하츠", "크롬하츠", "Chrome Hearts"),
]

_MODELS = [
    (r"Lady\s*D[-\s]?Joy|LadyDJoy|D-?Joy|Djoy|迪奥?D.?joy|레이디\s*D\s*조이", "레이디 D 조이", "Lady D Joy"),
    (r"My\s*Dior|我的迪奥|DiorMyDior|마이\s*디올", "마이 디올", "My Dior"),
    (r"Coco\s*Beach|코코\s*비치|可可海滩", "코코 비치", "Coco Beach"),
    (r"Fantasy\s*Pearl|판타지\s*펄|판타지펄", "판타지 펄", "Fantasy Pearl"),
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
    (r"판타지\s*펄\s*캣아이\s*선글라스|Fantasy\s*Pearl\s*Cat[\s\-]?Eye", "판타지 펄 캣아이 선글라스", "Fantasy Pearl Cat-Eye Sunglasses"),
    (r"로고\s*런웨이\s*무테\s*선글라스|Logo\s*Rimless\s*Sunglass", "로고 런웨이 무테 선글라스", "Logo Runway Rimless Sunglasses"),
    (r"무테\s*선글라스|rimless\s*sunglass", "무테 선글라스", "Rimless Sunglasses"),
    (r"캣아이\s*선글라스|cat[\s\-]?eye\s*sunglass", "캣아이 선글라스", "Cat-Eye Sunglasses"),
    (r"선글라스|선글래스|sunglasses?|墨镜|太阳镜|太陽鏡", "선글라스", "Sunglasses"),
    (r"眼镜|glasses|아이웨어|eyewear", "안경", "Glasses"),
    (r"벨트|belts?|腰带|皮带|皮帶|ベルト", "벨트", "Belt"),
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
    (r"카키\s*그린|khaki\s*green|kaki\s*green", "카키 그린", "Khaki Green"),
    (r"卡其|khaki|kaki|카키|军绿|橄榄绿|olive|올리브", "카키", "Khaki"),
    (r"绿色|绿|green|그린", "그린", "Green"),
    (r"黄色|黄|yellow|옐로우", "옐로우", "Yellow"),
    (r"橙色|橘|orange|오렌지", "오렌지", "Orange"),
    (r"紫色|紫|purple|퍼플", "퍼플", "Purple"),
    (r"灰色|灰|gr[ae]y|그레이", "그레이", "Gray"),
    (r"金色|gold|골드", "골드", "Gold"),
    (r"银色|silver|실버", "실버", "Silver"),
    (r"camel|카멜", "카멜", "Camel"),
]

# AI Mode labeled answers: **제품명:** … / **컬러:** … / **컬러/소재:** …
# NOTE: never use bare "색" — it false-matches mid-sentence junk
# Google often answers "컬러/소재:" — allow optional /소재·재료 suffix
_RE_AI_COLOR_LABEL = r"(?:컬러|색상)(?:\s*/\s*[^\s:*：*]+)?"
_RE_AI_NAME = re.compile(
    r"(?:^|[\n•·\-\*📌]\s*)(?:\*\*|__)?제품명(?:\*\*|__)?\s*[:：]\s*\**\s*(.+)$",
    re.I | re.M,
)
_RE_AI_COLOR = re.compile(
    rf"(?:^|[\n•·\-\*📌]\s*)(?:\*\*|__)?{_RE_AI_COLOR_LABEL}(?:\*\*|__)?\s*[:：]\s*\**\s*(.+)$",
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
            "goyard": "Goyard",
            "delvaux": "Delvaux",
            "alaia": "Alaïa",
            "valentino": "Valentino",
            "mm6": "MM6",
            "chloe": "Chloé",
            "goldengoose": "Golden Goose",
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
            "선글라스": ("선글라스", "Sunglasses"),
            "벨트": ("벨트", "Belt"),
            "여성옷": ("여성옷", "Womenswear"),
            "남성옷": ("남성옷", "Menswear"),
            "기타": ("아이템", "Item"),
        }
        type_ko, type_en = cat_map.get(attrs.category, ("백", "Bag"))
    elif models and not type_ko:
        # 모델만 있고 타입이 없으면 가방으로 단정하지 않음 (선글라스·의류 등)
        low = f"{text} {hint}".lower()
        if re.search(r"선글라스|선글래스|sunglass|墨镜|太阳镜", low):
            type_ko, type_en = "선글라스", "Sunglasses"
        elif re.search(r"벨트|belt|腰带|皮带", low):
            type_ko, type_en = "벨트", "Belt"
        elif re.search(r"신발|shoe|sneaker|靴|鞋", low):
            type_ko, type_en = "신발", "Shoes"
        else:
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


def _normalize_color_token(token: str) -> str:
    """One color token from AI — keep compound names like '카키 그린'."""
    p = re.sub(r"\s*투톤\s*$", "", (token or "").strip()).strip()
    if not p:
        return ""
    # 복합 표기(카키 그린 등)는 팔레트로 축소하지 않고 유지
    if re.search(r"[가-힣]", p) and (" " in p or len(p) >= 2):
        ko, _ = _match_first(_COLORS, p)
        # 전체(또는 거의 전체)가 팔레트 항목과 일치할 때만 정규화
        if ko and (
            ko == p
            or p.replace(" ", "") == ko.replace(" ", "")
            or p in (f"{ko} 컬러", f"{ko}색")
        ):
            return ko
        return p
    ko, _ = _match_first(_COLORS, p)
    return ko or p


def normalize_ai_color(raw: str) -> str:
    """Keep AI color wording — multi-tone as '카키 그린/카키' (slash, not comma)."""
    text = (raw or "").strip()
    if not text:
        return ""
    text = re.sub(r"[*\[\]`#_~]+", "", text)
    # 괄호 안 부연(소재 설명) 제거
    text = re.split(r"\s*[\(（]", text, maxsplit=1)[0].strip()
    if _RE_AI_SENTENCE_JUNK.search(text) or re.search(
        r"문의|제품은|입니다|보입니다|알려", text
    ):
        return ""
    if len(text) > 48:
        return ""
    if not re.search(r"[&/,]|및|하고|투톤|투\s*톤", text):
        text = re.split(r"\s*또는\s*|\s*or\s*", text, maxsplit=1, flags=re.I)[0].strip()
    text = re.sub(r"\s+", " ", text).strip(" -–—|/,")
    text = re.sub(
        r"\s*(투톤\s*컬러|투\s*톤|컬러|색상|소재)\s*$",
        "",
        text,
        flags=re.I,
    ).strip()
    if not text:
        return ""

    # 쉼표도 슬래시 계열로 취급 (AI가 ',' 로 쓸 때)
    parts = re.split(r"\s*(?:&|/|,|및)\s*", text)
    parts = [p.strip() for p in parts if p.strip()]
    if len(parts) >= 2:
        out: list[str] = []
        for p in parts:
            m = _normalize_color_token(p)
            if m and m not in out and not _RE_AI_SENTENCE_JUNK.search(m):
                out.append(m)
        return "/".join(out) if out else ""

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
        # "프레임은 시크한 블랙" 같이 설명형이면 팔레트 색만 사용
        if re.search(r"프레임|렌즈|그라데이션|시크한|진한|밝은|어두운", text):
            return best_ko
    if re.search(r"[가-힣]", text) and len(text) <= 28:
        return text
    return best_ko or ""


_AI_BRANDS_KO = (
    "샤넬|디올|구찌|프라다|에르메스|루이비통|생로랑|셀린느|펜디|미우미우|"
    "고야드|델보|알라이아|발렌티노|엠엠식스|클로에|골든구스|"
    "발렌시아가|보테가|톰브라운|크롬하츠"
)
_AI_TYPE_TAIL = (
    r"(?:선글라스|선글래스|sunglasses?|"
    r"백|가방|지갑|카드\s*홀더|스니커즈|샌들|힐|부츠|로퍼|"
    r"코트|자켓|원피스|스카프|시계|목걸이|귀걸이|팔찌|벨트|모자|머플러|"
    r"티셔츠|니트|후드|팬츠|스커트)"
)
_RE_AI_NARRATIVE_OF = re.compile(
    rf"(?P<brand>{_AI_BRANDS_KO})"
    rf"(?:\s*[\(（][^\)）]*[\)）])?"
    rf"\s*의\s*"
    rf"(?P<body>[^.\n]{{2,55}}?{_AI_TYPE_TAIL})",
    re.I,
)
# 사진 속 제품은 미우미우 로고 런웨이 무테 선글라스(…)이며
_RE_AI_PRODUCT_IS = re.compile(
    rf"(?:사진\s*속\s*)?(?:이\s*)?제품은\s*"
    rf"(?P<name>(?:{_AI_BRANDS_KO})[^.\n]{{0,60}}?{_AI_TYPE_TAIL})"
    rf"(?:\s*[\(（][^\)）]*[\)）])?"
    rf"(?:\s*이며|\s*이고|\s*로\b|\s*입니다|\s*이에요|\s*예요|,)",
    re.I,
)
_RE_AI_BOLD_PRODUCT = re.compile(
    rf"\*\*\s*((?:{_AI_BRANDS_KO})[^*.\n]{{2,55}}?{_AI_TYPE_TAIL})\s*"
    rf"(?:\([^)]*\))?\s*\*\*",
    re.I,
)
_RE_AI_INLINE_PRODUCT = re.compile(
    rf"(?<![가-힣A-Za-z])"
    rf"((?:{_AI_BRANDS_KO})(?:\s+[가-힣A-Za-z0-9\-]+){{0,8}}\s+{_AI_TYPE_TAIL})"
    rf"(?=\s*[\(（]|\s*이며|\s*이고|\s*로\b|\s*입니다|,|\.|$)",
    re.I,
)
_RE_AI_MODEL_CODE = re.compile(
    r"(?:모델명|모델\s*번호|model(?:\s*name|\s*no\.?)?)\s*[:：은는]?\s*"
    r"\*?\*?([A-Z]{1,6}\d{2,6}[A-Z0-9\-]*|\d{3,5}[A-Z0-9\-]*)\*?\*?",
    re.I,
)


def _finalize_ai_product_name(name: str, blob: str = "") -> str:
    name = re.sub(r"[*\[\]`#_~]+", "", (name or "").strip())
    name = re.split(r"\s*[\(（]", name, maxsplit=1)[0].strip()
    name = re.sub(r"\s+", " ", name).strip(" ,，·-–—")
    if not name or len(name) < 4:
        return ""
    if _RE_AI_SENTENCE_JUNK.search(name) or re.search(
        r"문의|알려|입니다|보입니다|확인됩니다", name
    ):
        return ""
    mm = _RE_AI_MODEL_CODE.search(blob or "")
    if mm:
        code = mm.group(1).strip()
        code = re.split(r"\s*또는\s*|\s*or\s*|[|/]", code, maxsplit=1, flags=re.I)[
            0
        ].strip()
        if code and code.upper() not in name.upper() and len(code) <= 16:
            name = f"{name} {code}"
    cleaned = _clean_ai_name(name)
    if cleaned and len(cleaned) >= 6:
        return cleaned
    if 6 <= len(name) <= 90:
        return name
    return cleaned or ""


def _extract_narrative_ai_name(blob: str) -> str:
    """Parse Google AI Mode narrative answers into a product name."""
    text = blob or ""
    m = _RE_AI_PRODUCT_IS.search(text)
    if m:
        got = _finalize_ai_product_name(m.group("name"), text)
        if got:
            return got
    m = _RE_AI_BOLD_PRODUCT.search(text)
    if m:
        got = _finalize_ai_product_name(m.group(1), text)
        if got:
            return got
    m = _RE_AI_NARRATIVE_OF.search(text)
    if m:
        brand = (m.group("brand") or "").strip()
        body = (m.group("body") or "").strip()
        got = _finalize_ai_product_name(f"{brand} {body}", text)
        if got:
            return got
    m = _RE_AI_INLINE_PRODUCT.search(text)
    if m:
        got = _finalize_ai_product_name(m.group(1), text)
        if got:
            return got
    return ""


def extract_ai_labeled_fields(lines: list[str]) -> tuple[str, str]:
    """Pull 제품명 / 컬러 from Google AI Mode labeled (or narrative) answers."""
    blob = "\n".join(lines or [])
    section = blob
    for marker in (
        "제품 상세 정보",
        "제품상세정보",
        "제품 정보",
        "제품정보",
        "정확한 정보",
        "Product info",
        "Product Info",
    ):
        idx = blob.find(marker)
        if idx >= 0:
            section = blob[idx:]
            break

    name = ""
    color = ""
    color_re = re.compile(
        rf"(?:\*\*|__)?{_RE_AI_COLOR_LABEL}(?:\*\*|__)?\s*[:：]\s*\**\s*(.+)$",
        re.I,
    )
    for ln in section.splitlines():
        ln = ln.strip().lstrip("•·-*📌 ").strip()
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
            m = color_re.search(ln)
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
    # AI가 「제품명:」 없이 설명형으로 답할 때 — 검색 결과 문장 그대로 사용
    if not name:
        name = _extract_narrative_ai_name(blob) or _extract_narrative_ai_name(section)
    if not color:
        m = _RE_AI_COLOR.search(section) or _RE_AI_COLOR.search(blob)
        if m:
            color = normalize_ai_color(m.group(1))
    if not color:
        m = re.search(
            r"그레이\s*그라디언트|그레이\s*그라데이션|Grey\s*Gradient|"
            r"블랙\s*그라디언트|Black\s*Gradient",
            blob,
            re.I,
        )
        if m:
            color = normalize_ai_color(m.group(0))
    return name, color


# 옷 이미지 검색: 상세 제품명 대신 종류만 (나시/니트/반팔티 …)
_CLOTHING_TYPE_ALIASES: list[tuple[str, str]] = [
    (r"민소매|나시|탱크\s*탑|tank\s*top|슬리브리스|sleeveless", "나시"),
    (r"크롭\s*니트|니트|스웨터|sweater|pullover|울\s*니트", "니트"),
    (r"반팔\s*티|반팔티|숏\s*슬리브|short\s*sleeve\s*t|t-?shirt.*short", "반팔티"),
    (r"긴팔\s*티|긴팔티|롱\s*슬리브\s*티|long\s*sleeve\s*t", "긴팔티"),
    (r"가디건|cardigan", "가디건"),
    (r"후드\s*티|후디|hoodie|후드집업", "후드"),
    (r"맨투맨|스웨트셔츠|sweatshirt", "맨투맨"),
    (r"블라우스|blouse", "블라우스"),
    (r"셔츠|shirt(?!\s*dress)", "셔츠"),
    (r"원피스|드레스|dress", "원피스"),
    (r"스커트|skirt", "스커트"),
    (r"팬츠|슬랙스|바지|pants|trousers", "팬츠"),
    (r"청바지|데님\s*팬츠|jeans", "데님팬츠"),
    (r"자켓|재킷|jacket|블레이저|blazer", "자켓"),
    (r"코트|coat", "코트"),
    (r"패딩|다운|puffer", "패딩"),
    (r"조끼|베스트|vest", "조끼"),
    (r"폴로|polo", "폴로"),
    (r"터틀넥|목폴라|turtleneck", "터틀넥"),
]
_CLOTHING_CANON = (
    "나시",
    "니트",
    "반팔티",
    "긴팔티",
    "가디건",
    "후드",
    "맨투맨",
    "블라우스",
    "셔츠",
    "원피스",
    "스커트",
    "팬츠",
    "데님팬츠",
    "자켓",
    "코트",
    "패딩",
    "조끼",
    "폴로",
    "터틀넥",
)
_RE_AI_CLOTHING_TYPE = re.compile(
    r"(?:^|[\n•·\-\*📌]\s*)(?:\*\*|__)?"
    r"(?:옷\s*종류|의류\s*종류|종류|제품\s*종류|아이템)"
    r"(?:\*\*|__)?\s*[:：]\s*\**\s*(.+)$",
    re.I | re.M,
)


def is_clothing_category(category: str | None) -> bool:
    c = (category or "").strip()
    return c in {"여성옷", "남성옷", "상의", "하의", "자켓"}


def normalize_clothing_type(raw: str) -> str:
    """Map free-form AI clothing type to a short Korean label."""
    text = (raw or "").strip()
    text = re.sub(r"[*_`#]+", "", text)
    text = re.split(r"[/|·,，、(\[]", text, maxsplit=1)[0].strip()
    text = re.sub(r"\s+", "", text)
    if not text:
        return ""
    for canon in _CLOTHING_CANON:
        if text == canon or text.endswith(canon):
            return canon
    blob = (raw or "").strip()
    for pat, canon in _CLOTHING_TYPE_ALIASES:
        if re.search(pat, blob, re.I):
            return canon
    # already short Korean word
    if re.fullmatch(r"[가-힣]{2,8}", text):
        return text
    return ""


def extract_ai_clothing_fields(lines: list[str]) -> tuple[str, str]:
    """옷 검색 답변에서 (옷종류, 컬러) 추출."""
    blob = "\n".join(lines or [])
    clothing = ""
    m = _RE_AI_CLOTHING_TYPE.search(blob)
    if m:
        clothing = normalize_clothing_type(m.group(1))
    name, color = extract_ai_labeled_fields(lines)
    if not clothing and name:
        clothing = normalize_clothing_type(name)
        # 「샤넬 니트」처럼 붙어 있으면 종류만 남김
        if not clothing:
            for pat, canon in _CLOTHING_TYPE_ALIASES:
                if re.search(pat, name, re.I):
                    clothing = canon
                    break
    if not clothing:
        for pat, canon in _CLOTHING_TYPE_ALIASES:
            if re.search(pat, blob, re.I):
                clothing = canon
                break
    return clothing, color


def format_clothing_product_name(brand: str, clothing_type: str) -> str:
    """제품명 입력: 「샤넬 니트」."""
    b = (brand or "").strip()
    t = normalize_clothing_type(clothing_type) or (clothing_type or "").strip()
    if b and t:
        if t.startswith(b):
            return t
        return f"{b} {t}".strip()
    return t or b


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
        # 절대 "bag" 기본값으로 가방명을 만들지 않음 — 힌트/본문에서만 복구
        fallback_src = " ".join(
            x for x in (hint, " ".join(lines[:8] if lines else [])) if x
        ).strip()
        if re.search(r"선글라스|선글래스|sunglass|墨镜|太阳镜", fallback_src, re.I):
            fallback_src = (hint or "") + " 선글라스"
        best_parts = _extract_parts(fallback_src or hint or "", hint="")
        # 그래도 타입이 가방으로만 잡히면 선글라스 힌트가 있을 때 교정
        if best_parts.type_ko in ("백", "가방") and re.search(
            r"선글라스|선글래스|sunglass|墨镜", fallback_src, re.I
        ):
            best_parts.type_ko, best_parts.type_en = "선글라스", "Sunglasses"

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
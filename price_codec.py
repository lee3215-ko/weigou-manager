# -*- coding: utf-8 -*-
"""Decode supplier NO codes into cost / sell price.

Rule (examples):
  NO 8888010
  → digits after 88880 → 10
  → cost = 10 * 10,000원 = 100,000원
  → sell = cost + 50% margin = 150,000원

  NO 8888033.5
  → 33.5만원 = 335,000원 (소수점 = 천원 단위)
  → sell = 502,500원

  NO 00008
  → digits after the last 0 → 8
  → cost = 8만원 = 80,000원
"""
from __future__ import annotations

import re
from dataclasses import dataclass


MARGIN_RATE = 0.5  # 50%
MANWON = 10_000


@dataclass
class PriceInfo:
    code: str
    cost_manwon: float
    cost: int
    sell: int
    margin: int

    @property
    def label(self) -> str:
        mw = self.cost_manwon
        mw_txt = str(int(mw)) if float(mw).is_integer() else f"{mw:g}"
        return (
            f"원가 {mw_txt}만원 ({self.cost:,}원) "
            f"+ 마진 {self.margin:,}원 = 판매가 {self.sell:,}원"
        )


def normalize_sell_price(amount: int) -> int:
    """판매가 정리.

    1) 1만원 미만 버림 — 515,000 → 510,000 / 502,500 → 500,000
    2) 딱 N00,000원이면 1만원 내려 N90,000 — 500,000 → 490,000
       (510,000은 그대로)
    """
    try:
        n = int(amount)
    except (TypeError, ValueError):
        return 0
    if n <= 0:
        return 0
    # drop below 10,000
    p = (n // MANWON) * MANWON
    # 100,000 / 200,000 / 500,000 … → 90,000 / 190,000 / 490,000
    if p >= 100_000 and p % 100_000 == 0:
        p -= MANWON
    return p


def normalize_sku(raw: str) -> str:
    """Keep digits and a single decimal point (8888033.5)."""
    s = (raw or "").strip().upper()
    s = re.sub(r"^NO\s*[：:]\s*", "", s)
    s = s.replace(",", "").replace(" ", "")
    # Prefer explicit 88880… fragment if buried in a longer title
    m = re.search(r"88880\d+(?:\.\d+)?", s)
    if m:
        return m.group(0)
    # Keep one decimal: strip other non-digit except first '.'
    out: list[str] = []
    seen_dot = False
    for ch in s:
        if ch.isdigit():
            out.append(ch)
        elif ch == "." and not seen_dot:
            out.append(ch)
            seen_dot = True
    return "".join(out)


# Empty 가격코드 → homepage shows this Korean label instead of failing
DEFAULT_PRICE_TEXT = "반수제품 가격문의"


def effective_price_code(raw: str | None) -> str:
    """Return price code, or DEFAULT_PRICE_TEXT when blank."""
    s = (raw or "").strip()
    return s if s else DEFAULT_PRICE_TEXT


def is_text_price_label(raw: str) -> bool:
    """True when price code is a Korean display label (e.g. 반수제품 가격문의)."""
    s = (raw or "").strip()
    if not s:
        return False
    if decode_price_code(s) is not None:
        return False
    return bool(re.search(r"[가-힣]", s))


def decode_price_code(raw: str, margin_rate: float = MARGIN_RATE) -> PriceInfo | None:
    """
    Decode patterns like:
      8888010    → 10만원
      8888033    → 33만원
      8888033.5  → 33.5만원 (335,000원)
      88880100   → 100만원
      00008      → 8만원  (leading zeros; digits after last 0)
      000033.5   → 33.5만원
    Prefers `88880` + manwon. Then `0…0` + manwon. Else last `0` + trailing digits.
    """
    code = normalize_sku(raw)
    if not code:
        return None

    manwon: float | None = None

    m = re.match(r"^88880(\d+(?:\.\d+)?)$", code)
    if m:
        try:
            manwon = float(m.group(1))
        except ValueError:
            manwon = None
    else:
        # 00008 → 8 / 000010 → 10  (zeros then amount after last 0)
        m_pad = re.match(r"^0+(\d+(?:\.\d+)?)$", code)
        if m_pad:
            try:
                manwon = float(m_pad.group(1))
            except ValueError:
                manwon = None
        else:
            m2 = re.search(r"0(\d+(?:\.\d+)?)$", code)
            if m2 and m2.start(0) > 0:
                try:
                    manwon = float(m2.group(1))
                except ValueError:
                    manwon = None

    if manwon is None or manwon <= 0:
        return None

    cost = int(round(manwon * MANWON))
    raw_sell = cost + int(round(cost * margin_rate))
    sell = normalize_sell_price(raw_sell)
    margin = sell - cost
    return PriceInfo(
        code=code,
        cost_manwon=manwon,
        cost=cost,
        sell=sell,
        margin=margin,
    )


if __name__ == "__main__":
    for sample in (
        "8888010",
        "8888033",
        "8888033.5",
        "88880100",
        "00008",
        "000010",
        "000033.5",
        "NO：8888033.5",
        "CHANEL size 14*21*6 NO：8888033.5",
    ):
        info = decode_price_code(sample)
        print(sample, "→", info.label if info else None)
    print("--- normalize ---")
    for n in (515_000, 509_000, 510_000, 502_500, 495_000, 150_000, 1_500_000):
        print(f"{n:,} → {normalize_sell_price(n):,}")

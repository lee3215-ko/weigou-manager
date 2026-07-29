# -*- coding: utf-8 -*-
"""Order management tab helpers for ManagerApp."""
from __future__ import annotations

import re
from typing import Any

ORDER_STATUS_KO = {
    "pending": "주문접수",
    "paid": "결제완료",
    "shipping": "배송중",
    "delivered": "배송완료",
    "cancelled": "취소",
}

ORDER_STATUS_VALUES = list(ORDER_STATUS_KO.keys())


def status_label(status: str) -> str:
    return ORDER_STATUS_KO.get(status or "", status or "-")


def product_no(order: dict[str, Any]) -> str:
    return str(
        order.get("productNo")
        or order.get("searchCode")
        or order.get("skuNo")
        or ""
    ).strip()


def product_id_digits(order: dict[str, Any]) -> str:
    """상품 ID에서 숫자만 (예: wg-123 → 123)."""
    pid = str(order.get("productId") or "").strip()
    if not pid:
        return ""
    m = re.search(r"(\d+)", pid)
    return m.group(1) if m else ""


def format_order_line(order: dict[str, Any]) -> str:
    st = status_label(str(order.get("status") or ""))
    created = str(order.get("createdAt") or "")[:16].replace("T", " ")
    name = str(order.get("customerName") or "?")
    product = str(order.get("productName") or "?")
    if len(product) > 22:
        product = product[:22] + "…"
    no = product_no(order)
    no_part = f" NO:{no}" if no else ""
    qty = order.get("quantity") or 1
    try:
        total = int(order.get("price") or 0) * int(qty)
    except (TypeError, ValueError):
        total = 0
    return f"[{st}] {created} · {name} · {product}{no_part} ×{qty} · {total:,}원"


def order_detail_text(order: dict[str, Any]) -> str:
    qty = order.get("quantity") or 1
    try:
        price = int(order.get("price") or 0)
        total = price * int(qty)
    except (TypeError, ValueError):
        price, total = 0, 0
    pay = {
        "bank": "무통장입금",
        "card": "카드",
        "transfer": "계좌이체",
    }.get(str(order.get("paymentMethod") or ""), "-")
    no = product_no(order)
    pid = product_id_digits(order) or "-"
    opts = " / ".join(
        str(x) for x in (order.get("color"), order.get("size")) if x
    ) or "-"
    lines = [
        f"주문번호: {order.get('id') or '-'}",
        f"상태: {status_label(str(order.get('status') or ''))}",
        f"일시: {str(order.get('createdAt') or '').replace('T', ' ')[:19]}",
        f"구분: {'회원' if order.get('orderType') == 'member' else '비회원'}",
        "",
        f"주문자: {order.get('customerName') or '-'}",
        f"연락처: {order.get('phone') or '-'}",
        f"통관부호: {order.get('pccc') or '-'}",
        "",
        f"수령인: {order.get('recipientName') or '-'}",
        f"수령연락처: {order.get('recipientPhone') or '-'}",
        f"주소: {order.get('address') or '-'}",
        f"배송메모: {order.get('deliveryMemo') or '-'}",
        "",
        f"상품: {order.get('productName') or '-'}",
        f"상품NO: {no or '-'}",
        f"상품ID: {pid}",
        f"브랜드: {order.get('brand') or '-'}",
        f"옵션: {opts}",
        f"수량: {qty}",
        f"단가: {price:,}원",
        f"합계: {total:,}원",
        f"결제: {pay}",
        f"메모: {order.get('memo') or '-'}",
    ]
    return "\n".join(lines)

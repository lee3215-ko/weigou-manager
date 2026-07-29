# -*- coding: utf-8 -*-
"""Customer management helpers — merge members + orders for ManagerApp."""
from __future__ import annotations

import re
from typing import Any

from orders_ui import format_order_line, status_label


def normalize_phone(raw: str | None) -> str:
    digits = re.sub(r"\D+", "", str(raw or ""))
    if digits.startswith("82") and len(digits) >= 10:
        digits = "0" + digits[2:]
    return digits


def order_line_total(order: dict[str, Any]) -> int:
    try:
        return int(order.get("price") or 0) * int(order.get("quantity") or 1)
    except (TypeError, ValueError):
        return 0


def _pick_str(*vals: Any) -> str:
    for v in vals:
        s = str(v or "").strip()
        if s:
            return s
    return ""


def build_customers(
    orders: list[dict[str, Any]] | None,
    members: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """
    Unify members + guest orderers.
    Key: member userId preferred, else normalized phone.
    """
    customers: dict[str, dict[str, Any]] = {}
    by_phone: dict[str, str] = {}

    def ensure(key: str, *, kind: str) -> dict[str, Any]:
        c = customers.get(key)
        if c is None:
            c = {
                "key": key,
                "kind": kind,  # member | guest
                "userId": "",
                "name": "",
                "phone": "",
                "phoneDigits": "",
                "pccc": "",
                "email": "",
                "zipcode": "",
                "address1": "",
                "address2": "",
                "address": "",
                "points": 0,
                "memberCreatedAt": "",
                "memberUpdatedAt": "",
                "orderCount": 0,
                "paidOrderCount": 0,
                "totalAmount": 0,
                "cancelledCount": 0,
                "firstOrderAt": "",
                "lastOrderAt": "",
                "orders": [],
            }
            customers[key] = c
        return c

    for m in members or []:
        if not isinstance(m, dict):
            continue
        uid = str(m.get("id") or "").strip()
        if not uid:
            continue
        key = f"u:{uid}"
        c = ensure(key, kind="member")
        c["kind"] = "member"
        c["userId"] = uid
        c["name"] = _pick_str(m.get("name"), c["name"])
        phone = _pick_str(m.get("phone"), c["phone"])
        c["phone"] = phone
        digits = normalize_phone(phone)
        c["phoneDigits"] = digits
        c["pccc"] = _pick_str(m.get("pccc"), c["pccc"])
        c["zipcode"] = _pick_str(m.get("zipcode"), c["zipcode"])
        c["address1"] = _pick_str(m.get("address1"), c["address1"])
        c["address2"] = _pick_str(m.get("address2"), c["address2"])
        addr = " ".join(x for x in (c["address1"], c["address2"]) if x).strip()
        if addr:
            c["address"] = addr
        try:
            c["points"] = int(m.get("points") or 0)
        except (TypeError, ValueError):
            c["points"] = 0
        c["memberCreatedAt"] = _pick_str(m.get("created_at"), c["memberCreatedAt"])
        c["memberUpdatedAt"] = _pick_str(m.get("updated_at"), c["memberUpdatedAt"])
        if digits:
            by_phone[digits] = key

    sorted_orders = sorted(
        [o for o in (orders or []) if isinstance(o, dict)],
        key=lambda o: str(o.get("createdAt") or ""),
    )

    for o in sorted_orders:
        uid = str(o.get("userId") or "").strip()
        phone = _pick_str(o.get("phone"), o.get("recipientPhone"))
        digits = normalize_phone(phone)
        key = ""
        if uid and f"u:{uid}" in customers:
            key = f"u:{uid}"
        elif digits and digits in by_phone:
            key = by_phone[digits]
        elif uid:
            key = f"u:{uid}"
            c = ensure(key, kind="member")
            c["userId"] = uid
            c["kind"] = "member"
        elif digits:
            key = f"p:{digits}"
            ensure(key, kind="guest")
            by_phone.setdefault(digits, key)
        else:
            # no phone/user — isolate by order id
            key = f"o:{o.get('id') or id(o)}"
            ensure(key, kind="guest")

        c = customers[key]
        if uid and not c["userId"]:
            c["userId"] = uid
            c["kind"] = "member"
        if digits:
            c["phoneDigits"] = digits
            by_phone.setdefault(digits, key)
        c["name"] = _pick_str(c["name"], o.get("customerName"), o.get("recipientName"))
        c["phone"] = _pick_str(c["phone"], o.get("phone"), o.get("recipientPhone"))
        c["pccc"] = _pick_str(c["pccc"], o.get("pccc"))
        c["address"] = _pick_str(c["address"], o.get("address"))
        if o.get("orderType") == "member":
            c["kind"] = "member"

        created = str(o.get("createdAt") or "")
        c["orders"].append(o)
        c["orderCount"] = len(c["orders"])
        st = str(o.get("status") or "")
        if st == "cancelled":
            c["cancelledCount"] = int(c["cancelledCount"]) + 1
        else:
            c["paidOrderCount"] = int(c["paidOrderCount"]) + 1
            c["totalAmount"] = int(c["totalAmount"]) + order_line_total(o)
        if created:
            if not c["firstOrderAt"] or created < c["firstOrderAt"]:
                c["firstOrderAt"] = created
            if not c["lastOrderAt"] or created > c["lastOrderAt"]:
                c["lastOrderAt"] = created

    # newest order first in history
    for c in customers.values():
        c["orders"] = sorted(
            c["orders"],
            key=lambda o: str(o.get("createdAt") or ""),
            reverse=True,
        )

    out = list(customers.values())
    out.sort(
        key=lambda c: (
            str(c.get("lastOrderAt") or ""),
            str(c.get("memberCreatedAt") or ""),
            str(c.get("name") or ""),
        ),
        reverse=True,
    )
    return out


def filter_customers(
    customers: list[dict[str, Any]],
    *,
    query: str = "",
    kind: str = "all",  # all | member | guest
) -> list[dict[str, Any]]:
    q = (query or "").strip().lower()
    q_digits = normalize_phone(query)
    out: list[dict[str, Any]] = []
    for c in customers:
        if kind == "member" and c.get("kind") != "member":
            continue
        if kind == "guest" and c.get("kind") != "guest":
            continue
        if q or q_digits:
            hay = " ".join(
                [
                    str(c.get("name") or ""),
                    str(c.get("phone") or ""),
                    str(c.get("phoneDigits") or ""),
                    str(c.get("pccc") or ""),
                    str(c.get("address") or ""),
                    str(c.get("userId") or ""),
                    str(c.get("email") or ""),
                ]
            ).lower()
            if q and q not in hay and (not q_digits or q_digits not in hay):
                continue
        out.append(c)
    return out


def format_customer_line(c: dict[str, Any]) -> str:
    kind = "회원" if c.get("kind") == "member" else "비회원"
    name = str(c.get("name") or "(이름없음)")
    phone = str(c.get("phone") or c.get("phoneDigits") or "-")
    pts = int(c.get("points") or 0)
    cnt = int(c.get("orderCount") or 0)
    total = int(c.get("totalAmount") or 0)
    last = str(c.get("lastOrderAt") or "")[:10]
    last_part = f" · 최근 {last}" if last else " · 주문없음"
    return (
        f"[{kind}] {name} · {phone} · 마일리지 {pts:,} · "
        f"주문 {cnt}회 · {total:,}원{last_part}"
    )


def customer_detail_text(c: dict[str, Any]) -> str:
    kind = "회원" if c.get("kind") == "member" else "비회원"
    first = str(c.get("firstOrderAt") or "").replace("T", " ")[:19] or "-"
    last = str(c.get("lastOrderAt") or "").replace("T", " ")[:19] or "-"
    joined = str(c.get("memberCreatedAt") or "").replace("T", " ")[:19] or "-"
    addr = _pick_str(
        c.get("address"),
        " ".join(
            x for x in (c.get("zipcode"), c.get("address1"), c.get("address2")) if x
        ),
    ) or "-"

    lines = [
        f"구분: {kind}",
        f"이름: {c.get('name') or '-'}",
        f"전화번호: {c.get('phone') or c.get('phoneDigits') or '-'}",
        f"마일리지(포인트): {int(c.get('points') or 0):,}P",
        f"통관부호(PCCC): {c.get('pccc') or '-'}",
        f"주소: {addr}",
        f"회원ID: {c.get('userId') or '-'}",
        f"가입일: {joined}",
        "",
        f"주문횟수: {int(c.get('orderCount') or 0)}회 "
        f"(유효 {int(c.get('paidOrderCount') or 0)} · 취소 {int(c.get('cancelledCount') or 0)})",
        f"주문총금액: {int(c.get('totalAmount') or 0):,}원  (취소 제외)",
        f"첫 주문: {first}",
        f"최근 주문: {last}",
        "",
        "—— 주문 이력 ——",
    ]
    orders = c.get("orders") or []
    if not orders:
        lines.append("(주문 없음)")
    else:
        for o in orders:
            if isinstance(o, dict):
                lines.append(format_order_line(o))
                st = status_label(str(o.get("status") or ""))
                oid = o.get("id") or "-"
                lines.append(f"    └ 주문번호 {oid} · {st}")
    return "\n".join(lines)

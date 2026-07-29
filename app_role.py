# -*- coding: utf-8 -*-
"""PC role: full (A) vs manager (B — no collect/debug)."""
from __future__ import annotations

from typing import Literal

from catalog_sync import load_sync_settings, save_sync_settings

Role = Literal["full", "manager"]


def get_app_role() -> Role:
    cfg = load_sync_settings()
    raw = str(cfg.get("role") or "full").strip().lower()
    return "manager" if raw == "manager" else "full"


def is_manager_role() -> bool:
    return get_app_role() == "manager"


def is_full_role() -> bool:
    return get_app_role() == "full"


def set_app_role(role: Role) -> None:
    cfg = load_sync_settings()
    cfg["role"] = "manager" if role == "manager" else "full"
    save_sync_settings(cfg)


def role_label(role: Role | None = None) -> str:
    r = role or get_app_role()
    return "관리(B) — 수집·디버그 없음" if r == "manager" else "전체(A) — 수집 포함"

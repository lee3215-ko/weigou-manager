# -*- coding: utf-8 -*-
"""App paths / version for Weigou Manager (exe + auto-update)."""
from __future__ import annotations

import os
import shutil
import sys

APP_NAME = "WeigouManager"
APP_DISPLAY_NAME = "Weigou Product Manager"
APP_VERSION = "1.0.18"
EXE_NAME = "WeigouManager.exe"
RELEASE_ASSET = "WeigouManager.zip"
UPDATE_VERSION_URL = (
    "https://raw.githubusercontent.com/lee3215-ko/weigou-manager/main/version.json"
)

# Preserved across updates (robocopy /XD data)
DATA_FILES = (
    "sync_settings.json",
)


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def get_app_dir() -> str:
    if is_frozen():
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def get_data_dir() -> str:
    """User data next to exe (data/) ??kept when auto-updating."""
    data_dir = os.path.join(get_app_dir(), "data")
    os.makedirs(data_dir, exist_ok=True)
    return data_dir


def data_path(filename: str) -> str:
    return os.path.join(get_data_dir(), filename)


def get_catalog_root() -> str:
    """SQLite + images live under data/catalog (shared sync + local files)."""
    root = os.path.join(get_data_dir(), "catalog")
    os.makedirs(root, exist_ok=True)
    return root


def get_resource_path(*parts: str) -> str:
    if is_frozen():
        base = getattr(sys, "_MEIPASS", get_app_dir())
    else:
        base = get_app_dir()
    return os.path.join(base, *parts)


def migrate_legacy_data() -> None:
    """Move old Documents/WeigouManager into data/catalog once."""
    legacy = os.path.join(os.path.expanduser("~"), "Documents", "WeigouManager")
    target = get_catalog_root()
    marker = os.path.join(target, ".migrated_from_documents")
    if os.path.isfile(marker):
        return
    if not os.path.isdir(legacy):
        open(marker, "w", encoding="utf-8").write("none\n")
        return
    # Only migrate if target looks empty
    has_db = os.path.isfile(os.path.join(target, "catalog.db"))
    if has_db:
        open(marker, "w", encoding="utf-8").write("skipped\n")
        return
    try:
        for name in os.listdir(legacy):
            src = os.path.join(legacy, name)
            dst = os.path.join(target, name)
            if os.path.exists(dst):
                continue
            if os.path.isdir(src):
                shutil.copytree(src, dst)
            else:
                shutil.copy2(src, dst)
        open(marker, "w", encoding="utf-8").write(legacy + "\n")
    except OSError:
        pass


def init_runtime_paths() -> None:
    os.chdir(get_app_dir())
    migrate_legacy_data()













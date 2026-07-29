# -*- coding: utf-8 -*-
"""Local product catalog: SQLite + image files."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import pathlib
import re
import shutil
import sqlite3
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable

from product_parse import ParsedProduct

ProgressCb = Callable[[str], None]

CATEGORY_ORDER = ["가방", "신발", "여성옷", "남성옷", "선글라스", "벨트", "악세사리", "기타"]


def search_code_sort_num(code: str) -> int:
    """Numeric key for search_code — identical on every PC (does not use local id)."""
    s = (code or "").strip()
    if not s:
        return 10**18
    if s.isdigit():
        return int(s)
    m = re.match(r"(\d+)", s)
    return int(m.group(1)) if m else 10**18


# Shared across A/B: local SQLite id differs per machine, so never ORDER BY id for UI lists.
_PRODUCT_ORDER_SQL = (
    " ORDER BY "
    "CASE WHEN trim(COALESCE(search_code,'')) = '' THEN 1 ELSE 0 END, "
    "CASE WHEN search_code GLOB '[0-9]*' AND search_code NOT GLOB '*[^0-9]*' "
    "THEN CAST(search_code AS INTEGER) ELSE 1000000000000000000 END, "
    "lower(COALESCE(search_code,'')), "
    "lower(COALESCE(goods_id,'')), "
    "created_at DESC"
)

_EXCLUDED_ORDER_SQL = (
    " ORDER BY "
    "CASE WHEN trim(COALESCE(search_code,'')) = '' THEN 1 ELSE 0 END, "
    "CASE WHEN search_code GLOB '[0-9]*' AND search_code NOT GLOB '*[^0-9]*' "
    "THEN CAST(search_code AS INTEGER) ELSE 1000000000000000000 END, "
    "lower(COALESCE(search_code,'')), "
    "lower(COALESCE(goods_id,'')), "
    "created_at DESC"
)

_PUBLISHED_ORDER_SQL = (
    " ORDER BY "
    "CASE WHEN trim(COALESCE(search_code,'')) = '' THEN 1 ELSE 0 END, "
    "CASE WHEN search_code GLOB '[0-9]*' AND search_code NOT GLOB '*[^0-9]*' "
    "THEN CAST(search_code AS INTEGER) ELSE 1000000000000000000 END, "
    "lower(COALESCE(search_code,'')), "
    "lower(COALESCE(goods_id,'')), "
    "lower(COALESCE(mall_id,'')), "
    "created_at DESC"
)


def _prefer_text(remote: str | None, local: str | None) -> str:
    """Prefer non-empty remote text; fall back to local so sync never blanks a field."""
    r = (remote or "").strip()
    l = (local or "").strip()
    return r if r else l


def _prefer_urls(remote: list[str] | None, local: list[str] | None) -> list[str]:
    """Prefer the longer non-empty image-url list between remote and local."""
    r = [u for u in (remote or []) if isinstance(u, str) and u]
    l = [u for u in (local or []) if isinstance(u, str) and u]
    if r and l:
        return r if len(r) >= len(l) else l
    return r or l


def default_root() -> pathlib.Path:
    try:
        from paths import get_catalog_root

        return pathlib.Path(get_catalog_root())
    except Exception:
        return pathlib.Path.home() / "Documents" / "WeigouManager"


@dataclass
class Product:
    id: int
    goods_id: str
    shop_id: str
    title: str
    search_code: str
    sku_no: str
    tags: str
    description: str
    cover_path: str
    image_paths: list[str]
    created_at: str
    updated_at: str
    category: str = ""
    google_name: str = ""
    name_en: str = ""
    image_urls: list[str] = field(default_factory=list)
    colors: str = ""
    sizes: str = ""


@dataclass
class ExcludedItem:
    id: int
    goods_id: str
    shop_id: str
    search_code: str
    sku_no: str
    title: str
    tags: str
    category: str
    cover_path: str
    note: str
    created_at: str


@dataclass
class PublishedItem:
    id: int
    goods_id: str
    shop_id: str
    search_code: str
    sku_no: str
    title: str
    tags: str
    category: str
    cover_path: str
    note: str
    mall_id: str
    created_at: str
    updated_at: str = ""
    google_name: str = ""
    name_en: str = ""
    colors: str = ""
    sizes: str = ""
    description: str = ""
    recommended: bool = False
    image_paths: list[str] = field(default_factory=list)
    image_urls: list[str] = field(default_factory=list)


class ProductStore:
    def __init__(self, root: pathlib.Path | None = None) -> None:
        self.root = root or default_root()
        self.img_root = self.root / "images"
        self.excluded_img_root = self.root / "excluded_covers"
        self.published_img_root = self.root / "published_covers"
        self.db_path = self.root / "catalog.db"
        self._db_lock = threading.RLock()
        self.root.mkdir(parents=True, exist_ok=True)
        self.img_root.mkdir(parents=True, exist_ok=True)
        self.excluded_img_root.mkdir(parents=True, exist_ok=True)
        self.published_img_root.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        """Per-call connection; WAL + busy_timeout allow collect/publish/search together."""
        con = sqlite3.connect(str(self.db_path), timeout=60.0)
        con.row_factory = sqlite3.Row
        try:
            con.execute("PRAGMA busy_timeout=60000")
        except Exception:
            pass
        return con

    def _init_db(self) -> None:
        with self._db_lock:
            with self._connect() as con:
                try:
                    con.execute("PRAGMA journal_mode=WAL")
                except Exception:
                    pass
                con.executescript(
                    """
                CREATE TABLE IF NOT EXISTS products (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    goods_id TEXT,
                    shop_id TEXT,
                    title TEXT NOT NULL DEFAULT '',
                    search_code TEXT NOT NULL DEFAULT '',
                    sku_no TEXT NOT NULL DEFAULT '',
                    tags TEXT NOT NULL DEFAULT '',
                    description TEXT NOT NULL DEFAULT '',
                    cover_path TEXT NOT NULL DEFAULT '',
                    image_paths TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_products_search ON products(search_code);
                CREATE INDEX IF NOT EXISTS idx_products_goods ON products(goods_id);
                CREATE INDEX IF NOT EXISTS idx_products_title ON products(title);
                CREATE TABLE IF NOT EXISTS excluded (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    goods_id TEXT NOT NULL DEFAULT '',
                    shop_id TEXT NOT NULL DEFAULT '',
                    search_code TEXT NOT NULL DEFAULT '',
                    sku_no TEXT NOT NULL DEFAULT '',
                    title TEXT NOT NULL DEFAULT '',
                    tags TEXT NOT NULL DEFAULT '',
                    category TEXT NOT NULL DEFAULT '',
                    cover_path TEXT NOT NULL DEFAULT '',
                    note TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_excluded_goods ON excluded(goods_id);
                CREATE INDEX IF NOT EXISTS idx_excluded_search ON excluded(search_code);
                CREATE INDEX IF NOT EXISTS idx_excluded_category ON excluded(category);
                CREATE TABLE IF NOT EXISTS published (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    goods_id TEXT NOT NULL DEFAULT '',
                    shop_id TEXT NOT NULL DEFAULT '',
                    search_code TEXT NOT NULL DEFAULT '',
                    sku_no TEXT NOT NULL DEFAULT '',
                    title TEXT NOT NULL DEFAULT '',
                    tags TEXT NOT NULL DEFAULT '',
                    category TEXT NOT NULL DEFAULT '',
                    cover_path TEXT NOT NULL DEFAULT '',
                    note TEXT NOT NULL DEFAULT '',
                    mall_id TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_published_goods ON published(goods_id);
                CREATE INDEX IF NOT EXISTS idx_published_search ON published(search_code);
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS sync_tombstones (
                    kind TEXT NOT NULL,
                    sync_key TEXT NOT NULL,
                    deleted_at TEXT NOT NULL,
                    PRIMARY KEY (kind, sync_key)
                );
                """
                )
                cols = {r[1] for r in con.execute("PRAGMA table_info(products)").fetchall()}
                migrations = [
                    ("category", "TEXT NOT NULL DEFAULT ''"),
                    ("google_name", "TEXT NOT NULL DEFAULT ''"),
                    ("name_en", "TEXT NOT NULL DEFAULT ''"),
                    ("image_urls", "TEXT NOT NULL DEFAULT '[]'"),
                    ("colors", "TEXT NOT NULL DEFAULT ''"),
                    ("sizes", "TEXT NOT NULL DEFAULT ''"),
                ]
                for name, decl in migrations:
                    if name not in cols:
                        con.execute(f"ALTER TABLE products ADD COLUMN {name} {decl}")
                con.execute(
                    "CREATE INDEX IF NOT EXISTS idx_products_category ON products(category)"
                )
                con.execute(
                    "CREATE INDEX IF NOT EXISTS idx_products_updated ON products(updated_at)"
                )
                con.execute(
                    "CREATE INDEX IF NOT EXISTS idx_products_search_code ON products(search_code)"
                )
                con.execute(
                    "CREATE INDEX IF NOT EXISTS idx_products_goods_id ON products(goods_id)"
                )
                con.execute(
                    "CREATE INDEX IF NOT EXISTS idx_products_sku ON products(sku_no)"
                )
                con.execute(
                    "CREATE INDEX IF NOT EXISTS idx_products_tags ON products(tags)"
                )
                pub_cols = {
                    r[1] for r in con.execute("PRAGMA table_info(published)").fetchall()
                }
                pub_migrations = [
                    ("google_name", "TEXT NOT NULL DEFAULT ''"),
                    ("name_en", "TEXT NOT NULL DEFAULT ''"),
                    ("colors", "TEXT NOT NULL DEFAULT ''"),
                    ("sizes", "TEXT NOT NULL DEFAULT ''"),
                    ("description", "TEXT NOT NULL DEFAULT ''"),
                    ("image_paths", "TEXT NOT NULL DEFAULT '[]'"),
                    ("image_urls", "TEXT NOT NULL DEFAULT '[]'"),
                    ("recommended", "INTEGER NOT NULL DEFAULT 0"),
                    ("updated_at", "TEXT NOT NULL DEFAULT ''"),
                ]
                for name, decl in pub_migrations:
                    if name not in pub_cols:
                        con.execute(f"ALTER TABLE published ADD COLUMN {name} {decl}")
                con.execute(
                    "CREATE INDEX IF NOT EXISTS idx_published_updated ON published(updated_at)"
                )
                # 상의/하의/자켓 → 여성옷 (성별 미상 기존 의류)
                for table in ("products", "excluded", "published"):
                    con.execute(
                        f"UPDATE {table} SET category='여성옷' "
                        "WHERE category IN ('상의','하의','자켓')"
                    )

    def _row_to_product(self, row: sqlite3.Row) -> Product:
        def jlist(key: str) -> list[str]:
            try:
                keys = row.keys()
                if key not in keys:
                    return []
                return list(json.loads(row[key] or "[]"))
            except (json.JSONDecodeError, TypeError):
                return []

        keys = row.keys()
        return Product(
            id=row["id"],
            goods_id=row["goods_id"] or "",
            shop_id=row["shop_id"] or "",
            title=row["title"] or "",
            search_code=row["search_code"] or "",
            sku_no=row["sku_no"] or "",
            tags=row["tags"] or "",
            description=row["description"] or "",
            cover_path=row["cover_path"] or "",
            image_paths=jlist("image_paths"),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            category=(row["category"] if "category" in keys else "") or "",
            google_name=(row["google_name"] if "google_name" in keys else "") or "",
            name_en=(row["name_en"] if "name_en" in keys else "") or "",
            image_urls=jlist("image_urls"),
            colors=(row["colors"] if "colors" in keys else "") or "",
            sizes=(row["sizes"] if "sizes" in keys else "") or "",
        )

    def _list_products_sql(
        self, query: str, category: str, *, searched_only: bool = False
    ) -> tuple[str, list[str]]:
        q = (query or "").strip()
        cat = (category or "").strip()
        sql = " WHERE 1=1"
        args: list[str] = []
        if q:
            like = f"%{q}%"
            sql += (
                " AND (title LIKE ? OR search_code LIKE ? OR sku_no LIKE ?"
                " OR description LIKE ? OR tags LIKE ? OR google_name LIKE ?"
                " OR name_en LIKE ? OR category LIKE ?)"
            )
            args.extend([like, like, like, like, like, like, like, like])
        if cat and cat != "전체":
            sql += " AND category = ?"
            args.append(cat)
        if searched_only:
            sql += " AND trim(COALESCE(google_name,'')) != ''"
        return sql, args

    def list_products(
        self,
        query: str = "",
        category: str = "",
        *,
        limit: int | None = None,
        offset: int = 0,
        searched_only: bool = False,
    ) -> list[Product]:
        """List products matching query/category.

        Order is stable across machines (A/B): search_code numeric, then
        search_code/goods_id/created_at — never local ``id`` (those differ per PC).
        """
        where_sql, args = self._list_products_sql(
            query, category, searched_only=searched_only
        )
        sql = "SELECT * FROM products" + where_sql + _PRODUCT_ORDER_SQL
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            args = [*args, int(limit), int(offset)]
        with self._connect() as con:
            rows = con.execute(sql, args).fetchall()
        products = [self._row_to_product(r) for r in rows]

        if limit is not None:
            return products

        # Full export: same key as SQL (covers non-pure-digit codes like "12A")
        def sort_key(p: Product) -> tuple:
            return (
                search_code_sort_num(p.search_code),
                (p.search_code or "").lower(),
                (p.goods_id or "").lower(),
                p.created_at or "",
            )

        products.sort(key=sort_key)
        return products

    def count_products(
        self, query: str = "", category: str = "", *, searched_only: bool = False
    ) -> int:
        where_sql, args = self._list_products_sql(
            query, category, searched_only=searched_only
        )
        with self._connect() as con:
            row = con.execute(
                "SELECT COUNT(*) AS c FROM products" + where_sql, args
            ).fetchone()
        return int(row["c"] if row else 0)

    def get(self, product_id: int) -> Product | None:
        with self._connect() as con:
            row = con.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
        return self._row_to_product(row) if row else None

    def get_setting(self, key: str, default: str = "") -> str:
        with self._connect() as con:
            row = con.execute(
                "SELECT value FROM settings WHERE key=?", (key,)
            ).fetchone()
        return (row["value"] if row else default) or default

    def set_setting(self, key: str, value: str) -> None:
        with self._connect() as con:
            con.execute(
                """
                INSERT INTO settings(key, value) VALUES(?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
                """,
                (key, value),
            )

    def get_list_cursor(self, shop_id: str) -> int:
        """Last handled data-index for this album (-1 = none yet)."""
        sid = (shop_id or "").strip() or "_default"
        raw = self.get_setting(f"list_cursor:{sid}", "-1")
        try:
            return int(raw)
        except ValueError:
            return -1

    def set_list_cursor(self, shop_id: str, index: int) -> None:
        sid = (shop_id or "").strip() or "_default"
        prev = self.get_list_cursor(sid)
        if index > prev:
            self.set_setting(f"list_cursor:{sid}", str(int(index)))

    def record_tombstone(self, kind: str, sync_key: str) -> None:
        """Remember a deletion so other synced devices can remove it too."""
        key = (sync_key or "").strip()
        k = (kind or "").strip()
        if not key or not k:
            return
        now = dt.datetime.now().isoformat(timespec="seconds")
        with self._connect() as con:
            con.execute(
                """
                INSERT INTO sync_tombstones(kind, sync_key, deleted_at)
                VALUES (?, ?, ?)
                ON CONFLICT(kind, sync_key) DO UPDATE SET deleted_at=excluded.deleted_at
                """,
                (k, key, now),
            )

    def list_tombstones_since(self, iso_ts: str) -> list[dict]:
        """Tombstones with deleted_at >= iso_ts (all tombstones if iso_ts is empty)."""
        since = (iso_ts or "").strip()
        with self._connect() as con:
            if since:
                rows = con.execute(
                    "SELECT kind, sync_key, deleted_at FROM sync_tombstones "
                    "WHERE deleted_at >= ? ORDER BY deleted_at",
                    (since,),
                ).fetchall()
            else:
                rows = con.execute(
                    "SELECT kind, sync_key, deleted_at FROM sync_tombstones "
                    "ORDER BY deleted_at"
                ).fetchall()
        return [dict(r) for r in rows]

    def clear_tombstone(self, kind: str, sync_key: str) -> None:
        key = (sync_key or "").strip()
        k = (kind or "").strip()
        if not key or not k:
            return
        with self._connect() as con:
            con.execute(
                "DELETE FROM sync_tombstones WHERE kind=? AND sync_key=?", (k, key)
            )

    def delete(self, product_id: int) -> None:
        p = self.get(product_id)
        if not p:
            return
        folder = self.img_root / str(product_id)
        if folder.exists():
            shutil.rmtree(folder, ignore_errors=True)
        with self._connect() as con:
            con.execute("DELETE FROM products WHERE id=?", (product_id,))
        key = p.goods_id or p.search_code or f"id:{product_id}"
        self.record_tombstone("product", key)

    def _row_to_excluded(self, row: sqlite3.Row) -> ExcludedItem:
        return ExcludedItem(
            id=int(row["id"]),
            goods_id=row["goods_id"] or "",
            shop_id=row["shop_id"] or "",
            search_code=row["search_code"] or "",
            sku_no=row["sku_no"] or "",
            title=row["title"] or "",
            tags=row["tags"] or "",
            category=row["category"] or "",
            cover_path=row["cover_path"] or "",
            note=row["note"] or "",
            created_at=row["created_at"] or "",
        )

    def _list_excluded_sql(self, query: str, category: str) -> tuple[str, list[str]]:
        q = (query or "").strip()
        cat = (category or "").strip()
        sql = " WHERE 1=1"
        args: list[str] = []
        if q:
            like = f"%{q}%"
            sql += (
                " AND (title LIKE ? OR search_code LIKE ? OR sku_no LIKE ?"
                " OR tags LIKE ? OR goods_id LIKE ? OR category LIKE ?)"
            )
            args.extend([like, like, like, like, like, like])
        if cat and cat != "전체":
            sql += " AND category = ?"
            args.append(cat)
        return sql, args

    def list_excluded(
        self,
        query: str = "",
        category: str = "",
        *,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[ExcludedItem]:
        """List excluded items. See ``list_products`` for the pagination contract."""
        where_sql, args = self._list_excluded_sql(query, category)
        sql = "SELECT * FROM excluded" + where_sql + _EXCLUDED_ORDER_SQL
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            args = [*args, int(limit), int(offset)]
        with self._connect() as con:
            rows = con.execute(sql, args).fetchall()
        return [self._row_to_excluded(r) for r in rows]

    def count_excluded(self, query: str = "", category: str = "") -> int:
        where_sql, args = self._list_excluded_sql(query, category)
        with self._connect() as con:
            row = con.execute(
                "SELECT COUNT(*) AS c FROM excluded" + where_sql, args
            ).fetchone()
        return int(row["c"] if row else 0)

    def get_excluded(self, excluded_id: int) -> ExcludedItem | None:
        with self._connect() as con:
            row = con.execute("SELECT * FROM excluded WHERE id=?", (excluded_id,)).fetchone()
        return self._row_to_excluded(row) if row else None

    def excluded_goods_ids(self) -> set[str]:
        with self._connect() as con:
            rows = con.execute(
                "SELECT goods_id FROM excluded WHERE trim(goods_id) != ''"
            ).fetchall()
        return {str(r["goods_id"]) for r in rows}

    def collected_goods_ids(self) -> set[str]:
        """goods_id already saved in catalog — skip without opening detail."""
        with self._connect() as con:
            rows = con.execute(
                "SELECT goods_id FROM products WHERE trim(COALESCE(goods_id,'')) != ''"
            ).fetchall()
        return {str(r["goods_id"]) for r in rows}

    def excluded_search_codes(self) -> set[str]:
        with self._connect() as con:
            rows = con.execute(
                "SELECT search_code FROM excluded WHERE trim(search_code) != ''"
            ).fetchall()
        return {str(r["search_code"]) for r in rows}

    def is_excluded(
        self,
        *,
        goods_id: str = "",
        search_code: str = "",
    ) -> bool:
        gid = (goods_id or "").strip()
        code = (search_code or "").strip()
        with self._connect() as con:
            if gid:
                row = con.execute(
                    "SELECT 1 FROM excluded WHERE goods_id=? LIMIT 1", (gid,)
                ).fetchone()
                if row:
                    return True
            if code:
                row = con.execute(
                    "SELECT 1 FROM excluded WHERE search_code=? LIMIT 1", (code,)
                ).fetchone()
                if row:
                    return True
        return False

    def exclude_product(self, product_id: int, note: str = "") -> int | None:
        """Move product into excluded list (remember forever) and remove from catalog."""
        p = self.get(product_id)
        if not p:
            return None
        if not (p.goods_id or p.search_code):
            # still allow exclude by title+sku so we have something to remember
            pass
        now = dt.datetime.now().isoformat(timespec="seconds")
        cover_keep = ""
        src = pathlib.Path(p.cover_path) if p.cover_path else None
        if (not src or not src.exists()) and p.image_paths:
            cand = pathlib.Path(p.image_paths[0])
            if cand.exists():
                src = cand
        with self._connect() as con:
            cur = con.execute(
                """
                INSERT INTO excluded
                (goods_id, shop_id, search_code, sku_no, title, tags, category,
                 cover_path, note, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, '', ?, ?)
                """,
                (
                    p.goods_id,
                    p.shop_id,
                    p.search_code,
                    p.sku_no,
                    p.title or p.google_name,
                    p.tags,
                    p.category,
                    note or "",
                    now,
                ),
            )
            eid = int(cur.lastrowid)
        if src and src.exists():
            dest = self.excluded_img_root / f"{eid}{src.suffix or '.jpg'}"
            try:
                shutil.copy2(src, dest)
                cover_keep = str(dest)
                with self._connect() as con:
                    con.execute(
                        "UPDATE excluded SET cover_path=? WHERE id=?",
                        (cover_keep, eid),
                    )
            except OSError:
                pass
        self.delete(product_id)
        return eid

    def unexclude(self, excluded_id: int) -> bool:
        """Remove from exclusion list so the item can be collected again."""
        item = self.get_excluded(excluded_id)
        if not item:
            return False
        if item.cover_path:
            try:
                pathlib.Path(item.cover_path).unlink(missing_ok=True)
            except OSError:
                pass
        with self._connect() as con:
            con.execute("DELETE FROM excluded WHERE id=?", (excluded_id,))
        key = item.goods_id or item.search_code or f"id:{excluded_id}"
        self.record_tombstone("excluded", key)
        return True

    def _row_to_published(self, row: sqlite3.Row) -> PublishedItem:
        keys = row.keys()

        def jlist(key: str) -> list[str]:
            try:
                if key not in keys:
                    return []
                return list(json.loads(row[key] or "[]"))
            except (json.JSONDecodeError, TypeError):
                return []

        return PublishedItem(
            id=int(row["id"]),
            goods_id=row["goods_id"] or "",
            shop_id=row["shop_id"] or "",
            search_code=row["search_code"] or "",
            sku_no=row["sku_no"] or "",
            title=row["title"] or "",
            tags=row["tags"] or "",
            category=row["category"] or "",
            cover_path=row["cover_path"] or "",
            note=row["note"] or "",
            mall_id=(row["mall_id"] if "mall_id" in keys else "") or "",
            created_at=row["created_at"] or "",
            updated_at=(row["updated_at"] if "updated_at" in keys else "") or "",
            google_name=(row["google_name"] if "google_name" in keys else "") or "",
            name_en=(row["name_en"] if "name_en" in keys else "") or "",
            colors=(row["colors"] if "colors" in keys else "") or "",
            sizes=(row["sizes"] if "sizes" in keys else "") or "",
            description=(row["description"] if "description" in keys else "") or "",
            recommended=bool(int(row["recommended"]))
            if "recommended" in keys and row["recommended"] is not None
            else False,
            image_paths=jlist("image_paths"),
            image_urls=jlist("image_urls"),
        )

    def _list_published_sql(
        self, query: str, category: str, recommended_only: bool
    ) -> tuple[str, list[object]]:
        q = (query or "").strip()
        cat = (category or "").strip()
        sql = " WHERE 1=1"
        args: list[object] = []
        if q:
            like = f"%{q}%"
            sql += (
                " AND (title LIKE ? OR search_code LIKE ? OR sku_no LIKE ?"
                " OR tags LIKE ? OR goods_id LIKE ? OR category LIKE ?"
                " OR mall_id LIKE ? OR note LIKE ? OR google_name LIKE ?"
                " OR name_en LIKE ? OR colors LIKE ?)"
            )
            args.extend([like, like, like, like, like, like, like, like, like, like, like])
        if cat and cat != "전체":
            sql += " AND category = ?"
            args.append(cat)
        if recommended_only:
            sql += " AND recommended = 1"
        return sql, args

    def list_published(
        self,
        query: str = "",
        category: str = "",
        *,
        recommended_only: bool = False,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[PublishedItem]:
        """List published items. See ``list_products`` for the pagination contract."""
        where_sql, args = self._list_published_sql(query, category, recommended_only)
        sql = "SELECT * FROM published" + where_sql + _PUBLISHED_ORDER_SQL
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            args = [*args, int(limit), int(offset)]
        with self._connect() as con:
            rows = con.execute(sql, args).fetchall()
        return [self._row_to_published(r) for r in rows]

    def count_published(
        self, query: str = "", category: str = "", *, recommended_only: bool = False
    ) -> int:
        where_sql, args = self._list_published_sql(query, category, recommended_only)
        with self._connect() as con:
            row = con.execute(
                "SELECT COUNT(*) AS c FROM published" + where_sql, args
            ).fetchone()
        return int(row["c"] if row else 0)

    def get_published(self, published_id: int) -> PublishedItem | None:
        with self._connect() as con:
            row = con.execute(
                "SELECT * FROM published WHERE id=?", (published_id,)
            ).fetchone()
        return self._row_to_published(row) if row else None

    def find_published_by_search_code(self, search_code: str) -> PublishedItem | None:
        code = (search_code or "").strip()
        if not code:
            return None
        with self._connect() as con:
            row = con.execute(
                "SELECT * FROM published WHERE search_code=? ORDER BY id DESC LIMIT 1",
                (code,),
            ).fetchone()
            if not row:
                # 숫자만 저장된 경우 / 앞뒤 공백
                row = con.execute(
                    "SELECT * FROM published WHERE trim(search_code)=? "
                    "ORDER BY id DESC LIMIT 1",
                    (code,),
                ).fetchone()
            if not row and code.isdigit():
                like = f"%{code}%"
                row = con.execute(
                    "SELECT * FROM published WHERE search_code LIKE ? "
                    "ORDER BY id DESC LIMIT 1",
                    (like,),
                ).fetchone()
        return self._row_to_published(row) if row else None

    def find_product_by_search_code(self, search_code: str) -> Product | None:
        code = (search_code or "").strip()
        if not code:
            return None
        with self._connect() as con:
            row = con.execute(
                "SELECT * FROM products WHERE search_code=? ORDER BY id DESC LIMIT 1",
                (code,),
            ).fetchone()
        return self._row_to_product(row) if row else None

    def update_published(
        self,
        published_id: int,
        *,
        title: str | None = None,
        search_code: str | None = None,
        sku_no: str | None = None,
        tags: str | None = None,
        description: str | None = None,
        category: str | None = None,
        google_name: str | None = None,
        name_en: str | None = None,
        colors: str | None = None,
        sizes: str | None = None,
        note: str | None = None,
        mall_id: str | None = None,
        recommended: bool | None = None,
    ) -> None:
        """Update fields on a published row (for edit → 재등록)."""
        item = self.get_published(published_id)
        if not item:
            return
        now = dt.datetime.now().isoformat(timespec="seconds")
        with self._connect() as con:
            con.execute(
                """
                UPDATE published SET
                    title=?, search_code=?, sku_no=?, tags=?, description=?,
                    category=?, google_name=?, name_en=?, colors=?, sizes=?,
                    note=?, mall_id=?, recommended=?, updated_at=?
                WHERE id=?
                """,
                (
                    title if title is not None else item.title,
                    search_code if search_code is not None else item.search_code,
                    sku_no if sku_no is not None else item.sku_no,
                    tags if tags is not None else item.tags,
                    description if description is not None else item.description,
                    category if category is not None else item.category,
                    google_name if google_name is not None else item.google_name,
                    name_en if name_en is not None else item.name_en,
                    colors if colors is not None else item.colors,
                    sizes if sizes is not None else item.sizes,
                    note if note is not None else item.note,
                    mall_id if mall_id is not None else item.mall_id,
                    int(
                        item.recommended
                        if recommended is None
                        else bool(recommended)
                    ),
                    now,
                    published_id,
                ),
            )

    def published_to_product(self, item: PublishedItem) -> Product:
        """Build a Product snapshot from published row for homepage re-publish."""
        mall = (item.mall_id or "").strip()
        pid = item.id
        if mall.startswith("wg-"):
            try:
                pid = int(mall[3:])
            except ValueError:
                pid = item.id
        paths = list(item.image_paths or [])
        if not paths and item.cover_path:
            paths = [item.cover_path]
        return Product(
            id=pid,
            goods_id=item.goods_id,
            shop_id=item.shop_id,
            title=item.title,
            search_code=item.search_code,
            sku_no=item.sku_no,
            tags=item.tags,
            description=item.description,
            cover_path=item.cover_path,
            image_paths=paths,
            image_urls=list(item.image_urls or []),
            category=item.category,
            google_name=item.google_name,
            name_en=item.name_en,
            colors=item.colors,
            sizes=item.sizes,
            created_at=item.created_at,
            updated_at=item.created_at,
        )

    def published_goods_ids(self) -> set[str]:
        with self._connect() as con:
            rows = con.execute(
                "SELECT goods_id FROM published WHERE trim(goods_id) != ''"
            ).fetchall()
        return {str(r["goods_id"]) for r in rows}

    def published_search_codes(self) -> set[str]:
        with self._connect() as con:
            rows = con.execute(
                "SELECT search_code FROM published WHERE trim(search_code) != ''"
            ).fetchall()
        return {str(r["search_code"]) for r in rows}

    def is_published(self, *, goods_id: str = "", search_code: str = "") -> bool:
        gid = (goods_id or "").strip()
        code = (search_code or "").strip()
        with self._connect() as con:
            if gid:
                row = con.execute(
                    "SELECT 1 FROM published WHERE goods_id=? LIMIT 1", (gid,)
                ).fetchone()
                if row:
                    return True
            if code:
                row = con.execute(
                    "SELECT 1 FROM published WHERE search_code=? LIMIT 1", (code,)
                ).fetchone()
                if row:
                    return True
        return False

    def archive_published(
        self,
        product_id: int,
        *,
        mall_id: str = "",
        note: str = "",
    ) -> int | None:
        """Move product into published list after homepage registration."""
        p = self.get(product_id)
        if not p:
            return None
        now = dt.datetime.now().isoformat(timespec="seconds")
        with self._connect() as con:
            cur = con.execute(
                """
                INSERT INTO published
                (goods_id, shop_id, search_code, sku_no, title, tags, category,
                 cover_path, note, mall_id, created_at, updated_at,
                 google_name, name_en, colors, sizes, description,
                 image_paths, image_urls)
                VALUES (?, ?, ?, ?, ?, ?, ?, '', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    p.goods_id,
                    p.shop_id,
                    p.search_code,
                    p.sku_no,
                    p.title or p.google_name,
                    p.tags,
                    p.category,
                    note or "",
                    mall_id or "",
                    now,
                    now,
                    p.google_name,
                    p.name_en,
                    p.colors,
                    p.sizes,
                    p.description,
                    json.dumps(p.image_paths, ensure_ascii=False),
                    json.dumps(p.image_urls, ensure_ascii=False),
                ),
            )
            pid = int(cur.lastrowid)

        # Keep full gallery under published pack so 등록목록 제거 can restore
        pack = self.published_img_root / f"p{pid}"
        src_folder = self.img_root / str(product_id)
        new_paths: list[str] = []
        cover_keep = ""
        try:
            if src_folder.exists():
                if pack.exists():
                    shutil.rmtree(pack, ignore_errors=True)
                shutil.move(str(src_folder), str(pack))
            elif p.image_paths or p.cover_path:
                pack.mkdir(parents=True, exist_ok=True)
                seen: set[str] = set()
                for path in ([p.cover_path] if p.cover_path else []) + list(p.image_paths):
                    src = pathlib.Path(path)
                    if not src.exists() or str(src) in seen:
                        continue
                    seen.add(str(src))
                    dest = pack / src.name
                    try:
                        shutil.copy2(src, dest)
                    except OSError:
                        continue
            if pack.exists():
                files = sorted(
                    [f for f in pack.iterdir() if f.is_file()],
                    key=lambda f: f.name,
                )
                new_paths = [str(f) for f in files]
                if p.cover_path:
                    cname = pathlib.Path(p.cover_path).name
                    for f in files:
                        if f.name == cname:
                            cover_keep = str(f)
                            break
                if not cover_keep and new_paths:
                    cover_keep = new_paths[0]
        except OSError:
            pass

        if not cover_keep:
            src = pathlib.Path(p.cover_path) if p.cover_path else None
            if (not src or not src.exists()) and p.image_paths:
                cand = pathlib.Path(p.image_paths[0])
                if cand.exists():
                    src = cand
            if src and src.exists():
                dest = self.published_img_root / f"{pid}{src.suffix or '.jpg'}"
                try:
                    shutil.copy2(src, dest)
                    cover_keep = str(dest)
                except OSError:
                    pass

        with self._connect() as con:
            con.execute(
                """
                UPDATE published
                SET cover_path=?, image_paths=?
                WHERE id=?
                """,
                (
                    cover_keep,
                    json.dumps(new_paths or p.image_paths, ensure_ascii=False),
                    pid,
                ),
            )
            # Remove catalog row only (images already moved)
            con.execute("DELETE FROM products WHERE id=?", (product_id,))
        return pid

    def unpublish(self, published_id: int) -> int | None:
        """Remove from published list and restore into product management catalog."""
        item = self.get_published(published_id)
        if not item:
            return None
        now = dt.datetime.now().isoformat(timespec="seconds")
        with self._connect() as con:
            cur = con.execute(
                """
                INSERT INTO products
                (goods_id, shop_id, title, search_code, sku_no, tags, description,
                 cover_path, image_paths, image_urls, category, google_name, name_en,
                 colors, sizes, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, '', '[]', ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item.goods_id,
                    item.shop_id,
                    item.title,
                    item.search_code,
                    item.sku_no,
                    item.tags,
                    item.description,
                    json.dumps(item.image_urls, ensure_ascii=False),
                    item.category,
                    item.google_name,
                    item.name_en,
                    item.colors,
                    item.sizes,
                    now,
                    now,
                ),
            )
            new_id = int(cur.lastrowid)

        pack = self.published_img_root / f"p{published_id}"
        dest_folder = self.img_root / str(new_id)
        new_paths: list[str] = []
        cover_keep = ""
        try:
            if pack.exists():
                if dest_folder.exists():
                    shutil.rmtree(dest_folder, ignore_errors=True)
                shutil.move(str(pack), str(dest_folder))
            elif item.cover_path and pathlib.Path(item.cover_path).exists():
                dest_folder.mkdir(parents=True, exist_ok=True)
                src = pathlib.Path(item.cover_path)
                dest = dest_folder / src.name
                shutil.copy2(src, dest)
                try:
                    src.unlink(missing_ok=True)
                except OSError:
                    pass
            if dest_folder.exists():
                files = sorted(
                    [f for f in dest_folder.iterdir() if f.is_file()],
                    key=lambda f: f.name,
                )
                new_paths = [str(f) for f in files]
                if item.cover_path:
                    cname = pathlib.Path(item.cover_path).name
                    for f in files:
                        if f.name == cname:
                            cover_keep = str(f)
                            break
                if not cover_keep and new_paths:
                    cover_keep = new_paths[0]
        except OSError:
            pass

        with self._connect() as con:
            con.execute(
                """
                UPDATE products SET cover_path=?, image_paths=? WHERE id=?
                """,
                (
                    cover_keep,
                    json.dumps(new_paths, ensure_ascii=False),
                    new_id,
                ),
            )
            con.execute("DELETE FROM published WHERE id=?", (published_id,))
        key = item.goods_id or item.search_code or f"id:{published_id}"
        self.record_tombstone("published", key)

        # leftover single cover file (legacy archives)
        if item.cover_path:
            try:
                p = pathlib.Path(item.cover_path)
                if p.exists() and p.parent == self.published_img_root:
                    p.unlink(missing_ok=True)
            except OSError:
                pass
        return new_id

    def update_description(
        self,
        product_id: int,
        *,
        title: str | None = None,
        search_code: str | None = None,
        sku_no: str | None = None,
        tags: str | None = None,
        description: str | None = None,
        category: str | None = None,
        google_name: str | None = None,
        name_en: str | None = None,
        colors: str | None = None,
        sizes: str | None = None,
    ) -> None:
        p = self.get(product_id)
        if not p:
            return
        now = dt.datetime.now().isoformat(timespec="seconds")
        with self._connect() as con:
            con.execute(
                """
                UPDATE products SET
                    title=?, search_code=?, sku_no=?, tags=?, description=?,
                    category=?, google_name=?, name_en=?, colors=?, sizes=?, updated_at=?
                WHERE id=?
                """,
                (
                    title if title is not None else p.title,
                    search_code if search_code is not None else p.search_code,
                    sku_no if sku_no is not None else p.sku_no,
                    tags if tags is not None else p.tags,
                    description if description is not None else p.description,
                    category if category is not None else p.category,
                    google_name if google_name is not None else p.google_name,
                    name_en if name_en is not None else p.name_en,
                    colors if colors is not None else p.colors,
                    sizes if sizes is not None else p.sizes,
                    now,
                    product_id,
                ),
            )

    def count_by_exact_tag(self, tags: str) -> int:
        """Count products whose tags field matches exactly (trimmed)."""
        tag = (tags or "").strip()
        if not tag:
            return 0
        with self._connect() as con:
            row = con.execute(
                "SELECT COUNT(*) AS c FROM products WHERE trim(tags) = ?",
                (tag,),
            ).fetchone()
        return int(row["c"] if row else 0)

    def count_published_by_exact_tag(self, tags: str) -> int:
        """Count published rows whose tags field matches exactly (trimmed)."""
        tag = (tags or "").strip()
        if not tag:
            return 0
        with self._connect() as con:
            row = con.execute(
                "SELECT COUNT(*) AS c FROM published WHERE trim(tags) = ?",
                (tag,),
            ).fetchone()
        return int(row["c"] if row else 0)

    def list_published_ids_by_exact_tag(self, tags: str) -> list[int]:
        """Published ids with the same exact tags (trimmed)."""
        tag = (tags or "").strip()
        if not tag:
            return []
        with self._connect() as con:
            rows = con.execute(
                "SELECT id FROM published WHERE trim(tags) = ? ORDER BY id",
                (tag,),
            ).fetchall()
        return [int(r["id"]) for r in rows]

    def bulk_update_category_by_tag(self, tags: str, category: str) -> int:
        """Set category for all products with the same exact tags. Returns updated count."""
        tag = (tags or "").strip()
        cat = (category or "").strip()
        if not tag or not cat:
            return 0
        now = dt.datetime.now().isoformat(timespec="seconds")
        with self._connect() as con:
            cur = con.execute(
                """
                UPDATE products
                SET category=?, updated_at=?
                WHERE trim(tags) = ?
                """,
                (cat, now, tag),
            )
            return int(cur.rowcount or 0)

    def bulk_update_published_category_by_tag(
        self, tags: str, category: str
    ) -> list[int]:
        """Set category for published rows with the same exact tags. Returns updated ids."""
        tag = (tags or "").strip()
        cat = (category or "").strip()
        if not tag or not cat:
            return []
        ids = self.list_published_ids_by_exact_tag(tag)
        if not ids:
            return []
        with self._connect() as con:
            con.execute(
                f"""
                UPDATE published
                SET category=?
                WHERE id IN ({",".join("?" * len(ids))})
                """,
                (cat, *ids),
            )
        return ids

    def apply_google_result(
        self,
        product_id: int,
        *,
        google_name: str,
        name_en: str = "",
        category: str,
        update_title: bool = True,
    ) -> None:
        p = self.get(product_id)
        if not p:
            return
        desc = p.description or ""
        if p.title and p.title not in desc:
            desc = (desc + "\n" if desc else "") + f"원본: {p.title}"
        title = google_name if update_title and google_name else p.title
        self.update_description(
            product_id,
            title=title,
            description=desc.strip(),
            category=category or p.category,
            google_name=google_name,
            name_en=name_en or p.name_en,
        )

    def _download(self, url: str, dest: pathlib.Path) -> bool:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Referer": "https://www.szwego.com/",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = resp.read()
            if len(data) < 200:
                return False
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            return True
        except (urllib.error.URLError, TimeoutError, OSError):
            return False

    def _find_existing(self, parsed: ParsedProduct) -> int | None:
        with self._connect() as con:
            if parsed.goods_id:
                row = con.execute(
                    "SELECT id FROM products WHERE goods_id=? ORDER BY id DESC LIMIT 1",
                    (parsed.goods_id,),
                ).fetchone()
                if row:
                    return int(row["id"])
            if parsed.search_code and parsed.title:
                row = con.execute(
                    "SELECT id FROM products WHERE search_code=? AND title=? ORDER BY id DESC LIMIT 1",
                    (parsed.search_code, parsed.title),
                ).fetchone()
                if row:
                    return int(row["id"])
        return None

    def import_parsed(
        self,
        parsed: ParsedProduct,
        on_progress: ProgressCb | None = None,
        merge: bool = True,
    ) -> int:
        def log(msg: str) -> None:
            if on_progress:
                on_progress(msg)

        now = dt.datetime.now().isoformat(timespec="seconds")
        if self.is_excluded(goods_id=parsed.goods_id, search_code=parsed.search_code):
            log(
                f"제외 목록 — 건너뜀: {parsed.title or parsed.goods_id or parsed.search_code}"
            )
            return -1
        if self.is_published(goods_id=parsed.goods_id, search_code=parsed.search_code):
            log(
                f"등록 목록 — 건너뜀: {parsed.title or parsed.goods_id or parsed.search_code}"
            )
            return -1

        existing_id = self._find_existing(parsed) if merge else None

        try:
            from product_attrs import extract_attrs

            attrs0 = extract_attrs(parsed.title, parsed.tags, parsed.description)
            cat = attrs0.category
            colors0 = ", ".join(attrs0.colors)
            sizes0 = ", ".join(attrs0.sizes)
        except Exception:
            cat = ""
            colors0 = ""
            sizes0 = ""

        if existing_id is None:
            with self._connect() as con:
                cur = con.execute(
                    """
                    INSERT INTO products
                    (goods_id, shop_id, title, search_code, sku_no, tags, description,
                     cover_path, image_paths, image_urls, category, google_name,
                     colors, sizes, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, '', '[]', ?, ?, '', ?, ?, ?, ?)
                    """,
                    (
                        parsed.goods_id,
                        parsed.shop_id,
                        parsed.title,
                        parsed.search_code,
                        parsed.sku_no,
                        parsed.tags,
                        parsed.description,
                        json.dumps(parsed.image_urls, ensure_ascii=False),
                        cat,
                        colors0,
                        sizes0,
                        now,
                        now,
                    ),
                )
                product_id = int(cur.lastrowid)
        else:
            product_id = existing_id
            with self._connect() as con:
                con.execute(
                    """
                    UPDATE products SET
                        shop_id=?, title=?, search_code=?, sku_no=?, tags=?, description=?,
                        image_urls=?,
                        category=COALESCE(NULLIF(category,''), ?),
                        colors=CASE WHEN trim(COALESCE(colors,''))='' THEN ? ELSE colors END,
                        sizes=CASE WHEN trim(COALESCE(sizes,''))='' THEN ? ELSE sizes END,
                        updated_at=?
                    WHERE id=?
                    """,
                    (
                        parsed.shop_id or "",
                        parsed.title,
                        parsed.search_code,
                        parsed.sku_no,
                        parsed.tags,
                        parsed.description,
                        json.dumps(parsed.image_urls, ensure_ascii=False),
                        cat,
                        colors0,
                        sizes0,
                        now,
                        product_id,
                    ),
                )

        folder = self.img_root / str(product_id)
        folder.mkdir(parents=True, exist_ok=True)
        saved: list[str] = []
        for i, url in enumerate(parsed.image_urls, start=1):
            digest = hashlib.md5(url.encode("utf-8")).hexdigest()[:10]
            name = url.rstrip("/").split("/")[-1]
            name = re.sub(r"[^\w.\-]+", "_", name)
            if not name.lower().endswith((".jpg", ".jpeg", ".png", ".webp", ".gif")):
                name += ".jpg"
            dest = folder / f"{i:02d}_{digest}_{name}"
            if dest.exists() or self._download(url, dest):
                saved.append(str(dest))
                log(f"이미지 저장 {i}/{len(parsed.image_urls)}: {parsed.title[:40]}")
            else:
                log(f"이미지 실패 {i}: {url}")

        if existing_id is not None:
            old = self.get(product_id)
            if old:
                for pth in old.image_paths:
                    if pth not in saved and pathlib.Path(pth).exists():
                        saved.append(pth)

        cover = saved[0] if saved else ""

        # refine color from product photos (ignore packaging / gold hardware)
        try:
            from product_attrs import extract_attrs

            attrs1 = extract_attrs(
                parsed.title,
                parsed.tags,
                parsed.description,
                image_path=cover or None,
                image_paths=saved[:6],
            )
            colors1 = ", ".join(attrs1.colors)
            sizes1 = ", ".join(attrs1.sizes) or sizes0
            cat1 = attrs1.category or cat
        except Exception:
            colors1, sizes1, cat1 = colors0, sizes0, cat

        with self._connect() as con:
            con.execute(
                """
                UPDATE products SET
                    cover_path=?, image_paths=?,
                    category=COALESCE(NULLIF(category,''), ?),
                    colors=CASE
                        WHEN trim(COALESCE(colors,''))='' THEN ?
                        WHEN colors IN ('베이지', '골드', '실버', '아이보리')
                             AND ? NOT IN ('', '베이지', '골드', '실버', '아이보리')
                        THEN ?
                        ELSE colors
                    END,
                    sizes=CASE WHEN trim(COALESCE(sizes,''))='' THEN ? ELSE sizes END,
                    updated_at=?
                WHERE id=?
                """,
                (
                    cover,
                    json.dumps(saved, ensure_ascii=False),
                    cat1,
                    colors1,
                    colors1,
                    colors1,
                    sizes1,
                    now,
                    product_id,
                ),
            )
        meta = folder / "product.txt"
        meta.write_text(
            "\n".join(
                [
                    parsed.title,
                    f"搜索码：{parsed.search_code}" if parsed.search_code else "",
                    f"NO：{parsed.sku_no}" if parsed.sku_no else "",
                    parsed.tags,
                    f"카테고리：{cat1}" if cat1 else "",
                    f"컬러：{colors1}" if colors1 else "",
                    f"사이즈：{sizes1}" if sizes1 else "",
                    "",
                    parsed.description,
                    "",
                    f"goods_id={parsed.goods_id}",
                ]
            ).strip()
            + "\n",
            encoding="utf-8",
        )
        return product_id

    def import_many(
        self,
        items: list[ParsedProduct],
        on_progress: ProgressCb | None = None,
    ) -> tuple[int, int]:
        ok = 0
        fail = 0
        skipped = 0
        total = len(items)
        for i, item in enumerate(items, start=1):
            try:
                if on_progress:
                    on_progress(f"상품 {i}/{total}: {item.title or item.goods_id or '(무제)'}")
                pid = self.import_parsed(item, on_progress=on_progress)
                if pid is not None and pid < 0:
                    skipped += 1
                else:
                    ok += 1
            except Exception as e:
                fail += 1
                if on_progress:
                    on_progress(f"실패: {e}")
        if skipped and on_progress:
            on_progress(f"제외 목록으로 건너뜀 {skipped}건")
        return ok, fail

    @staticmethod
    def _prod_row(p: Product) -> dict:
        return {
            "goods_id": p.goods_id,
            "shop_id": p.shop_id,
            "title": p.title,
            "search_code": p.search_code,
            "sku_no": p.sku_no,
            "tags": p.tags,
            "description": p.description,
            "category": p.category,
            "google_name": p.google_name,
            "name_en": p.name_en,
            "colors": p.colors,
            "sizes": p.sizes,
            "image_urls": list(p.image_urls or []),
            "created_at": p.created_at,
            "updated_at": p.updated_at,
        }

    @staticmethod
    def _excl_row(e: ExcludedItem) -> dict:
        return {
            "goods_id": e.goods_id,
            "shop_id": e.shop_id,
            "search_code": e.search_code,
            "sku_no": e.sku_no,
            "title": e.title,
            "tags": e.tags,
            "category": e.category,
            "note": e.note,
            "created_at": e.created_at,
        }

    @staticmethod
    def _pub_row(p: PublishedItem) -> dict:
        return {
            "goods_id": p.goods_id,
            "shop_id": p.shop_id,
            "search_code": p.search_code,
            "sku_no": p.sku_no,
            "title": p.title,
            "tags": p.tags,
            "category": p.category,
            "note": p.note,
            "mall_id": p.mall_id,
            "google_name": p.google_name,
            "name_en": p.name_en,
            "colors": p.colors,
            "sizes": p.sizes,
            "description": p.description,
            "recommended": bool(p.recommended),
            "image_urls": list(p.image_urls or []),
            "created_at": p.created_at,
            "updated_at": p.updated_at,
        }

    def _tombstones_to_deleted(self, tombstones: list[dict]) -> dict[str, list[str]]:
        deleted: dict[str, list[str]] = {"products": [], "excluded": [], "published": []}
        kind_to_key = {"product": "products", "excluded": "excluded", "published": "published"}
        for t in tombstones:
            key = kind_to_key.get(str(t.get("kind") or ""))
            sync_key = t.get("sync_key")
            if key and sync_key:
                deleted[key].append(str(sync_key))
        return deleted

    def export_sync_bundle(self) -> dict:
        """Serialize catalog for multi-user GitHub sync (metadata + image URLs).

        schema 2: adds a "deleted" section (all current tombstone keys) so
        other devices can remove rows that were deleted locally.
        """
        products = [self._prod_row(p) for p in self.list_products("", "전체")]
        excluded = [self._excl_row(e) for e in self.list_excluded("")]
        published = [self._pub_row(p) for p in self.list_published("")]
        deleted = self._tombstones_to_deleted(self.list_tombstones_since(""))
        rev = int(self.get_setting("sync_rev", "0") or "0")
        return {
            "schema": 2,
            "type": "full",
            "rev": rev,
            "updated_at": dt.datetime.now().isoformat(timespec="seconds"),
            "products": products,
            "excluded": excluded,
            "published": published,
            "deleted": deleted,
        }

    def export_sync_delta(self, since_iso: str) -> dict:
        """Serialize only rows changed since ``since_iso`` plus tombstones since then.

        Lighter-weight alternative to :meth:`export_sync_bundle` for frequent
        syncs — schema 2, ``type: "delta"``.
        """
        since = (since_iso or "").strip()
        with self._connect() as con:
            prod_rows = con.execute(
                "SELECT * FROM products WHERE updated_at >= ? ORDER BY id DESC",
                (since,),
            ).fetchall()
            excl_rows = con.execute(
                "SELECT * FROM excluded WHERE created_at >= ? ORDER BY id DESC",
                (since,),
            ).fetchall()
            pub_rows = con.execute(
                "SELECT * FROM published WHERE created_at >= ? OR updated_at >= ? "
                "ORDER BY id DESC",
                (since, since),
            ).fetchall()
        products = [self._prod_row(self._row_to_product(r)) for r in prod_rows]
        excluded = [self._excl_row(self._row_to_excluded(r)) for r in excl_rows]
        published = [self._pub_row(self._row_to_published(r)) for r in pub_rows]
        deleted = self._tombstones_to_deleted(self.list_tombstones_since(since))
        rev = int(self.get_setting("sync_rev", "0") or "0")
        return {
            "schema": 2,
            "type": "delta",
            "since": since,
            "rev": rev,
            "updated_at": dt.datetime.now().isoformat(timespec="seconds"),
            "products": products,
            "excluded": excluded,
            "published": published,
            "deleted": deleted,
        }

    def _lookup_product_id(self, goods_id: str, search_code: str) -> int | None:
        gid = (goods_id or "").strip()
        code = (search_code or "").strip()
        with self._connect() as con:
            if gid:
                row = con.execute(
                    "SELECT id FROM products WHERE goods_id=? LIMIT 1", (gid,)
                ).fetchone()
                if row:
                    return int(row["id"])
            if code:
                row = con.execute(
                    "SELECT id FROM products WHERE search_code=? LIMIT 1", (code,)
                ).fetchone()
                if row:
                    return int(row["id"])
        return None

    def _ts(self, value: str) -> str:
        return (value or "").strip()

    def _apply_deleted_products(self, keys: list[str]) -> int:
        removed = 0
        with self._connect() as con:
            for key in keys:
                k = (key or "").strip()
                if not k or k.startswith("id:"):
                    continue
                rows = con.execute(
                    "SELECT id FROM products WHERE goods_id=? OR search_code=?",
                    (k, k),
                ).fetchall()
                for r in rows:
                    self.delete(int(r["id"]))
                    removed += 1
        return removed

    def _apply_deleted_excluded(self, keys: list[str]) -> int:
        removed = 0
        with self._connect() as con:
            for key in keys:
                k = (key or "").strip()
                if not k or k.startswith("id:"):
                    continue
                rows = con.execute(
                    "SELECT id FROM excluded WHERE goods_id=? OR search_code=?",
                    (k, k),
                ).fetchall()
                for r in rows:
                    self.unexclude(int(r["id"]))
                    removed += 1
        return removed

    def _apply_deleted_published(self, keys: list[str]) -> int:
        removed = 0
        for key in keys:
            k = (key or "").strip()
            if not k or k.startswith("id:"):
                continue
            with self._connect() as con:
                rows = con.execute(
                    "SELECT id FROM published WHERE mall_id=? OR goods_id=? OR search_code=?",
                    (k, k, k),
                ).fetchall()
            for r in rows:
                pubid = int(r["id"])
                item = self.get_published(pubid)
                with self._connect() as con:
                    con.execute("DELETE FROM published WHERE id=?", (pubid,))
                pack = self.published_img_root / f"p{pubid}"
                if pack.exists():
                    shutil.rmtree(pack, ignore_errors=True)
                if item and item.cover_path:
                    try:
                        pathlib.Path(item.cover_path).unlink(missing_ok=True)
                    except OSError:
                        pass
                self.record_tombstone("published", k)
                removed += 1
        return removed

    def apply_sync_bundle(self, bundle: dict) -> dict[str, int]:
        """Merge remote catalog into local DB. Returns change counts.

        Only metadata/URLs are merged here — actual image bytes are fetched
        lazily via :meth:`ensure_product_images` / :meth:`ensure_published_images`
        so a sync pull stays fast and cheap.
        """
        if not isinstance(bundle, dict):
            return {"products": 0, "excluded": 0, "published": 0}
        now = dt.datetime.now().isoformat(timespec="seconds")
        stats = {"products": 0, "excluded": 0, "published": 0}

        deleted = bundle.get("deleted") or {}
        if isinstance(deleted, dict):
            self._apply_deleted_products(list(deleted.get("products") or []))
            self._apply_deleted_excluded(list(deleted.get("excluded") or []))
            self._apply_deleted_published(list(deleted.get("published") or []))

        for row in bundle.get("products") or []:
            if not isinstance(row, dict):
                continue
            gid = (row.get("goods_id") or "").strip()
            code = (row.get("search_code") or "").strip()
            if not gid and not code and not (row.get("title") or "").strip():
                continue
            remote_u = self._ts(str(row.get("updated_at") or ""))
            pid = self._lookup_product_id(gid, code)
            urls = row.get("image_urls") or []
            if not isinstance(urls, list):
                urls = []
            if pid is None:
                with self._connect() as con:
                    con.execute(
                        """
                        INSERT INTO products
                        (goods_id, shop_id, title, search_code, sku_no, tags, description,
                         cover_path, image_paths, image_urls, category, google_name, name_en,
                         colors, sizes, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, '', '[]', ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            gid,
                            row.get("shop_id") or "",
                            row.get("title") or "",
                            code,
                            row.get("sku_no") or "",
                            row.get("tags") or "",
                            row.get("description") or "",
                            json.dumps(urls, ensure_ascii=False),
                            row.get("category") or "",
                            row.get("google_name") or "",
                            row.get("name_en") or "",
                            row.get("colors") or "",
                            row.get("sizes") or "",
                            row.get("created_at") or now,
                            remote_u or now,
                        ),
                    )
                stats["products"] += 1
            else:
                local = self.get(pid)
                if local and remote_u and local.updated_at and remote_u < local.updated_at:
                    continue
                local_urls = list(local.image_urls or []) if local else []
                merged_urls = _prefer_urls(urls, local_urls)
                with self._connect() as con:
                    con.execute(
                        """
                        UPDATE products SET
                            goods_id=?, shop_id=?, title=?, search_code=?, sku_no=?, tags=?,
                            description=?, image_urls=?, category=?, google_name=?, name_en=?,
                            colors=?, sizes=?, updated_at=?
                        WHERE id=?
                        """,
                        (
                            gid or (local.goods_id if local else ""),
                            row.get("shop_id") or (local.shop_id if local else ""),
                            _prefer_text(row.get("title"), local.title if local else ""),
                            _prefer_text(code, local.search_code if local else ""),
                            _prefer_text(row.get("sku_no"), local.sku_no if local else ""),
                            _prefer_text(row.get("tags"), local.tags if local else ""),
                            _prefer_text(
                                row.get("description"), local.description if local else ""
                            ),
                            json.dumps(merged_urls, ensure_ascii=False),
                            _prefer_text(row.get("category"), local.category if local else ""),
                            _prefer_text(
                                row.get("google_name"), local.google_name if local else ""
                            ),
                            _prefer_text(row.get("name_en"), local.name_en if local else ""),
                            _prefer_text(row.get("colors"), local.colors if local else ""),
                            _prefer_text(row.get("sizes"), local.sizes if local else ""),
                            remote_u or now,
                            pid,
                        ),
                    )
                stats["products"] += 1
            if gid:
                self.clear_tombstone("product", gid)
            if code:
                self.clear_tombstone("product", code)

        for row in bundle.get("excluded") or []:
            if not isinstance(row, dict):
                continue
            gid = (row.get("goods_id") or "").strip()
            code = (row.get("search_code") or "").strip()
            if not self.is_excluded(goods_id=gid, search_code=code):
                with self._connect() as con:
                    con.execute(
                        """
                        INSERT INTO excluded
                        (goods_id, shop_id, search_code, sku_no, title, tags, category,
                         cover_path, note, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, '', ?, ?)
                        """,
                        (
                            gid,
                            row.get("shop_id") or "",
                            code,
                            row.get("sku_no") or "",
                            row.get("title") or "",
                            row.get("tags") or "",
                            row.get("category") or "",
                            row.get("note") or "",
                            row.get("created_at") or now,
                        ),
                    )
                stats["excluded"] += 1
            if gid:
                self.clear_tombstone("excluded", gid)
            if code:
                self.clear_tombstone("excluded", code)

        for row in bundle.get("published") or []:
            if not isinstance(row, dict):
                continue
            gid = (row.get("goods_id") or "").strip()
            code = (row.get("search_code") or "").strip()
            mall = (row.get("mall_id") or "").strip()
            with self._connect() as con:
                exists = None
                if mall:
                    exists = con.execute(
                        "SELECT id FROM published WHERE mall_id=? LIMIT 1", (mall,)
                    ).fetchone()
                if exists is None and gid:
                    exists = con.execute(
                        "SELECT id FROM published WHERE goods_id=? LIMIT 1", (gid,)
                    ).fetchone()
                if exists is None and code:
                    exists = con.execute(
                        "SELECT id FROM published WHERE search_code=? LIMIT 1", (code,)
                    ).fetchone()
                urls = row.get("image_urls") or []
                if not isinstance(urls, list):
                    urls = []
                if exists is None:
                    remote_updated = row.get("updated_at") or row.get("created_at") or now
                    con.execute(
                        """
                        INSERT INTO published
                        (goods_id, shop_id, search_code, sku_no, title, tags, category,
                         cover_path, note, mall_id, created_at, updated_at,
                         google_name, name_en, colors, sizes, description,
                         recommended, image_paths, image_urls)
                        VALUES (?, ?, ?, ?, ?, ?, ?, '', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '[]', ?)
                        """,
                        (
                            gid,
                            row.get("shop_id") or "",
                            code,
                            row.get("sku_no") or "",
                            row.get("title") or "",
                            row.get("tags") or "",
                            row.get("category") or "",
                            row.get("note") or "",
                            mall,
                            row.get("created_at") or now,
                            remote_updated,
                            row.get("google_name") or "",
                            row.get("name_en") or "",
                            row.get("colors") or "",
                            row.get("sizes") or "",
                            row.get("description") or "",
                            int(bool(row.get("recommended"))),
                            json.dumps(urls, ensure_ascii=False),
                        ),
                    )
                    stats["published"] += 1
            if mall:
                self.clear_tombstone("published", mall)
            if gid:
                self.clear_tombstone("published", gid)
            if code:
                self.clear_tombstone("published", code)

        remote_rev = int(bundle.get("rev") or 0)
        local_rev = int(self.get_setting("sync_rev", "0") or "0")
        if remote_rev > local_rev:
            self.set_setting("sync_rev", str(remote_rev))
        return stats

    def bump_sync_rev(self) -> int:
        rev = int(self.get_setting("sync_rev", "0") or "0") + 1
        self.set_setting("sync_rev", str(rev))
        return rev

    @staticmethod
    def _download_gallery(
        urls: list[str], folder: pathlib.Path, max_images: int
    ) -> list[str]:
        """Shared gallery downloader for products and published items."""
        clean = [u for u in urls if isinstance(u, str) and u.startswith("http")]
        if not clean:
            return []
        folder.mkdir(parents=True, exist_ok=True)
        saved: list[str] = []
        for i, url in enumerate(clean[:max_images]):
            ext = ".jpg"
            low = url.lower().split("?")[0]
            for e in (".png", ".webp", ".jpeg", ".jpg"):
                if low.endswith(e):
                    ext = e
                    break
            dest = folder / f"{i:02d}{ext}"
            if dest.exists():
                saved.append(str(dest))
                continue
            try:
                req = urllib.request.Request(
                    url,
                    headers={"User-Agent": "WeigouManager/1.0"},
                )
                with urllib.request.urlopen(req, timeout=25) as resp:
                    dest.write_bytes(resp.read())
                saved.append(str(dest))
            except Exception:
                continue
        return saved

    def resolve_local_images(
        self,
        product_id: int,
        *,
        paths: list[str] | None = None,
        cover_path: str = "",
    ) -> list[str]:
        """Return existing image file paths for a product.

        Recovers when DB still points at an old catalog location (e.g. Documents
        migrate) but files now live under ``images/<id>/``.
        """
        p = self.get(product_id)
        raw = list(paths if paths is not None else (p.image_paths if p else []))
        cover = (cover_path or (p.cover_path if p else "") or "").strip()
        folder = self.img_root / str(product_id)

        def resolve_one(path: str) -> str | None:
            s = (path or "").strip()
            if not s:
                return None
            cand = pathlib.Path(s)
            try:
                if cand.is_file():
                    return str(cand.resolve())
            except OSError:
                pass
            name = cand.name
            if name:
                alt = folder / name
                try:
                    if alt.is_file():
                        return str(alt.resolve())
                except OSError:
                    pass
            # relative to catalog root
            alt2 = self.root / s
            try:
                if alt2.is_file():
                    return str(alt2.resolve())
            except OSError:
                pass
            return None

        out: list[str] = []
        for path in raw:
            hit = resolve_one(path)
            if hit and hit not in out:
                out.append(hit)
        if not out and cover:
            hit = resolve_one(cover)
            if hit:
                out.append(hit)
        if not out and folder.is_dir():
            exts = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
            for f in sorted(folder.iterdir()):
                if f.is_file() and f.suffix.lower() in exts:
                    try:
                        out.append(str(f.resolve()))
                    except OSError:
                        out.append(str(f))
        return out

    def rewrite_product_image_paths(self, product_id: int, paths: list[str]) -> None:
        """Persist healed local paths after resolve (keeps cover + gallery in sync)."""
        if not paths:
            return
        with self._connect() as con:
            con.execute(
                "UPDATE products SET cover_path=?, image_paths=? WHERE id=?",
                (paths[0], json.dumps(paths, ensure_ascii=False), product_id),
            )

    def ensure_product_images(
        self,
        product_id: int,
        *,
        max_images: int = 20,
        urls: list[str] | None = None,
    ) -> list[str]:
        """Download product gallery images on demand and return local paths.

        Skips download when files already exist under ``images/<id>/`` even if
        DB paths point at an old location.
        """
        p = self.get(product_id)
        if not p:
            return []
        existing = self.resolve_local_images(
            product_id, paths=list(p.image_paths or []), cover_path=p.cover_path or ""
        )
        if existing:
            if existing != list(p.image_paths or []):
                self.rewrite_product_image_paths(product_id, existing)
            return existing
        src_urls = urls if urls is not None else list(p.image_urls or [])
        folder = self.img_root / str(product_id)
        saved = self._download_gallery(src_urls, folder, max_images)
        if not saved:
            return []
        with self._connect() as con:
            con.execute(
                "UPDATE products SET cover_path=?, image_paths=?, updated_at=? WHERE id=?",
                (
                    saved[0],
                    json.dumps(saved, ensure_ascii=False),
                    dt.datetime.now().isoformat(timespec="seconds"),
                    product_id,
                ),
            )
        self.write_product_txt(product_id)
        return saved

    def write_product_txt(self, product_id: int, folder: pathlib.Path | None = None) -> pathlib.Path | None:
        """Write/refresh ``product.txt`` next to gallery images (folder 열기용)."""
        p = self.get(product_id)
        if not p:
            return None
        dest_dir = folder or (self.img_root / str(product_id))
        dest_dir.mkdir(parents=True, exist_ok=True)
        meta = dest_dir / "product.txt"
        lines = [
            p.google_name or p.title or "",
            f"搜索码：{p.search_code}" if p.search_code else "",
            f"NO：{p.sku_no}" if p.sku_no else "",
            p.tags or "",
            f"카테고리：{p.category}" if p.category else "",
            f"컬러：{p.colors}" if p.colors else "",
            f"사이즈：{p.sizes}" if p.sizes else "",
            "",
            p.description or "",
            "",
            f"goods_id={p.goods_id}" if p.goods_id else "",
        ]
        try:
            meta.write_text(
                "\n".join(x for x in lines if x is not None).strip() + "\n",
                encoding="utf-8",
            )
        except OSError:
            return None
        return meta

    def ensure_published_images(
        self, published_id: int, max_images: int = 20
    ) -> list[str]:
        """Download published-item gallery images on demand and return local paths."""
        item = self.get_published(published_id)
        if not item:
            return []
        folder = self.published_img_root / f"p{published_id}"
        existing = [x for x in (item.image_paths or []) if pathlib.Path(x).exists()]
        if not existing and folder.is_dir():
            exts = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
            for f in sorted(folder.iterdir()):
                if f.is_file() and f.suffix.lower() in exts:
                    existing.append(str(f))
        if existing:
            return existing
        saved = self._download_gallery(list(item.image_urls or []), folder, max_images)
        if not saved:
            return []
        with self._connect() as con:
            con.execute(
                "UPDATE published SET cover_path=?, image_paths=? WHERE id=?",
                (saved[0], json.dumps(saved, ensure_ascii=False), published_id),
            )
        return saved

    def _ensure_images_for_product(self, product_id: int, urls: list[str]) -> None:
        """Legacy wrapper kept for backward compatibility; see ``ensure_product_images``."""
        self.ensure_product_images(product_id, urls=urls)

    def prune_image_cache(
        self, *, max_bytes: int = 8_000_000_000, keep_ids: set[int] | None = None
    ) -> int:
        """Delete oldest product image folders under img_root until under max_bytes.

        Folders are ranked by their most-recent file mtime (oldest first) and
        removed until the running total drops below ``max_bytes``. Folders
        whose numeric product id is in ``keep_ids`` are never removed.
        Returns the number of bytes freed.
        """
        keep = keep_ids or set()
        if not self.img_root.exists():
            return 0
        entries: list[tuple[float, int, pathlib.Path, int | None]] = []
        total = 0
        for folder in self.img_root.iterdir():
            if not folder.is_dir():
                continue
            try:
                pid: int | None = int(folder.name)
            except ValueError:
                pid = None
            size = 0
            mtime = 0.0
            for f in folder.rglob("*"):
                if not f.is_file():
                    continue
                try:
                    st = f.stat()
                except OSError:
                    continue
                size += st.st_size
                mtime = max(mtime, st.st_mtime)
            total += size
            entries.append((mtime, size, folder, pid))
        if total <= max_bytes:
            return 0
        entries.sort(key=lambda e: e[0])
        freed = 0
        for mtime, size, folder, pid in entries:
            if total - freed <= max_bytes:
                break
            if pid is not None and pid in keep:
                continue
            try:
                shutil.rmtree(folder, ignore_errors=True)
                freed += size
            except OSError:
                continue
        return freed

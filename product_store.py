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

CATEGORY_ORDER = ["가방", "신발", "상의", "하의", "자켓", "악세사리", "기타"]

# 갤러리 2번째 장은 배송사(중국우정/EMS 등) 안내인 경우가 많아 수집에서 제외
SKIP_GALLERY_INDEX = 1  # 0-based


def drop_second_gallery_image(urls: list[str]) -> list[str]:
    """Keep cover + remaining shots; drop the 2nd slide (logistics poster)."""
    if len(urls) <= SKIP_GALLERY_INDEX:
        return list(urls)
    return [u for i, u in enumerate(urls) if i != SKIP_GALLERY_INDEX]


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
    google_name: str = ""
    name_en: str = ""
    colors: str = ""
    sizes: str = ""
    description: str = ""
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
                ]
                for name, decl in pub_migrations:
                    if name not in pub_cols:
                        con.execute(f"ALTER TABLE published ADD COLUMN {name} {decl}")

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

    def list_products(self, query: str = "", category: str = "") -> list[Product]:
        q = (query or "").strip()
        cat = (category or "").strip()
        with self._connect() as con:
            sql = "SELECT * FROM products WHERE 1=1"
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
            sql += " ORDER BY id DESC"
            rows = con.execute(sql, args).fetchall()
        products = [self._row_to_product(r) for r in rows]

        def search_code_num(code: str) -> int:
            s = (code or "").strip()
            if not s:
                return 10**18  # 搜索码 없는 항목은 맨 뒤
            if s.isdigit():
                return int(s)
            m = re.match(r"(\d+)", s)
            return int(m.group(1)) if m else 10**18

        def sort_key(p: Product) -> tuple:
            return (search_code_num(p.search_code), -p.id)

        products.sort(key=sort_key)
        return products

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

    def delete(self, product_id: int) -> None:
        p = self.get(product_id)
        if not p:
            return
        folder = self.img_root / str(product_id)
        if folder.exists():
            shutil.rmtree(folder, ignore_errors=True)
        with self._connect() as con:
            con.execute("DELETE FROM products WHERE id=?", (product_id,))

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

    def list_excluded(self, query: str = "") -> list[ExcludedItem]:
        q = (query or "").strip()
        with self._connect() as con:
            sql = "SELECT * FROM excluded WHERE 1=1"
            args: list[str] = []
            if q:
                like = f"%{q}%"
                sql += (
                    " AND (title LIKE ? OR search_code LIKE ? OR sku_no LIKE ?"
                    " OR tags LIKE ? OR goods_id LIKE ? OR category LIKE ?)"
                )
                args.extend([like, like, like, like, like, like])
            sql += " ORDER BY id DESC"
            rows = con.execute(sql, args).fetchall()
        return [self._row_to_excluded(r) for r in rows]

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
            google_name=(row["google_name"] if "google_name" in keys else "") or "",
            name_en=(row["name_en"] if "name_en" in keys else "") or "",
            colors=(row["colors"] if "colors" in keys else "") or "",
            sizes=(row["sizes"] if "sizes" in keys else "") or "",
            description=(row["description"] if "description" in keys else "") or "",
            image_paths=jlist("image_paths"),
            image_urls=jlist("image_urls"),
        )

    def list_published(self, query: str = "") -> list[PublishedItem]:
        q = (query or "").strip()
        with self._connect() as con:
            sql = "SELECT * FROM published WHERE 1=1"
            args: list[str] = []
            if q:
                like = f"%{q}%"
                sql += (
                    " AND (title LIKE ? OR search_code LIKE ? OR sku_no LIKE ?"
                    " OR tags LIKE ? OR goods_id LIKE ? OR category LIKE ?"
                    " OR mall_id LIKE ? OR note LIKE ? OR google_name LIKE ?"
                    " OR name_en LIKE ? OR colors LIKE ?)"
                )
                args.extend([like, like, like, like, like, like, like, like, like, like, like])
            sql += " ORDER BY id DESC"
            rows = con.execute(sql, args).fetchall()
        return [self._row_to_published(r) for r in rows]

    def get_published(self, published_id: int) -> PublishedItem | None:
        with self._connect() as con:
            row = con.execute(
                "SELECT * FROM published WHERE id=?", (published_id,)
            ).fetchone()
        return self._row_to_published(row) if row else None

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
                 cover_path, note, mall_id, created_at,
                 google_name, name_en, colors, sizes, description,
                 image_paths, image_urls)
                VALUES (?, ?, ?, ?, ?, ?, ?, '', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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

        # 2번째 이미지(배송 안내 등)는 저장하지 않음
        before = len(parsed.image_urls)
        parsed.image_urls = drop_second_gallery_image(parsed.image_urls)
        if before >= 2 and len(parsed.image_urls) < before:
            log(f"2번째 이미지 제외 ({before} → {len(parsed.image_urls)}장)")

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

    def export_sync_bundle(self) -> dict:
        """Serialize catalog for multi-user GitHub sync (metadata + image URLs)."""

        def prod_row(p: Product) -> dict:
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

        def excl_row(e: ExcludedItem) -> dict:
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

        def pub_row(p: PublishedItem) -> dict:
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
                "image_urls": list(p.image_urls or []),
                "created_at": p.created_at,
            }

        products = [prod_row(p) for p in self.list_products("", "전체")]
        excluded = [excl_row(e) for e in self.list_excluded("")]
        published = [pub_row(p) for p in self.list_published("")]
        rev = int(self.get_setting("sync_rev", "0") or "0")
        return {
            "schema": 1,
            "rev": rev,
            "updated_at": dt.datetime.now().isoformat(timespec="seconds"),
            "products": products,
            "excluded": excluded,
            "published": published,
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

    def apply_sync_bundle(self, bundle: dict) -> dict[str, int]:
        """Merge remote catalog into local DB. Returns change counts."""
        if not isinstance(bundle, dict):
            return {"products": 0, "excluded": 0, "published": 0}
        now = dt.datetime.now().isoformat(timespec="seconds")
        stats = {"products": 0, "excluded": 0, "published": 0}

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
                    cur = con.execute(
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
                    new_id = int(cur.lastrowid)
                self._ensure_images_for_product(new_id, urls)
                stats["products"] += 1
                continue
            local = self.get(pid)
            if local and remote_u and local.updated_at and remote_u < local.updated_at:
                continue
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
                        remote_u or now,
                        pid,
                    ),
                )
            self._ensure_images_for_product(pid, urls)
            stats["products"] += 1

        for row in bundle.get("excluded") or []:
            if not isinstance(row, dict):
                continue
            gid = (row.get("goods_id") or "").strip()
            code = (row.get("search_code") or "").strip()
            if self.is_excluded(goods_id=gid, search_code=code):
                continue
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
                if exists:
                    continue
                con.execute(
                    """
                    INSERT INTO published
                    (goods_id, shop_id, search_code, sku_no, title, tags, category,
                     cover_path, note, mall_id, created_at,
                     google_name, name_en, colors, sizes, description,
                     image_paths, image_urls)
                    VALUES (?, ?, ?, ?, ?, ?, ?, '', ?, ?, ?, ?, ?, ?, ?, ?, '[]', ?)
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
                        row.get("google_name") or "",
                        row.get("name_en") or "",
                        row.get("colors") or "",
                        row.get("sizes") or "",
                        row.get("description") or "",
                        json.dumps(urls, ensure_ascii=False),
                    ),
                )
            stats["published"] += 1

        remote_rev = int(bundle.get("rev") or 0)
        local_rev = int(self.get_setting("sync_rev", "0") or "0")
        if remote_rev > local_rev:
            self.set_setting("sync_rev", str(remote_rev))
        return stats

    def bump_sync_rev(self) -> int:
        rev = int(self.get_setting("sync_rev", "0") or "0") + 1
        self.set_setting("sync_rev", str(rev))
        return rev

    def _ensure_images_for_product(self, product_id: int, urls: list[str]) -> None:
        """Download missing gallery images from synced URLs."""
        p = self.get(product_id)
        if not p:
            return
        existing = [x for x in (p.image_paths or []) if pathlib.Path(x).exists()]
        if existing:
            return
        urls = [u for u in urls if isinstance(u, str) and u.startswith("http")]
        if not urls:
            return
        folder = self.img_root / str(product_id)
        folder.mkdir(parents=True, exist_ok=True)
        saved: list[str] = []
        for i, url in enumerate(urls[:20]):
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
        if not saved:
            return
        cover = saved[0]
        with self._connect() as con:
            con.execute(
                "UPDATE products SET cover_path=?, image_paths=?, updated_at=? WHERE id=?",
                (
                    cover,
                    json.dumps(saved, ensure_ascii=False),
                    dt.datetime.now().isoformat(timespec="seconds"),
                    product_id,
                ),
            )

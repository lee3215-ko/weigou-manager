# -*- coding: utf-8 -*-
"""Weigou product manager — browse, search, import with description."""
from __future__ import annotations

import concurrent.futures
import datetime as dt
import io
import json
import os
import pathlib
import queue
import re
import threading
import time
import tkinter as tk
import urllib.request
import webbrowser
import zipfile
from tkinter import filedialog, messagebox, scrolledtext, ttk

# 로그 관리 탭 채널
LOG_COLLECT = "collect"  # 수집·가져오기·디버그
LOG_MALL = "mall"  # 홈페이지등록·추천·AI코디
LOG_SEARCH = "search"  # 이미지검색
LOG_CHANNELS = (LOG_COLLECT, LOG_MALL, LOG_SEARCH)
LOG_CHANNEL_LABELS = {
    LOG_COLLECT: "1. 수집",
    LOG_MALL: "2. 홈페이지/추천/AI코디",
    LOG_SEARCH: "3. 이미지검색",
}

from app_role import get_app_role, is_manager_role, role_label
from auto_collect import walk_list_details
from catalog_sync import (
    CatalogSyncService,
    ensure_sync_defaults,
    load_sync_settings,
    save_sync_settings,
)
from collector import collect_page_best, is_cdp_up
from google_lens import close_ai_browsers, search_product_images, search_products_multi
from launcher import DEFAULT_PORT, is_running, start_debug
from ime_win import (
    commit_composition,
    get_composition,
    restore_text_if_stripped,
    snapshot_widget_text,
)
from customers_ui import (
    customer_detail_text,
    filter_customers,
    format_customer_line,
)
from mall_cloud import (
    cloud_config_issue,
    cloud_enabled,
    delete_order_remote,
    ensure_cloud_settings,
    fetch_customers,
    fetch_orders,
    mall_customers_api,
    mall_orders_api,
    mall_product_page_url,
    mall_site_base,
    patch_member_points,
    patch_order,
)
from mall_publish import (
    _api_failed,
    delete_mall_products,
    preview_price,
    publish_product,
    push_published_metadata,
    recommend_slot_label,
    recommend_slots_for_category,
    reconcile_published_to_homepage,
    resolve_mall_id,
    set_products_recommended,
)
from orders_ui import (
    ORDER_STATUS_KO,
    ORDER_STATUS_VALUES,
    format_order_line,
    order_detail_text,
    product_id_digits,
    product_no,
    status_label,
)
from paths import (
    APP_DISPLAY_NAME,
    APP_NAME,
    APP_VERSION,
    EXE_NAME,
    RELEASE_ASSET,
    UPDATE_VERSION_URL,
    data_path,
    init_runtime_paths,
)
from price_codec import DEFAULT_PRICE_TEXT, effective_price_code
from product_attrs import extract_attrs, resolve_product_category
from product_name import is_clothing_category, ko_name_to_en
from product_parse import parse_products
from product_store import (
    CATEGORY_ORDER,
    ExcludedItem,
    Product,
    ProductStore,
    PublishedItem,
    default_root,
)
from style_publish import publish_style_look
from update_ui import schedule_update_check
from url_thumbs import fetch_thumb_file, prune_thumb_cache
from desktop_notify import alert as desktop_alert

init_runtime_paths()

try:
    from PIL import Image, ImageTk
except ImportError:  # pragma: no cover
    Image = None  # type: ignore
    ImageTk = None  # type: ignore


def re_split_csv(raw: str) -> list[str]:
    """Sizes etc.: comma / slash / pipe as option separators."""
    return [p.strip() for p in re.split(r"[,/|]+", raw or "") if p.strip()]


def re_split_colors(raw: str) -> list[str]:
    """색상: 쉼표(,)는 / 로 바꿔 한 색으로 인식. | 만 별도 옵션 구분.

    예) '화이트, 핑크' → ['화이트/핑크']
        '화이트/핑크' → ['화이트/핑크']
        '블랙|화이트' → ['블랙', '화이트']
    """
    s = (raw or "").strip()
    if not s:
        return []
    options: list[str] = []
    for part in re.split(r"\|+", s):
        part = part.strip()
        if not part:
            continue
        part = re.sub(r"\s*,\s*", "/", part)
        part = re.sub(r"\s*/\s*", "/", part)
        options.append(part)
    return options


class EntryField:
    """Entry-backed field without StringVar — keeps Hangul IME composition intact."""

    def __init__(self) -> None:
        self._entry: tk.Entry | None = None
        self._value = ""

    def attach(self, entry: tk.Entry) -> None:
        self._entry = entry
        entry.delete(0, tk.END)
        entry.insert(0, self._value)

    def get(self) -> str:
        if self._entry is not None:
            return self._entry.get()
        return self._value

    def set(self, value: str) -> None:
        self._value = value or ""
        if self._entry is None:
            return
        # Never rewrite the widget while the user is composing Hangul in it
        try:
            if self._entry.focus_get() is self._entry and getattr(
                self._entry, "_ime_composing", False
            ):
                return
        except Exception:
            pass
        cur = self._entry.get()
        if cur == self._value:
            return
        self._entry.delete(0, tk.END)
        self._entry.insert(0, self._value)


class ManagerApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(f"{APP_DISPLAY_NAME}  v{APP_VERSION}")
        self.geometry("1480x880")
        self.minsize(1280, 760)
        self.configure(bg="#f3efe8")

        self.store = ProductStore()
        self.products: list[Product] = []
        self.excluded_items: list[ExcludedItem] = []
        self.published_items: list[PublishedItem] = []
        self.current_id: int | None = None
        self.current_excluded_id: int | None = None
        self.current_published_id: int | None = None
        # Ctrl/Shift 다중 선택 시 상세(포커스)는 처음 선택한 상품 유지
        self._list_select_mods: frozenset[str] = frozenset()
        self.list_mode = tk.StringVar(value="products")  # products | excluded | published
        self._photo_cache: list[tk.PhotoImage] = []
        self._photo_refs: list[tk.PhotoImage] = []  # URL-thumbnail cache (separate from local-file cache)
        self._list_photos: dict[int, tk.PhotoImage] = {}
        self._log_q: queue.Queue[str | tuple[str, str]] = queue.Queue()
        self._log_widgets: dict[str, scrolledtext.ScrolledText] = {}
        self._log_recent: scrolledtext.ScrolledText | None = None
        # Independent background jobs — collect does NOT block publish/search
        self._jobs: set[str] = set()
        self._jobs_lock = threading.Lock()
        self._stop = threading.Event()
        self._cancel_job = threading.Event()
        self._collect_pause = threading.Event()  # set = paused
        self._form_loading = False
        self._ime_composing = False
        self._pending_soft_save = False
        self._detail_loaded_at = ""  # updated_at when form was loaded (sync overwrite guard)
        self._ime_focus_widget: tk.Misc | None = None
        self._select_gen = 0
        self._thumb_after: str | None = None
        self._attr_after: str | None = None
        self._collect_refresh_after: str | None = None
        self._job_labels = {
            "collect": "자동수집",
            "import": "가져오기",
            "search": "이미지검색",
            "publish": "홈페이지등록",
            "launch": "디버그실행",
        }
        self._job_log_channel = {
            "collect": LOG_COLLECT,
            "import": LOG_COLLECT,
            "launch": LOG_COLLECT,
            "publish": LOG_MALL,
            "search": LOG_SEARCH,
        }

        self.query = tk.StringVar()
        self.filter_category = tk.StringVar(value="전체")
        self.filter_recommended_only = tk.BooleanVar(value=False)
        self.filter_searched_only = tk.BooleanVar(value=False)
        # 자동수집 신규 한도 — 표시값 / 내부값(0=무제한)
        self.collect_limit_var = tk.StringVar(value="100건")
        self.status = tk.StringVar(value="준비됨")
        self.album_status = tk.StringVar(value="앨범: …")
        self.sync_status = tk.StringVar(value="목록: 연결 중…")
        self.sync_banner_var = tk.StringVar(
            value="클라우드 목록에 자동 연결합니다. 잠시만 기다려 주세요…"
        )
        self.title_var = EntryField()
        self.google_name_var = EntryField()
        self.name_en_var = EntryField()
        self.code_var = EntryField()
        self.sku_var = EntryField()
        self.tags_var = EntryField()
        self.color_var = EntryField()
        self.size_var = EntryField()
        self.category_var = tk.StringVar(value="가방")
        self.price_preview = tk.StringVar(value="")
        # AI 코디: 선택한 등록 상품 [{code, category, name, label}]
        self.ai_style_items: list[dict[str, str]] = []
        self.ai_model_image: pathlib.Path | None = None
        # 주문 관리
        self._orders: list[dict] = []
        self._order_selected_id: str | None = None
        self._orders_poll_after: str | None = None
        # 고객 관리
        self._customers_all: list[dict] = []
        self._customers_view: list[dict] = []
        self._customer_selected_key: str | None = None
        self.customer_query = tk.StringVar()
        self.customer_kind = tk.StringVar(value="all")
        self.customer_points_var = tk.StringVar(value="0")
        # 신규 주문/고객 알림 (소리 + 작업표시줄 풍선)
        self._watch_order_ids: set[str] | None = None
        self._watch_customer_keys: set[str] | None = None
        self._mall_watch_after: str | None = None
        self._mall_watch_interval_ms = 45_000
        self._load_mall_watch_seen()
        # 목록 새로고침 시에도 유지할 선택(상품/등록/제외 id)
        self._sticky_selected_ids: list[int] = []
        # 목록 페이지네이션 (대용량 카탈로그에서도 목록 로딩이 느려지지 않도록)
        self._list_page = 0
        self._list_page_size = 200
        self._list_total = 0
        self.list_page_var = tk.StringVar(value="")
        # 홈페이지 등록 대기열 (진행 중 추가 등록)
        self._publish_q: queue.Queue = queue.Queue()
        self._publish_lock = threading.Lock()
        self._publish_total = 0
        self._publish_done = 0
        self._publish_ok = 0
        self._publish_fail = 0
        self._publish_lines: list[str] = []
        self._publish_queued_ids: set[int] = set()
        self._publish_active_id: int | None = None
        self._publish_prog: dict | None = None
        self._publish_next_id: int | None = None
        self._publish_yview: tuple[float, float] | None = None
        # 이미지 검색 대기열 (진행 중 추가 검색)
        self._search_q: queue.Queue = queue.Queue()
        self._search_lock = threading.Lock()
        self._search_total = 0
        self._search_done = 0
        self._search_ok = 0
        self._search_fail = 0
        self._search_lines: list[str] = []
        self._search_queued_ids: set[int] = set()
        self._search_active_id: int | None = None
        self._search_prog: dict | None = None

        # Cloud-first: seed credentials + sync settings before UI (no manual sync click)
        ensure_cloud_settings(repair_invalid=True)
        ensure_sync_defaults()

        self._sync = CatalogSyncService(
            self.store,
            on_log=lambda m: self._put_log(m, channel=LOG_COLLECT),
            on_pulled=lambda: self.after(0, self._on_catalog_pulled),
            on_status=lambda st: self.after(0, lambda s=st: self._apply_sync_status(s)),
        )

        self._build()
        # 포커스가 빠지기 전에 한글 조합을 확정 (Alt+Tab / 다른 곳 클릭)
        self.bind_all("<ButtonPress>", self._on_global_pre_focus_change, add="+")
        self.bind_all("<KeyPress-Alt_L>", self._on_global_pre_focus_change, add="+")
        self.bind_all("<KeyPress-Alt_R>", self._on_global_pre_focus_change, add="+")
        self.bind("<Deactivate>", self._on_app_deactivate, add="+")
        self.refresh_list()
        self.after(200, self._poll_log)
        threading.Thread(target=self._status_loop, daemon=True).start()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        # Start sync ASAP — pull shared catalog without user pressing a button
        self.after(400, self._sync.start)
        self.after(4000, self._start_mall_watch)
        # Once per install/version: push missing 「등록」 to homepage + dedupe live catalog
        self.after(12000, self._maybe_auto_reconcile_homepage)
        # Every launch: copy 등록 필드(카테고리 등) onto homepage so they cannot drift
        self.after(8000, self._maybe_sync_homepage_metadata)
        self.after(
            1800,
            lambda: schedule_update_check(
                self,
                version_url=UPDATE_VERSION_URL,
                current_version=APP_VERSION,
                app_name=APP_NAME,
                exe_name=EXE_NAME,
                zip_inner_folder=APP_NAME,
                release_asset=RELEASE_ASSET,
                log_callback=lambda m: self._put_log(m, channel=LOG_COLLECT),
            ),
        )

    def _on_sku_typed(self, _event=None) -> None:
        if self._form_loading or self._ime_composing:
            return
        self._refresh_price_preview()

    def _build(self) -> None:
        self.main_nb = ttk.Notebook(self)
        self.main_nb.pack(fill="both", expand=True, padx=8, pady=8)

        page_products = tk.Frame(self.main_nb, bg="#f3efe8")
        page_logs = tk.Frame(self.main_nb, bg="#f3efe8")
        page_orders = tk.Frame(self.main_nb, bg="#f3efe8")
        page_customers = tk.Frame(self.main_nb, bg="#f3efe8")
        self.main_nb.add(page_products, text="  상품 관리  ")
        self.main_nb.add(page_orders, text="  주문 관리  ")
        self.main_nb.add(page_customers, text="  고객 관리  ")
        self.main_nb.add(page_logs, text="  로그 관리  ")

        # 로그 위젯을 먼저 만든 뒤 상품 페이지 안내 로그를 씀
        self._build_logs_page(page_logs)
        self._build_products_page(page_products)
        self._build_orders_page(page_orders)
        self._build_customers_page(page_customers)
        self.main_nb.bind("<<NotebookTabChanged>>", self._on_main_tab_changed)

    def _on_main_tab_changed(self, _event=None) -> None:
        try:
            tab = self.main_nb.index(self.main_nb.select())
        except Exception:
            return
        if tab == 1:
            self.refresh_orders()
        elif tab == 2:
            self.refresh_customers()

    def _build_orders_page(self, parent: tk.Frame) -> None:
        head = tk.Frame(parent, bg="#f3efe8")
        head.pack(fill="x", padx=12, pady=(10, 6))
        tk.Label(
            head,
            text="주문 관리",
            font=("Malgun Gothic", 16, "bold"),
            bg="#f3efe8",
            fg="#1f1a17",
        ).pack(side="left")
        self.orders_status = tk.StringVar(value="홈페이지 주문을 불러오세요")
        tk.Label(
            head,
            textvariable=self.orders_status,
            bg="#f3efe8",
            fg="#666",
            font=("Malgun Gothic", 9),
        ).pack(side="left", padx=(12, 0))
        tk.Button(
            head,
            text="새로고침",
            command=self.refresh_orders,
            font=("Malgun Gothic", 10, "bold"),
            bg="#1f4e79",
            fg="white",
            activebackground="#163a5c",
            relief="flat",
            padx=10,
        ).pack(side="right")
        tk.Button(
            head,
            text="관리자페이지 바로가기",
            command=self._open_admin_page,
            font=("Malgun Gothic", 10, "bold"),
            bg="#c45c26",
            fg="white",
            activebackground="#a34a1d",
            relief="flat",
            padx=10,
        ).pack(side="right", padx=(0, 8))

        body = tk.Frame(parent, bg="#f3efe8")
        body.pack(fill="both", expand=True, padx=12, pady=(0, 12))

        left = tk.Frame(body, bg="#f3efe8", width=520)
        left.pack(side="left", fill="both", expand=True)
        left.pack_propagate(True)
        self.orders_list = tk.Listbox(
            left,
            font=("Malgun Gothic", 10),
            activestyle="dotbox",
            bg="#fffdf9",
            relief="solid",
            borderwidth=1,
            exportselection=False,
        )
        self.orders_list.pack(fill="both", expand=True)
        self.orders_list.bind("<<ListboxSelect>>", self._on_order_select)

        right = tk.Frame(body, bg="#f3efe8", width=420)
        right.pack(side="left", fill="both", padx=(12, 0))
        right.pack_propagate(False)

        tk.Label(
            right, text="주문 상세", bg="#f3efe8", font=("Malgun Gothic", 11, "bold")
        ).pack(anchor="w")
        self.order_detail = scrolledtext.ScrolledText(
            right,
            height=18,
            font=("Malgun Gothic", 10),
            bg="#fffdf9",
            relief="solid",
            borderwidth=1,
            wrap="word",
        )
        self.order_detail.pack(fill="both", expand=True, pady=(6, 8))
        self.order_detail.configure(state="disabled")

        row = tk.Frame(right, bg="#f3efe8")
        row.pack(fill="x", pady=(0, 6))
        tk.Label(row, text="상태", bg="#f3efe8", font=("Malgun Gothic", 9)).pack(
            side="left"
        )
        self.order_status_var = tk.StringVar(value="pending")
        self.order_status_box = ttk.Combobox(
            row,
            textvariable=self.order_status_var,
            values=[f"{k} — {ORDER_STATUS_KO[k]}" for k in ORDER_STATUS_VALUES],
            state="readonly",
            font=("Malgun Gothic", 9),
            width=18,
        )
        self.order_status_box.pack(side="left", padx=6)
        tk.Button(
            row,
            text="상태 저장",
            command=self._on_order_status_save,
            font=("Malgun Gothic", 9, "bold"),
            bg="#c45c26",
            fg="white",
            relief="flat",
            padx=8,
        ).pack(side="left", padx=4)

        btns = tk.Frame(right, bg="#f3efe8")
        btns.pack(fill="x")
        tk.Button(
            btns,
            text="상품 페이지 바로가기",
            command=self._on_order_open_product_page,
            font=("Malgun Gothic", 9, "bold"),
            bg="#1f4e79",
            fg="white",
            activebackground="#163a5c",
            relief="flat",
            padx=8,
        ).pack(side="left")
        tk.Button(
            btns,
            text="상품코드 폴더 열기",
            command=self._on_order_open_code_folder,
            font=("Malgun Gothic", 9, "bold"),
            bg="#2f6b4f",
            fg="white",
            activebackground="#24553e",
            relief="flat",
            padx=8,
        ).pack(side="left", padx=6)
        tk.Button(
            btns,
            text="주문 삭제",
            command=self._on_order_delete,
            font=("Malgun Gothic", 9),
            bg="#ebe4da",
            relief="flat",
            padx=8,
        ).pack(side="left")
        self.orders_api_hint = tk.Label(
            right,
            text="",
            bg="#f3efe8",
            fg="#888",
            font=("Malgun Gothic", 8),
            wraplength=400,
            justify="left",
        )
        self.orders_api_hint.pack(anchor="w", pady=(8, 0))
        try:
            self.orders_api_hint.configure(text=f"API: {mall_orders_api()}")
        except Exception:
            pass

    def _open_admin_page(self) -> None:
        url = f"{mall_site_base().rstrip('/')}/admin"
        try:
            webbrowser.open(url)
            self._append(f"관리자페이지 열기: {url}", channel=LOG_MALL)
        except Exception as e:
            messagebox.showerror("관리자페이지", str(e) or "브라우저를 열 수 없습니다.")

    def refresh_orders(self) -> None:
        def work() -> None:
            try:
                orders = fetch_orders()
                self.after(0, lambda o=orders: self._apply_orders(o, ""))
            except Exception as e:
                msg = str(e).strip() or repr(e)
                self.after(0, lambda m=msg: self._apply_orders([], m))

        self.orders_status.set("주문 불러오는 중…")
        threading.Thread(target=work, daemon=True).start()

    def _apply_orders(self, orders: list, err: str) -> None:
        self._orders = list(orders or [])
        keep_id = self._order_selected_id
        self.orders_list.delete(0, tk.END)
        for o in self._orders:
            if isinstance(o, dict):
                self.orders_list.insert(tk.END, format_order_line(o))
        if err:
            self.orders_status.set(f"오류: {err}")
            self._set_order_detail("주문을 불러오지 못했습니다.\n" + err)
            return
        self.orders_status.set(f"주문 {len(self._orders)}건 · {mall_orders_api()}")
        if keep_id:
            for i, o in enumerate(self._orders):
                if str(o.get("id") or "") == keep_id:
                    self.orders_list.selection_set(i)
                    self.orders_list.see(i)
                    self._show_order(o)
                    return
        if self._orders:
            self.orders_list.selection_set(0)
            self._show_order(self._orders[0])
        else:
            self._order_selected_id = None
            self._set_order_detail("주문이 없습니다.")

    def _on_order_select(self, _event=None) -> None:
        sel = self.orders_list.curselection()
        if not sel:
            return
        idx = int(sel[0])
        if 0 <= idx < len(self._orders):
            self._show_order(self._orders[idx])

    def _show_order(self, order: dict) -> None:
        self._order_selected_id = str(order.get("id") or "") or None
        st = str(order.get("status") or "pending")
        label = f"{st} — {status_label(st)}"
        if label in self.order_status_box.cget("values"):
            self.order_status_var.set(label)
        else:
            self.order_status_var.set(f"pending — {status_label('pending')}")
        self._set_order_detail(order_detail_text(order))

    def _set_order_detail(self, text: str) -> None:
        self.order_detail.configure(state="normal")
        self.order_detail.delete("1.0", tk.END)
        self.order_detail.insert("1.0", text)
        self.order_detail.configure(state="disabled")

    def _selected_order(self) -> dict | None:
        if not self._order_selected_id:
            return None
        for o in self._orders:
            if str(o.get("id") or "") == self._order_selected_id:
                return o
        return None

    def _on_order_open_product_page(self) -> None:
        order = self._selected_order()
        if not order:
            messagebox.showwarning("주문", "주문을 선택하세요.")
            return
        # 몰 상품 URL은 원래 productId(wg-123 등) 사용 — 표시만 숫자
        pid = str(order.get("productId") or "").strip()
        if not pid:
            digits = product_id_digits(order)
            pid = f"wg-{digits}" if digits else ""
        url = mall_product_page_url(pid)
        if not url:
            messagebox.showwarning("상품 페이지", "주문에 상품ID가 없습니다.")
            return
        try:
            webbrowser.open(url)
        except Exception as e:
            messagebox.showerror("상품 페이지", f"열기 실패: {e}\n{url}")

    def _on_order_open_code_folder(self) -> None:
        """상품NO(搜索码)로 로컬 등록 이미지 폴더 열기."""
        order = self._selected_order()
        if not order:
            messagebox.showwarning("주문", "주문을 선택하세요.")
            return
        code = product_no(order)
        if not code:
            messagebox.showwarning(
                "폴더 열기",
                "주문에 상품NO(검색코드)가 없습니다.",
            )
            return
        path = self._resolve_folder_by_product_code(code)
        if path is None:
            messagebox.showwarning(
                "폴더 열기",
                f"상품NO 「{code}」에 해당하는 등록/수집 폴더를 찾지 못했습니다.",
            )
            return
        try:
            path.mkdir(parents=True, exist_ok=True)
            os.startfile(str(path))  # type: ignore[attr-defined]
        except Exception as e:
            messagebox.showerror("폴더 열기", f"{path}\n{e}")

    def _resolve_folder_by_product_code(self, code: str) -> pathlib.Path | None:
        code = (code or "").strip()
        if not code:
            return None
        # 1) 등록(published) 우선 — published_covers/p#
        pub = self.store.find_published_by_search_code(code)
        if pub:
            pack = self.store.published_img_root / f"p{pub.id}"
            if pack.is_dir():
                return pack
            for cand in ([pub.cover_path] if pub.cover_path else []) + list(
                pub.image_paths or []
            ):
                try:
                    fp = pathlib.Path(cand)
                    if fp.is_file():
                        return fp.parent
                    if fp.is_dir():
                        return fp
                except Exception:
                    continue
        # 2) 수집 products
        prod = self.store.find_product_by_search_code(code)
        if prod:
            folder = self.store.img_root / str(prod.id)
            if folder.is_dir():
                return folder
            for cand in ([prod.cover_path] if prod.cover_path else []) + list(
                prod.image_paths or []
            ):
                try:
                    fp = pathlib.Path(cand)
                    if fp.is_file():
                        return fp.parent
                    if fp.is_dir():
                        return fp
                except Exception:
                    continue
        return None

    def _on_order_status_save(self) -> None:
        order = self._selected_order()
        if not order:
            messagebox.showwarning("주문", "주문을 선택하세요.")
            return
        raw = (self.order_status_var.get() or "").split("—", 1)[0].strip()
        if raw not in ORDER_STATUS_VALUES:
            messagebox.showwarning("주문", "상태를 선택하세요.")
            return
        oid = str(order.get("id") or "")

        def work() -> None:
            try:
                patch_order(oid, status=raw)
                self.after(0, lambda: self._append(
                    f"주문 상태 변경: {oid} → {status_label(raw)}",
                    channel=LOG_MALL,
                ))
                self.after(0, self.refresh_orders)
                self.after(
                    0,
                    lambda: messagebox.showinfo(
                        "완료", f"상태를 «{status_label(raw)}»로 저장했습니다."
                    ),
                )
            except Exception as e:
                self.after(0, lambda: messagebox.showerror("오류", str(e)))

        threading.Thread(target=work, daemon=True).start()

    def _on_order_delete(self) -> None:
        order = self._selected_order()
        if not order:
            messagebox.showwarning("주문", "주문을 선택하세요.")
            return
        oid = str(order.get("id") or "")
        if not messagebox.askyesno("삭제", f"주문 {oid} 을(를) 삭제할까요?"):
            return

        def work() -> None:
            try:
                delete_order_remote(oid)
                self.after(0, lambda: self._append(
                    f"주문 삭제: {oid}", channel=LOG_MALL
                ))
                self._order_selected_id = None
                self.after(0, self.refresh_orders)
            except Exception as e:
                self.after(0, lambda: messagebox.showerror("오류", str(e)))

        threading.Thread(target=work, daemon=True).start()

    def _build_customers_page(self, parent: tk.Frame) -> None:
        head = tk.Frame(parent, bg="#f3efe8")
        head.pack(fill="x", padx=12, pady=(10, 6))
        tk.Label(
            head,
            text="고객 관리",
            font=("Malgun Gothic", 16, "bold"),
            bg="#f3efe8",
            fg="#1f1a17",
        ).pack(side="left")
        self.customers_status = tk.StringVar(value="회원·비회원 고객을 불러오세요")
        tk.Label(
            head,
            textvariable=self.customers_status,
            bg="#f3efe8",
            fg="#666",
            font=("Malgun Gothic", 9),
        ).pack(side="left", padx=(12, 0))
        tk.Button(
            head,
            text="새로고침",
            command=self.refresh_customers,
            font=("Malgun Gothic", 10, "bold"),
            bg="#1f4e79",
            fg="white",
            activebackground="#163a5c",
            relief="flat",
            padx=10,
        ).pack(side="right")
        tk.Button(
            head,
            text="관리자페이지 바로가기",
            command=self._open_admin_page,
            font=("Malgun Gothic", 10, "bold"),
            bg="#c45c26",
            fg="white",
            activebackground="#a34a1d",
            relief="flat",
            padx=10,
        ).pack(side="right", padx=(0, 8))

        filt = tk.Frame(parent, bg="#f3efe8")
        filt.pack(fill="x", padx=12, pady=(0, 6))
        tk.Label(
            filt, text="검색", bg="#f3efe8", font=("Malgun Gothic", 9)
        ).pack(side="left")
        ent = tk.Entry(
            filt,
            textvariable=self.customer_query,
            font=("Malgun Gothic", 11),
            width=28,
        )
        ent.pack(side="left", padx=(6, 10))
        ent.bind("<Return>", lambda _e: self._apply_customer_filter())
        self._bind_ime_safe_entry(ent)
        for label, val in (("전체", "all"), ("회원", "member"), ("비회원", "guest")):
            tk.Radiobutton(
                filt,
                text=label,
                variable=self.customer_kind,
                value=val,
                command=self._apply_customer_filter,
                bg="#f3efe8",
                activebackground="#f3efe8",
                font=("Malgun Gothic", 9),
            ).pack(side="left", padx=(0, 6))
        tk.Button(
            filt,
            text="검색",
            command=self._apply_customer_filter,
            font=("Malgun Gothic", 9, "bold"),
            bg="#2f6b4f",
            fg="white",
            relief="flat",
            padx=10,
        ).pack(side="left", padx=(4, 0))

        body = tk.Frame(parent, bg="#f3efe8")
        body.pack(fill="both", expand=True, padx=12, pady=(0, 12))

        left = tk.Frame(body, bg="#f3efe8")
        left.pack(side="left", fill="both", expand=True)
        list_wrap = tk.Frame(left, bg="#f3efe8")
        list_wrap.pack(fill="both", expand=True)
        self.customers_list = tk.Listbox(
            list_wrap,
            font=("Malgun Gothic", 10),
            activestyle="dotbox",
            bg="#fffdf9",
            relief="solid",
            borderwidth=1,
            exportselection=False,
        )
        cust_scroll = ttk.Scrollbar(
            list_wrap, orient="vertical", command=self.customers_list.yview
        )
        self.customers_list.configure(yscrollcommand=cust_scroll.set)
        self.customers_list.pack(side="left", fill="both", expand=True)
        cust_scroll.pack(side="right", fill="y")
        self.customers_list.bind("<<ListboxSelect>>", self._on_customer_select)

        def _cust_wheel(event: tk.Event) -> str | None:
            if getattr(event, "num", None) == 4:
                self.customers_list.yview_scroll(-3, "units")
            elif getattr(event, "num", None) == 5:
                self.customers_list.yview_scroll(3, "units")
            else:
                delta = int(getattr(event, "delta", 0) or 0)
                if delta:
                    self.customers_list.yview_scroll(int(-delta / 120), "units")
            return "break"

        self.customers_list.bind("<MouseWheel>", _cust_wheel)
        self.customers_list.bind("<Button-4>", _cust_wheel)
        self.customers_list.bind("<Button-5>", _cust_wheel)

        right = tk.Frame(body, bg="#f3efe8", width=440)
        right.pack(side="left", fill="both", padx=(12, 0))
        right.pack_propagate(False)
        tk.Label(
            right, text="고객 상세", bg="#f3efe8", font=("Malgun Gothic", 11, "bold")
        ).pack(anchor="w")
        self.customer_detail = scrolledtext.ScrolledText(
            right,
            height=18,
            font=("Malgun Gothic", 10),
            bg="#fffdf9",
            relief="solid",
            borderwidth=1,
            wrap="word",
        )
        self.customer_detail.pack(fill="both", expand=True, pady=(6, 8))
        self.customer_detail.configure(state="disabled")

        pts_row = tk.Frame(right, bg="#f3efe8")
        pts_row.pack(fill="x", pady=(0, 6))
        tk.Label(
            pts_row, text="마일리지", bg="#f3efe8", font=("Malgun Gothic", 9)
        ).pack(side="left")
        pts_ent = tk.Entry(
            pts_row,
            textvariable=self.customer_points_var,
            font=("Malgun Gothic", 11),
            width=12,
        )
        pts_ent.pack(side="left", padx=6)
        tk.Button(
            pts_row,
            text="마일리지 저장",
            command=self._on_customer_points_save,
            font=("Malgun Gothic", 9, "bold"),
            bg="#c45c26",
            fg="white",
            relief="flat",
            padx=8,
        ).pack(side="left")
        tk.Label(
            right,
            text="회원만 마일리지 수정 가능 · 이름·전화·주문으로 검색",
            bg="#f3efe8",
            fg="#888",
            font=("Malgun Gothic", 8),
            wraplength=420,
            justify="left",
        ).pack(anchor="w")
        try:
            tk.Label(
                right,
                text=f"API: {mall_customers_api()}",
                bg="#f3efe8",
                fg="#888",
                font=("Malgun Gothic", 8),
                wraplength=420,
                justify="left",
            ).pack(anchor="w", pady=(6, 0))
        except Exception:
            pass

    def refresh_customers(self) -> None:
        def work() -> None:
            try:
                customers = fetch_customers()
                self.after(0, lambda c=customers: self._apply_customers(c, ""))
            except Exception as e:
                msg = str(e).strip() or repr(e)
                self.after(0, lambda m=msg: self._apply_customers([], m))

        self.customers_status.set("고객 불러오는 중…")
        threading.Thread(target=work, daemon=True).start()

    def _apply_customers(self, customers: list, err: str) -> None:
        self._customers_all = list(customers or [])
        if err:
            self.customers_status.set(f"오류: {err}")
            self._customers_view = []
            self.customers_list.delete(0, tk.END)
            self._set_customer_detail("고객을 불러오지 못했습니다.\n" + err)
            return
        self._apply_customer_filter()

    def _apply_customer_filter(self) -> None:
        keep = self._customer_selected_key
        kind = (self.customer_kind.get() or "all").strip()
        self._customers_view = filter_customers(
            self._customers_all,
            query=self.customer_query.get() or "",
            kind=kind,
        )
        self.customers_list.delete(0, tk.END)
        for c in self._customers_view:
            self.customers_list.insert(tk.END, format_customer_line(c))
        members = sum(1 for c in self._customers_view if c.get("kind") == "member")
        guests = len(self._customers_view) - members
        self.customers_status.set(
            f"표시 {len(self._customers_view)}명 (회원 {members} · 비회원 {guests}) · 전체 {len(self._customers_all)}명"
        )
        if keep:
            for i, c in enumerate(self._customers_view):
                if str(c.get("key") or "") == keep:
                    self.customers_list.selection_set(i)
                    self.customers_list.see(i)
                    self._show_customer(c)
                    return
        if self._customers_view:
            self.customers_list.selection_set(0)
            self._show_customer(self._customers_view[0])
        else:
            self._customer_selected_key = None
            self.customer_points_var.set("0")
            self._set_customer_detail("검색 결과가 없습니다.")

    def _on_customer_select(self, _event=None) -> None:
        sel = self.customers_list.curselection()
        if not sel:
            return
        idx = int(sel[0])
        if 0 <= idx < len(self._customers_view):
            self._show_customer(self._customers_view[idx])

    def _show_customer(self, customer: dict) -> None:
        self._customer_selected_key = str(customer.get("key") or "") or None
        self.customer_points_var.set(str(int(customer.get("points") or 0)))
        self._set_customer_detail(customer_detail_text(customer))

    def _set_customer_detail(self, text: str) -> None:
        self.customer_detail.configure(state="normal")
        self.customer_detail.delete("1.0", tk.END)
        self.customer_detail.insert("1.0", text)
        self.customer_detail.configure(state="disabled")

    def _selected_customer(self) -> dict | None:
        if not self._customer_selected_key:
            return None
        for c in self._customers_view:
            if str(c.get("key") or "") == self._customer_selected_key:
                return c
        for c in self._customers_all:
            if str(c.get("key") or "") == self._customer_selected_key:
                return c
        return None

    def _on_customer_points_save(self) -> None:
        customer = self._selected_customer()
        if not customer:
            messagebox.showwarning("고객", "고객을 선택하세요.")
            return
        if customer.get("kind") != "member" or not customer.get("userId"):
            messagebox.showwarning(
                "마일리지",
                "비회원은 마일리지 저장이 없습니다.\n(회원 계정에만 저장됩니다)",
            )
            return
        raw = (self.customer_points_var.get() or "").strip().replace(",", "")
        try:
            pts = int(raw)
        except ValueError:
            messagebox.showwarning("마일리지", "숫자로 입력하세요.")
            return
        uid = str(customer.get("userId") or "")

        def work() -> None:
            try:
                patch_member_points(uid, pts)
                self.after(
                    0,
                    lambda: self._append(
                        f"고객 마일리지 변경: {customer.get('name') or uid} → {pts:,}P",
                        channel=LOG_MALL,
                    ),
                )
                self.after(0, self.refresh_customers)
                self.after(
                    0,
                    lambda: messagebox.showinfo(
                        "완료", f"마일리지를 {pts:,}P 로 저장했습니다."
                    ),
                )
            except Exception as e:
                self.after(0, lambda: messagebox.showerror("오류", str(e)))

        threading.Thread(target=work, daemon=True).start()

    def _build_logs_page(self, parent: tk.Frame) -> None:
        head = tk.Frame(parent, bg="#f3efe8")
        head.pack(fill="x", padx=8, pady=(8, 4))
        tk.Label(
            head,
            text="로그 관리",
            font=("Malgun Gothic", 16, "bold"),
            bg="#f3efe8",
            fg="#1f1a17",
        ).pack(side="left")
        tk.Label(
            head,
            text="수집 · 홈페이지/추천/AI코디 · 이미지검색 로그를 나눠 봅니다",
            bg="#f3efe8",
            fg="#666",
            font=("Malgun Gothic", 9),
        ).pack(side="left", padx=(12, 0))
        tk.Button(
            head,
            text="현재 탭 지우기",
            command=self._clear_current_log_tab,
            font=("Malgun Gothic", 9),
            bg="#ebe4da",
            relief="flat",
            padx=8,
        ).pack(side="right", padx=4)
        tk.Button(
            head,
            text="전체 지우기",
            command=self._clear_all_logs,
            font=("Malgun Gothic", 9),
            bg="#ebe4da",
            relief="flat",
            padx=8,
        ).pack(side="right", padx=4)

        self.log_nb = ttk.Notebook(parent)
        self.log_nb.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self._log_widgets = {}
        self._log_progress: dict[str, dict] = {}
        default_idle = {
            LOG_COLLECT: "수집 대기",
            LOG_MALL: "홈페이지 등록 대기",
            LOG_SEARCH: "이미지 검색 대기",
        }
        for key in LOG_CHANNELS:
            frame = tk.Frame(self.log_nb, bg="#f3efe8")
            self.log_nb.add(frame, text=f"  {LOG_CHANNEL_LABELS[key]}  ")

            prog_wrap = tk.Frame(frame, bg="#f3efe8")
            prog_wrap.pack(fill="x", padx=6, pady=(6, 2))
            title_var = tk.StringVar(value=default_idle.get(key, "대기"))
            detail_var = tk.StringVar(value="")
            tk.Label(
                prog_wrap,
                textvariable=title_var,
                bg="#f3efe8",
                fg="#1f1a17",
                font=("Malgun Gothic", 10, "bold"),
                anchor="w",
            ).pack(fill="x")
            bar = ttk.Progressbar(
                prog_wrap,
                maximum=100,
                mode="determinate",
                value=0,
            )
            bar.pack(fill="x", pady=(4, 2))
            tk.Label(
                prog_wrap,
                textvariable=detail_var,
                bg="#f3efe8",
                fg="#666",
                font=("Malgun Gothic", 8),
                anchor="w",
            ).pack(fill="x")
            self._log_progress[key] = {
                "title": title_var,
                "detail": detail_var,
                "bar": bar,
                "idle": default_idle.get(key, "대기"),
                "total": 0,
                "done": 0,
            }

            txt = scrolledtext.ScrolledText(
                frame,
                font=("Consolas", 10),
                bg="#fffdf9",
                relief="solid",
                borderwidth=1,
                wrap="word",
            )
            txt.pack(fill="both", expand=True, padx=4, pady=(2, 4))
            txt.configure(state="disabled")
            self._log_widgets[key] = txt

        # 상품관리 하단 미리보기와 동일 버퍼를 쓰도록 alias
        self.log = self._log_widgets[LOG_COLLECT]

    def _build_products_page(self, parent: tk.Frame) -> None:
        top = tk.Frame(parent, bg="#f3efe8")
        top.pack(fill="x", padx=12, pady=10)

        tk.Label(
            top,
            text="상품 관리",
            font=("Malgun Gothic", 16, "bold"),
            bg="#f3efe8",
            fg="#1f1a17",
        ).pack(side="left")
        self.job_banner_var = tk.StringVar(value="")
        self._job_banner_bits: dict[str, str] = {}
        tk.Label(
            top,
            textvariable=self.job_banner_var,
            font=("Malgun Gothic", 11, "bold"),
            bg="#f3efe8",
            fg="#c45c26",
            anchor="w",
        ).pack(side="left", padx=(14, 8))

        tk.Button(
            top,
            text="동기화 설정",
            command=self._on_sync_settings,
            font=("Malgun Gothic", 10),
            bg="#ebe4da",
        ).pack(side="right", padx=4)
        self.btn_launch = tk.Button(
            top,
            text="디버그 실행",
            command=self._on_launch,
            font=("Malgun Gothic", 10),
            bg="#ebe4da",
        )
        self.btn_launch.pack(side="right", padx=4)

        # Cloud catalog banner — separate from album CDP connection
        self._sync_banner = tk.Label(
            parent,
            textvariable=self.sync_banner_var,
            font=("Malgun Gothic", 10, "bold"),
            bg="#fff7ed",
            fg="#9a3412",
            anchor="w",
            padx=12,
            pady=6,
        )
        self._sync_banner.pack(fill="x", padx=12, pady=(0, 4))
        tk.Button(
            top,
            text="선택된 이미지 검색",
            command=self._on_google_selected,
            font=("Malgun Gothic", 10),
            bg="#ebe4da",
        ).pack(side="right", padx=4)
        tk.Button(
            top,
            text="중지",
            command=self._on_cancel_job,
            font=("Malgun Gothic", 10),
            bg="#ebe4da",
        ).pack(side="right", padx=4)
        self.btn_auto_collect = tk.Button(
            top,
            text="목록→상세 자동수집",
            command=self._on_auto_collect,
            font=("Malgun Gothic", 10, "bold"),
            bg="#1f4e79",
            fg="white",
            activebackground="#163a5c",
            activeforeground="white",
            relief="flat",
            padx=10,
        )
        self.btn_auto_collect.pack(side="right", padx=4)
        self.collect_opt = tk.Frame(top, bg="#f3efe8")
        self.collect_opt.pack(side="right", padx=(4, 2))
        tk.Label(
            self.collect_opt,
            text="수집개수",
            bg="#f3efe8",
            font=("Malgun Gothic", 9),
        ).pack(side="left")
        self.collect_limit_box = ttk.Combobox(
            self.collect_opt,
            textvariable=self.collect_limit_var,
            values=("30건", "50건", "100건", "200건", "300건", "무제한"),
            state="readonly",
            font=("Malgun Gothic", 9),
            width=7,
        )
        self.collect_limit_box.pack(side="left", padx=(4, 0))
        self.collect_limit_box.bind("<<ComboboxSelected>>", self._on_collect_limit_changed)
        # 저장된 선택값 복원
        try:
            saved = (self.store.get_setting("collect_limit", "100") or "100").strip()
            label_map = {
                "30": "30건",
                "50": "50건",
                "100": "100건",
                "200": "200건",
                "300": "300건",
                "0": "무제한",
                "무제한": "무제한",
            }
            self.collect_limit_var.set(label_map.get(saved, "100건"))
        except Exception:
            pass
        self.btn_import = tk.Button(
            top,
            text="현재 화면 가져오기",
            command=self._on_import,
            font=("Malgun Gothic", 11, "bold"),
            bg="#c45c26",
            fg="white",
            activebackground="#a64c1f",
            activeforeground="white",
            relief="flat",
            padx=12,
        )
        self.btn_import.pack(side="right", padx=4)

        body = tk.Frame(parent, bg="#f3efe8")
        body.pack(fill="both", expand=True, padx=12, pady=(0, 8))

        # Left pane — 목록 제목이 잘리지 않도록 넓게
        left = tk.Frame(body, bg="#f3efe8", width=480)
        left.pack(side="left", fill="y")
        left.pack_propagate(False)

        search_row = tk.Frame(left, bg="#f3efe8")
        search_row.pack(fill="x", pady=(0, 6))
        ent = tk.Entry(search_row, textvariable=self.query, font=("Malgun Gothic", 10))
        ent.pack(side="left", fill="x", expand=True)
        ent.bind("<Return>", lambda _e: self._reset_list_page_and_refresh())
        tk.Button(
            search_row,
            text="검색",
            command=self._reset_list_page_and_refresh,
            font=("Malgun Gothic", 9),
        ).pack(side="left", padx=(6, 0))

        filter_row = tk.Frame(left, bg="#f3efe8")
        filter_row.pack(fill="x", pady=(0, 6))
        tk.Label(filter_row, text="분류", bg="#f3efe8", font=("Malgun Gothic", 9)).pack(side="left")
        self.filter_box = ttk.Combobox(
            filter_row,
            textvariable=self.filter_category,
            values=["전체", *CATEGORY_ORDER],
            state="readonly",
            font=("Malgun Gothic", 9),
            width=10,
        )
        self.filter_box.pack(side="left", padx=6)
        self.filter_box.bind(
            "<<ComboboxSelected>>", lambda _e: self._reset_list_page_and_refresh()
        )
        self.chk_recommended_only = tk.Checkbutton(
            filter_row,
            text="추천만",
            variable=self.filter_recommended_only,
            command=self.refresh_list,
            bg="#f3efe8",
            activebackground="#f3efe8",
            font=("Malgun Gothic", 9),
            state="disabled",
        )
        self.chk_recommended_only.pack(side="left", padx=(10, 0))
        self.chk_searched_only = tk.Checkbutton(
            filter_row,
            text="검색완료만",
            variable=self.filter_searched_only,
            command=self._reset_list_page_and_refresh,
            bg="#f3efe8",
            activebackground="#f3efe8",
            font=("Malgun Gothic", 9),
        )
        self.chk_searched_only.pack(side="left", padx=(8, 0))

        mode_row = tk.Frame(left, bg="#f3efe8")
        mode_row.pack(fill="x", pady=(0, 6))
        tk.Radiobutton(
            mode_row,
            text="상품",
            variable=self.list_mode,
            value="products",
            command=self._on_list_mode,
            bg="#f3efe8",
            font=("Malgun Gothic", 9),
            activebackground="#f3efe8",
        ).pack(side="left")
        tk.Radiobutton(
            mode_row,
            text="등록",
            variable=self.list_mode,
            value="published",
            command=self._on_list_mode,
            bg="#f3efe8",
            font=("Malgun Gothic", 9),
            activebackground="#f3efe8",
        ).pack(side="left", padx=(8, 0))
        tk.Radiobutton(
            mode_row,
            text="제외",
            variable=self.list_mode,
            value="excluded",
            command=self._on_list_mode,
            bg="#f3efe8",
            font=("Malgun Gothic", 9),
            activebackground="#f3efe8",
        ).pack(side="left", padx=(8, 0))
        list_wrap = tk.Frame(left, bg="#f3efe8")
        list_wrap.pack(fill="both", expand=True)
        self.listbox = tk.Listbox(
            list_wrap,
            font=("Malgun Gothic", 10),
            activestyle="dotbox",
            selectmode=tk.EXTENDED,  # Ctrl/Shift 다중 선택 → 일괄 등록·제외
            bg="#fffdf9",
            relief="solid",
            borderwidth=1,
            exportselection=False,
        )
        list_scroll = ttk.Scrollbar(
            list_wrap,
            orient="vertical",
            command=self.listbox.yview,
        )
        self.listbox.configure(yscrollcommand=list_scroll.set)
        self.listbox.pack(side="left", fill="both", expand=True)
        list_scroll.pack(side="right", fill="y")
        self.listbox.bind("<Button-1>", self._on_list_button1, add="+")
        self.listbox.bind("<KeyPress>", self._on_list_keypress, add="+")
        self.listbox.bind("<<ListboxSelect>>", self._on_select)

        def _on_list_mousewheel(event: tk.Event) -> str | None:
            # Windows: event.delta 120 단위 / Linux: Button-4/5
            if getattr(event, "num", None) == 4:
                self.listbox.yview_scroll(-3, "units")
            elif getattr(event, "num", None) == 5:
                self.listbox.yview_scroll(3, "units")
            else:
                delta = int(getattr(event, "delta", 0) or 0)
                if delta:
                    self.listbox.yview_scroll(int(-delta / 120), "units")
            return "break"

        self.listbox.bind("<MouseWheel>", _on_list_mousewheel)
        self.listbox.bind("<Button-4>", _on_list_mousewheel)
        self.listbox.bind("<Button-5>", _on_list_mousewheel)
        self.list_hint = tk.Label(
            left,
            text="Ctrl·Shift 클릭으로 여러 개 선택 → 등록/제외/AI코디",
            bg="#f3efe8",
            fg="#666",
            font=("Malgun Gothic", 8),
            anchor="w",
        )
        self.list_hint.pack(fill="x", pady=(4, 0))

        page_row = tk.Frame(left, bg="#f3efe8")
        page_row.pack(fill="x", pady=(4, 0))
        tk.Button(
            page_row,
            text="◀ 이전",
            command=self._on_list_prev_page,
            font=("Malgun Gothic", 8),
            bg="#ebe4da",
            relief="flat",
            padx=6,
        ).pack(side="left")
        tk.Button(
            page_row,
            text="다음 ▶",
            command=self._on_list_next_page,
            font=("Malgun Gothic", 8),
            bg="#ebe4da",
            relief="flat",
            padx=6,
        ).pack(side="left", padx=(4, 0))
        tk.Label(
            page_row,
            textvariable=self.list_page_var,
            bg="#f3efe8",
            fg="#666",
            font=("Malgun Gothic", 8),
        ).pack(side="left", padx=(8, 0))

        # Right pane
        right = tk.Frame(body, bg="#f3efe8")
        right.pack(side="left", fill="both", expand=True, padx=(12, 0))

        form = tk.Frame(right, bg="#f3efe8")
        form.pack(fill="x")

        def labeled(row: int, label: str, field: EntryField) -> tk.Entry:
            tk.Label(form, text=label, bg="#f3efe8", font=("Malgun Gothic", 9), width=8, anchor="w").grid(
                row=row, column=0, sticky="w", pady=3
            )
            ent = tk.Entry(form, font=("Malgun Gothic", 11))
            ent.grid(row=row, column=1, sticky="ew", pady=3)
            field.attach(ent)
            self._bind_ime_safe_entry(ent)
            return ent

        form.columnconfigure(1, weight=1)
        labeled(0, "제목", self.title_var)

        # 제품명(한)
        tk.Label(form, text="제품명(한)", bg="#f3efe8", font=("Malgun Gothic", 9), width=8, anchor="w").grid(
            row=1, column=0, sticky="w", pady=3
        )
        ko_ent = tk.Entry(form, font=("Malgun Gothic", 11))
        ko_ent.grid(row=1, column=1, sticky="ew", pady=3)
        self.google_name_var.attach(ko_ent)
        self._bind_ime_safe_entry(ko_ent)

        # 제품명(영) + 한글→영어 번역 버튼
        tk.Label(form, text="제품명(영)", bg="#f3efe8", font=("Malgun Gothic", 9), width=8, anchor="w").grid(
            row=2, column=0, sticky="w", pady=3
        )
        en_row = tk.Frame(form, bg="#f3efe8")
        en_row.grid(row=2, column=1, sticky="ew", pady=3)
        en_row.columnconfigure(0, weight=1)
        en_ent = tk.Entry(en_row, font=("Malgun Gothic", 11))
        en_ent.grid(row=0, column=0, sticky="ew")
        self.name_en_var.attach(en_ent)
        self._bind_ime_safe_entry(en_ent)
        tk.Button(
            en_row,
            text="한→영",
            command=self._on_translate_ko_en,
            font=("Malgun Gothic", 9, "bold"),
            bg="#1f4e79",
            fg="white",
            activebackground="#163a5c",
            activeforeground="white",
            relief="flat",
            padx=8,
        ).grid(row=0, column=1, padx=(6, 0))

        labeled(3, "搜索码(NO)", self.code_var)
        sku_ent = labeled(4, "가격코드", self.sku_var)
        sku_ent.bind("<KeyRelease>", self._on_sku_typed)
        labeled(5, "태그", self.tags_var)
        labeled(6, "컬러", self.color_var)
        labeled(7, "사이즈", self.size_var)

        cat_row = tk.Frame(form, bg="#f3efe8")
        cat_row.grid(row=8, column=0, columnspan=2, sticky="ew", pady=3)
        tk.Label(cat_row, text="카테고리", bg="#f3efe8", font=("Malgun Gothic", 9), width=8, anchor="w").pack(
            side="left"
        )
        self.category_box = ttk.Combobox(
            cat_row,
            textvariable=self.category_var,
            values=CATEGORY_ORDER,
            state="readonly",
            font=("Malgun Gothic", 10),
            width=16,
        )
        self.category_box.pack(side="left")
        self.category_box.bind("<<ComboboxSelected>>", self._on_category_chosen)
        tk.Button(
            cat_row,
            text="자동인식",
            command=lambda: self._auto_fill_attrs(silent=False),
            font=("Malgun Gothic", 9),
        ).pack(side="left", padx=8)
        tk.Button(
            cat_row,
            text="같은 태그 일괄수정",
            command=self._on_bulk_category_by_tag,
            font=("Malgun Gothic", 9),
            bg="#ebe4da",
            relief="flat",
        ).pack(side="left", padx=4)
        tk.Button(
            cat_row,
            text="이미지로 제품명 찾기",
            command=self._on_google_one,
            font=("Malgun Gothic", 9, "bold"),
            bg="#c45c26",
            fg="white",
            activebackground="#a64c1f",
            activeforeground="white",
            relief="flat",
        ).pack(side="left", padx=4)
        tk.Button(
            cat_row,
            text="두번째 이미지 검색",
            command=self._on_google_second,
            font=("Malgun Gothic", 9, "bold"),
            bg="#1f4e79",
            fg="white",
            activebackground="#163a5c",
            activeforeground="white",
            relief="flat",
        ).pack(side="left", padx=4)

        tk.Label(
            right,
            textvariable=self.price_preview,
            bg="#f3efe8",
            fg="#9a3412",
            font=("Malgun Gothic", 10, "bold"),
            anchor="w",
            wraplength=780,
            justify="left",
        ).pack(fill="x", pady=(6, 0))

        tk.Label(right, text="상품 설명", bg="#f3efe8", font=("Malgun Gothic", 9), anchor="w").pack(
            fill="x", pady=(8, 2)
        )
        self.desc = scrolledtext.ScrolledText(
            right, height=4, font=("Malgun Gothic", 11), bg="#fffdf9", relief="solid", borderwidth=1
        )
        self.desc.pack(fill="x")
        self._bind_ime_safe_text(self.desc)

        btn_row = tk.Frame(right, bg="#f3efe8")
        btn_row.pack(fill="x", pady=8)
        self.btn_save = tk.Button(
            btn_row, text="설명 저장", command=self._on_save, font=("Malgun Gothic", 10), bg="#ebe4da"
        )
        self.btn_save.pack(side="left")
        self.btn_publish = tk.Button(
            btn_row,
            text="홈페이지 등록",
            command=self._on_publish,
            font=("Malgun Gothic", 10, "bold"),
            bg="#121212",
            fg="white",
            activebackground="#333",
            activeforeground="white",
            relief="flat",
            padx=10,
        )
        self.btn_publish.pack(side="left", padx=6)
        self.btn_exclude = tk.Button(
            btn_row,
            text="제외",
            command=self._on_exclude,
            font=("Malgun Gothic", 10, "bold"),
            bg="#7c2d12",
            fg="white",
            activebackground="#5c210d",
            activeforeground="white",
            relief="flat",
            padx=10,
        )
        self.btn_exclude.pack(side="left", padx=6)
        self.btn_merge_images = tk.Button(
            btn_row,
            text="이미지 합치기",
            command=self._on_merge_images,
            font=("Malgun Gothic", 10, "bold"),
            bg="#0f766e",
            fg="white",
            activebackground="#0d9488",
            activeforeground="white",
            relief="flat",
            padx=10,
        )
        self.btn_merge_images.pack(side="left", padx=6)
        self.btn_unexclude = tk.Button(
            btn_row,
            text="제외 해제",
            command=self._on_unexclude,
            font=("Malgun Gothic", 10, "bold"),
            bg="#166534",
            fg="white",
            activebackground="#14532d",
            activeforeground="white",
            relief="flat",
            padx=10,
        )
        self.btn_unexclude.pack(side="left", padx=6)
        self.btn_unpublish = tk.Button(
            btn_row,
            text="등록목록에서 제거",
            command=self._on_unpublish,
            font=("Malgun Gothic", 10, "bold"),
            bg="#166534",
            fg="white",
            activebackground="#14532d",
            activeforeground="white",
            relief="flat",
            padx=10,
        )
        self.btn_unpublish.pack(side="left", padx=6)
        self.btn_republish = tk.Button(
            btn_row,
            text="재등록",
            command=self._on_republish,
            font=("Malgun Gothic", 10, "bold"),
            bg="#c45c26",
            fg="white",
            activebackground="#a64c1f",
            activeforeground="white",
            relief="flat",
            padx=10,
        )
        self.btn_republish.pack(side="left", padx=6)
        self.btn_reconcile_site = tk.Button(
            btn_row,
            text="사이트 전체 맞추기",
            command=self._on_reconcile_homepage,
            font=("Malgun Gothic", 10, "bold"),
            bg="#1f4e79",
            fg="white",
            activebackground="#163a5c",
            activeforeground="white",
            relief="flat",
            padx=10,
        )
        self.btn_reconcile_site.pack(side="left", padx=6)
        self.btn_recommend = tk.Button(
            btn_row,
            text="추천상품으로 재등록하기",
            command=self._on_recommend,
            font=("Malgun Gothic", 10, "bold"),
            bg="#9f1239",
            fg="white",
            activebackground="#881337",
            activeforeground="white",
            relief="flat",
            padx=10,
        )
        self.btn_recommend.pack(side="left", padx=6)
        self.btn_unrecommend = tk.Button(
            btn_row,
            text="추천상품 해제",
            command=self._on_unrecommend,
            font=("Malgun Gothic", 10, "bold"),
            bg="#57534e",
            fg="white",
            activebackground="#44403c",
            activeforeground="white",
            relief="flat",
            padx=10,
        )
        self.btn_unrecommend.pack(side="left", padx=6)
        self.btn_ai_select = tk.Button(
            btn_row,
            text="AI 상품선택",
            command=self._on_ai_select,
            font=("Malgun Gothic", 10, "bold"),
            bg="#6b21a8",
            fg="white",
            activebackground="#581c87",
            activeforeground="white",
            relief="flat",
            padx=10,
        )
        self.btn_ai_select.pack(side="left", padx=6)
        self.btn_ai_apply = tk.Button(
            btn_row,
            text="AI 코디 만들기",
            command=self._on_ai_style_dialog,
            font=("Malgun Gothic", 10, "bold"),
            bg="#c026d3",
            fg="white",
            activebackground="#a21caf",
            activeforeground="white",
            relief="flat",
            padx=10,
        )
        self.btn_ai_apply.pack(side="left", padx=6)
        self.btn_delete = tk.Button(
            btn_row, text="삭제", command=self._on_delete, font=("Malgun Gothic", 10)
        )
        self.btn_delete.pack(side="left", padx=6)
        tk.Button(
            btn_row, text="폴더 열기", command=self._open_folder, font=("Malgun Gothic", 10)
        ).pack(side="left")
        self.btn_unexclude.pack_forget()
        self.btn_unpublish.pack_forget()
        self.btn_republish.pack_forget()
        self.btn_reconcile_site.pack_forget()
        self.btn_recommend.pack_forget()
        self.btn_unrecommend.pack_forget()
        self.btn_ai_select.pack_forget()
        self.btn_ai_apply.pack_forget()

        tk.Label(right, text="이미지", bg="#f3efe8", font=("Malgun Gothic", 9), anchor="w").pack(fill="x")
        canvas_wrap = tk.Frame(right, bg="#fffdf9", relief="solid", borderwidth=1)
        canvas_wrap.pack(fill="both", expand=True)
        self.img_canvas = tk.Canvas(canvas_wrap, bg="#fffdf9", highlightthickness=0)
        scroll = ttk.Scrollbar(canvas_wrap, orient="vertical", command=self.img_canvas.yview)
        self.img_canvas.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.img_canvas.pack(side="left", fill="both", expand=True)
        self.img_frame = tk.Frame(self.img_canvas, bg="#fffdf9")
        self._img_window = self.img_canvas.create_window((0, 0), window=self.img_frame, anchor="nw")
        self.img_frame.bind(
            "<Configure>", lambda _e: self.img_canvas.configure(scrollregion=self.img_canvas.bbox("all"))
        )
        self.img_canvas.bind(
            "<Configure>", lambda e: self.img_canvas.itemconfigure(self._img_window, width=e.width)
        )

        def _on_img_mousewheel(event: tk.Event) -> str | None:
            if getattr(event, "num", None) == 4:
                self.img_canvas.yview_scroll(-3, "units")
            elif getattr(event, "num", None) == 5:
                self.img_canvas.yview_scroll(3, "units")
            else:
                delta = int(getattr(event, "delta", 0) or 0)
                if delta:
                    # Windows: delta ±120 per notch; some devices send smaller values
                    steps = -1 if delta > 0 else 1
                    if abs(delta) >= 120:
                        steps = int(-delta / 120)
                    self.img_canvas.yview_scroll(steps, "units")
            return "break"

        def _img_wheel_bind(_event=None) -> None:
            self.img_canvas.bind_all("<MouseWheel>", _on_img_mousewheel)
            self.img_canvas.bind_all("<Button-4>", _on_img_mousewheel)
            self.img_canvas.bind_all("<Button-5>", _on_img_mousewheel)

        def _img_wheel_unbind(event: tk.Event | None = None) -> None:
            # Leave only when pointer actually left the image area
            if event is not None:
                try:
                    under = self.winfo_containing(event.x_root, event.y_root)
                except tk.TclError:
                    under = None
                w = under
                while w is not None:
                    if w in (canvas_wrap, self.img_canvas, self.img_frame):
                        return
                    w = getattr(w, "master", None)
            self.img_canvas.unbind_all("<MouseWheel>")
            self.img_canvas.unbind_all("<Button-4>")
            self.img_canvas.unbind_all("<Button-5>")

        self._bind_img_mousewheel = _on_img_mousewheel
        self._img_wheel_bind = _img_wheel_bind
        self._img_wheel_unbind = _img_wheel_unbind
        for w in (canvas_wrap, self.img_canvas, self.img_frame):
            w.bind("<Enter>", _img_wheel_bind)
            w.bind("<Leave>", _img_wheel_unbind)
            w.bind("<MouseWheel>", _on_img_mousewheel)
            w.bind("<Button-4>", _on_img_mousewheel)
            w.bind("<Button-5>", _on_img_mousewheel)

        bottom = tk.Frame(parent, bg="#f3efe8")
        bottom.pack(fill="x", padx=12, pady=(0, 8))
        status_row = tk.Frame(bottom, bg="#f3efe8")
        status_row.pack(fill="x")
        self._album_status_lbl = tk.Label(
            status_row,
            textvariable=self.album_status,
            bg="#f3efe8",
            fg="#b91c1c",
            font=("Malgun Gothic", 9, "bold"),
        )
        self._album_status_lbl.pack(side="left", anchor="w")
        tk.Label(
            status_row,
            text=" | ",
            bg="#f3efe8",
            fg="#999",
            font=("Malgun Gothic", 9),
        ).pack(side="left")
        self._sync_status_lbl = tk.Label(
            status_row,
            textvariable=self.sync_status,
            bg="#f3efe8",
            fg="#9a3412",
            font=("Malgun Gothic", 9, "bold"),
        )
        self._sync_status_lbl.pack(side="left", anchor="w")
        tk.Label(
            status_row,
            textvariable=self.status,
            bg="#f3efe8",
            fg="#2d6a4f",
            font=("Malgun Gothic", 9),
        ).pack(side="left", anchor="w", padx=(10, 0))
        tk.Button(
            status_row,
            text="로그 관리 열기",
            command=lambda: self.main_nb.select(3),
            font=("Malgun Gothic", 8),
            bg="#ebe4da",
            relief="flat",
            padx=6,
        ).pack(side="right")
        tk.Button(
            status_row,
            text="고객 관리 열기",
            command=lambda: self.main_nb.select(2),
            font=("Malgun Gothic", 8),
            bg="#ebe4da",
            relief="flat",
            padx=6,
        ).pack(side="right", padx=(0, 4))
        tk.Button(
            status_row,
            text="주문 관리 열기",
            command=lambda: self.main_nb.select(1),
            font=("Malgun Gothic", 8),
            bg="#ebe4da",
            relief="flat",
            padx=6,
        ).pack(side="right", padx=(0, 4))
        self._log_recent = scrolledtext.ScrolledText(
            bottom, height=4, font=("Consolas", 9), bg="#fffdf9"
        )
        self._log_recent.pack(fill="x", pady=(4, 0))
        self._append(
            f"저장 위치: {default_root()}\n"
            "1) 디버그 실행 → 2) 친구 앨범 목록 열기 → 3) [목록→상세 자동수집]\n"
            "   · 맨 위부터 PageDown · 하단 로딩 대기 · 상단 [수집개수]로 한도 선택\n"
            "   · 이미 수집·제외·등록은 상품ID로 패스 · 이미지 1~2장은 제외 목록으로\n"
            "   · 수집 중 버튼 = 일시정지 / 수집 계속 · 완전 종료는 [중지]\n"
            "4) [이미지로 제품명 찾기] → 1번째 이미지로 제품명·카테고리 기록\n"
            "   [두번째 이미지 검색] → 선택 상품을 하나씩 2번째 이미지로 검색\n"
            "5) NO·컬러·사이즈 확인 후 [홈페이지 등록] → 등록 목록으로 이동\n"
            "   (제품명 한/영·컬러·사이즈 함께 저장됨)\n"
            "[제외]/[등록] 상품은 이후 자동/수동 수집에서 건너뜁니다.\n"
            "[등록목록에서 제거] 시 홈페이지 상품도 삭제되고 상품 관리 목록으로 복원됩니다.\n"
            "[등록] 탭: 상품 선택 → [AI 상품선택] → [AI 코디 만들기]에서 모델 이미지 업로드 후 홈페이지 적용\n"
            "상세 로그는 상단 [로그 관리] 탭에서 채널별로 확인할 수 있습니다.\n",
            channel=LOG_COLLECT,
        )
        self._append(
            "홈페이지 등록 · 추천 등록/해제 · AI 코디 로그가 여기에 쌓입니다.\n",
            channel=LOG_MALL,
        )
        self._append(
            "이미지로 제품명 찾기 · 선택 이미지 검색 로그가 여기에 쌓입니다.\n",
            channel=LOG_SEARCH,
        )
        self._apply_role_ui()

    def _apply_role_ui(self) -> None:
        """Manager(B) role hides collect/debug/import controls — those PCs only curate and publish."""
        widgets = (self.btn_launch, self.btn_import, self.btn_auto_collect, self.collect_opt)
        if is_manager_role():
            for w in widgets:
                w.pack_forget()
        else:
            # Re-pack in the same relative side="right" order used at build time
            self.btn_launch.pack(side="right", padx=4)
            self.btn_auto_collect.pack(side="right", padx=4)
            self.collect_opt.pack(side="right", padx=(4, 2))
            self.btn_import.pack(side="right", padx=4)

    def _on_close(self) -> None:
        self._stop.set()
        if self._mall_watch_after is not None:
            try:
                self.after_cancel(self._mall_watch_after)
            except Exception:
                pass
            self._mall_watch_after = None
        try:
            self._sync.stop()
        except Exception:
            pass
        self.destroy()

    def _mall_watch_seen_path(self) -> pathlib.Path:
        return pathlib.Path(data_path("mall_watch_seen.json"))

    def _load_mall_watch_seen(self) -> None:
        path = self._mall_watch_seen_path()
        try:
            if not path.is_file():
                return
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                return
            orders = raw.get("orders") or []
            customers = raw.get("customers") or []
            if isinstance(orders, list) and orders:
                self._watch_order_ids = {str(x) for x in orders if str(x).strip()}
            if isinstance(customers, list) and customers:
                self._watch_customer_keys = {
                    str(x) for x in customers if str(x).strip()
                }
        except Exception:
            pass

    def _save_mall_watch_seen(self) -> None:
        try:
            payload = {
                "orders": sorted(self._watch_order_ids or []),
                "customers": sorted(self._watch_customer_keys or []),
                "updated_at": dt.datetime.now().isoformat(timespec="seconds"),
            }
            self._mall_watch_seen_path().write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        except Exception:
            pass

    def _start_mall_watch(self) -> None:
        """Background poll for new homepage orders / customers."""
        if self._stop.is_set():
            return
        self._mall_watch_tick()

    def _mall_watch_tick(self) -> None:
        if self._stop.is_set():
            return
        threading.Thread(target=self._mall_watch_work, daemon=True).start()
        try:
            self._mall_watch_after = self.after(
                self._mall_watch_interval_ms, self._mall_watch_tick
            )
        except Exception:
            self._mall_watch_after = None

    def _mall_watch_work(self) -> None:
        orders: list | None = None
        customers: list | None = None
        try:
            orders = fetch_orders()
        except Exception:
            pass
        try:
            customers = fetch_customers()
        except Exception:
            pass
        if self._stop.is_set():
            return
        self.after(0, lambda: self._mall_watch_apply(orders, customers))

    def _mall_watch_apply(
        self, orders: list | None, customers: list | None
    ) -> None:
        if self._stop.is_set():
            return
        changed_seen = False

        if orders is not None:
            order_ids = {
                str(o.get("id") or "").strip()
                for o in orders
                if isinstance(o, dict) and str(o.get("id") or "").strip()
            }
            if self._watch_order_ids is None:
                self._watch_order_ids = set(order_ids)
                changed_seen = True
            else:
                new_order_ids = order_ids - self._watch_order_ids
                if new_order_ids:
                    self._watch_order_ids |= order_ids
                    changed_seen = True
                    samples = []
                    for o in orders:
                        if not isinstance(o, dict):
                            continue
                        oid = str(o.get("id") or "").strip()
                        if oid not in new_order_ids:
                            continue
                        name = str(
                            o.get("customerName") or o.get("name") or ""
                        ).strip()
                        code = product_no(o) or str(o.get("productId") or "")
                        samples.append(f"{name or '고객'} · {code or oid}")
                        if len(samples) >= 3:
                            break
                    body = f"새 주문 {len(new_order_ids)}건"
                    if samples:
                        body += "\n" + "\n".join(samples)
                    desktop_alert("새 주문", body)
                    self._put_log(
                        f"[알림] 새 주문 {len(new_order_ids)}건", channel=LOG_MALL
                    )
                    try:
                        self.deiconify()
                        self.lift()
                    except Exception:
                        pass
                else:
                    self._watch_order_ids |= order_ids

        if customers is not None:
            customer_keys = {
                str(c.get("key") or "").strip()
                for c in customers
                if isinstance(c, dict) and str(c.get("key") or "").strip()
            }
            if self._watch_customer_keys is None:
                self._watch_customer_keys = set(customer_keys)
                changed_seen = True
            else:
                new_customer_keys = customer_keys - self._watch_customer_keys
                if new_customer_keys:
                    self._watch_customer_keys |= customer_keys
                    changed_seen = True
                    samples = []
                    for c in customers:
                        if not isinstance(c, dict):
                            continue
                        key = str(c.get("key") or "").strip()
                        if key not in new_customer_keys:
                            continue
                        kind = "회원" if c.get("kind") == "member" else "비회원"
                        name = str(c.get("name") or "").strip() or "(이름 없음)"
                        phone = str(c.get("phone") or "").strip()
                        samples.append(
                            f"{kind} {name}" + (f" · {phone}" if phone else "")
                        )
                        if len(samples) >= 3:
                            break
                    body = f"새 고객 {len(new_customer_keys)}명"
                    if samples:
                        body += "\n" + "\n".join(samples)
                    desktop_alert("새 고객", body)
                    self._put_log(
                        f"[알림] 새 고객 {len(new_customer_keys)}명",
                        channel=LOG_MALL,
                    )
                    try:
                        self.deiconify()
                        self.lift()
                    except Exception:
                        pass
                else:
                    self._watch_customer_keys |= customer_keys

        if changed_seen:
            self._save_mall_watch_seen()
        self._mall_watch_refresh_ui(orders, customers)

    def _mall_watch_refresh_ui(
        self, orders: list | None, customers: list | None
    ) -> None:
        """If user is on orders/customers tab, refresh list without status flicker."""
        try:
            tab = self.main_nb.index(self.main_nb.select())
        except Exception:
            return
        if tab == 1 and orders is not None:
            self._apply_orders(list(orders), "")
        elif tab == 2 and customers is not None:
            self._apply_customers(list(customers), "")

    def _mark_catalog_dirty(self, *, push_now: bool = False) -> None:
        try:
            self._sync.mark_dirty()
            if push_now:
                self._sync.sync_now()
        except Exception:
            pass

    def _apply_sync_status(self, st: dict | None = None) -> None:
        """Update sync banner + status chip from CatalogSyncService."""
        try:
            st = st or self._sync.get_status()
        except Exception:
            st = {"state": "error", "detail": "동기화 상태 확인 실패"}
        state = str(st.get("state") or "idle")
        detail = str(st.get("detail") or "")
        err = str(st.get("last_error") or "")
        age = st.get("last_ok_age_sec")
        issue = cloud_config_issue()

        if state == "ok":
            age_txt = ""
            if isinstance(age, (int, float)) and age is not None:
                if age < 5:
                    age_txt = " · 방금"
                elif age < 120:
                    age_txt = f" · {int(age)}초 전"
                else:
                    age_txt = f" · {int(age // 60)}분 전"
            self.sync_status.set(f"목록: 동기화됨{age_txt}")
            self.sync_banner_var.set(
                f"클라우드 목록 공유 중{age_txt} — 다른 PC와 자동으로 맞춰집니다. (수동 동기화 불필요)"
            )
            try:
                self._sync_banner.configure(bg="#ecfdf5", fg="#166534")
                self._sync_status_lbl.configure(fg="#166534")
            except Exception:
                pass
            return

        if state in ("starting", "syncing", "idle"):
            self.sync_status.set(f"목록: {detail or '연결 중…'}")
            self.sync_banner_var.set(
                detail or "클라우드 목록에 자동 연결합니다. 잠시만 기다려 주세요…"
            )
            try:
                self._sync_banner.configure(bg="#fff7ed", fg="#9a3412")
                self._sync_status_lbl.configure(fg="#9a3412")
            except Exception:
                pass
            return

        if state == "disabled":
            msg = "목록 동기화가 꺼져 있습니다. [동기화 설정]에서 켜 주세요."
            self.sync_status.set("목록: 동기화 꺼짐")
            self.sync_banner_var.set(msg)
        elif state == "no_cloud":
            msg = issue or detail or "클라우드 설정 없음"
            self.sync_status.set("목록: 이 PC만 (동기화 안 됨)")
            self.sync_banner_var.set(
                f"⚠ {msg} — 지금 보이는 목록은 이 PC에만 있습니다."
            )
        else:
            msg = err or detail or issue or "동기화 오류"
            self.sync_status.set("목록: 동기화 실패")
            self.sync_banner_var.set(
                f"⚠ {msg[:120]} — 이 PC 목록과 다른 PC가 다를 수 있습니다."
            )
        try:
            self._sync_banner.configure(bg="#fef2f2", fg="#b91c1c")
            self._sync_status_lbl.configure(fg="#b91c1c")
        except Exception:
            pass

    def _on_catalog_pulled(self) -> None:
        """Refresh list after remote sync; reload open detail if DB is newer than the form."""
        try:
            self._apply_sync_status()
        except Exception:
            pass
        cur_id = self.current_id
        cur_pub = self.current_published_id
        mode = self.list_mode.get()
        self.refresh_list(reload_detail=False, quiet=True)
        if self._form_loading or self._ime_composing:
            return
        try:
            if mode == "products" and cur_id is not None:
                p = self.store.get(cur_id)
                if p and (
                    not self._detail_loaded_at
                    or (
                        (p.updated_at or "")
                        and (p.updated_at or "") > self._detail_loaded_at
                    )
                ):
                    self._show_product(p)
            elif mode == "published" and cur_pub is not None:
                item = self.store.get_published(cur_pub)
                if item and (
                    not self._detail_loaded_at
                    or (
                        (item.updated_at or "")
                        and (item.updated_at or "") > self._detail_loaded_at
                    )
                ):
                    self._show_published(item)
        except Exception:
            pass

    def _remember_detail_loaded(self, updated_at: str | None) -> None:
        self._detail_loaded_at = (updated_at or "").strip()

    def _db_newer_than_form(self, updated_at: str | None) -> bool:
        db_ts = (updated_at or "").strip()
        if not db_ts or not self._detail_loaded_at:
            return False
        return db_ts > self._detail_loaded_at

    def _on_sync_settings(self) -> None:
        from mall_cloud import load_cloud_settings

        ensure_cloud_settings(repair_invalid=True)
        cfg = load_sync_settings()
        cloud = load_cloud_settings()
        win = tk.Toplevel(self)
        win.title("동기화 설정")
        win.geometry("540x460")
        win.configure(bg="#f3efe8")
        win.transient(self)
        win.grab_set()
        tk.Label(
            win,
            text="목록은 클라우드가 본체입니다. 프로그램을 켜면 자동으로 맞춰집니다.\n"
            "보통은 이 창을 열 필요가 없습니다. (PC 이름·역할만 바꾸면 됩니다)",
            bg="#f3efe8",
            font=("Malgun Gothic", 9),
            justify="left",
            anchor="w",
        ).pack(fill="x", padx=16, pady=(14, 8))

        enabled = tk.BooleanVar(value=bool(cfg.get("enabled", True)))
        tk.Checkbutton(
            win,
            text="동기화 사용 (권장: 항상 켜기)",
            variable=enabled,
            bg="#f3efe8",
            font=("Malgun Gothic", 10),
        ).pack(anchor="w", padx=16)

        issue = cloud_config_issue()
        if cloud_enabled() and not issue:
            status = "Supabase 연결됨 — 자동 동기화 동작 중"
            status_fg = "#166534"
        elif cloud_enabled() and issue:
            status = f"설정 문제: {issue}"
            status_fg = "#b91c1c"
        else:
            status = issue or "Supabase 미설정 — bundled/mall_cloud.json 확인"
            status_fg = "#b91c1c"
        tk.Label(
            win,
            text=status,
            bg="#f3efe8",
            fg=status_fg,
            font=("Malgun Gothic", 9, "bold"),
            anchor="w",
            wraplength=500,
            justify="left",
        ).pack(fill="x", padx=16, pady=(4, 2))
        url = (cloud.get("supabaseUrl") or "").strip()
        if url:
            tk.Label(
                win,
                text=url,
                bg="#f3efe8",
                fg="#666",
                font=("Consolas", 8),
                anchor="w",
            ).pack(fill="x", padx=16)

        def row(label: str, value: str) -> tk.Entry:
            fr = tk.Frame(win, bg="#f3efe8")
            fr.pack(fill="x", padx=16, pady=4)
            tk.Label(fr, text=label, width=14, anchor="w", bg="#f3efe8").pack(side="left")
            e = tk.Entry(fr, font=("Malgun Gothic", 10))
            e.pack(side="left", fill="x", expand=True)
            e.insert(0, value)
            return e

        e_interval = row("받기 주기(초)", str(cfg.get("interval_sec") or 2))
        e_device = row("이 PC 이름", str(cfg.get("device_name") or ""))
        tk.Label(
            win,
            text="※ 내 변경은 저장 직후 바로 올리고, 위 주기는 다른 PC 변경을 받아오는 간격입니다.\n"
            "※ 기본 2초면 거의 실시간입니다. (Supabase라 GitHub처럼 막히지 않습니다)",
            bg="#f3efe8",
            fg="#555",
            font=("Malgun Gothic", 8),
            anchor="w",
            justify="left",
        ).pack(fill="x", padx=16, pady=(2, 0))

        role_row = tk.Frame(win, bg="#f3efe8")
        role_row.pack(fill="x", padx=16, pady=(10, 0))
        tk.Label(role_row, text="PC 역할", width=14, anchor="w", bg="#f3efe8").pack(side="left")
        role_to_code = {"전체(A)": "full", "관리(B)": "manager"}
        code_to_role = {v: k for k, v in role_to_code.items()}
        role_var = tk.StringVar(value=code_to_role.get(get_app_role(), "전체(A)"))
        role_box = ttk.Combobox(
            role_row,
            textvariable=role_var,
            values=["전체(A)", "관리(B)"],
            state="readonly",
            font=("Malgun Gothic", 10),
            width=12,
        )
        role_box.pack(side="left")
        tk.Label(
            win,
            text="※ 관리(B)는 수집·디버그 숨김, 이미지는 필요할 때만 다운로드",
            bg="#f3efe8",
            fg="#555",
            font=("Malgun Gothic", 8),
            anchor="w",
            justify="left",
        ).pack(fill="x", padx=16, pady=(2, 0))

        def save_and_close() -> None:
            try:
                interval = int(float(e_interval.get().strip() or "2"))
            except ValueError:
                interval = 2
            new_cfg = {
                **cfg,
                "enabled": bool(enabled.get()),
                "backend": "supabase",
                "interval_sec": max(1, min(60, interval)),
                "device_name": e_device.get().strip(),
                "role": role_to_code.get(role_var.get(), "full"),
            }
            save_sync_settings(new_cfg)
            self._mark_catalog_dirty()
            self._sync.force_full_sync()
            self._apply_role_ui()
            self._append(f"[동기화] Supabase 설정 저장 — {role_label()} · 전체 목록 맞추기")
            win.destroy()

        btn = tk.Frame(win, bg="#f3efe8")
        btn.pack(fill="x", padx=16, pady=16)
        tk.Button(
            btn,
            text="저장 후 전체 맞추기",
            command=save_and_close,
            font=("Malgun Gothic", 10, "bold"),
            bg="#1f4e79",
            fg="white",
            relief="flat",
            padx=12,
            pady=4,
        ).pack(side="left")
        tk.Button(
            btn,
            text="전체 목록 맞추기",
            command=lambda: (
                self._append("[동기화] 전체 목록 맞추기 요청"),
                self._sync.force_full_sync(),
            ),
            font=("Malgun Gothic", 10, "bold"),
            bg="#b45309",
            fg="white",
            relief="flat",
            padx=10,
        ).pack(side="left", padx=8)
        tk.Button(
            btn,
            text="증분 동기화",
            command=lambda: (self._mark_catalog_dirty(), self._sync.sync_now()),
            font=("Malgun Gothic", 10),
            bg="#ebe4da",
            relief="flat",
            padx=10,
        ).pack(side="left", padx=4)
        tk.Button(btn, text="닫기", command=win.destroy, bg="#ebe4da", relief="flat").pack(
            side="right"
        )

    def _put_log(self, msg: str, *, channel: str | None = None) -> None:
        """Thread-safe log enqueue. channel: collect | mall | search."""
        self._log_q.put((channel or "", msg))

    def _channel_from_running_jobs(self) -> str | None:
        with self._jobs_lock:
            jobs = set(self._jobs)
        chans: list[str] = []
        if "search" in jobs:
            chans.append(LOG_SEARCH)
        if "publish" in jobs:
            chans.append(LOG_MALL)
        if jobs & {"collect", "import", "launch"}:
            chans.append(LOG_COLLECT)
        if len(chans) == 1:
            return chans[0]
        return None

    def _infer_log_channel(self, msg: str) -> str:
        text = msg or ""
        # 명확한 키워드 우선
        if any(
            k in text
            for k in (
                "이미지 검색",
                "구글 검색",
                "구글 다중",
                "선택된 이미지 검색",
                "제품명 찾",
                "Lens",
                "AI Mode",
            )
        ):
            return LOG_SEARCH
        if any(
            k in text
            for k in (
                "홈페이지 등록",
                "홈페이지 재등록",
                "재등록",
                "추천상품",
                "추천 반영",
                "추천 해제",
                "AI 코디",
                "AI 상품선택",
                "등록목록 제거",
                "등록 실패",
                "등록 완료",
                "등록 중",
            )
        ):
            return LOG_MALL
        if any(
            k in text
            for k in (
                "자동수집",
                "가져오기",
                "PageDown",
                "목록→상세",
                "디버그",
                "동기화",
                "제외됨",
                "제외 해제",
                "이미지 부족",
                "세션 한도",
                "목록 로딩",
            )
        ):
            return LOG_COLLECT
        job_ch = self._channel_from_running_jobs()
        if job_ch:
            return job_ch
        return LOG_COLLECT

    def _append(self, msg: str, *, channel: str | None = None) -> None:
        if not msg:
            return
        ch = channel if channel in LOG_CHANNELS else self._infer_log_channel(msg)
        stamp = dt.datetime.now().strftime("%H:%M:%S")
        # 여러 줄이면 첫 줄에만 시각 표시
        lines = msg.replace("\r\n", "\n").replace("\r", "\n")
        if not lines.endswith("\n"):
            lines += "\n"
        parts = lines.splitlines(keepends=True)
        if parts:
            parts[0] = f"[{stamp}] {parts[0]}"
        body = "".join(parts)

        widget = self._log_widgets.get(ch)
        if widget is not None:
            widget.configure(state="normal")
            widget.insert("end", body)
            widget.see("end")
            widget.configure(state="disabled")

        # 상품관리 하단 미리보기 (채널 태그 포함)
        if self._log_recent is not None:
            tag = LOG_CHANNEL_LABELS.get(ch, ch)
            # 라벨이 길면 짧게
            short = {
                LOG_COLLECT: "수집",
                LOG_MALL: "등록",
                LOG_SEARCH: "검색",
            }.get(ch, ch)
            preview = body
            if preview.startswith(f"[{stamp}] "):
                preview = f"[{stamp}][{short}] " + preview[len(f"[{stamp}] ") :]
            else:
                preview = f"[{short}] {preview}"
            self._log_recent.insert("end", preview)
            self._log_recent.see("end")
            # 미리보기는 최근 800줄만 유지
            try:
                total = int(float(self._log_recent.index("end-1c").split(".")[0]))
                if total > 800:
                    self._log_recent.delete("1.0", f"{total - 700}.0")
            except Exception:
                pass

    def _clear_log_channel(self, channel: str) -> None:
        widget = self._log_widgets.get(channel)
        if widget is None:
            return
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.configure(state="disabled")

    def _clear_current_log_tab(self) -> None:
        try:
            idx = self.log_nb.index(self.log_nb.select())
            ch = LOG_CHANNELS[idx]
        except Exception:
            ch = LOG_COLLECT
        self._clear_log_channel(ch)

    def _clear_all_logs(self) -> None:
        for ch in LOG_CHANNELS:
            self._clear_log_channel(ch)
        if self._log_recent is not None:
            self._log_recent.delete("1.0", "end")

    def _poll_log(self) -> None:
        # channel → lines
        buckets: dict[str, list[str]] = {c: [] for c in LOG_CHANNELS}
        while True:
            try:
                item = self._log_q.get_nowait()
            except queue.Empty:
                break
            if isinstance(item, tuple) and len(item) == 2:
                ch_raw, msg = item
                ch = ch_raw if ch_raw in LOG_CHANNELS else self._infer_log_channel(str(msg))
                buckets[ch].append(str(msg))
            else:
                msg = str(item)
                ch = self._infer_log_channel(msg)
                buckets[ch].append(msg)
        for ch, lines in buckets.items():
            if not lines:
                continue
            self._append("\n".join(lines), channel=ch)
        if not self._stop.is_set():
            self.after(250, self._poll_log)

    def _status_loop(self) -> None:
        while not self._stop.is_set():
            try:
                running = is_running()
                cdp = is_cdp_up(DEFAULT_PORT)
                if cdp:
                    album = "앨범: 연결됨"
                    album_fg = "#166534"
                elif running:
                    album = "앨범: 실행중·디버그 없음"
                    album_fg = "#b45309"
                else:
                    album = "앨범: 미실행"
                    album_fg = "#b91c1c"
                try:
                    ex_n = len(self.store.list_excluded())
                    pub_n = len(self.store.list_published())
                except Exception:
                    ex_n = 0
                    pub_n = 0
                n = len(self.products)
                mode = self.list_mode.get()
                jobs = self._jobs_status_text()
                job_bit = f"  |  {jobs}" if jobs else ""
                if mode == "excluded":
                    counts = f"제외 목록 {ex_n}개{job_bit}"
                elif mode == "published":
                    counts = f"등록 목록 {pub_n}개{job_bit}"
                else:
                    counts = f"상품 {n}개  ·  등록 {pub_n}개  ·  제외 {ex_n}개{job_bit}"

                def _apply(
                    a=album,
                    af=album_fg,
                    c=counts,
                ) -> None:
                    self.album_status.set(a)
                    try:
                        self._album_status_lbl.configure(fg=af)
                    except Exception:
                        pass
                    self.status.set(c)
                    try:
                        self._apply_sync_status()
                    except Exception:
                        pass

                self.after(0, _apply)
            except Exception:
                pass
            self._stop.wait(2.0)

    def _job_start(self, name: str) -> bool:
        """Start a named job. Same name cannot overlap; different jobs can run together.

        collect/import share the Weigou CDP browser — only one of them at a time.
        """
        with self._jobs_lock:
            if name in self._jobs:
                return False
            if name in ("collect", "import"):
                if "collect" in self._jobs or "import" in self._jobs:
                    return False
            self._jobs.add(name)
        self.after(0, self._refresh_job_banner)
        return True

    def _job_end(self, name: str) -> None:
        with self._jobs_lock:
            self._jobs.discard(name)
        # Clear banner bit for finished job family
        if name in ("collect", "import", "launch"):
            self._job_banner_bits.pop(LOG_COLLECT, None)
        elif name == "publish":
            self._job_banner_bits.pop(LOG_MALL, None)
        elif name == "search":
            self._job_banner_bits.pop(LOG_SEARCH, None)
        self.after(0, self._refresh_job_banner)

    def _job_running(self, name: str) -> bool:
        with self._jobs_lock:
            return name in self._jobs

    def _jobs_status_text(self) -> str:
        with self._jobs_lock:
            names = sorted(self._jobs)
        if not names:
            return ""
        parts: list[str] = []
        for n in names:
            # Prefer live progress label (e.g. 사이트맞추기중) over generic job name
            ch = self._job_log_channel.get(n)
            bit = self._job_banner_bits.get(ch or "", "") if ch else ""
            if bit:
                parts.append(bit)
            else:
                parts.append(f"{self._job_labels.get(n, n)} 중")
        return " · ".join(parts)

    def _refresh_job_banner(self) -> None:
        """Show running job labels next to 상품 관리 title."""
        try:
            with self._jobs_lock:
                jobs = set(self._jobs)
        except Exception:
            jobs = set()
        parts: list[str] = []
        if jobs & {"collect", "import"}:
            parts.append(self._job_banner_bits.get(LOG_COLLECT) or "수집진행중")
        if "publish" in jobs:
            parts.append(self._job_banner_bits.get(LOG_MALL) or "홈페이지등록중")
        if "search" in jobs:
            parts.append(self._job_banner_bits.get(LOG_SEARCH) or "이미지검색중")
        try:
            self.job_banner_var.set("  ·  ".join(parts))
        except Exception:
            pass

    def _banner_label_for_channel(self, channel: str) -> str:
        if channel == LOG_COLLECT:
            return "수집진행중"
        if channel == LOG_MALL:
            return "홈페이지등록중"
        if channel == LOG_SEARCH:
            return "이미지검색중"
        return "진행중"

    def _warn_job_busy(self, name: str) -> None:
        label = self._job_labels.get(name, name)
        extra = ""
        if name in ("collect", "import"):
            if self._job_running("collect") and name == "import":
                label = "자동수집"
            elif self._job_running("import") and name == "collect":
                label = "가져오기"
            extra = "\n(수집·가져오기는 微购 화면을 같이 쓰므로 동시에 할 수 없습니다)"
        messagebox.showinfo(
            "진행 중",
            f"이미 [{label}] 작업이 실행 중입니다.{extra}\n"
            "홈페이지 등록·이미지 검색은 수집과 별도로 사용할 수 있습니다.",
        )

    def _schedule_list_refresh(self, *, reload_detail: bool = False) -> None:
        """Debounced list refresh that does not wipe the form being edited."""
        if self._collect_refresh_after is not None:
            try:
                self.after_cancel(self._collect_refresh_after)
            except Exception:
                pass

        def _do() -> None:
            self._collect_refresh_after = None
            try:
                yview = self.listbox.yview()
                self.refresh_list(
                    preserve_yview=(float(yview[0]), float(yview[1])),
                    reload_detail=reload_detail,
                    quiet=True,
                )
            except Exception:
                self.refresh_list(reload_detail=reload_detail, quiet=True)

        self._collect_refresh_after = self.after(700, _do)

    def _on_list_mode(self) -> None:
        self.current_id = None
        self.current_excluded_id = None
        self.current_published_id = None
        self._sticky_selected_ids = []
        self._list_page = 0
        self._apply_mode_buttons()
        self.refresh_list()

    def _reset_list_page_and_refresh(self, *_args) -> None:
        self._list_page = 0
        self.refresh_list()

    def _on_list_prev_page(self) -> None:
        if self._list_page > 0:
            self._list_page -= 1
            self.refresh_list()

    def _on_list_next_page(self) -> None:
        max_page = (
            max(0, (self._list_total - 1) // self._list_page_size)
            if self._list_total
            else 0
        )
        if self._list_page < max_page:
            self._list_page += 1
            self.refresh_list()

    @staticmethod
    def _shared_list_ref(
        *,
        search_code: str = "",
        goods_id: str = "",
    ) -> str:
        """A/B-identical badge for list rows (never local SQLite id)."""
        code = (search_code or "").strip()
        if code:
            return code
        gid = (goods_id or "").strip()
        if gid:
            return gid[-10:] if len(gid) > 10 else gid
        return "-"

    def _product_busy_tags(self, product_id: int) -> list[str]:
        """Status tags for in-progress search / homepage publish."""
        tags: list[str] = []
        if product_id == self._search_active_id:
            tags.append("검색중")
        elif product_id in self._search_queued_ids:
            tags.append("검색대기")
        if product_id == self._publish_active_id:
            tags.append("등록중")
        elif product_id in self._publish_queued_ids:
            tags.append("등록대기")
        return tags

    def _format_product_list_line(self, p: Product) -> str:
        cat = p.category or "?"
        name = p.google_name or p.title or "(제목 없음)"
        if len(name) > 48:
            name = name[:48] + "…"
        ref = self._shared_list_ref(search_code=p.search_code, goods_id=p.goods_id)
        tags = self._product_busy_tags(p.id)
        badge = "".join(f"[{t}]" for t in tags)
        return f"{badge}[{cat}] #{ref} {name}"

    def _paint_product_list_styles(self) -> None:
        """Tint busy rows (search=amber, publish=blue) after list rebuild."""
        if self.list_mode.get() != "products":
            return
        try:
            n = int(self.listbox.size())
        except Exception:
            return
        for i, p in enumerate(self.products):
            if i >= n:
                break
            tags = self._product_busy_tags(p.id)
            try:
                if "검색중" in tags:
                    self.listbox.itemconfig(i, bg="#ffe8a3", fg="#5c4800")
                elif "등록중" in tags:
                    self.listbox.itemconfig(i, bg="#b8d4f0", fg="#0b3d66")
                elif "검색대기" in tags:
                    self.listbox.itemconfig(i, bg="#fff6d9", fg="#6b5720")
                elif "등록대기" in tags:
                    self.listbox.itemconfig(i, bg="#e3eef8", fg="#1e3a5f")
                else:
                    self.listbox.itemconfig(i, bg="#fffdf9", fg="#222222")
            except Exception:
                pass

    def _refresh_list_busy(self) -> None:
        """Quiet list refresh so busy tags/colors stay current during jobs."""
        if self.list_mode.get() != "products":
            return
        self.refresh_list(reload_detail=False, quiet=True)

    def _set_search_busy(
        self, active: int | None, queued: set[int] | None = None
    ) -> None:
        self._search_active_id = active
        if queued is not None:
            self._search_queued_ids = set(queued)
        self.after(0, self._refresh_list_busy)

    def _update_list_page_label(self, shown: int) -> None:
        total = self._list_total
        size = self._list_page_size
        page = self._list_page
        if total <= 0:
            self.list_page_var.set("0 / 0")
            return
        start = page * size + 1
        end = min(total, page * size + max(0, shown))
        max_page = max(0, (total - 1) // size)
        self.list_page_var.set(f"{start}–{end} / {total} · 페이지 {page + 1}/{max_page + 1}")

    def _apply_mode_buttons(self) -> None:
        mode = self.list_mode.get()
        self.btn_save.pack_forget()
        self.btn_publish.pack_forget()
        self.btn_exclude.pack_forget()
        self.btn_merge_images.pack_forget()
        self.btn_delete.pack_forget()
        self.btn_unexclude.pack_forget()
        self.btn_unpublish.pack_forget()
        self.btn_republish.pack_forget()
        self.btn_reconcile_site.pack_forget()
        self.btn_recommend.pack_forget()
        self.btn_unrecommend.pack_forget()
        self.btn_ai_select.pack_forget()
        self.btn_ai_apply.pack_forget()
        if mode == "excluded":
            self.btn_unexclude.pack(side="left", padx=6)
            self.listbox.configure(selectmode=tk.EXTENDED)
            self.chk_recommended_only.configure(state="disabled")
            self.chk_searched_only.configure(state="disabled")
            self.list_hint.configure(text="Ctrl·Shift 클릭으로 여러 개 선택 → 제외 해제")
        elif mode == "published":
            self.btn_republish.pack(side="left", padx=6)
            self.btn_reconcile_site.pack(side="left", padx=6)
            self.btn_recommend.pack(side="left", padx=6)
            self.btn_unrecommend.pack(side="left", padx=6)
            self.btn_unpublish.pack(side="left", padx=6)
            self.btn_ai_select.pack(side="left", padx=6)
            self.btn_ai_apply.pack(side="left", padx=6)
            # 일반 클릭=1개, Ctrl/Shift=다중 (MULTIPLE 토글 방식 아님)
            self.listbox.configure(selectmode=tk.EXTENDED)
            self.chk_recommended_only.configure(state="normal")
            self.chk_searched_only.configure(state="disabled")
            if self.filter_recommended_only.get():
                self.list_hint.configure(
                    text="추천 상품만 표시 중 · [추천 해제]로 목록에서 빠짐"
                )
            else:
                self.list_hint.configure(
                    text="클릭=1개 · [재등록]/[추천]/[추천 해제] · 「추천만」체크 가능"
                )
        else:
            self.btn_save.pack(side="left")
            self.btn_publish.pack(side="left", padx=6)
            self.btn_exclude.pack(side="left", padx=6)
            self.btn_merge_images.pack(side="left", padx=6)
            self.btn_delete.pack(side="left", padx=6)
            self.listbox.configure(selectmode=tk.EXTENDED)
            self.chk_recommended_only.configure(state="disabled")
            self.chk_searched_only.configure(state="normal")
            hint = "먼저 클릭한 상품이 기준(상세) · Ctrl/Shift로 추가 → 등록/제외/이미지 합치기"
            if self.filter_searched_only.get():
                hint = "검색완료(제품명 있음)만 표시 중 · " + hint
            self.list_hint.configure(text=hint)

    def _list_entity_ids(self) -> list[int]:
        """Current listbox row → entity ids for active mode."""
        mode = self.list_mode.get()
        if mode == "excluded":
            return [int(x.id) for x in self.excluded_items]
        if mode == "published":
            return [int(x.id) for x in self.published_items]
        return [int(x.id) for x in self.products]

    def _ids_from_listbox_selection(self) -> list[int]:
        ids = self._list_entity_ids()
        out: list[int] = []
        try:
            for i in self.listbox.curselection():
                ii = int(i)
                if 0 <= ii < len(ids):
                    out.append(ids[ii])
        except Exception:
            pass
        return out

    def _remember_list_selection(self) -> None:
        ids = self._ids_from_listbox_selection()
        if ids:
            self._sticky_selected_ids = ids

    def _capture_list_ui_state(self) -> dict:
        y0, y1 = 0.0, 1.0
        try:
            yview = self.listbox.yview()
            y0, y1 = float(yview[0]), float(yview[1])
        except Exception:
            pass
        live = self._ids_from_listbox_selection()
        if live:
            self._sticky_selected_ids = live
        # live가 비어도 sticky 유지 (새로고침 순간 선택 손실 방지)
        sel = live or list(self._sticky_selected_ids)
        mode = self.list_mode.get()
        current = None
        if mode == "excluded":
            current = self.current_excluded_id
        elif mode == "published":
            current = self.current_published_id
        else:
            current = self.current_id
        # 포커스 중인 항목은 선택에 꼭 포함
        if current is not None and int(current) not in sel:
            sel = list(sel) + [int(current)]
        return {
            "yview": (y0, y1),
            "selected_ids": sel,
            "current_id": current,
            "mode": mode,
        }

    def _restore_list_ui_state(
        self,
        state: dict,
        *,
        preserve_yview: tuple[float, float] | None = None,
        reload_detail: bool = True,
        quiet: bool = False,
        focus_list: bool = False,
    ) -> None:
        """Restore multi-selection + scroll. Never jump to row 0 during quiet refresh."""
        mode = self.list_mode.get()
        items = self._list_entity_ids()
        id_to_idx = {eid: i for i, eid in enumerate(items)}
        want = [i for i in (state.get("selected_ids") or []) if i in id_to_idx]
        current = state.get("current_id")
        if current is not None and current in id_to_idx and current not in want:
            want.append(int(current))

        self.listbox.selection_clear(0, tk.END)
        for eid in want:
            self.listbox.selection_set(id_to_idx[eid])
        if want:
            self._sticky_selected_ids = list(want)
        elif quiet:
            # quiet 새로고침에서 대상이 목록에 아직 없으면 sticky 유지
            pass
        else:
            self._sticky_selected_ids = []

        yview = preserve_yview or state.get("yview")
        if yview is not None:
            try:
                self.listbox.yview_moveto(float(yview[0]))
            except Exception:
                pass

        # Activate: prefer still-visible current, else last selected, else nothing
        activate_idx = None
        if current is not None and current in id_to_idx:
            activate_idx = id_to_idx[int(current)]
        elif want:
            activate_idx = id_to_idx[want[-1]]

        if activate_idx is not None:
            try:
                self.listbox.activate(activate_idx)
                # quiet: 스크롤 위치 유지 (see로 점프하지 않음)
                if yview is None and not quiet:
                    self.listbox.see(activate_idx)
            except Exception:
                pass
            if focus_list:
                try:
                    self.listbox.focus_set()
                except Exception:
                    pass

        if not reload_detail:
            return

        if activate_idx is None:
            if not quiet and not items:
                self._clear_detail()
            return

        # quiet: 이미 같은 항목을 보고 있으면 상세 폼을 다시 채우지 않음 (입력/선택 유지)
        if quiet:
            if mode == "excluded" and self.current_excluded_id == current:
                return
            if mode == "published" and self.current_published_id == current:
                return
            if mode == "products" and self.current_id == current:
                return
            # quiet + reload_detail 이더라도 선택만 복원하고 상세는 건드리지 않음
            return

        if mode == "excluded":
            self._show_excluded(self.excluded_items[activate_idx])
        elif mode == "published":
            self._show_published(self.published_items[activate_idx])
        else:
            self._show_product(self.products[activate_idx])

    def _focus_list_at(
        self,
        idx: int,
        *,
        preserve_yview: tuple[float, float] | None = None,
        grab_focus: bool = False,
        keep_multi: bool = False,
    ) -> None:
        """Select + activate so ↑↓ keys continue from this row (not the top)."""
        size = int(self.listbox.size())
        if size <= 0:
            return
        idx = max(0, min(int(idx), size - 1))
        if not keep_multi:
            self.listbox.selection_clear(0, tk.END)
        self.listbox.selection_set(idx)
        self.listbox.activate(idx)
        if preserve_yview is not None:
            self.listbox.yview_moveto(preserve_yview[0])
        else:
            self.listbox.see(idx)
        if grab_focus:
            self.listbox.focus_set()
        self._remember_list_selection()

    def refresh_list(
        self,
        *,
        preserve_yview: tuple[float, float] | None = None,
        reload_detail: bool = True,
        focus_list: bool = False,
        quiet: bool = False,
    ) -> None:
        """Rebuild list. quiet=True: keep selection/scroll, never jump to first row."""
        ui = self._capture_list_ui_state()
        if preserve_yview is not None:
            ui["yview"] = preserve_yview

        self.listbox.delete(0, tk.END)
        mode = self.list_mode.get()

        def _clamp_page(total: int) -> None:
            self._list_total = total
            max_page = max(0, (total - 1) // self._list_page_size) if total else 0
            if self._list_page > max_page:
                self._list_page = max_page

        if mode == "excluded":
            _clamp_page(
                self.store.count_excluded(
                    self.query.get(), category=self.filter_category.get()
                )
            )
            self.excluded_items = self.store.list_excluded(
                self.query.get(),
                category=self.filter_category.get(),
                limit=self._list_page_size,
                offset=self._list_page * self._list_page_size,
            )
            self._update_list_page_label(len(self.excluded_items))
            self.products = []
            self.published_items = []
            for item in self.excluded_items:
                cat = item.category or "?"
                name = item.title or "(제목 없음)"
                if len(name) > 52:
                    name = name[:52] + "…"
                ref = self._shared_list_ref(
                    search_code=item.search_code, goods_id=item.goods_id
                )
                self.listbox.insert(tk.END, f"[제외][{cat}] #{ref} {name}")
            if self.excluded_items:
                self._restore_list_ui_state(
                    ui,
                    preserve_yview=preserve_yview,
                    reload_detail=reload_detail,
                    quiet=quiet,
                    focus_list=focus_list,
                )
                # first open / empty sticky → pick first only when not quiet
                if not self._sticky_selected_ids and not quiet:
                    self._focus_list_at(0, preserve_yview=preserve_yview, grab_focus=focus_list)
                    if reload_detail:
                        self._show_excluded(self.excluded_items[0])
            else:
                self.current_excluded_id = None
                self._sticky_selected_ids = []
                if reload_detail and not quiet:
                    self._clear_detail()
            return

        if mode == "published":
            rec_only = bool(self.filter_recommended_only.get())
            _clamp_page(
                self.store.count_published(
                    self.query.get(),
                    category=self.filter_category.get(),
                    recommended_only=rec_only,
                )
            )
            self.published_items = self.store.list_published(
                self.query.get(),
                category=self.filter_category.get(),
                recommended_only=rec_only,
                limit=self._list_page_size,
                offset=self._list_page * self._list_page_size,
            )
            self._update_list_page_label(len(self.published_items))
            self.products = []
            self.excluded_items = []
            for item in self.published_items:
                cat = item.category or "?"
                name = item.google_name or item.title or "(제목 없음)"
                if len(name) > 52:
                    name = name[:52] + "…"
                ref = self._shared_list_ref(
                    search_code=item.search_code, goods_id=item.goods_id
                )
                color = f" · {item.colors}" if item.colors else ""
                if item.recommended:
                    slot = recommend_slots_for_category(item.category)
                    slot_ko = {"bag": "가방", "clothes": "옷", "accessory": "악세사리"}.get(
                        slot[0] if slot else "", ""
                    )
                    rec = f"[추천·{slot_ko}]" if slot_ko else "[추천]"
                else:
                    rec = "[등록]"
                self.listbox.insert(tk.END, f"{rec}[{cat}] #{ref} {name}{color}")
            if rec_only:
                self.list_hint.configure(
                    text=f"추천 상품만 {len(self.published_items)}건 · 체크 해제 시 전체 등록 목록"
                )
            else:
                self.list_hint.configure(
                    text="클릭=1개 · [재등록]/[추천]/[추천 해제] · 「추천만」체크 가능"
                )
            if self.published_items:
                self._restore_list_ui_state(
                    ui,
                    preserve_yview=preserve_yview,
                    reload_detail=reload_detail,
                    quiet=quiet,
                    focus_list=focus_list,
                )
                if not self._sticky_selected_ids and not quiet:
                    self._focus_list_at(0, preserve_yview=preserve_yview, grab_focus=focus_list)
                    if reload_detail:
                        self._show_published(self.published_items[0])
            else:
                self.current_published_id = None
                self._sticky_selected_ids = []
                if reload_detail and not quiet:
                    self._clear_detail()
            return

        _clamp_page(
            self.store.count_products(
                self.query.get(),
                category=self.filter_category.get(),
                searched_only=bool(self.filter_searched_only.get()),
            )
        )
        self.products = self.store.list_products(
            self.query.get(),
            category=self.filter_category.get(),
            limit=self._list_page_size,
            offset=self._list_page * self._list_page_size,
            searched_only=bool(self.filter_searched_only.get()),
        )
        self._update_list_page_label(len(self.products))
        self.excluded_items = []
        self.published_items = []
        for p in self.products:
            self.listbox.insert(tk.END, self._format_product_list_line(p))
        self._paint_product_list_styles()

        if self.products:
            self._restore_list_ui_state(
                ui,
                preserve_yview=preserve_yview,
                reload_detail=reload_detail,
                quiet=quiet,
                focus_list=focus_list,
            )
            if not self._sticky_selected_ids and not quiet:
                # 의도적 새로고침(검색 등)이고 선택 없을 때만 첫 행
                self._focus_list_at(0, preserve_yview=preserve_yview, grab_focus=focus_list)
                if reload_detail:
                    self._show_product(self.products[0])
        else:
            if not quiet:
                self._sticky_selected_ids = []
            if reload_detail and not quiet:
                self._clear_detail()

    def _clear_detail(self) -> None:
        self._begin_form_load()
        try:
            self.current_id = None
            self.current_excluded_id = None
            self.current_published_id = None
            self._detail_loaded_at = ""
            self.title_var.set("")
            self.google_name_var.set("")
            self.name_en_var.set("")
            self.code_var.set("")
            self.sku_var.set("")
            self.tags_var.set("")
            self.color_var.set("")
            self.size_var.set("")
            self.price_preview.set("")
            self.desc.delete("1.0", tk.END)
            self._clear_images()
        finally:
            self._end_form_load()

    def _capture_list_select_mods(self, event: tk.Event) -> None:
        """Remember Ctrl/Shift at click/key so multi-select keeps first focus."""
        state = int(getattr(event, "state", 0) or 0)
        mods: set[str] = set()
        if state & 0x4:  # Control
            mods.add("control")
        if state & 0x1:  # Shift
            mods.add("shift")
        self._list_select_mods = frozenset(mods)

    def _on_list_button1(self, event: tk.Event) -> None:
        self._capture_list_select_mods(event)

    def _on_list_keypress(self, event: tk.Event) -> None:
        key = str(getattr(event, "keysym", "") or "")
        if key in ("Up", "Down", "Prior", "Next", "Home", "End", "space", "Space"):
            self._capture_list_select_mods(event)

    def _focus_index_from_selection(self, sel: tuple[int, ...] | list[int]) -> int:
        """Index whose detail panel should show.

        Plain click → newly activated row.
        Ctrl/Shift multi-select → keep the previously focused row if still selected
        (so the first-clicked product stays the merge/register focus).
        """
        if not sel:
            return 0
        indices = [int(i) for i in sel]
        mods = getattr(self, "_list_select_mods", frozenset())
        multi_extend = bool(mods & {"control", "shift"}) and len(indices) > 1
        mode = self.list_mode.get()

        current: int | None = None
        items_len = 0
        if mode == "excluded":
            current = self.current_excluded_id
            items_len = len(self.excluded_items)
        elif mode == "published":
            current = self.current_published_id
            items_len = len(self.published_items)
        else:
            current = self.current_id
            items_len = len(self.products)

        if multi_extend and current is not None:
            for i in indices:
                if mode == "excluded":
                    if 0 <= i < items_len and self.excluded_items[i].id == current:
                        return i
                elif mode == "published":
                    if 0 <= i < items_len and self.published_items[i].id == current:
                        return i
                else:
                    if 0 <= i < items_len and self.products[i].id == current:
                        return i

        # New primary selection: prefer Tk "active" (the row just clicked)
        try:
            active = int(self.listbox.index("active"))
            if active in indices:
                return active
        except Exception:
            pass
        return indices[-1]

    def _on_select(self, _event=None) -> None:
        sel = self.listbox.curselection()
        if not sel:
            return
        self._remember_list_selection()
        idx = self._focus_index_from_selection(sel)
        mode = self.list_mode.get()
        if mode == "excluded":
            if 0 <= idx < len(self.excluded_items):
                item = self.excluded_items[idx]
                if self.current_excluded_id == item.id:
                    return
                self._show_excluded(item)
        elif mode == "published":
            if 0 <= idx < len(self.published_items):
                item = self.published_items[idx]
                if self.current_published_id == item.id:
                    return
                self._show_published(item)
        else:
            # 다른 항목 클릭 전 한글 조합 확정 + 현재 입력값 즉시 저장
            if self.current_id is not None:
                if 0 <= idx < len(self.products) and self.products[idx].id != self.current_id:
                    self._finalize_ime(self.focus_get() or self._ime_focus_widget)
                    self._soft_save_current(force=True)
            if 0 <= idx < len(self.products):
                p = self.products[idx]
                if self.current_id == p.id:
                    return
                self._show_product(p)
    def _selected_product_ids(self) -> list[int]:
        if self.list_mode.get() != "products":
            return []
        sel = self.listbox.curselection()
        out: list[int] = []
        for i in sel:
            if 0 <= i < len(self.products):
                out.append(self.products[i].id)
        if not out and self.current_id is not None:
            out = [self.current_id]
        return out

    def _selected_published_ids(self) -> list[int]:
        if self.list_mode.get() != "published":
            return []
        sel = self.listbox.curselection()
        out: list[int] = []
        for i in sel:
            if 0 <= i < len(self.published_items):
                out.append(self.published_items[i].id)
        if not out and self.current_published_id is not None:
            out = [self.current_published_id]
        return out

    def _selected_excluded_ids(self) -> list[int]:
        if self.list_mode.get() != "excluded":
            return []
        sel = self.listbox.curselection()
        out: list[int] = []
        for i in sel:
            if 0 <= i < len(self.excluded_items):
                out.append(self.excluded_items[i].id)
        if not out and self.current_excluded_id is not None:
            out = [self.current_excluded_id]
        return out

    def _bind_ime_safe_entry(self, entry: tk.Entry) -> None:
        entry._ime_composing = False  # type: ignore[attr-defined]
        entry._ime_preedit = ""  # type: ignore[attr-defined]
        entry._ime_snapshot = ""  # type: ignore[attr-defined]
        entry.bind("<<CompositionStart>>", self._on_composition_start, add="+")
        entry.bind("<<CompositionEnd>>", self._on_composition_end, add="+")
        entry.bind("<KeyRelease>", self._on_ime_key_release, add="+")
        entry.bind("<FocusIn>", self._on_field_focus_in, add="+")
        entry.bind("<FocusOut>", self._on_field_focus_out, add="+")

    def _bind_ime_safe_text(self, widget: tk.Text) -> None:
        widget._ime_composing = False  # type: ignore[attr-defined]
        widget._ime_preedit = ""  # type: ignore[attr-defined]
        widget._ime_snapshot = ""  # type: ignore[attr-defined]
        widget.bind("<<CompositionStart>>", self._on_composition_start, add="+")
        widget.bind("<<CompositionEnd>>", self._on_composition_end, add="+")
        widget.bind("<KeyRelease>", self._on_ime_key_release, add="+")
        widget.bind("<FocusIn>", self._on_field_focus_in, add="+")
        widget.bind("<FocusOut>", self._on_field_focus_out, add="+")

    def _on_field_focus_in(self, event=None) -> None:
        if event is not None:
            self._ime_focus_widget = event.widget

    def _on_ime_key_release(self, event=None) -> None:
        """Track live Hangul preedit so we can restore it if Windows cancels it."""
        if event is None or self._form_loading:
            return
        w = event.widget
        try:
            pre = get_composition(w)
            snap = snapshot_widget_text(w)
            w._ime_preedit = pre  # type: ignore[attr-defined]
            w._ime_snapshot = snap  # type: ignore[attr-defined]
            if pre:
                self._ime_composing = True
                w._ime_composing = True  # type: ignore[attr-defined]
        except Exception:
            pass

    def _finalize_ime(self, widget: tk.Misc | None) -> None:
        """Commit composition (랙 등) before focus leaves the field."""
        if widget is None:
            return
        try:
            preedit = getattr(widget, "_ime_preedit", "") or get_composition(widget)
            snapshot = getattr(widget, "_ime_snapshot", "") or snapshot_widget_text(widget)
            committed = commit_composition(widget)
            if not preedit:
                preedit = committed
            # Windows가 조합을 취소한 경우 스냅샷으로 복구
            restore_text_if_stripped(widget, snapshot, preedit)
            widget._ime_composing = False  # type: ignore[attr-defined]
            widget._ime_preedit = ""  # type: ignore[attr-defined]
        except Exception:
            pass
        self._ime_composing = False

    def _on_global_pre_focus_change(self, _event=None) -> None:
        """Mouse/Alt pressed — commit Hangul before focus actually moves."""
        w = self.focus_get()
        if w is None:
            w = self._ime_focus_widget
        if w is None:
            return
        try:
            cls = w.winfo_class()
        except Exception:
            return
        if cls in ("Entry", "Text", "TEntry"):
            self._finalize_ime(w)

    def _on_app_deactivate(self, _event=None) -> None:
        w = self.focus_get() or self._ime_focus_widget
        if w is not None:
            self._finalize_ime(w)
            if self.current_id is not None and self.list_mode.get() == "products":
                self._schedule_soft_save(delay_ms=30)

    def _on_composition_start(self, event=None) -> None:
        self._ime_composing = True
        if event is not None:
            try:
                event.widget._ime_composing = True  # type: ignore[attr-defined]
                self._ime_focus_widget = event.widget
            except Exception:
                pass

    def _on_composition_end(self, event=None) -> None:
        self._ime_composing = False
        if event is not None:
            try:
                event.widget._ime_composing = False  # type: ignore[attr-defined]
                event.widget._ime_preedit = ""  # type: ignore[attr-defined]
            except Exception:
                pass
        if self._pending_soft_save:
            self._pending_soft_save = False
            self._schedule_soft_save(delay_ms=30)

    def _schedule_soft_save(self, delay_ms: int = 180) -> None:
        if self._attr_after is not None:
            try:
                self.after_cancel(self._attr_after)
            except Exception:
                pass
        self._attr_after = self.after(delay_ms, self._soft_save_current)

    def _on_field_focus_out(self, event=None) -> None:
        """Persist edits quietly when leaving a field — never wipe in-progress typing."""
        widget = event.widget if event is not None else None
        # 조합 중 글자(랙)를 먼저 확정/복구한 뒤 저장
        self._finalize_ime(widget)
        if self._form_loading:
            return
        mode = self.list_mode.get()
        if mode == "products" and self.current_id is not None:
            self._schedule_soft_save(delay_ms=50)
        elif mode == "published" and self.current_published_id is not None:
            self._schedule_soft_save(delay_ms=50)

    def _published_desc_body(self) -> str:
        """Form description without the [등록 목록] meta footer."""
        raw = self.desc.get("1.0", "end").strip()
        if "[등록 목록]" in raw:
            raw = raw.split("[등록 목록]", 1)[0].strip()
        return raw

    def _soft_save_current(self, force: bool = False) -> None:
        self._attr_after = None
        if self._form_loading:
            return
        if self._ime_composing and not force:
            self._pending_soft_save = True
            return
        self._pending_soft_save = False
        self._ime_composing = False
        mode = self.list_mode.get()
        try:
            sku = effective_price_code(self.sku_var.get())
            if not self.sku_var.get().strip():
                self.sku_var.set(sku)
            if mode == "products" and self.current_id is not None:
                db = self.store.get(self.current_id)
                if db and self._db_newer_than_form(db.updated_at):
                    # B(또는 원격)가 이미 저장함 — 낡은 폼으로 덮지 말고 화면만 맞춤
                    self._show_product(db)
                    return
                self.store.update_description(
                    self.current_id,
                    title=self.title_var.get().strip(),
                    search_code=self.code_var.get().strip(),
                    sku_no=sku,
                    tags=self.tags_var.get().strip(),
                    description=self.desc.get("1.0", "end").strip(),
                    category=self.category_var.get().strip(),
                    google_name=self.google_name_var.get().strip(),
                    name_en=self.name_en_var.get().strip(),
                    colors=self.color_var.get().strip(),
                    sizes=self.size_var.get().strip(),
                )
                fresh = self.store.get(self.current_id)
                self._remember_detail_loaded(fresh.updated_at if fresh else None)
                self._mark_catalog_dirty()
                self._refresh_price_preview()
            elif mode == "published" and self.current_published_id is not None:
                db = self.store.get_published(self.current_published_id)
                if db and self._db_newer_than_form(db.updated_at):
                    self._show_published(db)
                    return
                self.store.update_published(
                    self.current_published_id,
                    title=self.title_var.get().strip(),
                    search_code=self.code_var.get().strip(),
                    sku_no=sku,
                    tags=self.tags_var.get().strip(),
                    description=self._published_desc_body(),
                    category=self.category_var.get().strip(),
                    google_name=self.google_name_var.get().strip(),
                    name_en=self.name_en_var.get().strip(),
                    colors=self.color_var.get().strip(),
                    sizes=self.size_var.get().strip(),
                )
                fresh = self.store.get_published(self.current_published_id)
                self._remember_detail_loaded(fresh.updated_at if fresh else None)
                self._mark_catalog_dirty()
                self._refresh_price_preview()
        except Exception:
            pass

    def _begin_form_load(self) -> None:
        self._form_loading = True
        self._select_gen += 1
        if self._thumb_after is not None:
            try:
                self.after_cancel(self._thumb_after)
            except Exception:
                pass
            self._thumb_after = None

    def _end_form_load(self) -> None:
        self._form_loading = False

    def _clear_images(self) -> None:
        for w in self.img_frame.winfo_children():
            w.destroy()
        self._photo_cache.clear()
        self._photo_refs.clear()

    def _thumb(self, path: str, size: tuple[int, int] = (180, 180)) -> tk.PhotoImage | None:
        p = pathlib.Path(path)
        if not p.is_file():
            return None
        if Image is None or ImageTk is None:
            return None
        try:
            with Image.open(p) as src:
                # Always flatten to RGB — some Tk builds reject RGBA/P/CMYK.
                im = src.convert("RGB")
                im.thumbnail(size, Image.Resampling.BILINEAR)
                # Copy so file handle can close before PhotoImage holds pixels.
                im = im.copy()
            try:
                photo = ImageTk.PhotoImage(im)
            except Exception:
                # Last resort: re-encode as PNG bytes (fixes odd JPEG/Tk combos)
                buf = io.BytesIO()
                im.save(buf, format="PNG")
                buf.seek(0)
                photo = ImageTk.PhotoImage(Image.open(buf))
            self._photo_cache.append(photo)
            return photo
        except Exception:
            return None

    def _thumb_from_url(self, url: str, size: tuple[int, int] = (180, 180)) -> tk.PhotoImage | None:
        """Load remote preview via disk cache + CDN resize (may hit network)."""
        if Image is None or ImageTk is None:
            return None
        path = fetch_thumb_file(url)
        if not path:
            return None
        try:
            im = Image.open(path)
            im.thumbnail(size, Image.Resampling.LANCZOS)
            photo = ImageTk.PhotoImage(im)
            self._photo_refs.append(photo)
            return photo
        except Exception:
            return None

    def _ensure_images_for_action(
        self, *, product_id: int | None = None, published_id: int | None = None
    ) -> list[str]:
        """Download local images on demand right before an action needs them.

        Manager(B) role keeps images off local disk until actually needed
        (folder open, search, publish…). Defaults to the current selection
        (based on ``list_mode``); pass ``product_id``/``published_id`` to
        target another row instead (e.g. one job in a publish queue).
        """
        pid = product_id
        pubid = published_id
        if pid is None and pubid is None:
            if self.list_mode.get() == "published":
                pubid = self.current_published_id
            else:
                pid = self.current_id
        paths: list[str] = []
        try:
            if pubid is not None:
                paths = self.store.ensure_published_images(pubid)
                # Don't prune on every open — that can wipe other local galleries on A.
            elif pid is not None:
                paths = self.store.ensure_product_images(pid)
        except Exception:
            pass
        return paths

    def _show_excluded(self, item: ExcludedItem) -> None:
        self._begin_form_load()
        try:
            self.current_id = None
            self.current_published_id = None
            self.current_excluded_id = item.id
            self.color_var.set("")
            self.size_var.set("")
            self.title_var.set(item.title)
            self.google_name_var.set("")
            self.name_en_var.set("")
            self.code_var.set(item.search_code)
            self.sku_var.set(item.sku_no)
            self.tags_var.set(item.tags)
            if item.category:
                self.category_var.set(item.category)
            self.desc.delete("1.0", tk.END)
            self.desc.insert(
                "1.0",
                "\n".join(
                    [
                        "[제외 목록]",
                        f"goods_id: {item.goods_id}" if item.goods_id else "",
                        f"제외일: {item.created_at}" if item.created_at else "",
                        item.note,
                    ]
                ).strip(),
            )
            self.price_preview.set("제외된 상품 — 다음 수집에서 자동으로 건너뜁니다")
            self._clear_images()
            if item.cover_path and pathlib.Path(item.cover_path).exists():
                photo = self._thumb(item.cover_path, (220, 220))
                if photo:
                    tk.Label(self.img_frame, image=photo, bg="#fffdf9").pack(padx=8, pady=8)
                else:
                    tk.Label(self.img_frame, text="커버 있음", bg="#fffdf9", fg="#888").pack(pady=20)
            else:
                tk.Label(self.img_frame, text="커버 없음", bg="#fffdf9", fg="#888").pack(pady=20)
        finally:
            self._end_form_load()

    def _show_published(self, item: PublishedItem) -> None:
        self._begin_form_load()
        try:
            self.current_id = None
            self.current_excluded_id = None
            self.current_published_id = item.id
            self.title_var.set(item.title)
            self.google_name_var.set(item.google_name)
            self.name_en_var.set(item.name_en)
            self.code_var.set(item.search_code)
            sku = (item.sku_no or "").strip() or DEFAULT_PRICE_TEXT
            if not (item.sku_no or "").strip():
                try:
                    self.store.update_published(item.id, sku_no=sku)
                except Exception:
                    pass
            self.sku_var.set(sku)
            self.tags_var.set(item.tags)
            self.color_var.set(item.colors)
            self.size_var.set(item.sizes)
            if item.category:
                self.category_var.set(item.category)
            self.desc.delete("1.0", tk.END)
            body = item.description.strip() if item.description else ""
            meta = "\n".join(
                x
                for x in (
                    "[등록 목록]",
                    "※ 정보 수정 후 [재등록] 하면 홈페이지에 반영됩니다",
                    f"mall_id: {item.mall_id}" if item.mall_id else "",
                    f"goods_id: {item.goods_id}" if item.goods_id else "",
                    f"등록일: {item.created_at}" if item.created_at else "",
                    item.note,
                )
                if x
            )
            self.desc.insert("1.0", (body + "\n\n" + meta).strip() if body else meta)
            self.price_preview.set(
                preview_price(sku) + "  ·  수정 후 [재등록]으로 홈페이지 갱신"
            )
            self._clear_images()
            paths = list(item.image_paths) if item.image_paths else []
            if not paths and item.cover_path:
                paths = [item.cover_path]
            # Prefer files that still exist; published pack folder is p{id}
            existing: list[str] = []
            for pth in paths:
                if pth and pathlib.Path(pth).exists():
                    existing.append(pth)
                    continue
                name = pathlib.Path(pth).name if pth else ""
                if name:
                    alt = self.store.published_img_root / f"p{item.id}" / name
                    if alt.is_file():
                        existing.append(str(alt))
            gen = self._select_gen
            self._schedule_thumbs(
                existing, gen, cover_only=False, urls=list(item.image_urls or [])
            )
        finally:
            self._end_form_load()
        fresh = self.store.get_published(item.id)
        self._remember_detail_loaded(
            (fresh.updated_at if fresh else None) or item.updated_at
        )

    def _schedule_url_thumbs(self, urls: list[str], gen: int) -> None:
        """Fallback preview from remote image_urls — used before images are downloaded locally.

        Parallel background fetch + disk cache so the UI thread stays responsive
        and repeat opens are near-instant.
        """
        if not urls:
            tk.Label(self.img_frame, text="이미지 없음", bg="#fffdf9", fg="#888").pack(pady=20)
            return

        # Cap preview count; cover first, rest in parallel.
        urls = list(urls)[:24]
        placeholders: list[tk.Label] = []
        for index, _url in enumerate(urls):
            cell = tk.Frame(self.img_frame, bg="#fffdf9", padx=4, pady=4)
            cell.grid(row=index // 3, column=index % 3, sticky="n")
            lbl = tk.Label(
                cell,
                text="로딩…" if index == 0 else "…",
                bg="#fffdf9",
                fg="#aaa",
                font=("Consolas", 8),
                width=18,
                height=8,
            )
            lbl.pack()
            placeholders.append(lbl)

        def place(index: int, path: str | None) -> None:
            if gen != self._select_gen or index >= len(placeholders):
                return
            try:
                lbl = placeholders[index]
                if not lbl.winfo_exists():
                    return
            except tk.TclError:
                return
            if not path:
                try:
                    lbl.configure(text="로드 실패", fg="#888", width=0, height=0)
                except tk.TclError:
                    pass
                return
            photo = self._thumb(path)
            try:
                if photo:
                    self._photo_refs.append(photo)
                    lbl.configure(image=photo, text="", width=0, height=0)
                    lbl.image = photo  # type: ignore[attr-defined]
                else:
                    lbl.configure(text="로드 실패", fg="#888", width=0, height=0)
            except tk.TclError:
                pass

        def worker() -> None:
            results: list[tuple[int, str | None]] = []

            def one(i_u: tuple[int, str]) -> tuple[int, str | None]:
                i, u = i_u
                p = fetch_thumb_file(u)
                return i, str(p) if p else None

            # Cover first (index 0), then remaining in parallel for snappy first paint.
            first = one((0, urls[0]))
            results.append(first)
            self.after(0, lambda f=first: place(f[0], f[1]))

            rest = list(enumerate(urls))[1:]
            if rest:
                with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
                    for fut in concurrent.futures.as_completed(
                        [pool.submit(one, item) for item in rest]
                    ):
                        if gen != self._select_gen:
                            return
                        try:
                            idx, path = fut.result()
                        except Exception:
                            continue
                        self.after(0, lambda i=idx, p=path: place(i, p))
            try:
                prune_thumb_cache()
            except Exception:
                pass

        threading.Thread(target=worker, daemon=True).start()

    def _schedule_thumbs(
        self,
        paths: list[str],
        gen: int,
        *,
        cover_only: bool = False,
        urls: list[str] | None = None,
        product_id: int | None = None,
    ) -> None:
        """Load thumbnails asynchronously so list selection stays instant.

        Prefer local files (including healed paths). URL preview is only a
        fallback when nothing readable exists on disk.
        """
        existing = [p for p in (paths or []) if p and pathlib.Path(p).is_file()]
        if not existing and product_id is not None:
            try:
                existing = self.store.resolve_local_images(product_id)
            except Exception:
                existing = []
        if not existing:
            if urls:
                self._schedule_url_thumbs(list(urls)[:24], gen)
                return
            tk.Label(self.img_frame, text="이미지 없음", bg="#fffdf9", fg="#888").pack(pady=20)
            return

        paths = existing[:40]

        def load_one(index: int = 0) -> None:
            self._thumb_after = None
            if gen != self._select_gen:
                return
            if index >= len(paths) or (cover_only and index >= 1):
                return
            # First frame immediately; rest deferred
            batch = 1 if index == 0 else 3
            end = min(len(paths), index + batch)
            for i in range(index, end):
                if gen != self._select_gen:
                    return
                path = paths[i]
                cell = tk.Frame(self.img_frame, bg="#fffdf9", padx=4, pady=4)
                cell.grid(row=i // 3, column=i % 3, sticky="n")
                photo = self._thumb(path)
                if photo:
                    lbl = tk.Label(cell, image=photo, bg="#fffdf9")
                    lbl.pack()
                else:
                    lbl = tk.Label(
                        cell,
                        text="열기 실패",
                        bg="#fffdf9",
                        fg="#888",
                        font=("Consolas", 8),
                    )
                    lbl.pack()
                # Thumbnails steal focus — bind wheel so scroll still works
                handler = getattr(self, "_bind_img_mousewheel", None)
                enter = getattr(self, "_img_wheel_bind", None)
                leave = getattr(self, "_img_wheel_unbind", None)
                for w in (cell, lbl):
                    if handler is not None:
                        w.bind("<MouseWheel>", handler)
                        w.bind("<Button-4>", handler)
                        w.bind("<Button-5>", handler)
                    if enter is not None:
                        w.bind("<Enter>", enter)
                    if leave is not None:
                        w.bind("<Leave>", leave)
            if end < len(paths) and not cover_only:
                self._thumb_after = self.after(1, lambda: load_one(end))

        load_one(0)

    def _show_product(self, p: Product) -> None:
        """Fill form from DB first — image color analysis only when color empty."""
        self._begin_form_load()
        try:
            self.current_excluded_id = None
            self.current_published_id = None
            self.current_id = p.id
            self.title_var.set(p.title)
            self.google_name_var.set(p.google_name)
            self.name_en_var.set(p.name_en)
            self.code_var.set(p.search_code)
            # 가격코드 빈칸 → 반수제품 가격문의 자동 기입
            sku = (p.sku_no or "").strip()
            if not sku:
                sku = DEFAULT_PRICE_TEXT
                try:
                    self.store.update_description(p.id, sku_no=sku)
                    self._mark_catalog_dirty()
                except Exception:
                    pass
            self.sku_var.set(sku)
            self.tags_var.set(p.tags)
            self.color_var.set(p.colors.strip())
            self.size_var.set(p.sizes.strip())
            if p.category:
                self.category_var.set(p.category)
            self.desc.delete("1.0", tk.END)
            self.desc.insert("1.0", p.description)
            self._refresh_price_preview()
            self._clear_images()
            gen = self._select_gen
            paths = self.store.resolve_local_images(
                p.id, paths=list(p.image_paths or []), cover_path=p.cover_path or ""
            )
            if paths and paths != list(p.image_paths or []):
                try:
                    self.store.rewrite_product_image_paths(p.id, paths)
                except Exception:
                    pass
            self._schedule_thumbs(
                paths, gen, urls=list(p.image_urls or []), product_id=p.id
            )
        finally:
            self._end_form_load()

        fresh = self.store.get(p.id)
        self._remember_detail_loaded((fresh.updated_at if fresh else None) or p.updated_at)

        # Heavy color/size fill only if missing — background, cancellable
        need_color = not p.colors.strip()
        need_size = not p.sizes.strip()
        need_cat = not p.category.strip()
        if need_color or need_size or need_cat:
            self.after(30, lambda: self._fill_missing_attrs(p.id, gen))

    def _fill_missing_attrs(self, product_id: int, gen: int) -> None:
        if gen != self._select_gen or self.current_id != product_id:
            return
        p = self.store.get(product_id)
        if not p:
            return
        cover = p.cover_path or (p.image_paths[0] if p.image_paths else "")
        # Text-only first for size/category (fast); images only if color still empty
        attrs = extract_attrs(
            p.title,
            p.tags,
            p.description,
            image_path=None,
            image_paths=None,
        )
        if gen != self._select_gen or self.current_id != product_id:
            return
        self._form_loading = True
        try:
            if not self.size_var.get().strip() and attrs.sizes:
                self.size_var.set(", ".join(attrs.sizes))
            if not self.category_var.get().strip() and attrs.category:
                self.category_var.set(attrs.category)
        finally:
            self._form_loading = False

        if self.color_var.get().strip():
            # persist size/cat if filled
            self._soft_save_current()
            return

        def work() -> None:
            img_attrs = extract_attrs(
                p.title,
                p.tags,
                p.description,
                image_path=cover or None,
                image_paths=p.image_paths[:6],
            )
            color = "/".join(img_attrs.colors) if img_attrs.colors else ""

            def apply() -> None:
                if gen != self._select_gen or self.current_id != product_id:
                    return
                if self.color_var.get().strip():
                    return
                self._form_loading = True
                try:
                    if color:
                        self.color_var.set(color)
                    if not self.size_var.get().strip() and img_attrs.sizes:
                        self.size_var.set(", ".join(img_attrs.sizes))
                finally:
                    self._form_loading = False
                self._soft_save_current()

            self.after(0, apply)

        threading.Thread(target=work, daemon=True).start()

    def _refresh_price_preview(self) -> None:
        self.price_preview.set(preview_price(self.sku_var.get()))

    def _auto_fill_attrs(self, silent: bool = True) -> None:
        cover = ""
        paths: list[str] = []
        if self.current_id is not None:
            cur = self.store.get(self.current_id)
            if cur:
                cover = cur.cover_path or (cur.image_paths[0] if cur.image_paths else "")
                paths = list(cur.image_paths[:6])
        attrs = extract_attrs(
            self.title_var.get(),
            self.tags_var.get(),
            self.desc.get("1.0", "end").strip() if hasattr(self, "desc") else "",
            image_path=cover or None,
            image_paths=paths,
        )
        self._form_loading = True
        try:
            if not silent:
                self.category_var.set(attrs.category)
                self.color_var.set("/".join(attrs.colors))
                self.size_var.set(", ".join(attrs.sizes))
            else:
                if not self.color_var.get().strip() and attrs.colors:
                    self.color_var.set("/".join(attrs.colors))
                if not self.size_var.get().strip() and attrs.sizes:
                    self.size_var.set(", ".join(attrs.sizes))
                if not self.category_var.get().strip() and attrs.category:
                    self.category_var.set(attrs.category)
        finally:
            self._form_loading = False
        if not silent:
            self._soft_save_current()

    def _on_bulk_category_by_tag(self) -> None:
        """같은 태그(정확히 일치) 상품들의 카테고리를 일괄 변경.

        상품관리·등록 목록 모두 지원. 등록 목록에서는 적용 후 재등록 가능.
        """
        mode = self.list_mode.get()
        if mode not in ("products", "published"):
            messagebox.showinfo(
                "일괄수정",
                "상품 목록 또는 등록된 상품 목록에서만 사용할 수 있습니다.",
            )
            return

        tag = self.tags_var.get().strip()
        if not tag:
            if mode == "published" and self.current_published_id is not None:
                cur = self.store.get_published(self.current_published_id)
                if cur:
                    tag = (cur.tags or "").strip()
            elif mode == "products" and self.current_id is not None:
                cur = self.store.get(self.current_id)
                if cur:
                    tag = (cur.tags or "").strip()
        if not tag:
            messagebox.showwarning(
                "일괄수정",
                "태그가 없습니다.\n태그가 있는 상품을 선택한 뒤 다시 시도하세요.",
            )
            return

        if mode == "published":
            count = self.store.count_published_by_exact_tag(tag)
            scope = "등록된 상품"
        else:
            count = self.store.count_by_exact_tag(tag)
            scope = "상품관리"
        if count <= 0:
            messagebox.showwarning(
                "일괄수정", f"태그 「{tag}」와 같은 {scope}이(가) 없습니다."
            )
            return

        win = tk.Toplevel(self)
        win.title("같은 태그 카테고리 일괄수정")
        win.configure(bg="#f3efe8")
        win.transient(self)
        win.grab_set()
        win.geometry("440x240")

        tk.Label(
            win,
            text="같은 태그 카테고리 일괄수정",
            font=("Malgun Gothic", 12, "bold"),
            bg="#f3efe8",
        ).pack(anchor="w", padx=16, pady=(14, 6))
        extra = (
            "\n적용 후 홈페이지 재등록을 선택할 수 있습니다."
            if mode == "published"
            else ""
        )
        tk.Label(
            win,
            text=(
                f"태그: {tag}\n"
                f"대상: {scope} {count}개 (태그 문구가 완전히 같은 항목)"
                f"{extra}"
            ),
            font=("Malgun Gothic", 10),
            bg="#f3efe8",
            justify="left",
        ).pack(anchor="w", padx=16, pady=(0, 10))

        row = tk.Frame(win, bg="#f3efe8")
        row.pack(fill="x", padx=16)
        tk.Label(row, text="새 카테고리", bg="#f3efe8", font=("Malgun Gothic", 9)).pack(
            side="left"
        )
        cat_var = tk.StringVar(value=self.category_var.get().strip() or "기타")
        ttk.Combobox(
            row,
            textvariable=cat_var,
            values=CATEGORY_ORDER,
            state="readonly",
            font=("Malgun Gothic", 10),
            width=14,
        ).pack(side="left", padx=8)

        def apply() -> None:
            cat = cat_var.get().strip()
            if cat not in CATEGORY_ORDER:
                messagebox.showwarning("일괄수정", "카테고리를 선택하세요.", parent=win)
                return
            if not messagebox.askyesno(
                "확인",
                f"태그 「{tag}」\n→ 카테고리 「{cat}」\n\n"
                f"{scope} {count}개의 카테고리를 바꿀까요?",
                parent=win,
            ):
                return

            republish_ids: list[int] = []
            if mode == "published":
                republish_ids = self.store.bulk_update_published_category_by_tag(
                    tag, cat
                )
                n = len(republish_ids)
                self._append(
                    f"등록 목록 같은 태그 일괄수정: 「{tag}」 → {cat} ({n}개)",
                    channel=LOG_MALL,
                )
            else:
                n = self.store.bulk_update_category_by_tag(tag, cat)
                self._mark_catalog_dirty()
                self._append(
                    f"같은 태그 일괄수정: 「{tag}」 → {cat} ({n}개)",
                    channel=LOG_COLLECT,
                )

            self.category_var.set(cat)
            win.destroy()
            self.refresh_list(reload_detail=True)

            if mode == "published" and republish_ids:
                self._push_published_meta_async(
                    republish_ids,
                    log_msg=f"같은 태그 {n}개 카테고리 「{cat}」 → 홈페이지 반영",
                )
                messagebox.showinfo(
                    "완료",
                    f"{n}개 상품 카테고리를 「{cat}」로 바꿨고\n"
                    "홈페이지에도 같이 반영합니다.",
                )
            else:
                messagebox.showinfo(
                    "완료", f"{n}개 상품 카테고리를 「{cat}」로 변경했습니다."
                )

        btn = tk.Frame(win, bg="#f3efe8")
        btn.pack(fill="x", padx=16, pady=16)
        tk.Button(
            btn,
            text="적용",
            command=apply,
            font=("Malgun Gothic", 10, "bold"),
            bg="#1f4e79",
            fg="white",
            activebackground="#163a5c",
            relief="flat",
            padx=14,
        ).pack(side="left")
        tk.Button(
            btn, text="취소", command=win.destroy, bg="#ebe4da", relief="flat", padx=10
        ).pack(side="right")

    def _on_translate_ko_en(self) -> None:
        ko = self.google_name_var.get().strip()
        if not ko:
            messagebox.showwarning("번역", "제품명(한)을 먼저 입력하세요.")
            return
        en = ko_name_to_en(ko)
        if not en:
            messagebox.showwarning("번역", "영어로 변환하지 못했습니다. 제품명(한)을 확인하세요.")
            return
        self.name_en_var.set(en)
        self._append(f"한→영: {ko}\n  → {en}")

    def _split_csv(self, raw: str) -> list[str]:
        return re_split_csv(raw)

    def _ensure_price_code(self, product: Product) -> Product:
        """Persist default Korean price label when 가격코드 is empty."""
        if (product.sku_no or "").strip():
            return product
        try:
            self.store.update_description(product.id, sku_no=DEFAULT_PRICE_TEXT)
            self._mark_catalog_dirty()
        except Exception:
            pass
        refreshed = self.store.get(product.id)
        if refreshed and self.current_id == product.id:
            self.after(0, lambda: self.sku_var.set(DEFAULT_PRICE_TEXT))
            self.after(0, self._refresh_price_preview)
        return refreshed or product

    def _set_channel_progress(
        self,
        channel: str,
        *,
        done: int,
        total: int,
        action: str = "진행중",
        detail: str = "",
        ok: int = 0,
        fail: int = 0,
        indeterminate: bool = False,
    ) -> None:
        """Update gauge at the top of a log-management tab (thread-safe via after)."""

        def _do() -> None:
            state = self._log_progress.get(channel)
            if not state:
                return
            try:
                bar: ttk.Progressbar = state["bar"]
                state["done"] = max(0, int(done))
                state["total"] = max(0, int(total))
                banner = (action or "").strip() or self._banner_label_for_channel(channel)
                if indeterminate or state["total"] <= 0:
                    if str(bar.cget("mode")) != "indeterminate":
                        bar.configure(mode="indeterminate")
                        bar.start(14)
                    title = f"{banner} ({state['done']}건)"
                    self._job_banner_bits[channel] = f"{banner} ({state['done']}건)"
                else:
                    if str(bar.cget("mode")) != "determinate":
                        try:
                            bar.stop()
                        except Exception:
                            pass
                        bar.configure(mode="determinate")
                    bar.configure(maximum=max(1, state["total"]))
                    bar["value"] = min(state["done"], state["total"])
                    title = f"{banner} ({state['done']}/{state['total']})"
                    self._job_banner_bits[channel] = (
                        f"{banner} ({state['done']}/{state['total']})"
                    )
                if ok or fail:
                    title += f"  · 성공 {ok} · 실패 {fail}"
                state["title"].set(title)
                state["detail"].set((detail or "").strip())
                self._refresh_job_banner()
            except Exception:
                pass

        try:
            self.after(0, _do)
        except Exception:
            _do()

    def _finish_channel_progress(
        self,
        channel: str,
        *,
        summary: str = "",
        done: int | None = None,
        total: int | None = None,
        ok: int = 0,
        fail: int = 0,
    ) -> None:
        def _do() -> None:
            state = self._log_progress.get(channel)
            if not state:
                return
            try:
                bar: ttk.Progressbar = state["bar"]
                try:
                    bar.stop()
                except Exception:
                    pass
                t = int(total if total is not None else state.get("total") or 0)
                d = int(done if done is not None else (t or state.get("done") or 0))
                if t <= 0:
                    t = max(d, 1)
                bar.configure(mode="determinate", maximum=max(1, t))
                bar["value"] = max(1, t) if (ok or d) else 0
                title = f"완료 ({d}/{t})"
                if ok or fail:
                    title = f"완료 — 성공 {ok} · 실패 {fail} ({d}/{t})"
                state["title"].set(title)
                state["detail"].set((summary or "").strip())
                state["done"] = d
                state["total"] = t
                self._job_banner_bits.pop(channel, None)
                self._refresh_job_banner()
            except Exception:
                pass

        try:
            self.after(0, _do)
        except Exception:
            _do()

    def _reset_channel_progress(self, channel: str) -> None:
        def _do() -> None:
            state = self._log_progress.get(channel)
            if not state:
                return
            try:
                bar: ttk.Progressbar = state["bar"]
                try:
                    bar.stop()
                except Exception:
                    pass
                bar.configure(mode="determinate", maximum=100)
                bar["value"] = 0
                state["title"].set(state.get("idle") or "대기")
                state["detail"].set("")
                state["done"] = 0
                state["total"] = 0
            except Exception:
                pass

        try:
            self.after(0, _do)
        except Exception:
            _do()

    def _open_publish_progress(self, total: int, *, action: str = "홈페이지상품등록중") -> dict:
        """Progress via log-tab gauge (no popup window)."""
        total = max(1, int(total))
        self._set_channel_progress(
            LOG_MALL,
            done=0,
            total=total,
            action=action,
            detail="준비 중…",
        )

        state = {
            "total": total,
            "done": 0,
            "ok": 0,
            "fail": 0,
            "action": action,
        }

        def ui_line(msg: str) -> None:
            self._put_log(msg, channel=LOG_MALL)

        def ui_progress(done: int, current: str, ok: int = 0, fail: int = 0) -> None:
            state["done"] = done
            state["ok"] = ok
            state["fail"] = fail
            tot = max(1, int(state.get("total") or total))
            self._set_channel_progress(
                LOG_MALL,
                done=done,
                total=tot,
                action=action,
                detail=current,
                ok=ok,
                fail=fail,
            )

        def ui_counts(ok: int, fail: int) -> None:
            state["ok"] = ok
            state["fail"] = fail

        def ui_finish(summary: str, ok: int, fail: int) -> None:
            state["ok"] = ok
            state["fail"] = fail
            tot = max(1, int(state.get("total") or total))
            self._finish_channel_progress(
                LOG_MALL,
                summary=summary,
                done=tot,
                total=tot,
                ok=ok,
                fail=fail,
            )

        def ui_set_total(new_total: int) -> None:
            state["total"] = max(1, int(new_total))
            self._set_channel_progress(
                LOG_MALL,
                done=int(state.get("done") or 0),
                total=state["total"],
                action=action,
                detail=state.get("detail_msg") or "",
                ok=int(state.get("ok") or 0),
                fail=int(state.get("fail") or 0),
            )

        state["line"] = ui_line
        state["progress"] = ui_progress
        state["counts"] = ui_counts
        state["finish"] = ui_finish
        state["set_total"] = ui_set_total
        return state

    def _open_search_progress(self, total: int, *, action: str = "이미지 검색중") -> dict:
        """Progress via log-tab gauge for image search (no popup window)."""
        total = max(1, int(total))
        self._set_channel_progress(
            LOG_SEARCH,
            done=0,
            total=total,
            action=action,
            detail="준비 중…",
        )

        state = {
            "total": total,
            "done": 0,
            "ok": 0,
            "fail": 0,
            "action": action,
        }

        def ui_line(msg: str) -> None:
            self._put_log(msg, channel=LOG_SEARCH)

        def ui_progress(done: int, current: str, ok: int = 0, fail: int = 0) -> None:
            state["done"] = done
            state["ok"] = ok
            state["fail"] = fail
            tot = max(1, int(state.get("total") or total))
            self._set_channel_progress(
                LOG_SEARCH,
                done=done,
                total=tot,
                action=action,
                detail=current,
                ok=ok,
                fail=fail,
            )

        def ui_counts(ok: int, fail: int) -> None:
            state["ok"] = ok
            state["fail"] = fail

        def ui_finish(summary: str, ok: int, fail: int) -> None:
            state["ok"] = ok
            state["fail"] = fail
            tot = max(1, int(state.get("total") or total))
            self._finish_channel_progress(
                LOG_SEARCH,
                summary=summary,
                done=tot,
                total=tot,
                ok=ok,
                fail=fail,
            )

        def ui_set_total(new_total: int) -> None:
            state["total"] = max(1, int(new_total))
            self._set_channel_progress(
                LOG_SEARCH,
                done=int(state.get("done") or 0),
                total=state["total"],
                action=action,
                detail=state.get("detail_msg") or "",
                ok=int(state.get("ok") or 0),
                fail=int(state.get("fail") or 0),
            )

        state["line"] = ui_line
        state["progress"] = ui_progress
        state["counts"] = ui_counts
        state["finish"] = ui_finish
        state["set_total"] = ui_set_total
        return state

    def _publish_enqueue_jobs(
        self,
        jobs: list[tuple[Product, list[str] | None, list[str] | None, str | None]],
    ) -> int:
        """Add publish jobs to the queue. Skips duplicates already queued/active. Returns added count."""
        added = 0
        with self._publish_lock:
            for job in jobs:
                pid = int(job[0].id)
                if pid == self._publish_active_id or pid in self._publish_queued_ids:
                    continue
                self._publish_queued_ids.add(pid)
                self._publish_q.put(job)
                self._publish_total += 1
                added += 1
            total = self._publish_total
            prog = self._publish_prog
        if added and prog is not None:
            try:
                prog["set_total"](total)
            except Exception:
                pass
            self.after(
                0,
                lambda t=total, a=added: self.status.set(
                    f"등록 대기열 +{a} · 총 {t}개"
                ),
            )
        if added:
            self.after(0, self._refresh_list_busy)
        return added

    def _on_publish(self) -> None:
        if self.list_mode.get() != "products":
            return
        ids = self._selected_product_ids()
        if not ids:
            messagebox.showwarning("선택", "등록할 상품을 선택하세요.")
            return

        # 현재 편집 중인 항목은 폼 값 먼저 저장 (빈 가격코드 → 자동 문구)
        if self.current_id is not None and self.current_id in ids:
            sku = effective_price_code(self.sku_var.get())
            if not self.sku_var.get().strip():
                self.sku_var.set(sku)
            self.store.update_description(
                self.current_id,
                title=self.title_var.get().strip(),
                search_code=self.code_var.get().strip(),
                sku_no=sku,
                tags=self.tags_var.get().strip(),
                description=self.desc.get("1.0", "end").strip(),
                category=self.category_var.get().strip(),
                google_name=self.google_name_var.get().strip(),
                name_en=self.name_en_var.get().strip(),
                colors=self.color_var.get().strip(),
                sizes=self.size_var.get().strip(),
            )
            self._refresh_price_preview()

        running = self._job_running("publish")
        if running:
            if not messagebox.askyesno(
                "등록 추가",
                f"홈페이지 등록이 진행 중입니다.\n"
                f"선택한 {len(ids)}개를 이어서 등록할까요?",
            ):
                return
        elif len(ids) > 1 and not messagebox.askyesno(
            "일괄 등록",
            f"선택한 {len(ids)}개 상품을 홈페이지에 등록할까요?\n"
            "진행 중에도 다른 상품을 추가 등록할 수 있습니다.",
        ):
            return

        # 등록 후 선택 유지용: 선택 밖 다음 상품 (첫 배치 기준)
        selected = set(ids)
        next_id: int | None = None
        last_i = max(
            (i for i, p in enumerate(self.products) if p.id in selected),
            default=-1,
        )
        for j in range(last_i + 1, len(self.products)):
            if self.products[j].id not in selected:
                next_id = self.products[j].id
                break
        if next_id is None:
            for j in range(last_i - 1, -1, -1):
                if self.products[j].id not in selected:
                    next_id = self.products[j].id
                    break
        yview = self.listbox.yview()
        y0, y1 = float(yview[0]), float(yview[1])

        jobs: list[tuple[Product, list[str] | None, list[str] | None, str | None]] = []
        for pid in ids:
            product = self.store.get(pid)
            if not product:
                continue
            product = self._ensure_price_code(product)
            if pid == self.current_id:
                colors = re_split_colors(self.color_var.get())
                sizes = self._split_csv(self.size_var.get())
                category = self.category_var.get().strip() or None
            else:
                colors = re_split_colors(product.colors)
                sizes = self._split_csv(product.sizes)
                category = product.category or None
            jobs.append((product, colors, sizes, category))

        if not jobs:
            messagebox.showwarning("없음", "등록할 상품이 없습니다.")
            return

        if running:
            added = self._publish_enqueue_jobs(jobs)
            if added <= 0:
                messagebox.showinfo(
                    "등록 추가",
                    "선택한 상품은 이미 등록 대기 중이거나 처리 중입니다.",
                )
            else:
                self._append(
                    f"등록 대기열에 {added}개 추가 (총 {self._publish_total}개)",
                    channel=LOG_MALL,
                )
                messagebox.showinfo(
                    "등록 추가",
                    f"{added}개를 등록 대기열에 넣었습니다.\n"
                    f"현재 총 {self._publish_total}개 진행·대기 중.",
                )
            return

        if not self._job_start("publish"):
            # 거의 동시에 시작된 경우 → 대기열에 추가
            added = self._publish_enqueue_jobs(jobs)
            if added:
                self._append(
                    f"등록 대기열에 {added}개 추가 (총 {self._publish_total}개)",
                    channel=LOG_MALL,
                )
            return

        with self._publish_lock:
            self._publish_total = 0
            self._publish_done = 0
            self._publish_ok = 0
            self._publish_fail = 0
            self._publish_lines = []
            self._publish_queued_ids.clear()
            self._publish_active_id = None
            # drain stale queue
            while True:
                try:
                    self._publish_q.get_nowait()
                except queue.Empty:
                    break

        added = self._publish_enqueue_jobs(jobs)
        if added <= 0:
            self._job_end("publish")
            messagebox.showwarning("없음", "등록할 상품이 없습니다.")
            return

        self._publish_next_id = next_id
        self._publish_yview = (y0, y1)
        total = self._publish_total
        prog = self._open_publish_progress(total)
        self._publish_prog = prog
        self._append(f"----- 홈페이지 등록 {total}개 (실시간 · 추가등록 가능) -----")
        self.status.set(f"홈페이지 등록 중 0/{total}")
        threading.Thread(target=self._publish_worker_loop, daemon=True).start()

    def _publish_worker_loop(self) -> None:
        """Drain homepage-publish queue until empty (supports mid-run enqueue)."""
        prog = self._publish_prog
        idle_rounds = 0
        try:
            while True:
                try:
                    job = self._publish_q.get(timeout=0.35)
                    idle_rounds = 0
                except queue.Empty:
                    idle_rounds += 1
                    # 잠시 비어 있어도 추가 등록을 받을 여유
                    if idle_rounds >= 2:
                        with self._publish_lock:
                            if self._publish_q.empty() and self._publish_active_id is None:
                                break
                    continue

                product, colors, sizes, category = job
                pid = int(product.id)
                try:
                    self._ensure_images_for_action(product_id=pid)
                    fresh = self.store.get(pid)
                    if fresh:
                        product.image_paths = fresh.image_paths
                        product.cover_path = fresh.cover_path
                except Exception:
                    pass
                with self._publish_lock:
                    self._publish_active_id = pid
                    self._publish_queued_ids.discard(pid)
                    done_before = self._publish_done
                    total = self._publish_total
                    ok_n = self._publish_ok
                    fail_n = self._publish_fail
                self.after(0, self._refresh_list_busy)

                name_hint = (
                    (product.google_name or product.title or f"#{product.id}").strip()
                )[:36]
                i = done_before + 1
                if prog:
                    prog["progress"](
                        done_before,
                        f"[{i}/{total}] 등록 중… #{product.id} {name_hint}",
                        ok_n,
                        fail_n,
                    )
                    prog["line"](f"→ [{i}/{total}] #{product.id} {name_hint}")
                self._put_log(
                    f"[{i}/{total}] 홈페이지 등록 중… #{product.id}",
                    channel=LOG_MALL,
                )
                self.after(
                    0,
                    lambda i=i, t=total: self.status.set(f"홈페이지 등록 중 {i}/{t}"),
                )

                try:
                    result = publish_product(
                        product,
                        colors=colors,
                        sizes=sizes,
                        category=category,
                        push_api=True,
                    )
                    item = result["product"]
                    mall_id = str(item.get("id") or "")
                    archived = self.store.archive_published(
                        product.id,
                        mall_id=mall_id,
                        note=result.get("priceLabel") or "",
                    )
                    # Push each success so the other PC sees 등록 list promptly.
                    self._mark_catalog_dirty(push_now=True)
                    line = (
                        f"  ✓ 성공 → 등록 #{archived} / {item.get('name') or ''} "
                        f"/ {result.get('priceLabel') or ''} / {result.get('api') or ''}"
                    )
                    with self._publish_lock:
                        self._publish_ok += 1
                        self._publish_lines.append(f"#{product.id} → 등록 #{archived}")
                        ok_n = self._publish_ok
                        fail_n = self._publish_fail
                    if prog:
                        prog["line"](line)
                    self._put_log(
                        f"홈페이지 등록: {mall_id} / {result.get('priceLabel')} "
                        f"→ 등록 목록 #{archived}",
                        channel=LOG_MALL,
                    )
                    # 목록만 조용히 갱신 — 등록 목록 선택/상세는 건드리지 않음
                    self.after(
                        0,
                        lambda: self.refresh_list(reload_detail=False, quiet=True),
                    )
                except Exception as e:
                    err = str(e)
                    with self._publish_lock:
                        self._publish_fail += 1
                        self._publish_lines.append(f"#{product.id} 실패: {err}")
                        ok_n = self._publish_ok
                        fail_n = self._publish_fail
                    if prog:
                        prog["line"](f"  ✗ 실패: {err}")
                    self._put_log(f"등록 실패 #{product.id}: {err}", channel=LOG_MALL)

                with self._publish_lock:
                    self._publish_done += 1
                    self._publish_active_id = None
                    done = self._publish_done
                    total = self._publish_total
                    ok_n = self._publish_ok
                    fail_n = self._publish_fail
                self.after(0, self._refresh_list_busy)

                if prog:
                    prog["progress"](
                        done,
                        f"[{done}/{total}] 완료 — 성공 {ok_n} · 실패 {fail_n}",
                        ok_n,
                        fail_n,
                    )

            with self._publish_lock:
                ok_final = self._publish_ok
                fail_final = self._publish_fail
                lines = list(self._publish_lines)
                next_id = self._publish_next_id
                yview = self._publish_yview
                # 마지막 순간에 추가된 항목이 있으면 종료하지 않고 이어서 처리
                if not self._publish_q.empty():
                    threading.Thread(
                        target=self._publish_worker_loop, daemon=True
                    ).start()
                    return

            summary = "\n".join(lines[:14])
            if len(lines) > 14:
                summary += f"\n… 외 {len(lines) - 14}건"
            if prog:
                prog["finish"](f"성공 {ok_final} / 실패 {fail_final}", ok_final, fail_final)

            def finish() -> None:
                # finish 직전 또 추가됐으면 워커만 재개 (팝업/job_end 생략)
                with self._publish_lock:
                    if not self._publish_q.empty():
                        threading.Thread(
                            target=self._publish_worker_loop, daemon=True
                        ).start()
                        return
                    self._publish_prog = None
                    self._publish_active_id = None
                    self._publish_queued_ids.clear()

                self._mark_catalog_dirty(push_now=True)
                mode = self.list_mode.get()
                if mode == "products":
                    if next_id is not None:
                        self.current_id = next_id
                        self._sticky_selected_ids = [next_id]
                    self.current_published_id = None
                    if yview is not None:
                        self.refresh_list(
                            preserve_yview=yview,
                            reload_detail=True,
                            quiet=False,
                        )
                    else:
                        self.refresh_list(reload_detail=True, quiet=False)
                else:
                    self.refresh_list(reload_detail=False, quiet=True)
                self.status.set(
                    f"홈페이지 등록 완료 — 성공 {ok_final} / 실패 {fail_final}"
                )
                self._job_end("publish")
                if fail_final and ok_final == 0:
                    messagebox.showerror("등록 실패", summary)
                else:
                    messagebox.showinfo(
                        "홈페이지 등록",
                        f"완료: 성공 {ok_final} / 실패 {fail_final}\n\n{summary}\n\n"
                        "쇼핑몰을 새로고침하면 반영됩니다.",
                    )

            self.after(0, finish)
        except Exception as e:
            self._put_log(f"등록 오류: {e}", channel=LOG_MALL)
            if prog:
                prog["line"](f"오류: {e}")
                prog["finish"]("오류로 중단", self._publish_ok, self._publish_fail)
            self.after(0, lambda: messagebox.showerror("등록 실패", str(e)))
            with self._publish_lock:
                self._publish_prog = None
                self._publish_active_id = None
                self._publish_queued_ids.clear()
            self.after(0, lambda: self._job_end("publish"))
        # 정상 완료는 finish()에서 job_end

    def _on_republish(self) -> None:
        """Re-push selected published items to homepage with current form/DB fields."""
        if self.list_mode.get() != "published":
            return
        ids = self._selected_published_ids()
        if not ids:
            messagebox.showwarning("선택", "재등록할 상품을 선택하세요.")
            return
        self._republish_ids(ids, confirm=True)

    def _on_category_chosen(self, _evt=None) -> None:
        """Changing 등록 카테고리 immediately updates the homepage row."""
        if self._form_loading:
            return
        if self.list_mode.get() != "published":
            self._soft_save_current(force=True)
            return
        prev_id = self.current_published_id
        self._soft_save_current(force=True)
        if prev_id is not None:
            self._push_published_meta_async(
                [prev_id],
                log_msg="카테고리 변경 → 홈페이지 반영",
            )

    def _push_published_meta_async(
        self, ids: list[int], *, log_msg: str = ""
    ) -> None:
        ids = [int(i) for i in ids if i]
        if not ids or not cloud_enabled():
            return
        if log_msg:
            self._append(log_msg, channel=LOG_MALL)

        def work() -> None:
            try:
                stats = push_published_metadata(
                    self.store,
                    ids,
                    on_log=lambda m: self._put_log(m, channel=LOG_MALL),
                )
                self._put_log(
                    f"[맞추기] 필드 반영 완료 patched={stats.get('patched', 0)} "
                    f"ok={stats.get('ok', 0)} missing={stats.get('missing', 0)}",
                    channel=LOG_MALL,
                )
                self._mark_catalog_dirty(push_now=True)
            except Exception as e:  # noqa: BLE001
                self._put_log(f"[맞추기] 필드 반영 실패: {e}", channel=LOG_MALL)

        threading.Thread(target=work, daemon=True).start()

    def _maybe_sync_homepage_metadata(self) -> None:
        """Background: keep homepage fields identical to 「등록」 on every launch."""
        if not cloud_enabled():
            return

        def work() -> None:
            try:
                stats = push_published_metadata(
                    self.store,
                    on_log=lambda m: self._put_log(m, channel=LOG_MALL),
                )
                n = int(stats.get("patched") or 0)
                if n:
                    self._put_log(
                        f"[맞추기] 시작 시 홈페이지 필드 {n}건을 등록 목록에 맞춤",
                        channel=LOG_MALL,
                    )
                    self._mark_catalog_dirty(push_now=True)
            except Exception as e:  # noqa: BLE001
                self._put_log(f"[맞추기] 시작 시 필드 동기화 실패: {e}", channel=LOG_MALL)

        threading.Thread(target=work, daemon=True).start()

    def _maybe_auto_reconcile_homepage(self) -> None:
        """Run site reconcile once after upgrade so existing 「등록」 land on homepage."""
        flag = f"homepage_reconcile_done_v{APP_VERSION}"
        try:
            if (self.store.get_setting(flag, "") or "").strip() == "1":
                return
        except Exception:
            return
        if not cloud_enabled():
            return
        self._put_log(
            "[맞추기] 업데이트 후 자동: 등록 목록 ↔ 홈페이지 확인/중복정리 시작",
            channel=LOG_MALL,
        )
        self._run_reconcile_homepage(confirm=False, mark_flag=flag)

    def _on_reconcile_homepage(self) -> None:
        self._run_reconcile_homepage(confirm=True, mark_flag="")

    def _run_reconcile_homepage(self, *, confirm: bool, mark_flag: str) -> None:
        if confirm and not messagebox.askyesno(
            "사이트 전체 맞추기",
            "로컬 「등록」 목록을 홈페이지와 맞춥니다.\n\n"
            "· 카테고리/이름/추천이 다르면 프로그램 값으로 수정\n"
            "· 홈페이지에 없는 등록 상품 → 재등록\n"
            "· 홈페이지 중복 상품 → 하나만 남기고 삭제\n\n"
            "시간이 걸릴 수 있습니다. 진행할까요?",
        ):
            return
        if not self._job_start("publish"):
            self._warn_job_busy("publish")
            return
        total = 0
        try:
            total = int(self.store.count_published() or 0)
        except Exception:
            total = 0
        prog = self._open_publish_progress(
            max(total, 1), action="사이트맞추기중"
        )
        self.status.set("사이트 전체 맞추기 중…")

        def work() -> None:
            try:
                stats = reconcile_published_to_homepage(
                    self.store,
                    on_log=lambda m: self._put_log(m, channel=LOG_MALL),
                    on_progress=lambda done, tot, msg: self.after(
                        0, lambda: self._reconcile_progress(prog, done, tot, msg)
                    ),
                    dedupe_first=True,
                )
                self._mark_catalog_dirty(push_now=True)
                summary = (
                    f"이미같음 {stats.get('ok', 0)} · "
                    f"필드수정 {stats.get('patched', 0)} · "
                    f"재등록 {stats.get('fixed', 0)} · "
                    f"실패 {stats.get('failed', 0)} · "
                    f"중복삭제 {stats.get('deduped', 0)}"
                )
                prog["finish"](
                    summary,
                    int(stats.get("fixed") or 0),
                    int(stats.get("failed") or 0),
                )

                def done_ui() -> None:
                    self._job_end("publish")
                    self.refresh_list(reload_detail=False, quiet=True)
                    self.status.set(f"사이트 맞추기 완료 — {summary}")
                    if confirm or int(stats.get("fixed") or 0) or int(
                        stats.get("deduped") or 0
                    ):
                        messagebox.showinfo("사이트 전체 맞추기", summary)

                self.after(0, done_ui)
            except Exception as e:  # noqa: BLE001
                err = str(e)
                prog["finish"](f"실패: {err}", 0, 1)

                def fail() -> None:
                    self._job_end("publish")
                    self._put_log(f"[맞추기] 오류: {err}", channel=LOG_MALL)
                    if confirm:
                        messagebox.showerror("사이트 전체 맞추기", err)

                self.after(0, fail)
            finally:
                # Auto-run once per version even if it failed (avoid every-launch retry)
                if mark_flag:
                    try:
                        self.store.set_setting(mark_flag, "1")
                    except Exception:
                        pass

        threading.Thread(target=work, daemon=True).start()

    def _reconcile_progress(self, prog: dict, done: int, total: int, msg: str) -> None:
        try:
            prog["progress"](done, msg, 0, 0)
        except Exception:
            pass

    def _republish_ids(self, ids: list[int], *, confirm: bool = True) -> None:
        """Re-push given published ids to the homepage."""
        if not ids:
            messagebox.showwarning("선택", "재등록할 상품을 선택하세요.")
            return
        if not self._job_start("publish"):
            self._warn_job_busy("publish")
            return

        # Persist current form onto the focused published row
        if self.current_published_id is not None and self.current_published_id in ids:
            self._soft_save_current(force=True)

        if confirm and not messagebox.askyesno(
            "재등록",
            f"선택한 {len(ids)}개 상품을 수정된 정보로\n"
            "홈페이지에 다시 올릴까요?\n\n"
            "같은 상품(mall_id)을 덮어씁니다.",
        ):
            self._job_end("publish")
            return

        jobs: list[tuple[PublishedItem, Product, list[str], list[str], str | None]] = []
        for pub_id in ids:
            item = self.store.get_published(pub_id)
            if not item:
                continue
            # Empty price → default label
            if not (item.sku_no or "").strip():
                self.store.update_published(pub_id, sku_no=DEFAULT_PRICE_TEXT)
                item = self.store.get_published(pub_id) or item
            product = self.store.published_to_product(item)
            if pub_id == self.current_published_id:
                colors = re_split_colors(self.color_var.get())
                sizes = self._split_csv(self.size_var.get())
                category = self.category_var.get().strip() or None
                # Prefer live form values for the focused row
                product.google_name = self.google_name_var.get().strip()
                product.name_en = self.name_en_var.get().strip()
                product.search_code = self.code_var.get().strip()
                product.sku_no = effective_price_code(self.sku_var.get())
                product.tags = self.tags_var.get().strip()
                product.title = self.title_var.get().strip() or product.title
                product.description = self._published_desc_body()
                product.colors = self.color_var.get().strip()
                product.sizes = self.size_var.get().strip()
                product.category = category or product.category
            else:
                colors = re_split_colors(item.colors)
                sizes = self._split_csv(item.sizes)
                category = item.category or None
            jobs.append((item, product, colors, sizes, category))

        if not jobs:
            self._job_end("publish")
            messagebox.showwarning("없음", "재등록할 상품이 없습니다.")
            return

        total = len(jobs)
        prog = self._open_publish_progress(total, action="홈페이지재등록중")
        self._append(f"----- 홈페이지 재등록 {total}개 -----")
        self.status.set(f"재등록 중 0/{total}")

        def work() -> None:
            ok_n = 0
            fail_n = 0
            lines: list[str] = []
            try:
                for i, (item, product, colors, sizes, category) in enumerate(jobs, start=1):
                    name_hint = (
                        (product.google_name or product.title or f"#{item.id}").strip()
                    )[:36]
                    mall_id = (item.mall_id or "").strip() or f"wg-{product.id}"
                    prog["progress"](
                        i - 1,
                        f"[{i}/{total}] 재등록 중… #{item.id} {name_hint}",
                        ok_n,
                        fail_n,
                    )
                    prog["line"](f"→ [{i}/{total}] 등록#{item.id} → {mall_id} · {name_hint}")
                    self._put_log(f"[{i}/{total}] 재등록 중… 등록#{item.id}", channel=LOG_MALL)
                    try:
                        self._ensure_images_for_action(published_id=item.id)
                        fresh_item = self.store.get_published(item.id)
                        if fresh_item:
                            product.image_paths = fresh_item.image_paths
                            product.cover_path = fresh_item.cover_path
                    except Exception:
                        pass
                    try:
                        result = publish_product(
                            product,
                            colors=colors,
                            sizes=sizes,
                            category=category,
                            push_api=True,
                            mall_id=mall_id,
                        )
                        new_mall = str((result.get("product") or {}).get("id") or mall_id)
                        self.store.update_published(
                            item.id,
                            mall_id=new_mall,
                            note=result.get("priceLabel") or item.note,
                            google_name=product.google_name,
                            name_en=product.name_en,
                            search_code=product.search_code,
                            sku_no=product.sku_no,
                            title=product.title,
                            tags=product.tags,
                            description=product.description,
                            category=product.category,
                            colors=product.colors,
                            sizes=product.sizes,
                        )
                        ok_n += 1
                        brand = (result.get("product") or {}).get("brand") or ""
                        line = (
                            f"  ✓ 재등록 성공 / {brand} / "
                            f"{result.get('priceLabel') or ''} / {result.get('api') or ''}"
                        )
                        lines.append(f"등록#{item.id} → {new_mall} ({brand})")
                        prog["line"](line)
                        self._put_log(f"재등록 완료: {new_mall} / {brand}", channel=LOG_MALL)
                    except Exception as e:
                        fail_n += 1
                        err = str(e)
                        lines.append(f"등록#{item.id} 실패: {err}")
                        prog["line"](f"  ✗ 실패: {err}")
                        self._put_log(f"재등록 실패 #{item.id}: {err}", channel=LOG_MALL)
                    prog["progress"](
                        i,
                        f"[{i}/{total}] 완료 — 성공 {ok_n} · 실패 {fail_n}",
                        ok_n,
                        fail_n,
                    )

                summary = "\n".join(lines[:14])
                if len(lines) > 14:
                    summary += f"\n… 외 {len(lines) - 14}건"
                ok_final, fail_final = ok_n, fail_n
                prog["finish"](f"성공 {ok_final} / 실패 {fail_final}", ok_final, fail_final)

                def finish() -> None:
                    self._mark_catalog_dirty()
                    # 등록 목록 선택·다중선택·상세 유지
                    self.refresh_list(reload_detail=False, quiet=True)
                    self.status.set(
                        f"재등록 완료 — 성공 {ok_final} / 실패 {fail_final}"
                    )
                    if fail_final and ok_final == 0:
                        messagebox.showerror("재등록 실패", summary)
                    else:
                        messagebox.showinfo(
                            "재등록",
                            f"완료: 성공 {ok_final} / 실패 {fail_final}\n\n{summary}\n\n"
                            "쇼핑몰을 새로고침하면 반영됩니다.",
                        )

                self.after(0, finish)
            except Exception as e:
                self._put_log(f"재등록 오류: {e}", channel=LOG_MALL)
                prog["line"](f"오류: {e}")
                prog["finish"]("오류로 중단", ok_n, fail_n)
                self.after(0, lambda: messagebox.showerror("재등록 실패", str(e)))
            finally:
                self.after(0, lambda: self._job_end("publish"))

        threading.Thread(target=work, daemon=True).start()

    def _on_recommend(self) -> None:
        """Mark selected published products as homepage recommended carousel items."""
        self._set_recommended_for_selection(True)

    def _on_unrecommend(self) -> None:
        """Remove selected products from homepage recommended carousel."""
        self._set_recommended_for_selection(False)

    def _set_recommended_for_selection(self, want: bool) -> None:
        if self.list_mode.get() != "published":
            return
        if not self._job_start("publish"):
            self._warn_job_busy("publish")
            return
        ids = self._selected_published_ids()
        if not ids:
            self._job_end("publish")
            messagebox.showwarning(
                "선택",
                "추천으로 올릴 상품을 선택하세요."
                if want
                else "추천 해제할 상품을 선택하세요.",
            )
            return

        selected = [self.store.get_published(i) for i in ids]
        selected = [p for p in selected if p]
        if not selected:
            self._job_end("publish")
            messagebox.showwarning("없음", "선택한 등록 상품을 찾을 수 없습니다.")
            return

        if want:
            from collections import Counter

            counts = Counter(recommend_slot_label(p.category) for p in selected)
            detail = "\n".join(f"· {name}  {n}개" for name, n in counts.items())
            if not messagebox.askyesno(
                "추천상품으로 재등록하기",
                f"선택한 {len(selected)}개를 분류에 맞는\n"
                "홈 추천 칸에 올릴까요?\n\n"
                f"{detail}\n\n"
                "가방 → 가방 추천상품\n"
                "여성옷/남성옷 → 옷 추천상품\n"
                "그 외 → 악세사리 추천상품",
            ):
                self._job_end("publish")
                return
        else:
            if not messagebox.askyesno(
                "추천상품 해제",
                f"선택한 {len(selected)}개를 홈 추천 칸에서\n"
                "해제할까요?\n\n"
                "상품 등록 자체는 유지됩니다.",
            ):
                self._job_end("publish")
                return

        jobs: list[tuple[PublishedItem, str]] = []
        for item in selected:
            mall_id = resolve_mall_id(
                mall_id=item.mall_id,
                search_code=item.search_code,
                goods_id=item.goods_id,
            )
            # Persist resolved id so later sync / recommend use the real homepage id.
            if mall_id and mall_id != (item.mall_id or "").strip():
                try:
                    self.store.update_published(item.id, mall_id=mall_id)
                    item = self.store.get_published(item.id) or item
                except Exception:
                    pass
            jobs.append((item, mall_id))

        self._append(
            f"----- 추천 상품 {'등록' if want else '해제'} {len(jobs)}개 -----"
        )
        self.status.set(f"추천 반영 중… 0/{len(jobs)}")

        def work() -> None:
            try:
                known = [(it, mid) for it, mid in jobs if mid]
                unknown = [it for it, mid in jobs if not mid]
                mall_ids = [m for _, m in known]
                result: dict = {
                    "updated": 0,
                    "api": "skipped",
                    "missing": [],
                    "products": [],
                }
                ok_malls: set[str] = set()
                if mall_ids:
                    result = set_products_recommended(
                        mall_ids, recommended=want, push_api=True
                    )
                    for p in result.get("products") or []:
                        if isinstance(p, dict) and p.get("id"):
                            ok_malls.add(str(p["id"]))
                missing = list(result.get("missing") or [])
                for it in unknown:
                    missing.append(f"(등록#{it.id})")

                republished = 0
                # Missing on homepage → publish with recommended flag in one shot.
                if want and (missing or unknown):
                    miss_set = set(missing)
                    need_items: list[PublishedItem] = []
                    for it, mid in jobs:
                        if not mid or mid in miss_set:
                            need_items.append(it)
                    for it in need_items:
                        try:
                            self._ensure_images_for_action(published_id=it.id)
                            fresh = self.store.get_published(it.id) or it
                            product = self.store.published_to_product(fresh)
                            colors = re_split_colors(fresh.colors)
                            sizes = self._split_csv(fresh.sizes)
                            category = fresh.category or None
                            force_mid = (fresh.mall_id or "").strip() or None
                            if not force_mid:
                                gid = (fresh.goods_id or "").strip()
                                code = (fresh.search_code or "").strip()
                                if gid:
                                    force_mid = f"wg-{gid}"
                                elif code:
                                    force_mid = f"wg-sc-{code}"
                            pub = publish_product(
                                product,
                                colors=colors or None,
                                sizes=sizes or None,
                                category=category,
                                push_api=True,
                                mall_id=force_mid,
                                recommended=True,
                            )
                            new_mid = str((pub.get("product") or {}).get("id") or "")
                            api_note = str(pub.get("api") or "")
                            if new_mid and not _api_failed(api_note):
                                # Confirm homepage flag
                                confirm = set_products_recommended(
                                    [new_mid], recommended=True, push_api=True
                                )
                                if int(confirm.get("updated") or 0) < 1:
                                    raise RuntimeError(
                                        f"홈페이지 추천 확인 실패: {new_mid}"
                                    )
                                self.store.update_published(
                                    it.id, mall_id=new_mid, recommended=True
                                )
                                ok_malls.add(new_mid)
                                republished += 1
                                if new_mid in missing:
                                    missing.remove(new_mid)
                                tag = f"(등록#{it.id})"
                                if tag in missing:
                                    missing.remove(tag)
                                result["updated"] = int(result.get("updated") or 0) + 1
                                result["api"] = api_note
                                self._put_log(
                                    f"추천: 홈 등록+추천 완료 #{it.id} → {new_mid}",
                                    channel=LOG_MALL,
                                )
                            else:
                                raise RuntimeError(api_note or "API 실패")
                        except Exception as e:
                            self._put_log(
                                f"추천 재등록 실패 등록#{it.id}: {e}",
                                channel=LOG_MALL,
                            )

                updated = len(ok_malls) if ok_malls else int(result.get("updated") or 0)
                # Local [추천] only when homepage actually accepted the flag.
                for item, mall_id in known:
                    if mall_id and mall_id in ok_malls:
                        self.store.update_published(item.id, recommended=want)
                    elif mall_id and mall_id in missing:
                        # Keep local honest if homepage reject
                        if not want:
                            continue
                        # leave recommended as-is; do not fake success

                def finish() -> None:
                    self._mark_catalog_dirty(push_now=True)
                    self.refresh_list(reload_detail=False, quiet=True)
                    label = "추천 등록" if want else "추천 해제"
                    self.status.set(f"{label} 완료 — {updated}개")
                    msg = (
                        f"{label} 완료: {updated}개\n"
                        f"API: {result.get('api') or ''}"
                    )
                    if republished:
                        msg += f"\n홈 재등록 후 추천: {republished}개"
                    msg += "\n\n쇼핑몰을 새로고침하면 반영됩니다."
                    still_missing = [m for m in missing if m]
                    if still_missing:
                        msg += (
                            "\n\n아직 홈에 없는 상품:\n"
                            + "\n".join(still_missing[:8])
                        )
                        if len(still_missing) > 8:
                            msg += f"\n… 외 {len(still_missing) - 8}건"
                    if updated == 0:
                        messagebox.showerror("추천 실패", msg)
                    else:
                        messagebox.showinfo("추천 상품", msg)
                    self._append(
                        f"추천 {'ON' if want else 'OFF'} {updated}개"
                        + (f" / 재등록 {republished}" if republished else "")
                        + (f" / 누락 {len(still_missing)}" if still_missing else "")
                    )

                self.after(0, finish)
            except Exception as e:
                self._put_log(f"추천 반영 오류: {e}", channel=LOG_MALL)
                self.after(0, lambda: messagebox.showerror("추천 실패", str(e)))
            finally:
                self.after(0, lambda: self._job_end("publish"))

        threading.Thread(target=work, daemon=True).start()

    def _on_unpublish(self) -> None:
        if self.list_mode.get() != "published":
            return
        ids = self._selected_published_ids()
        if not ids:
            return
        if not messagebox.askyesno(
            "등록목록에서 제거",
            f"선택한 {len(ids)}개를 등록 목록에서 제거하고\n"
            "상품 관리 목록으로 되돌릴까요?\n\n"
            "※ 홈페이지에 등록된 상품도 함께 삭제됩니다.",
        ):
            return

        # Collect mall ids before local unpublish
        mall_ids: list[str] = []
        for pub_id in ids:
            item = self.store.get_published(pub_id)
            if not item:
                continue
            mid = (item.mall_id or "").strip()
            if not mid:
                mid = f"wg-{item.id}"
            mall_ids.append(mid)

        def work() -> None:
            api_note = ""
            try:
                if mall_ids:
                    result = delete_mall_products(mall_ids, push_api=True)
                    api_note = str(result.get("api") or "")
                    self._put_log(
                        f"홈페이지 상품 삭제: {len(mall_ids)}건 — {api_note}",
                        channel=LOG_MALL,
                    )
            except Exception as e:
                self._put_log(f"홈페이지 삭제 실패: {e}", channel=LOG_MALL)
                self.after(
                    0,
                    lambda: messagebox.showerror(
                        "홈페이지 삭제 실패",
                        f"홈페이지 상품을 지우지 못했습니다.\n"
                        f"등록 목록은 그대로 둡니다.\n\n{e}",
                    ),
                )
                return

            last_restored: int | None = None
            for pub_id in ids:
                restored_id = self.store.unpublish(pub_id)
                self._put_log(
                    f"등록목록 제거 #{pub_id} → 상품관리 #{restored_id}",
                    channel=LOG_MALL,
                )
                if restored_id is not None:
                    last_restored = restored_id
            self._mark_catalog_dirty()

            def finish() -> None:
                self.current_published_id = None
                if last_restored is not None:
                    self.current_id = last_restored
                    self.list_mode.set("products")
                    self._apply_mode_buttons()
                    self.refresh_list()
                else:
                    self.current_id = None
                    self.refresh_list()
                messagebox.showinfo(
                    "완료",
                    f"등록 목록에서 제거했습니다.\n"
                    f"홈페이지 상품도 삭제했습니다 ({len(mall_ids)}건).",
                )

            self.after(0, finish)

        threading.Thread(target=work, daemon=True).start()

    def _published_product_code(self, item: PublishedItem) -> str:
        """Homepage NO / 搜索码 used to link AI pins → product pages."""
        code = (item.search_code or "").strip()
        if code:
            return code
        code = (item.sku_no or "").strip()
        if code and not code.startswith("88880"):
            return code
        mall = (item.mall_id or "").strip()
        if mall.startswith("wg-"):
            return mall[3:]
        return mall or str(item.id)

    def _ai_item_from_published(self, item: PublishedItem) -> dict[str, str] | None:
        code = self._published_product_code(item)
        if not code:
            return None
        cat = (item.category or "가방").strip() or "가방"
        name = (item.google_name or item.title or code).strip()
        return {
            "code": code,
            "category": cat,
            "label": cat,
            "name": name,
            "pub_id": str(item.id),
        }

    def _merge_ai_items(self, picked: list[dict[str, str]], *, replace: bool = False) -> list[dict[str, str]]:
        base = [] if replace else list(self.ai_style_items)
        merged: list[dict[str, str]] = []
        seen: set[str] = set()
        for row in [*base, *picked]:
            c = (row.get("code") or "").strip()
            if not c or c in seen:
                continue
            seen.add(c)
            merged.append(row)
        self.ai_style_items = merged
        return merged

    def _published_folder(self, pub_id: int | str) -> pathlib.Path | None:
        """등록 상품 이미지 폴더 (published_covers/p{id})."""
        try:
            pid = int(pub_id)
        except (TypeError, ValueError):
            return None
        pack = self.store.published_img_root / f"p{pid}"
        if pack.is_dir():
            return pack
        item = self.store.get_published(pid)
        if not item:
            return None
        for cand in ([item.cover_path] if item.cover_path else []) + list(
            item.image_paths or []
        ):
            try:
                fp = pathlib.Path(cand)
                if fp.is_file():
                    return fp.parent
                if fp.is_dir():
                    return fp
            except Exception:
                continue
        return None

    def _ensure_published_txt(self, folder: pathlib.Path, pub_id: int | str) -> None:
        """폴더에 product.txt 가 없으면 등록 정보로 생성."""
        meta = folder / "product.txt"
        if meta.exists():
            return
        try:
            pid = int(pub_id)
        except (TypeError, ValueError):
            return
        item = self.store.get_published(pid)
        if not item:
            return
        lines = [
            item.google_name or item.title or "",
            f"搜索码：{item.search_code}" if item.search_code else "",
            f"NO：{item.sku_no}" if item.sku_no else "",
            item.tags or "",
            f"카테고리：{item.category}" if item.category else "",
            f"컬러：{item.colors}" if item.colors else "",
            f"사이즈：{item.sizes}" if item.sizes else "",
            "",
            item.description or "",
        ]
        try:
            meta.write_text(
                "\n".join(x for x in lines if x is not None).strip() + "\n",
                encoding="utf-8",
            )
        except OSError:
            pass

    def _zip_ai_style_products(self, parent: tk.Misc | None = None) -> None:
        """AI 코디 선택 상품마다 이미지+txt → zip, 각 폴더 열기."""
        if not self.ai_style_items:
            messagebox.showwarning("ZIP", "선택된 상품이 없습니다.", parent=parent)
            return

        img_ext = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}
        ok_n = 0
        fail_lines: list[str] = []
        opened: set[str] = set()

        for row in self.ai_style_items:
            code = (row.get("code") or "").strip() or "product"
            pub_id = row.get("pub_id") or ""
            try:
                self._ensure_images_for_action(published_id=int(pub_id))
            except (TypeError, ValueError):
                pass
            folder = self._published_folder(pub_id)
            if folder is None or not folder.is_dir():
                fail_lines.append(f"NO {code}: 폴더 없음")
                continue
            self._ensure_published_txt(folder, pub_id)
            files = sorted(
                [
                    f
                    for f in folder.iterdir()
                    if f.is_file()
                    and (
                        f.suffix.lower() in img_ext
                        or f.suffix.lower() == ".txt"
                    )
                    and f.suffix.lower() != ".zip"
                ],
                key=lambda p: p.name.lower(),
            )
            if not files:
                fail_lines.append(f"NO {code}: 이미지/txt 없음")
                continue
            safe = re.sub(r'[<>:"/\\|?*]+', "_", code).strip(" ._") or "product"
            zip_path = folder / f"{safe}.zip"
            try:
                with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                    for f in files:
                        zf.write(f, arcname=f.name)
                ok_n += 1
                self._put_log(f"ZIP: {zip_path} ({len(files)}개 파일)", channel=LOG_MALL)
                key = str(folder.resolve())
                if key not in opened:
                    opened.add(key)
                    try:
                        os.startfile(str(folder))  # type: ignore[attr-defined]
                    except Exception as e:
                        fail_lines.append(f"NO {code}: 폴더 열기 실패 ({e})")
            except Exception as e:
                fail_lines.append(f"NO {code}: {e}")

        summary = f"ZIP 완료: 성공 {ok_n} / 전체 {len(self.ai_style_items)}"
        if fail_lines:
            summary += "\n\n" + "\n".join(fail_lines[:12])
            if len(fail_lines) > 12:
                summary += f"\n… 외 {len(fail_lines) - 12}건"
        if ok_n:
            messagebox.showinfo("상품 ZIP", summary, parent=parent)
        else:
            messagebox.showerror("상품 ZIP", summary, parent=parent)

    def _on_ai_select(self) -> None:
        """등록 목록 다중 선택 → AI 코디 후보에 추가 (여러 개 누적)."""
        if self.list_mode.get() != "published":
            messagebox.showinfo(
                "AI 상품선택",
                "[등록] 탭에서 상품을 선택한 뒤 눌러 주세요.\n"
                "(클릭=1개, Ctrl·Shift=여러 개)",
            )
            return
        ids = self._selected_published_ids()
        if not ids:
            messagebox.showwarning(
                "선택",
                "AI 코디에 넣을 등록 상품을 선택하세요.\n\n"
                "· 클릭 → 한 개만 선택\n"
                "· Ctrl + 클릭 → 여러 개 추가\n"
                "· Shift + 클릭 → 범위 선택",
            )
            return
        by_id = {p.id: p for p in self.published_items}
        if not by_id:
            by_id = {p.id: p for p in self.store.list_published()}
        picked: list[dict[str, str]] = []
        for pub_id in ids:
            item = by_id.get(pub_id)
            if item is None:
                continue
            row = self._ai_item_from_published(item)
            if row:
                picked.append(row)
        if not picked:
            messagebox.showwarning("AI 상품선택", "인식할 상품 번호(搜索码)가 없습니다.")
            return
        merged = self._merge_ai_items(picked, replace=False)
        codes = ", ".join(r["code"] for r in merged)
        self._append(f"AI 상품선택: {len(picked)}개 추가 → 총 {len(merged)}개 ({codes})")
        self.status.set(f"AI 코디 상품 {len(merged)}개 선택됨")
        messagebox.showinfo(
            "AI 상품선택",
            f"상품 번호를 인식했습니다.\n\n"
            f"추가: {len(picked)}개\n"
            f"누적: {len(merged)}개\n\n"
            f"NO: {codes}\n\n"
            "이어서 [AI 코디 만들기]에서 모델 이미지를 올리고\n"
            "[홈페이지에 적용]을 눌러 주세요.\n"
            "(제목·설명은 자동 생성됩니다)",
        )

    def _on_ai_style_dialog(self) -> None:
        """선택 상품 확인 + 모델 이미지 업로드 + 홈페이지 적용 (이전 버전 흐름)."""
        if not self.ai_style_items:
            if self.list_mode.get() == "published" and self._selected_published_ids():
                # 선택만 되어 있으면 바로 담기
                ids = self._selected_published_ids()
                by_id = {p.id: p for p in self.published_items} or {
                    p.id: p for p in self.store.list_published()
                }
                pre = []
                for pub_id in ids:
                    item = by_id.get(pub_id)
                    if item:
                        row = self._ai_item_from_published(item)
                        if row:
                            pre.append(row)
                if pre:
                    self._merge_ai_items(pre, replace=False)
            if not self.ai_style_items:
                messagebox.showwarning(
                    "AI 코디",
                    "먼저 [등록] 목록에서 상품을 여러 개 고르고\n[AI 상품선택]을 눌러 주세요.",
                )
                return

        win = tk.Toplevel(self)
        win.title("AI 모델 코디 → 홈페이지 적용")
        win.geometry("560x720")
        win.minsize(520, 480)
        win.configure(bg="#f3efe8")
        win.transient(self)
        win.grab_set()

        # Footer first so buttons stay visible when preview grows
        footer = tk.Frame(win, bg="#f3efe8")
        footer.pack(side="bottom", fill="x", padx=16, pady=(4, 12))

        body = tk.Frame(win, bg="#f3efe8")
        body.pack(fill="both", expand=True, padx=16, pady=(16, 0))

        tk.Label(
            body,
            text="AI 모델 추천 코디",
            bg="#f3efe8",
            font=("Malgun Gothic", 14, "bold"),
            anchor="w",
        ).pack(fill="x", pady=(0, 4))
        tk.Label(
            body,
            text="선택 상품을 확인한 뒤 모델 이미지를 올리고 적용하세요.\n제목·설명은 상품 정보로 자동 생성됩니다.",
            bg="#f3efe8",
            fg="#555",
            font=("Malgun Gothic", 9),
            anchor="w",
            wraplength=520,
            justify="left",
        ).pack(fill="x")

        codes_box = scrolledtext.ScrolledText(
            body, height=6, font=("Malgun Gothic", 10), bg="#fffdf9", relief="solid", borderwidth=1
        )
        codes_box.pack(fill="x", pady=8)
        lines = [
            f"{i}. NO {row['code']}  ·  {row.get('category','')}  ·  {row.get('name','')}"
            for i, row in enumerate(self.ai_style_items, 1)
        ]
        codes_box.insert("1.0", "\n".join(lines))
        codes_box.configure(state="disabled")

        img_var = tk.StringVar(value=str(self.ai_model_image or "(아직 없음)"))
        preview_label = tk.Label(body, bg="#ddd6ce")
        preview_photo: list[tk.PhotoImage | None] = [None]

        def fit_window() -> None:
            """Grow/shrink window so preview + footer buttons all fit on screen."""
            win.update_idletasks()
            req_w = max(win.winfo_reqwidth(), 560)
            req_h = win.winfo_reqheight() + 8
            sw = win.winfo_screenwidth()
            sh = win.winfo_screenheight()
            # Leave margin for taskbar / window chrome
            max_w = max(480, sw - 40)
            max_h = max(420, sh - 80)
            w = min(req_w, max_w)
            h = min(req_h, max_h)
            x = max(0, (sw - w) // 2)
            y = max(0, (sh - h) // 2)
            win.geometry(f"{w}x{h}+{x}+{y}")

        def show_preview(path: pathlib.Path) -> None:
            img_var.set(str(path))
            if Image is None:
                preview_label.configure(text=path.name, image="")
                fit_window()
                return
            try:
                im = Image.open(path)
                # Cap preview so dialog stays usable; window still auto-fits
                im.thumbnail((360, 420))
                photo = ImageTk.PhotoImage(im)
                preview_photo[0] = photo
                preview_label.configure(image=photo, text="")
            except Exception as e:
                preview_label.configure(text=f"미리보기 실패: {e}", image="")
            fit_window()

        def pick_image() -> None:
            path = filedialog.askopenfilename(
                parent=win,
                title="AI 모델 이미지 선택",
                filetypes=[
                    ("이미지", "*.png;*.jpg;*.jpeg;*.webp;*.bmp"),
                    ("모든 파일", "*.*"),
                ],
            )
            if not path:
                return
            p = pathlib.Path(path)
            self.ai_model_image = p
            show_preview(p)

        img_row = tk.Frame(body, bg="#f3efe8")
        img_row.pack(fill="x", pady=6)
        tk.Button(
            img_row,
            text="모델 이미지 업로드",
            command=pick_image,
            font=("Malgun Gothic", 10, "bold"),
            bg="#121212",
            fg="white",
            relief="flat",
            padx=12,
            pady=4,
        ).pack(side="left")
        tk.Label(
            img_row, textvariable=img_var, bg="#f3efe8", fg="#444", font=("Malgun Gothic", 8), anchor="w"
        ).pack(side="left", padx=8, fill="x", expand=True)

        preview_label.pack(pady=6)

        replace_var = tk.BooleanVar(value=False)
        tk.Checkbutton(
            footer,
            text="기존 AI 코디를 지우고 이 룩만 남기기",
            variable=replace_var,
            bg="#f3efe8",
            font=("Malgun Gothic", 9),
            activebackground="#f3efe8",
        ).pack(anchor="w", pady=(0, 6))

        if self.ai_model_image and self.ai_model_image.exists():
            show_preview(self.ai_model_image)
        else:
            fit_window()

        def apply_to_home() -> None:
            if not self.ai_style_items:
                messagebox.showwarning("상품", "선택된 상품이 없습니다.", parent=win)
                return
            if not self.ai_model_image or not self.ai_model_image.exists():
                messagebox.showwarning("이미지", "모델 이미지를 먼저 업로드해 주세요.", parent=win)
                return
            try:
                result = publish_style_look(
                    model_image=self.ai_model_image,
                    items=self.ai_style_items,
                    title="",
                    subtitle="",
                    replace_all=bool(replace_var.get()),
                )
            except Exception as e:
                messagebox.showerror("적용 실패", str(e), parent=win)
                return
            look = result["look"]
            self._append(
                f"AI 코디 적용: {look.get('id')} / 상품 {len(look.get('items') or [])}개 → {result.get('api')}"
            )
            self.status.set(f"AI 코디 적용 완료 · 상품 {len(self.ai_style_items)}개")
            messagebox.showinfo(
                "홈페이지에 적용",
                f"AI 모델 코디를 등록했습니다.\n\n"
                f"제목: {look.get('title')}\n"
                f"설명: {look.get('subtitle')}\n"
                f"상품: {len(look.get('items') or [])}개\n\n"
                f"{result.get('api')}\n\n"
                "쇼핑몰을 새로고침하면 반영됩니다.",
                parent=win,
            )
            win.destroy()

        zip_row = tk.Frame(footer, bg="#f3efe8")
        zip_row.pack(fill="x", pady=(0, 8))
        tk.Button(
            zip_row,
            text="상품 각각 zip 파일로 만들기",
            command=lambda: self._zip_ai_style_products(win),
            font=("Malgun Gothic", 10, "bold"),
            bg="#1f4e79",
            fg="white",
            activebackground="#163a5c",
            activeforeground="white",
            relief="flat",
            padx=12,
            pady=5,
        ).pack(side="left")
        tk.Label(
            zip_row,
            text="각 상품 폴더에 이미지+txt → zip 생성 후 폴더 열기",
            bg="#f3efe8",
            fg="#666",
            font=("Malgun Gothic", 8),
            anchor="w",
        ).pack(side="left", padx=8)

        btn_row = tk.Frame(footer, bg="#f3efe8")
        btn_row.pack(fill="x")
        tk.Button(
            btn_row,
            text="홈페이지에 적용",
            command=apply_to_home,
            font=("Malgun Gothic", 11, "bold"),
            bg="#c026d3",
            fg="white",
            activebackground="#a21caf",
            activeforeground="white",
            relief="flat",
            padx=16,
            pady=6,
        ).pack(side="left")
        tk.Button(
            btn_row,
            text="선택 비우기",
            command=lambda: (
                self.ai_style_items.clear(),
                self.status.set("AI 코디 선택 비움"),
                win.destroy(),
            ),
            font=("Malgun Gothic", 10),
            bg="#ebe4da",
            relief="flat",
            padx=10,
        ).pack(side="left", padx=8)
        tk.Button(
            btn_row,
            text="닫기",
            command=win.destroy,
            font=("Malgun Gothic", 10),
            bg="#ebe4da",
            relief="flat",
            padx=10,
        ).pack(side="right")

    def _on_save(self) -> None:
        if self.current_id is None:
            return
        self.store.update_description(
            self.current_id,
            title=self.title_var.get().strip(),
            search_code=self.code_var.get().strip(),
            sku_no=self.sku_var.get().strip(),
            tags=self.tags_var.get().strip(),
            description=self.desc.get("1.0", "end").strip(),
            category=self.category_var.get().strip(),
            google_name=self.google_name_var.get().strip(),
            name_en=self.name_en_var.get().strip(),
            colors=self.color_var.get().strip(),
            sizes=self.size_var.get().strip(),
        )
        self.refresh_list()
        messagebox.showinfo("저장", "상품 설명을 저장했습니다.")

    def _gallery_image_paths(self, product: Product) -> list[str]:
        """화면 썸네일과 같은 순서의 로컬 이미지 경로."""
        return self.store.resolve_local_images(
            product.id,
            paths=list(product.image_paths or []),
            cover_path=product.cover_path or "",
        )

    def _nth_image_path(self, product: Product, index: int) -> str:
        gallery = self._gallery_image_paths(product)
        if 0 <= index < len(gallery):
            return gallery[index]
        return ""

    def _product_is_clothing(self, product: Product) -> bool:
        """True when this item should use 옷종류 검색 (not full product name)."""
        cat = ""
        if self.current_id == product.id:
            cat = self.category_var.get().strip()
        if not cat:
            cat = (product.category or "").strip()
        if is_clothing_category(cat):
            return True
        try:
            resolved = resolve_product_category(
                tags=product.tags or "",
                title=product.title or "",
                description=product.description or "",
                google_name=product.google_name or "",
                name_en=product.name_en or "",
                existing=cat,
            )
            return is_clothing_category(resolved)
        except Exception:
            return False

    def _product_brand_for_search(self, product: Product) -> str:
        """Brand label for Google AI prompt (e.g. 샤넬)."""
        try:
            attrs = extract_attrs(
                product.title or "",
                product.tags or "",
                product.description or "",
                google_name=product.google_name or "",
                name_en=product.name_en or "",
            )
            name = (attrs.brand_name or "").strip()
            if name and name not in ("미확인", "unknown"):
                return name
        except Exception:
            pass
        # Fallback: first tag token often holds the brand from Weigou
        tags = (product.tags or "").strip()
        if tags:
            first = re.split(r"[,|/·\s]+", tags)[0].strip()
            if first and len(first) <= 20:
                return first
        return ""

    def _run_google_for(
        self,
        product: Product,
        *,
        headless: bool = False,
        image_index: int = 0,
    ) -> tuple[bool, str]:
        # Use UI field values when searching the selected product
        size = self.size_var.get().strip() if self.current_id == product.id else product.sizes
        color = self.color_var.get().strip() if self.current_id == product.id else product.colors
        if not size:
            size = product.sizes.strip()
        if not color:
            color = product.colors.strip()
        hint = " ".join(x for x in (product.title, product.tags, product.description) if x)
        brand = self._product_brand_for_search(product)
        clothing = self._product_is_clothing(product)
        gallery = self._gallery_image_paths(product)
        if image_index < 0:
            image_index = 0
        if gallery and image_index >= len(gallery):
            return (
                False,
                f"{image_index + 1}번째 이미지가 없습니다. (현재 {len(gallery)}장)",
            )
        pick_img = gallery[image_index] if gallery else ""
        paths = [pick_img] if pick_img else []
        nth_label = f"{image_index + 1}번째"
        prompt_preview = (
            f"{brand} 제품인데 사이즈 {size}"
            if brand and size
            else (f"{brand} 제품인데" if brand else (f"사이즈 {size}" if size else ""))
        )
        ask_bit = (
            "옷종류와 컬러를 알려줘 (예: 나시,니트,반팔티,긴팔티,가디건)"
            if clothing
            else "제품명과 컬러를 알려줘"
        )
        self._put_log(
            f"구글 검색({nth_label}): 이미지 복사붙여넣기 → "
            + (f"「{prompt_preview} / {ask_bit}」" if prompt_preview else f"「{ask_bit}」")
            + (f" · {pathlib.Path(pick_img).name}" if pick_img else " · 이미지 없음!")
            + (f" · {size}" if size and not prompt_preview else ""),
            channel=LOG_SEARCH,
        )

        if not pick_img and not product.image_urls:
            return False, f"첨부할 {nth_label} 이미지가 없습니다. 상품 폴더 이미지를 확인하세요."
        # 로컬 n번째 이미지만 사용 (URL 폴백은 1번째 검색에서만)
        use_urls = product.image_urls if (not paths and image_index == 0) else None
        result = search_product_images(
            paths,
            image_urls=use_urls,
            hint=hint,
            size=size,
            color="",  # 컬러는 질문에 넣지 않음 — 검색 결과로 받음
            brand=brand,
            headless=headless,
            clothing=clothing,
        )
        if result.error and not result.product_name:
            return False, result.error
        if not result.product_name:
            return False, "제품명을 찾지 못했습니다."
        category = resolve_product_category(
            tags=product.tags,
            title=product.title,
            description=hint,
            google_name=result.product_name,
            name_en=result.name_en or "",
            existing=product.category,
            ai_category=result.category or "",
        )
        raw_blob = "\n".join(result.raw_texts[:50]) if result.raw_texts else ""
        # 검색(AI) 컬러 우선 — 이미지 픽셀 추정(화이트,골드…)으로 덮지 않음
        from product_name import extract_ai_labeled_fields, normalize_ai_color

        colors = normalize_ai_color((result.color or "").strip())
        if not colors and result.raw_texts:
            _n, ai_c = extract_ai_labeled_fields(list(result.raw_texts))
            colors = normalize_ai_color(ai_c)
        if colors:
            attrs = extract_attrs(
                result.product_name,
                product.tags,
                "\n".join(x for x in (hint, raw_blob) if x),
            )
        else:
            cover = product.cover_path or (product.image_paths[0] if product.image_paths else "")
            attrs = extract_attrs(
                result.product_name,
                product.tags,
                "\n".join(x for x in (hint, raw_blob) if x),
                image_path=cover or None,
                image_paths=product.image_paths[:6],
            )
            if attrs.colors:
                colors = "/".join(attrs.colors)
        sizes = product.sizes.strip() or size or ", ".join(attrs.sizes)
        self.store.apply_google_result(
            product.id,
            google_name=result.product_name,
            name_en=result.name_en,
            category=category,
            update_title=True,
        )
        self.store.update_description(
            product.id,
            colors=colors or None,
            sizes=sizes or None,
            category=category,
        )
        self._mark_catalog_dirty(push_now=True)
        # 현재 선택 상품이면 입력란에도 바로 반영
        if self.current_id == product.id:
            self.after(0, lambda c=colors, s=sizes, cat=category, r=result: self._apply_search_fields(c, s, cat, r))
        extra = ""
        if result.candidates:
            extra = "\n후보: " + " | ".join(result.candidates[:4])
        bilingual = result.product_name
        if result.name_en:
            bilingual = f"{result.product_name}\n{result.name_en}"
        return True, f"{bilingual} / {category} / {colors or '-'} / {sizes or '-'}{extra}"

    def _apply_search_fields(self, colors: str, sizes: str, category: str, result) -> None:
        self._form_loading = True
        try:
            if result.product_name:
                self.google_name_var.set(result.product_name)
            if result.name_en:
                self.name_en_var.set(result.name_en)
            # 검색 컬러는 무조건 반영 (빈 문자열이면 유지)
            if colors:
                self.color_var.set(colors)
            elif (result.color or "").strip():
                self.color_var.set(result.color.strip())
            if sizes:
                self.size_var.set(sizes)
            if category:
                self.category_var.set(category)
        finally:
            self._form_loading = False

    def _first_image_path(self, product: Product) -> str:
        return self._nth_image_path(product, 0)

    def _apply_google_result_to_product(
        self, product: Product, result, *, size_fallback: str = ""
    ) -> tuple[bool, str]:
        if result.error and not result.product_name:
            return False, result.error
        if not result.product_name:
            return False, "제품명을 찾지 못했습니다."
        hint = " ".join(x for x in (product.title, product.tags, product.description) if x)
        category = resolve_product_category(
            tags=product.tags,
            title=product.title,
            description=hint,
            google_name=result.product_name,
            name_en=result.name_en or "",
            existing=product.category,
            ai_category=result.category or "",
        )
        from product_name import extract_ai_labeled_fields, normalize_ai_color

        colors = normalize_ai_color((result.color or "").strip())
        if not colors and result.raw_texts:
            _n, ai_c = extract_ai_labeled_fields(list(result.raw_texts))
            colors = normalize_ai_color(ai_c)
        if not colors:
            # raw_texts 한 줄에서라도 컬러 후보 추출
            for ln in result.raw_texts or []:
                if "컬러" in ln or "색상" in ln or "번째 이미지" in ln or "이미지" in ln:
                    from multi_ai_parse import _color_from_chunk

                    colors = normalize_ai_color(_color_from_chunk([ln], ln) or "")
                    if colors:
                        break
        if not colors:
            attrs = extract_attrs(result.product_name, product.tags, hint)
            if attrs.colors:
                colors = "/".join(attrs.colors)
        sizes = product.sizes.strip() or size_fallback
        # 제품명 + 컬러를 한 번에 저장 (컬러 누락 방지)
        self.store.apply_google_result(
            product.id,
            google_name=result.product_name,
            name_en=result.name_en,
            category=category,
            update_title=True,
        )
        self.store.update_description(
            product.id,
            colors=colors if colors else None,
            sizes=sizes or None,
            category=category,
        )
        # 검색 컬러가 있으면 기존 값을 덮어씀
        if colors:
            self.store.update_description(product.id, colors=colors)
        self._mark_catalog_dirty(push_now=True)
        if self.current_id == product.id:
            self.after(
                0,
                lambda c=colors, s=sizes, cat=category, r=result: self._apply_search_fields(
                    c, s, cat, r
                ),
            )
        bilingual = result.product_name
        if result.name_en:
            bilingual = f"{result.product_name}\n{result.name_en}"
        return True, f"{bilingual} / {category} / {colors or '-'} / {sizes or '-'}"

    def _run_google_multi(
        self, products: list[Product], *, image_index: int = 0
    ) -> tuple[int, int, str]:
        """Paste selected product images once (nth gallery image each), apply AI answers."""
        jobs: list[dict] = []
        ready: list[Product] = []
        skipped: list[str] = []
        nth = max(0, int(image_index)) + 1
        for p in products:
            img = self._nth_image_path(p, image_index)
            if not img:
                gallery_n = len(self._gallery_image_paths(p))
                skipped.append(f"#{p.id} ({gallery_n}장뿐 · {nth}번째 없음)")
                continue
            size = self.size_var.get().strip() if self.current_id == p.id else p.sizes
            if not size:
                size = p.sizes.strip()
            hint = " ".join(x for x in (p.title, p.tags, p.description) if x)
            brand = self._product_brand_for_search(p)
            clothing = self._product_is_clothing(p)
            jobs.append(
                {
                    "path": img,
                    "size": size,
                    "hint": hint,
                    "brand": brand,
                    "clothing": clothing,
                }
            )
            ready.append(p)
        if not jobs:
            detail = "\n".join(skipped[:8])
            return (
                0,
                len(products),
                f"선택한 상품에 {nth}번째 이미지가 없습니다."
                + (f"\n{detail}" if detail else ""),
            )

        brands = [str(j.get("brand") or "").strip() for j in jobs if j.get("brand")]
        brand_bit = ""
        if brands:
            uniq = list(dict.fromkeys(brands))
            brand_bit = uniq[0] if len(uniq) == 1 else ", ".join(uniq[:4])
        clothing_all = all(bool(j.get("clothing")) for j in jobs)
        ask_bit = (
            "각 제품의 옷종류와 컬러를 알려줘 (예: 나시,니트,반팔티…)"
            if clothing_all
            else "각 제품의 제품명과 컬러를 알려줘"
        )
        self._put_log(
            f"구글 다중 검색({nth}번째 이미지): {len(jobs)}장 붙여넣기 → "
            + (f"「{brand_bit} 제품인데 / {ask_bit}」" if brand_bit else f"「{ask_bit}」"),
            channel=LOG_SEARCH,
        )
        if skipped:
            self._put_log(
                f"  건너뜀 {len(skipped)}건: " + ", ".join(skipped[:6]),
                channel=LOG_SEARCH,
            )

        results = search_products_multi(jobs, headless=False)
        ok_n = 0
        fail_n = 0
        lines: list[str] = []
        for p, r, job in zip(ready, results, jobs):
            size = job.get("size") or ""
            ok, msg = self._apply_google_result_to_product(p, r, size_fallback=size)
            if ok:
                ok_n += 1
                lines.append(f"#{p.id} OK — {msg}")
                self._put_log(f"  #{p.id} → {msg}", channel=LOG_SEARCH)
            else:
                fail_n += 1
                lines.append(f"#{p.id} 실패 — {msg}")
                self._put_log(f"  #{p.id} 실패: {msg}", channel=LOG_SEARCH)
        # products without the requested image
        fail_n += len(skipped)
        for s in skipped:
            lines.append(f"{s} — 건너뜀")
        summary = "\n".join(lines[:16])
        if len(lines) > 16:
            summary += f"\n… 외 {len(lines) - 16}건"
        return ok_n, fail_n, summary

    def _on_google_one(self) -> None:
        self._ensure_images_for_action()
        self._start_google_search(image_index=0)

    def _on_google_second(self) -> None:
        """선택한 상품을 하나씩, 각 상품의 2번째 이미지로 검색 (선택된 이미지 검색과 동일 방식)."""
        self._ensure_images_for_action()
        self._on_google_selected(image_index=1)

    def _search_enqueue_jobs(
        self,
        jobs: list[tuple[Product, int]],
    ) -> int:
        """Add image-search jobs to the queue. Skips duplicates already queued/active."""
        added = 0
        with self._search_lock:
            for job in jobs:
                pid = int(job[0].id)
                if pid == self._search_active_id or pid in self._search_queued_ids:
                    continue
                self._search_queued_ids.add(pid)
                self._search_q.put(job)
                self._search_total += 1
                added += 1
            total = self._search_total
            prog = self._search_prog
        if added and prog is not None:
            try:
                prog["set_total"](total)
            except Exception:
                pass
            self.after(
                0,
                lambda t=total, a=added: self.status.set(
                    f"검색 대기열 +{a} · 총 {t}개"
                ),
            )
        if added:
            self.after(0, self._refresh_list_busy)
        return added

    def _google_search_submit(
        self,
        ids: list[int],
        *,
        image_index: int = 0,
        confirm_batch: bool = True,
    ) -> None:
        """Enqueue image-search jobs (supports mid-run additions like homepage publish)."""
        if self.current_id is not None and self.current_id in ids:
            self._soft_save_current(force=True)

        jobs: list[tuple[Product, int]] = []
        for pid in ids:
            product = self.store.get(pid)
            if product:
                jobs.append((product, int(image_index)))
        if not jobs:
            messagebox.showwarning("없음", "검색할 상품이 없습니다.")
            return

        nth = max(0, int(image_index)) + 1
        img_label = "첫 이미지" if image_index <= 0 else f"{nth}번째 이미지"
        title = (
            "선택된 이미지 검색"
            if image_index <= 0
            else f"선택된 {nth}번째 이미지 검색"
        )
        running = self._job_running("search")
        if running:
            if not messagebox.askyesno(
                "검색 추가",
                f"이미지 검색이 진행 중입니다.\n"
                f"선택한 {len(jobs)}개를 이어서 검색할까요?",
            ):
                return
        elif confirm_batch and len(jobs) > 1 and not messagebox.askyesno(
            title,
            f"선택한 {len(jobs)}개 상품을 하나씩 {img_label}로 검색할까요?\n"
            "시간이 걸릴 수 있습니다.\n"
            "진행 중에도 다른 상품을 추가 검색할 수 있습니다.\n"
            "(수집·등록과 동시에 진행할 수 있습니다)",
        ):
            return

        if running:
            added = self._search_enqueue_jobs(jobs)
            if added <= 0:
                messagebox.showinfo(
                    "검색 추가",
                    "선택한 상품은 이미 검색 대기 중이거나 처리 중입니다.",
                )
            else:
                self._append(
                    f"검색 대기열에 {added}개 추가 (총 {self._search_total}개)",
                    channel=LOG_SEARCH,
                )
                messagebox.showinfo(
                    "검색 추가",
                    f"{added}개를 검색 대기열에 넣었습니다.\n"
                    f"현재 총 {self._search_total}개 진행·대기 중.",
                )
            return

        if not self._job_start("search"):
            # 거의 동시에 시작된 경우 → 대기열에 추가
            added = self._search_enqueue_jobs(jobs)
            if added:
                self._append(
                    f"검색 대기열에 {added}개 추가 (총 {self._search_total}개)",
                    channel=LOG_SEARCH,
                )
            return

        with self._search_lock:
            self._search_total = 0
            self._search_done = 0
            self._search_ok = 0
            self._search_fail = 0
            self._search_lines = []
            self._search_queued_ids.clear()
            self._search_active_id = None
            while True:
                try:
                    self._search_q.get_nowait()
                except queue.Empty:
                    break

        added = self._search_enqueue_jobs(jobs)
        if added <= 0:
            self._job_end("search")
            messagebox.showwarning("없음", "검색할 상품이 없습니다.")
            return

        total = self._search_total
        prog = self._open_search_progress(total)
        self._search_prog = prog
        self._append(
            f"----- {title} {total}개 (실시간 · 추가검색 가능 · {img_label}) -----",
            channel=LOG_SEARCH,
        )
        self.status.set(f"이미지 검색 중 0/{total}")
        threading.Thread(target=self._search_worker_loop, daemon=True).start()

    def _search_worker_loop(self) -> None:
        """Drain image-search queue until empty (supports mid-run enqueue)."""
        prog = self._search_prog
        idle_rounds = 0
        try:
            while True:
                try:
                    job = self._search_q.get(timeout=0.35)
                    idle_rounds = 0
                except queue.Empty:
                    idle_rounds += 1
                    if idle_rounds >= 2:
                        with self._search_lock:
                            if self._search_q.empty() and self._search_active_id is None:
                                break
                    continue

                product, image_index = job
                pid = int(product.id)
                image_index = int(image_index)
                nth = max(0, image_index) + 1
                img_label = "첫 이미지" if image_index <= 0 else f"{nth}번째 이미지"
                try:
                    self._ensure_images_for_action(product_id=pid)
                    fresh = self.store.get(pid)
                    if fresh:
                        product = fresh
                except Exception:
                    pass
                with self._search_lock:
                    self._search_active_id = pid
                    self._search_queued_ids.discard(pid)
                    done_before = self._search_done
                    total = self._search_total
                    ok_n = self._search_ok
                    fail_n = self._search_fail
                self.after(0, self._refresh_list_busy)

                name_hint = (
                    (product.google_name or product.title or f"#{product.id}").strip()
                )[:36]
                i = done_before + 1
                if prog:
                    prog["progress"](
                        done_before,
                        f"[{i}/{total}] {img_label} 검색 중… #{product.id} {name_hint}",
                        ok_n,
                        fail_n,
                    )
                    prog["line"](
                        f"→ [{i}/{total}] #{product.id} {img_label} 검색 중… "
                        "(결과 나올 때까지 대기, 1분 초과 시 새로고침/재시작)"
                    )
                self.after(
                    0,
                    lambda i=i, t=total: self.status.set(f"이미지 검색 중 {i}/{t}"),
                )

                try:
                    ok, msg = self._run_google_for(
                        product, headless=False, image_index=image_index
                    )
                    if not ok:
                        self._put_log(
                            f"  → 창 종료 후 잠시 대기, 같은 이미지 재시도: {msg}",
                            channel=LOG_SEARCH,
                        )
                        try:
                            close_ai_browsers()
                        except Exception:
                            pass
                        time.sleep(4.0)
                        ok, msg = self._run_google_for(
                            product, headless=False, image_index=image_index
                        )
                    if ok:
                        with self._search_lock:
                            self._search_ok += 1
                            self._search_lines.append(f"#{product.id} OK — {msg}")
                            ok_n = self._search_ok
                            fail_n = self._search_fail
                        if prog:
                            prog["line"](f"  ✓ {msg}")
                        self._put_log(f"  → {msg}", channel=LOG_SEARCH)
                    else:
                        with self._search_lock:
                            self._search_fail += 1
                            self._search_lines.append(f"#{product.id} 실패 — {msg}")
                            ok_n = self._search_ok
                            fail_n = self._search_fail
                        if prog:
                            prog["line"](f"  ✗ 실패: {msg}")
                        self._put_log(
                            f"  → 실패(다음 상품으로): {msg}", channel=LOG_SEARCH
                        )
                    self.after(
                        0,
                        lambda: self.refresh_list(reload_detail=False, quiet=True),
                    )
                    if self.current_id == product.id:
                        refreshed = self.store.get(product.id)
                        if refreshed:
                            self.after(0, lambda r=refreshed: self._show_product(r))
                except Exception as e:
                    err = str(e)
                    with self._search_lock:
                        self._search_fail += 1
                        self._search_lines.append(f"#{product.id} 실패: {err}")
                        ok_n = self._search_ok
                        fail_n = self._search_fail
                    if prog:
                        prog["line"](f"  ✗ 실패: {err}")
                    self._put_log(f"검색 실패 #{product.id}: {err}", channel=LOG_SEARCH)

                with self._search_lock:
                    self._search_done += 1
                    self._search_active_id = None
                    done = self._search_done
                    total = self._search_total
                    ok_n = self._search_ok
                    fail_n = self._search_fail
                self.after(0, self._refresh_list_busy)

                if prog:
                    prog["progress"](
                        done,
                        f"[{done}/{total}] 완료 — 성공 {ok_n} · 실패 {fail_n}",
                        ok_n,
                        fail_n,
                    )

            with self._search_lock:
                ok_final = self._search_ok
                fail_final = self._search_fail
                lines = list(self._search_lines)
                if not self._search_q.empty():
                    threading.Thread(
                        target=self._search_worker_loop, daemon=True
                    ).start()
                    return

            summary = "\n".join(lines[:14])
            if len(lines) > 14:
                summary += f"\n… 외 {len(lines) - 14}건"
            if prog:
                prog["finish"](
                    f"성공 {ok_final} / 실패 {fail_final}", ok_final, fail_final
                )

            def finish() -> None:
                with self._search_lock:
                    if not self._search_q.empty():
                        threading.Thread(
                            target=self._search_worker_loop, daemon=True
                        ).start()
                        return
                    self._search_prog = None
                    self._search_active_id = None
                    self._search_queued_ids.clear()

                self.refresh_list(reload_detail=False, quiet=True)
                self._mark_catalog_dirty(push_now=True)
                self.status.set(
                    f"이미지 검색 완료 — 성공 {ok_final} / 실패 {fail_final}"
                )
                self._job_end("search")
                if fail_final and ok_final == 0:
                    messagebox.showerror("이미지 검색 실패", summary or "검색 실패")
                else:
                    messagebox.showinfo(
                        "이미지 검색",
                        f"완료: 성공 {ok_final} / 실패 {fail_final}\n\n{summary}",
                    )

            self.after(0, finish)
        except Exception as e:
            self._put_log(f"검색 오류: {e}", channel=LOG_SEARCH)
            if prog:
                prog["line"](f"오류: {e}")
                prog["finish"]("오류로 중단", self._search_ok, self._search_fail)
            self.after(0, lambda: messagebox.showerror("오류", str(e)))
            with self._search_lock:
                self._search_prog = None
                self._search_active_id = None
                self._search_queued_ids.clear()
            self.after(0, lambda: self._job_end("search"))

    def _start_google_search(self, *, image_index: int = 0) -> None:
        if self.list_mode.get() != "products":
            messagebox.showwarning("선택", "상품 목록에서 선택하세요.")
            return
        ids = self._selected_product_ids()
        if not ids and self.current_id is not None:
            ids = [self.current_id]
        if not ids:
            messagebox.showwarning("선택", "상품을 먼저 선택하세요.")
            return
        # 여러 개면 일괄 확인 후 대기열 등록
        self._google_search_submit(
            ids,
            image_index=image_index,
            confirm_batch=len(ids) > 1,
        )

    def _on_google_selected(self, *, image_index: int = 0) -> None:
        """목록에서 선택한 상품을 이미지 검색 대기열에 넣어 제품명을 갱신."""
        if self.list_mode.get() != "products":
            messagebox.showwarning("선택", "상품 목록에서 선택하세요.")
            return
        ids = self._selected_product_ids()
        if not ids:
            messagebox.showwarning("선택", "검색할 상품을 목록에서 선택하세요.")
            return
        self._google_search_submit(
            ids,
            image_index=image_index,
            confirm_batch=True,
        )

    def _on_exclude(self) -> None:
        if self.list_mode.get() != "products":
            return
        ids = self._selected_product_ids()
        if not ids:
            return
        n = len(ids)
        if not messagebox.askyesno(
            "제외",
            f"선택한 {n}개 상품을 제외 목록으로 옮길까요?\n\n"
            "상품·이미지는 목록에서 사라지고,\n"
            "같은 상품은 이후 수집에서 자동으로 건너뜁니다.",
        ):
            return

        selected = set(ids)
        next_id: int | None = None
        last_i = max(
            (i for i, p in enumerate(self.products) if p.id in selected),
            default=-1,
        )
        for j in range(last_i + 1, len(self.products)):
            if self.products[j].id not in selected:
                next_id = self.products[j].id
                break
        if next_id is None:
            for j in range(last_i - 1, -1, -1):
                if self.products[j].id not in selected:
                    next_id = self.products[j].id
                    break

        for pid in ids:
            eid = self.store.exclude_product(pid)
            self._append(f"제외됨 #{pid} → 제외 목록 #{eid}")
        self._mark_catalog_dirty()
        self.current_excluded_id = None
        self.current_id = next_id
        yview = self.listbox.yview()
        # 포커스·active를 다음 상품에 두어 ↓키가 맨 위로 튀지 않게 함
        self.refresh_list(
            preserve_yview=(float(yview[0]), float(yview[1])),
            focus_list=True,
        )

    def _on_unexclude(self) -> None:
        if self.list_mode.get() != "excluded":
            return
        ids = self._selected_excluded_ids()
        if not ids:
            return
        if not messagebox.askyesno(
            "제외 해제",
            f"선택한 {len(ids)}개 제외를 해제할까요?\n이후 수집 때 다시 가져올 수 있습니다.",
        ):
            return
        for eid in ids:
            self.store.unexclude(eid)
            self._append(f"제외 해제 #{eid}")
        self._mark_catalog_dirty()
        self.current_excluded_id = None
        self.refresh_list()

    def _on_merge_images(self) -> None:
        """Merge galleries of 2+ selected products into one folder (keep current)."""
        if self.list_mode.get() != "products":
            return
        ids = self._selected_product_ids()
        if len(ids) < 2:
            messagebox.showwarning(
                "이미지 합치기",
                "같은 제품의 상품을 2개 이상 선택하세요.\n"
                "(Ctrl 또는 Shift 클릭으로 다중 선택)",
            )
            return

        keep_id = (
            self.current_id
            if self.current_id is not None and self.current_id in ids
            else ids[0]
        )
        donors = [i for i in ids if i != keep_id]
        keep = self.store.get(keep_id)
        if not keep:
            messagebox.showwarning("없음", "기준 상품을 찾을 수 없습니다.")
            return

        def _label(pid: int) -> str:
            p = self.store.get(pid)
            if not p:
                return f"#{pid}"
            name = (p.google_name or p.title or "").strip() or "(이름 없음)"
            if len(name) > 40:
                name = name[:39] + "…"
            return f"#{pid} {name}"

        donor_lines = "\n".join(f"  · {_label(d)}" for d in donors)
        if not messagebox.askyesno(
            "이미지 합치기",
            f"기준(상세에 보이는) 상품:\n  {_label(keep_id)}\n\n"
            f"합친 뒤 삭제할 상품:\n{donor_lines}\n\n"
            "이미지를 기준 폴더로 합치고,\n"
            "합친 상품은 목록에서 삭제합니다.\n"
            "(먼저 클릭한 상품이 기준입니다)",
        ):
            return

        # Persist form edits on keep before merge
        if self.current_id == keep_id:
            self._soft_save_current()

        # Release thumbnail widgets so Windows does not lock image files
        if self._thumb_after is not None:
            try:
                self.after_cancel(self._thumb_after)
            except Exception:
                pass
            self._thumb_after = None
        self._select_gen += 1
        self._clear_images()
        try:
            self.update_idletasks()
        except Exception:
            pass

        try:
            result = self.store.merge_product_images(keep_id, donors)
        except Exception as e:
            messagebox.showerror("이미지 합치기", f"합치기 실패:\n{e}")
            return

        n_img = int(result.get("image_count") or 0)
        deleted = result.get("deleted_ids") or []
        self._append(
            f"이미지 합치기 → #{keep_id} ({n_img}장) · 삭제 {', '.join('#'+str(x) for x in deleted)}"
        )
        self._mark_catalog_dirty(push_now=True)
        self.current_id = keep_id
        self._sticky_selected_ids = [keep_id]
        yview = self.listbox.yview()
        self.refresh_list(
            preserve_yview=(float(yview[0]), float(yview[1])),
            focus_list=True,
        )
        messagebox.showinfo(
            "이미지 합치기",
            f"완료 — #{keep_id} 폴더에 이미지 {n_img}장.\n"
            "홈페이지 등록하면 합친 이미지가 모두 올라갑니다.",
        )

    def _on_delete(self) -> None:
        if self.list_mode.get() != "products":
            return
        if self.current_id is None:
            return
        if not messagebox.askyesno("삭제", "이 상품과 이미지를 삭제할까요?\n(제외 목록에는 넣지 않습니다)"):
            return
        pid = self.current_id
        self.store.delete(pid)
        self._mark_catalog_dirty()
        self.current_id = None
        self._clear_images()
        self.refresh_list()

    def _open_folder(self) -> None:
        import os

        self._ensure_images_for_action()
        path = self._resolve_image_folder()
        path.mkdir(parents=True, exist_ok=True)
        # product.txt 는 수집 시 만들어지지만, 경로 복구/재다운로드 후에는 빠질 수 있음
        try:
            mode = self.list_mode.get()
            if mode == "published" and self.current_published_id is not None:
                self._ensure_published_txt(path, self.current_published_id)
            elif mode == "products" and self.current_id is not None:
                self.store.write_product_txt(self.current_id, path)
        except Exception:
            pass
        os.startfile(str(path))  # type: ignore[attr-defined]

    def _resolve_image_folder(self) -> pathlib.Path:
        """Open the folder where product images were originally saved."""
        mode = self.list_mode.get()

        # Prefer actual saved image path (works after 등록 → published_covers/p#)
        cover = ""
        images: list[str] = []
        if mode == "published" and self.current_published_id is not None:
            for item in self.published_items:
                if item.id == self.current_published_id:
                    cover = item.cover_path or ""
                    images = list(item.image_paths or [])
                    break
            pack = self.store.published_img_root / f"p{self.current_published_id}"
            if pack.is_dir():
                return pack
        elif mode == "excluded" and self.current_excluded_id is not None:
            for item in self.excluded_items:
                if item.id == self.current_excluded_id:
                    cover = item.cover_path or ""
                    break
        elif self.current_id is not None:
            p = self.store.get(self.current_id)
            if p:
                cover = p.cover_path or ""
                images = list(p.image_paths or [])
            folder = self.store.img_root / str(self.current_id)
            if folder.is_dir():
                return folder

        for cand in ([cover] if cover else []) + images:
            try:
                fp = pathlib.Path(cand)
                if fp.is_file():
                    return fp.parent
                if fp.is_dir():
                    return fp
            except Exception:
                continue

        if self.current_id is not None:
            return self.store.img_root / str(self.current_id)
        return default_root() / "images"

    def _on_launch(self) -> None:
        if is_manager_role():
            messagebox.showinfo(
                "관리(B) 모드", "관리(B) PC에서는 디버그 실행을 사용하지 않습니다."
            )
            return
        if not self._job_start("launch"):
            self._warn_job_busy("launch")
            return
        if is_running():
            if not messagebox.askyesno(
                "재시작",
                "디버그 모드로 다시 실행하려면 微购相册을 종료합니다. 계속할까요?",
            ):
                self._job_end("launch")
                return

        def work() -> None:
            try:
                ok, msg = start_debug(DEFAULT_PORT)
                self._put_log(msg, channel=LOG_COLLECT)
                self.after(
                    0,
                    lambda: messagebox.showinfo("실행", msg)
                    if ok
                    else messagebox.showerror("실패", msg),
                )
            finally:
                self.after(0, lambda: self._job_end("launch"))

        threading.Thread(target=work, daemon=True).start()

    def _reset_collect_button(self) -> None:
        try:
            self.btn_auto_collect.configure(
                text="목록→상세 자동수집",
                bg="#1f4e79",
                fg="white",
                activebackground="#163a5c",
            )
        except Exception:
            pass
        self._collect_pause.clear()

    def _on_cancel_job(self) -> None:
        if self._job_running("collect") or self._job_running("import"):
            self._collect_pause.clear()  # unblock pause wait
            self._cancel_job.set()
            self._append("중지 요청… 수집/가져오기만 멈춥니다. (검색·등록은 계속됩니다)")
        elif self._jobs_status_text():
            self._append(
                f"중지 대상 없음 — 현재: {self._jobs_status_text()} "
                "(수집·가져오기만 중지 가능)"
            )
        else:
            self._append("진행 중인 작업이 없습니다.")

    def _parse_collect_limit(self) -> int:
        """UI 수집개수 → max_items (0 = 무제한)."""
        raw = (self.collect_limit_var.get() or "").strip()
        if raw in ("무제한", "0", "∞"):
            return 0
        m = re.search(r"(\d+)", raw)
        if not m:
            return 100
        n = int(m.group(1))
        return n if n > 0 else 0

    def _on_collect_limit_changed(self, _event=None) -> None:
        n = self._parse_collect_limit()
        try:
            self.store.set_setting("collect_limit", str(n))
        except Exception:
            pass

    def _on_auto_collect(self) -> None:
        if is_manager_role():
            messagebox.showinfo(
                "관리(B) 모드", "관리(B) PC에서는 자동수집을 사용하지 않습니다."
            )
            return
        # Running → toggle pause / resume
        if self._job_running("collect"):
            if self._collect_pause.is_set():
                self._collect_pause.clear()
                self.btn_auto_collect.configure(
                    text="일시정지",
                    bg="#b45309",
                    fg="white",
                    activebackground="#92400e",
                )
                self._append("수집 계속")
                self.status.set("자동수집 재개")
            else:
                self._collect_pause.set()
                self.btn_auto_collect.configure(
                    text="수집 계속",
                    bg="#15803d",
                    fg="white",
                    activebackground="#166534",
                )
                self._append("수집 일시정지")
                self.status.set("자동수집 일시정지")
            return

        if not self._job_start("collect"):
            self._warn_job_busy("collect")
            return
        if not is_cdp_up(DEFAULT_PORT) and not is_cdp_up():
            self._job_end("collect")
            messagebox.showwarning(
                "연결 없음",
                "디버그 포트에 연결되지 않았습니다.\n먼저 [디버그 실행] 후 친구 앨범 목록을 열어 주세요.",
            )
            return
        max_items = self._parse_collect_limit()
        try:
            self.store.set_setting("collect_limit", str(max_items))
        except Exception:
            pass
        limit_txt = "무제한(목록 끝까지)" if max_items <= 0 else f"신규 {max_items}건"
        if not messagebox.askyesno(
            "자동수집",
            "목록 맨 위부터 PageDown으로 수집합니다.\n"
            "아직 없는 상품만 상세로 들어가 저장합니다.\n\n"
            f"· 이번 수집 한도: {limit_txt}\n"
            "· 이미 수집 / 제외 / 등록 → 상품ID로 패스\n"
            "· 하단 로딩 스피너면 기다린 뒤 계속\n"
            "· 이미지 1~2장 상품 → 제외 목록으로 자동 저장\n"
            "· 같은 버튼 = 일시정지 / 수집 계속 · 완전 종료는 [중지]\n\n"
            "목록 화면을 연 상태로 두세요. 시작할까요?",
        ):
            self._job_end("collect")
            return
        self._cancel_job.clear()
        self._collect_pause.clear()
        self.btn_auto_collect.configure(
            text="일시정지",
            bg="#b45309",
            fg="white",
            activebackground="#92400e",
        )
        self._append(f"----- 목록→상세 자동수집 시작 ({limit_txt}) -----")
        self._append(
            "상품ID 스킵 · 로딩 대기/회복 · 이미지 1~2장→제외 · "
            "버튼=일시정지 · [중지]=종료"
        )
        collect_total = max_items if max_items > 0 else 0
        self._set_channel_progress(
            LOG_COLLECT,
            done=0,
            total=collect_total,
            action="수집중",
            detail=limit_txt,
            indeterminate=collect_total <= 0,
        )

        def work() -> None:
            ok_n = 0
            fail_n = 0
            auto_ex_n = 0

            def bump_collect(detail: str = "") -> None:
                done = ok_n + auto_ex_n + fail_n
                self._set_channel_progress(
                    LOG_COLLECT,
                    done=done,
                    total=collect_total,
                    action="수집중",
                    detail=detail or f"저장 {ok_n} · 제외 {auto_ex_n} · 실패 {fail_n}",
                    ok=ok_n,
                    fail=fail_n,
                    indeterminate=collect_total <= 0,
                )

            def save_one(p) -> None:
                nonlocal ok_n, fail_n, auto_ex_n
                try:
                    # drop_second 전 원본 장수 기준 (배송 안내 장 제거와 무관)
                    n_imgs = len(getattr(p, "image_urls", None) or [])
                    pid = self.store.import_parsed(p, on_progress=lambda m: self._put_log(m, channel=LOG_COLLECT))
                    if pid is not None and pid < 0:
                        return
                    if pid and n_imgs <= 2:
                        eid = self.store.exclude_product(
                            int(pid), note="자동제외: 이미지 1~2장"
                        )
                        auto_ex_n += 1
                        self._put_log(
                            f"이미지 {n_imgs}장 → 제외 목록 #{eid} "
                            f"({getattr(p, 'title', '')[:40] or getattr(p, 'goods_id', '')})", channel=LOG_COLLECT)

                    else:
                        ok_n += 1
                    bump_collect(
                        f"#{getattr(p, 'goods_id', '') or pid} "
                        f"저장 {ok_n} · 제외 {auto_ex_n} · 실패 {fail_n}"
                    )
                    self._mark_catalog_dirty()
                    self.after(0, lambda: self._schedule_list_refresh(reload_detail=False))
                except Exception as e:
                    fail_n += 1
                    bump_collect(str(e)[:80])
                    self._put_log(f"저장 실패: {e}", channel=LOG_COLLECT)
            def refresh_skips():
                return (
                    self.store.excluded_goods_ids(),
                    self.store.excluded_search_codes() | self.store.published_search_codes(),
                    self.store.collected_goods_ids() | self.store.published_goods_ids(),
                )

            def on_collect_progress(m: str) -> None:
                self._put_log(m, channel=LOG_COLLECT)
                # "신규 열기 (3/100)" 형태면 게이지 동기화
                m_open = re.search(r"신규 열기\s*\((\d+)\s*/\s*(\d+)", m or "")
                if m_open and collect_total > 0:
                    try:
                        cur = int(m_open.group(1))
                        tot = int(m_open.group(2))
                        self._set_channel_progress(
                            LOG_COLLECT,
                            done=max(0, cur - 1),
                            total=tot,
                            action="수집중",
                            detail=(m or "")[:120],
                            ok=ok_n,
                            fail=fail_n,
                        )
                        return
                    except Exception:
                        pass
                bump_collect((m or "")[:120])

            try:
                products, msg = walk_list_details(
                    on_progress=on_collect_progress,
                    on_product=save_one,
                    cancel=self._cancel_job,
                    pause=self._collect_pause,
                    excluded_goods_ids=self.store.excluded_goods_ids(),
                    excluded_search_codes=(
                        self.store.excluded_search_codes()
                        | self.store.published_search_codes()
                    ),
                    known_goods_ids=(
                        self.store.collected_goods_ids()
                        | self.store.published_goods_ids()
                    ),
                    max_items=max_items,
                    refresh_skips=refresh_skips,
                )
                self._put_log(msg, channel=LOG_COLLECT)
                done_n = ok_n + auto_ex_n + fail_n
                self._finish_channel_progress(
                    LOG_COLLECT,
                    summary=msg or f"저장 {ok_n} · 제외 {auto_ex_n} · 실패 {fail_n}",
                    done=done_n if collect_total <= 0 else min(done_n, collect_total) or collect_total,
                    total=collect_total if collect_total > 0 else max(done_n, 1),
                    ok=ok_n,
                    fail=fail_n,
                )
                if not products and ok_n == 0 and auto_ex_n == 0:
                    self.after(
                        0,
                        lambda: messagebox.showwarning(
                            "없음",
                            msg or "수집된 상품이 없습니다.\n목록 화면인지, 디버그 연결인지 확인해 주세요.",
                        ),
                    )
                    return
                self.after(0, lambda: self.refresh_list(reload_detail=False, quiet=True))
                self.after(
                    0,
                    lambda: messagebox.showinfo(
                        "완료",
                        f"자동수집 완료\n처리 {len(products)}건\n"
                        f"상품관리 저장 {ok_n} · 이미지부족 제외 {auto_ex_n} · 실패 {fail_n}",
                    ),
                )
            except Exception as e:
                self._put_log(f"오류: {e}", channel=LOG_COLLECT)
                self._finish_channel_progress(
                    LOG_COLLECT,
                    summary=str(e),
                    done=ok_n + auto_ex_n + fail_n,
                    total=collect_total if collect_total > 0 else max(ok_n + auto_ex_n + fail_n, 1),
                    ok=ok_n,
                    fail=fail_n,
                )
                self.after(0, lambda: messagebox.showerror("오류", str(e)))
            finally:
                self.after(0, self._reset_collect_button)
                self.after(0, lambda: self._job_end("collect"))

        threading.Thread(target=work, daemon=True).start()

    def _on_import(self) -> None:
        if is_manager_role():
            messagebox.showinfo(
                "관리(B) 모드", "관리(B) PC에서는 현재 화면 가져오기를 사용하지 않습니다."
            )
            return
        if not self._job_start("import"):
            self._warn_job_busy("import")
            return
        try:
            clip = self.clipboard_get()
        except tk.TclError:
            clip = ""
        self._cancel_job.clear()
        self._append("----- 가져오기 시작 -----")

        def work() -> None:
            try:
                html, text, source, msg = collect_page_best(clip)
                self._put_log(f"{msg} (source={source})", channel=LOG_COLLECT)
                products = parse_products(html, text)
                if not products:
                    self._put_log("상품을 찾지 못했습니다. 상세 화면이거나 목록을 스크롤한 뒤 다시 시도하세요.", channel=LOG_COLLECT)
                    self.after(0, lambda: messagebox.showwarning("없음", "상품을 찾지 못했습니다."))
                    return
                self._put_log(f"인식된 상품 {len(products)}개", channel=LOG_COLLECT)
                # 목록만 잡힌 경우(커버 위주) → 자동 상세 순회 제안은 로그로
                if len(products) > 1:
                    avg_imgs = sum(len(p.image_urls) for p in products) / max(1, len(products))
                    if avg_imgs <= 1.5:
                        self._put_log(
                            "목록 커버만 감지됨. 전체 갤러리는 [목록→상세 자동수집]을 사용하세요.", channel=LOG_COLLECT)

                total_imp = len(products)
                done_imp = {"n": 0}

                def on_imp(m: str) -> None:
                    self._put_log(m, channel=LOG_COLLECT)
                    done_imp["n"] = min(done_imp["n"] + 1, total_imp)
                    self._set_channel_progress(
                        LOG_COLLECT,
                        done=done_imp["n"],
                        total=total_imp,
                        action="가져오기중",
                        detail=(m or "")[:100],
                    )

                self._set_channel_progress(
                    LOG_COLLECT,
                    done=0,
                    total=total_imp,
                    action="가져오기중",
                    detail=f"{total_imp}개",
                )
                ok, fail = self.store.import_many(products, on_progress=on_imp)
                self._finish_channel_progress(
                    LOG_COLLECT,
                    summary=f"성공 {ok} · 실패 {fail}",
                    done=total_imp,
                    total=total_imp,
                    ok=ok,
                    fail=fail,
                )
                self._mark_catalog_dirty()
                self.after(0, lambda: self.refresh_list(reload_detail=False, quiet=True))
                self.after(
                    0,
                    lambda: messagebox.showinfo("완료", f"가져오기 완료\n성공 {ok} / 실패 {fail}"),
                )
            except Exception as e:
                self._put_log(f"오류: {e}", channel=LOG_COLLECT)
                self._finish_channel_progress(
                    LOG_COLLECT, summary=str(e), done=0, total=1, ok=0, fail=1
                )
                self.after(0, lambda: messagebox.showerror("오류", str(e)))
            finally:
                self.after(0, lambda: self._job_end("import"))

        threading.Thread(target=work, daemon=True).start()


def main() -> None:
    app = ManagerApp()
    app.mainloop()


if __name__ == "__main__":
    main()

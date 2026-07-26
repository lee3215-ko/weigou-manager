# -*- coding: utf-8 -*-
"""Weigou product manager — browse, search, import with description."""
from __future__ import annotations

import os
import pathlib
import queue
import re
import threading
import tkinter as tk
import zipfile
from tkinter import filedialog, messagebox, scrolledtext, ttk

from auto_collect import walk_list_details
from catalog_sync import CatalogSyncService, load_sync_settings, save_sync_settings
from collector import collect_page_best, is_cdp_up
from google_lens import search_product_images, search_products_multi
from launcher import DEFAULT_PORT, is_running, start_debug
from ime_win import (
    commit_composition,
    get_composition,
    restore_text_if_stripped,
    snapshot_widget_text,
)
from mall_publish import preview_price, publish_products
from paths import (
    APP_DISPLAY_NAME,
    APP_NAME,
    APP_VERSION,
    EXE_NAME,
    UPDATE_VERSION_URL,
    init_runtime_paths,
)
from product_attrs import extract_attrs
from product_name import ko_name_to_en
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
        self.list_mode = tk.StringVar(value="products")  # products | excluded | published
        self._photo_cache: list[tk.PhotoImage] = []
        self._list_photos: dict[int, tk.PhotoImage] = {}
        self._log_q: queue.Queue[str] = queue.Queue()
        # Independent background jobs — collect does NOT block publish/search
        self._jobs: set[str] = set()
        self._jobs_lock = threading.Lock()
        self._stop = threading.Event()
        self._cancel_job = threading.Event()
        self._form_loading = False
        self._ime_composing = False
        self._pending_soft_save = False
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

        self.query = tk.StringVar()
        self.filter_category = tk.StringVar(value="전체")
        self.status = tk.StringVar(value="준비됨")
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

        self._sync = CatalogSyncService(
            self.store,
            on_log=lambda m: self._log_q.put(m),
            on_pulled=lambda: self.after(
                0, lambda: self.refresh_list(reload_detail=False)
            ),
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
        self.after(1200, self._sync.start)
        self.after(
            1800,
            lambda: schedule_update_check(
                self,
                version_url=UPDATE_VERSION_URL,
                current_version=APP_VERSION,
                app_name=APP_NAME,
                exe_name=EXE_NAME,
                zip_inner_folder=APP_NAME,
                log_callback=lambda m: self._log_q.put(m),
            ),
        )

    def _on_sku_typed(self, _event=None) -> None:
        if self._form_loading or self._ime_composing:
            return
        self._refresh_price_preview()

    def _build(self) -> None:
        top = tk.Frame(self, bg="#f3efe8")
        top.pack(fill="x", padx=12, pady=10)

        tk.Label(
            top,
            text="상품 관리",
            font=("Malgun Gothic", 16, "bold"),
            bg="#f3efe8",
            fg="#1f1a17",
        ).pack(side="left")

        tk.Button(
            top,
            text="목록 동기화",
            command=self._on_sync_settings,
            font=("Malgun Gothic", 10),
            bg="#ebe4da",
        ).pack(side="right", padx=4)
        tk.Button(
            top,
            text="디버그 실행",
            command=self._on_launch,
            font=("Malgun Gothic", 10),
            bg="#ebe4da",
        ).pack(side="right", padx=4)
        tk.Button(
            top,
            text="전체 이미지 검색",
            command=self._on_google_all,
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
        tk.Button(
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
        ).pack(side="right", padx=4)
        tk.Button(
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
        ).pack(side="right", padx=4)

        body = tk.Frame(self, bg="#f3efe8")
        body.pack(fill="both", expand=True, padx=12, pady=(0, 8))

        # Left pane — 목록 제목이 잘리지 않도록 넓게
        left = tk.Frame(body, bg="#f3efe8", width=480)
        left.pack(side="left", fill="y")
        left.pack_propagate(False)

        search_row = tk.Frame(left, bg="#f3efe8")
        search_row.pack(fill="x", pady=(0, 6))
        ent = tk.Entry(search_row, textvariable=self.query, font=("Malgun Gothic", 10))
        ent.pack(side="left", fill="x", expand=True)
        ent.bind("<Return>", lambda _e: self.refresh_list())
        tk.Button(
            search_row, text="검색", command=self.refresh_list, font=("Malgun Gothic", 9)
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
        self.filter_box.bind("<<ComboboxSelected>>", lambda _e: self.refresh_list())

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
        self.listbox = tk.Listbox(
            left,
            font=("Malgun Gothic", 10),
            activestyle="dotbox",
            selectmode=tk.EXTENDED,  # Ctrl/Shift 다중 선택 → 일괄 등록·제외
            bg="#fffdf9",
            relief="solid",
            borderwidth=1,
            exportselection=False,
        )
        self.listbox.pack(fill="both", expand=True)
        self.listbox.bind("<<ListboxSelect>>", self._on_select)
        self.list_hint = tk.Label(
            left,
            text="Ctrl·Shift 클릭으로 여러 개 선택 → 등록/제외/AI코디",
            bg="#f3efe8",
            fg="#666",
            font=("Malgun Gothic", 8),
            anchor="w",
        )
        self.list_hint.pack(fill="x", pady=(4, 0))

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
        tk.Button(
            cat_row,
            text="자동인식",
            command=lambda: self._auto_fill_attrs(silent=False),
            font=("Malgun Gothic", 9),
        ).pack(side="left", padx=8)
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

        bottom = tk.Frame(self, bg="#f3efe8")
        bottom.pack(fill="x", padx=12, pady=(0, 8))
        tk.Label(bottom, textvariable=self.status, bg="#f3efe8", fg="#2d6a4f", font=("Malgun Gothic", 9)).pack(
            anchor="w"
        )
        self.log = scrolledtext.ScrolledText(bottom, height=4, font=("Consolas", 9), bg="#fffdf9")
        self.log.pack(fill="x", pady=(4, 0))
        self._append(
            f"저장 위치: {default_root()}\n"
            "1) 디버그 실행 → 2) 친구 앨범 목록 열기 → 3) [목록→상세 자동수집]\n"
            "   · 한 화면씩 처리 (끝까지 무한 스크롤 안 함)\n"
            "   · 이미 수집·제외는 클릭 없이 패스, 1회 신규 최대 40개\n"
            "   · 마지막 data-index 기억 → 다음 실행 시 그 다음부터 스크롤·수집\n"
            "4) [이미지로 제품명 찾기] → 제품명·카테고리 자동 기록\n"
            "5) NO·컬러·사이즈 확인 후 [홈페이지 등록] → 등록 목록으로 이동\n"
            "   (제품명 한/영·컬러·사이즈 함께 저장됨)\n"
            "[제외]/[등록] 상품은 이후 자동/수동 수집에서 건너뜁니다.\n"
            "[등록목록에서 제거] 시 상품 관리 목록으로 복원됩니다.\n"
            "[등록] 탭: 상품 선택 → [AI 상품선택] → [AI 코디 만들기]에서 모델 이미지 업로드 후 홈페이지 적용\n"
            "자동수집 중 멈추려면 [중지]\n"
        )

    def _on_close(self) -> None:
        self._stop.set()
        try:
            self._sync.stop()
        except Exception:
            pass
        self.destroy()

    def _mark_catalog_dirty(self) -> None:
        try:
            self._sync.mark_dirty()
        except Exception:
            pass

    def _on_sync_settings(self) -> None:
        cfg = load_sync_settings()
        win = tk.Toplevel(self)
        win.title("목록 동기화 설정")
        win.geometry("520x360")
        win.configure(bg="#f3efe8")
        win.transient(self)
        win.grab_set()
        tk.Label(
            win,
            text="여러 PC가 같은 상품/제외/등록 목록을 공유합니다.\n"
            "GitHub 토큰(repo 권한)을 넣으면 올리기·받기가 됩니다.",
            bg="#f3efe8",
            font=("Malgun Gothic", 9),
            justify="left",
            anchor="w",
        ).pack(fill="x", padx=16, pady=(14, 8))

        enabled = tk.BooleanVar(value=bool(cfg.get("enabled", True)))
        tk.Checkbutton(
            win,
            text="동기화 사용",
            variable=enabled,
            bg="#f3efe8",
            font=("Malgun Gothic", 10),
        ).pack(anchor="w", padx=16)

        def row(label: str, value: str, show: str | None = None) -> tk.Entry:
            fr = tk.Frame(win, bg="#f3efe8")
            fr.pack(fill="x", padx=16, pady=4)
            tk.Label(fr, text=label, width=14, anchor="w", bg="#f3efe8").pack(side="left")
            e = tk.Entry(fr, show=show or "", font=("Consolas", 10))
            e.pack(side="left", fill="x", expand=True)
            e.insert(0, value)
            return e

        e_token = row("GitHub 토큰", str(cfg.get("github_token") or ""), show="*")
        e_owner = row("owner", str(cfg.get("github_owner") or ""))
        e_repo = row("repo", str(cfg.get("github_repo") or ""))
        e_interval = row("주기(초)", str(cfg.get("interval_sec") or 12))
        e_device = row("이 PC 이름", str(cfg.get("device_name") or ""))

        def save_and_close() -> None:
            try:
                interval = int(float(e_interval.get().strip() or "12"))
            except ValueError:
                interval = 12
            new_cfg = {
                **cfg,
                "enabled": bool(enabled.get()),
                "github_token": e_token.get().strip(),
                "github_owner": e_owner.get().strip() or "lee3215-ko",
                "github_repo": e_repo.get().strip() or "weigou-manager",
                "interval_sec": max(5, interval),
                "device_name": e_device.get().strip(),
            }
            save_sync_settings(new_cfg)
            self._mark_catalog_dirty()
            self._sync.sync_now()
            self._append("[동기화] 설정 저장 — 바로 동기화 시도")
            win.destroy()

        btn = tk.Frame(win, bg="#f3efe8")
        btn.pack(fill="x", padx=16, pady=16)
        tk.Button(
            btn,
            text="저장 후 동기화",
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
            text="지금 동기화만",
            command=lambda: (self._mark_catalog_dirty(), self._sync.sync_now()),
            font=("Malgun Gothic", 10),
            bg="#ebe4da",
            relief="flat",
            padx=10,
        ).pack(side="left", padx=8)
        tk.Button(btn, text="닫기", command=win.destroy, bg="#ebe4da", relief="flat").pack(
            side="right"
        )

    def _append(self, msg: str) -> None:
        self.log.insert("end", msg if msg.endswith("\n") else msg + "\n")
        self.log.see("end")

    def _poll_log(self) -> None:
        batch: list[str] = []
        while True:
            try:
                batch.append(self._log_q.get_nowait())
            except queue.Empty:
                break
        if batch:
            self._append("\n".join(batch))
        if not self._stop.is_set():
            self.after(250, self._poll_log)

    def _status_loop(self) -> None:
        while not self._stop.is_set():
            try:
                running = is_running()
                cdp = is_cdp_up(DEFAULT_PORT)
                if cdp:
                    text = "연결됨 · 디버그 포트 OK"
                elif running:
                    text = "앱 실행 중 · 디버그 포트 없음 → [디버그 실행] 필요"
                else:
                    text = "앱 미실행"
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
                    self.after(
                        0,
                        lambda t=text, e=ex_n, j=job_bit: self.status.set(
                            f"{t}  |  제외 목록 {e}개{j}"
                        ),
                    )
                elif mode == "published":
                    self.after(
                        0,
                        lambda t=text, p=pub_n, j=job_bit: self.status.set(
                            f"{t}  |  등록 목록 {p}개{j}"
                        ),
                    )
                else:
                    self.after(
                        0,
                        lambda t=text, n=n, e=ex_n, p=pub_n, j=job_bit: self.status.set(
                            f"{t}  |  상품 {n}개  ·  등록 {p}개  ·  제외 {e}개{j}"
                        ),
                    )
            except Exception:
                pass
            self._stop.wait(3.0)

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
            return True

    def _job_end(self, name: str) -> None:
        with self._jobs_lock:
            self._jobs.discard(name)

    def _job_running(self, name: str) -> bool:
        with self._jobs_lock:
            return name in self._jobs

    def _jobs_status_text(self) -> str:
        with self._jobs_lock:
            names = sorted(self._jobs)
        if not names:
            return ""
        labels = [self._job_labels.get(n, n) for n in names]
        return " · ".join(f"{lb} 중" for lb in labels)

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
                )
            except Exception:
                self.refresh_list(reload_detail=reload_detail)

        self._collect_refresh_after = self.after(700, _do)

    def _on_list_mode(self) -> None:
        self.current_id = None
        self.current_excluded_id = None
        self.current_published_id = None
        self._apply_mode_buttons()
        self.refresh_list()

    def _apply_mode_buttons(self) -> None:
        mode = self.list_mode.get()
        self.btn_save.pack_forget()
        self.btn_publish.pack_forget()
        self.btn_exclude.pack_forget()
        self.btn_delete.pack_forget()
        self.btn_unexclude.pack_forget()
        self.btn_unpublish.pack_forget()
        self.btn_ai_select.pack_forget()
        self.btn_ai_apply.pack_forget()
        if mode == "excluded":
            self.btn_unexclude.pack(side="left", padx=6)
            self.listbox.configure(selectmode=tk.EXTENDED)
            self.list_hint.configure(text="Ctrl·Shift 클릭으로 여러 개 선택 → 제외 해제")
        elif mode == "published":
            self.btn_unpublish.pack(side="left", padx=6)
            self.btn_ai_select.pack(side="left", padx=6)
            self.btn_ai_apply.pack(side="left", padx=6)
            # 일반 클릭=1개, Ctrl/Shift=다중 (MULTIPLE 토글 방식 아님)
            self.listbox.configure(selectmode=tk.EXTENDED)
            self.list_hint.configure(
                text="클릭=1개 선택 · Ctrl/Shift=여러 개 → AI 상품선택 · AI 코디 만들기"
            )
        else:
            self.btn_save.pack(side="left")
            self.btn_publish.pack(side="left", padx=6)
            self.btn_exclude.pack(side="left", padx=6)
            self.btn_delete.pack(side="left", padx=6)
            self.listbox.configure(selectmode=tk.EXTENDED)
            self.list_hint.configure(text="Ctrl·Shift 클릭으로 여러 개 선택 → 등록/제외")

    def refresh_list(
        self,
        *,
        preserve_yview: tuple[float, float] | None = None,
        reload_detail: bool = True,
    ) -> None:
        self.listbox.delete(0, tk.END)
        mode = self.list_mode.get()
        if mode == "excluded":
            self.excluded_items = self.store.list_excluded(self.query.get())
            self.products = []
            self.published_items = []
            for item in self.excluded_items:
                cat = item.category or "?"
                name = item.title or "(제목 없음)"
                if len(name) > 52:
                    name = name[:52] + "…"
                code = f" [{item.search_code}]" if item.search_code else ""
                self.listbox.insert(tk.END, f"[제외][{cat}] #{item.id} {name}{code}")
            if self.excluded_items:
                idx = 0
                if self.current_excluded_id is not None:
                    for i, item in enumerate(self.excluded_items):
                        if item.id == self.current_excluded_id:
                            idx = i
                            break
                self.listbox.selection_set(idx)
                if preserve_yview is not None:
                    self.listbox.yview_moveto(preserve_yview[0])
                else:
                    self.listbox.see(idx)
                if reload_detail:
                    self._show_excluded(self.excluded_items[idx])
            else:
                self.current_excluded_id = None
                if reload_detail:
                    self._clear_detail()
            return

        if mode == "published":
            self.published_items = self.store.list_published(self.query.get())
            self.products = []
            self.excluded_items = []
            for item in self.published_items:
                cat = item.category or "?"
                name = item.google_name or item.title or "(제목 없음)"
                if len(name) > 52:
                    name = name[:52] + "…"
                code = f" [{item.search_code}]" if item.search_code else ""
                color = f" · {item.colors}" if item.colors else ""
                self.listbox.insert(tk.END, f"[등록][{cat}] #{item.id} {name}{code}{color}")
            if self.published_items:
                idx = 0
                if self.current_published_id is not None:
                    for i, item in enumerate(self.published_items):
                        if item.id == self.current_published_id:
                            idx = i
                            break
                self.listbox.selection_set(idx)
                if preserve_yview is not None:
                    self.listbox.yview_moveto(preserve_yview[0])
                else:
                    self.listbox.see(idx)
                if reload_detail:
                    self._show_published(self.published_items[idx])
            else:
                self.current_published_id = None
                if reload_detail:
                    self._clear_detail()
            return

        self.products = self.store.list_products(
            self.query.get(),
            category=self.filter_category.get(),
        )
        self.excluded_items = []
        self.published_items = []
        for p in self.products:
            cat = p.category or "?"
            name = p.google_name or p.title or "(제목 없음)"
            if len(name) > 52:
                name = name[:52] + "…"
            code = f" [{p.search_code}]" if p.search_code else ""
            self.listbox.insert(tk.END, f"[{cat}] #{p.id} {name}{code}")
        if self.products and self.current_id is None:
            self.listbox.selection_set(0)
            if preserve_yview is not None:
                self.listbox.yview_moveto(preserve_yview[0])
            else:
                self.listbox.see(0)
            if reload_detail:
                self._show_product(self.products[0])
        elif self.current_id is not None:
            selected = False
            for i, p in enumerate(self.products):
                if p.id == self.current_id:
                    self.listbox.selection_set(i)
                    if preserve_yview is not None:
                        self.listbox.yview_moveto(preserve_yview[0])
                    else:
                        self.listbox.see(i)
                    if reload_detail:
                        self._show_product(p)
                    selected = True
                    break
            if not selected and self.products:
                # Current product archived/removed — keep form unless forced
                self.listbox.selection_set(0)
                if preserve_yview is not None:
                    self.listbox.yview_moveto(preserve_yview[0])
                else:
                    self.listbox.see(0)
                if reload_detail:
                    self._show_product(self.products[0])
        elif not self.products:
            if reload_detail:
                self._clear_detail()

    def _clear_detail(self) -> None:
        self._begin_form_load()
        try:
            self.current_id = None
            self.current_excluded_id = None
            self.current_published_id = None
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

    def _on_select(self, _event=None) -> None:
        sel = self.listbox.curselection()
        if not sel:
            return
        # 다중 선택 시 마지막(활성) 항목 상세를 표시
        idx = int(sel[-1])
        mode = self.list_mode.get()
        if mode == "excluded":
            if 0 <= idx < len(self.excluded_items):
                self._show_excluded(self.excluded_items[idx])
        elif mode == "published":
            if 0 <= idx < len(self.published_items):
                self._show_published(self.published_items[idx])
        else:
            # 다른 항목 클릭 전 한글 조합 확정 + 현재 입력값 즉시 저장
            if self.current_id is not None:
                if 0 <= idx < len(self.products) and self.products[idx].id != self.current_id:
                    self._finalize_ime(self.focus_get() or self._ime_focus_widget)
                    self._soft_save_current(force=True)
            if 0 <= idx < len(self.products):
                self._show_product(self.products[idx])

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
        if self._form_loading or self.list_mode.get() != "products":
            return
        if self.current_id is None:
            return
        self._schedule_soft_save(delay_ms=50)

    def _soft_save_current(self, force: bool = False) -> None:
        self._attr_after = None
        if self._form_loading or self.current_id is None:
            return
        if self.list_mode.get() != "products":
            return
        if self._ime_composing and not force:
            self._pending_soft_save = True
            return
        self._pending_soft_save = False
        self._ime_composing = False
        try:
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
            self._mark_catalog_dirty()
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

    def _thumb(self, path: str, size: tuple[int, int] = (180, 180)) -> tk.PhotoImage | None:
        p = pathlib.Path(path)
        if not p.exists():
            return None
        if Image is None or ImageTk is None:
            # Fallback without Pillow: skip large raw PhotoImage
            return None
        try:
            im = Image.open(p)
            im.thumbnail(size, Image.Resampling.LANCZOS)
            photo = ImageTk.PhotoImage(im)
            self._photo_cache.append(photo)
            return photo
        except Exception:
            return None

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
            self.sku_var.set(item.sku_no)
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
                    f"mall_id: {item.mall_id}" if item.mall_id else "",
                    f"goods_id: {item.goods_id}" if item.goods_id else "",
                    f"등록일: {item.created_at}" if item.created_at else "",
                    item.note,
                )
                if x
            )
            self.desc.insert("1.0", (body + "\n\n" + meta).strip() if body else meta)
            self.price_preview.set("홈페이지 등록 완료 — 이후 수집에서 자동으로 건너뜁니다")
            self._clear_images()
            paths = list(item.image_paths) if item.image_paths else []
            if not paths and item.cover_path:
                paths = [item.cover_path]
            gen = self._select_gen
            self._schedule_thumbs(paths, gen, cover_only=False)
        finally:
            self._end_form_load()

    def _schedule_thumbs(
        self,
        paths: list[str],
        gen: int,
        *,
        cover_only: bool = False,
    ) -> None:
        """Load thumbnails asynchronously so list selection stays instant."""
        if not paths:
            tk.Label(self.img_frame, text="이미지 없음", bg="#fffdf9", fg="#888").pack(pady=20)
            return

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
                    tk.Label(cell, image=photo, bg="#fffdf9").pack()
                else:
                    tk.Label(
                        cell,
                        text=pathlib.Path(path).name,
                        bg="#fffdf9",
                        font=("Consolas", 8),
                        wraplength=160,
                    ).pack()
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
            self.sku_var.set(p.sku_no)
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
            paths = list(p.image_paths) if p.image_paths else []
            self._schedule_thumbs(paths, gen)
        finally:
            self._end_form_load()

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
            color = ", ".join(img_attrs.colors) if img_attrs.colors else ""

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
                self.color_var.set(", ".join(attrs.colors))
                self.size_var.set(", ".join(attrs.sizes))
            else:
                if not self.color_var.get().strip() and attrs.colors:
                    self.color_var.set(", ".join(attrs.colors))
                if not self.size_var.get().strip() and attrs.sizes:
                    self.size_var.set(", ".join(attrs.sizes))
                if not self.category_var.get().strip() and attrs.category:
                    self.category_var.set(attrs.category)
        finally:
            self._form_loading = False
        if not silent:
            self._soft_save_current()

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

    def _on_publish(self) -> None:
        if self.list_mode.get() != "products":
            return
        if not self._job_start("publish"):
            self._warn_job_busy("publish")
            return
        ids = self._selected_product_ids()
        if not ids:
            self._job_end("publish")
            messagebox.showwarning("선택", "등록할 상품을 선택하세요.")
            return

        # 현재 편집 중인 항목은 폼 값 먼저 저장
        if self.current_id is not None and self.current_id in ids:
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

        if len(ids) > 1 and not messagebox.askyesno(
            "일괄 등록",
            f"선택한 {len(ids)}개 상품을 홈페이지에 등록할까요?",
        ):
            self._job_end("publish")
            return

        # 등록 후 선택 유지용: 선택 밖 다음 상품
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
        pre_fail = 0
        pre_lines: list[str] = []
        for pid in ids:
            product = self.store.get(pid)
            if not product:
                pre_fail += 1
                pre_lines.append(f"#{pid} 없음")
                continue
            if pid == self.current_id:
                colors = re_split_colors(self.color_var.get())
                sizes = self._split_csv(self.size_var.get())
                category = self.category_var.get().strip() or None
            else:
                colors = re_split_colors(product.colors)
                sizes = self._split_csv(product.sizes)
                category = product.category or None
            jobs.append((product, colors, sizes, category))

        self._append(f"----- 홈페이지 등록 {len(jobs)}개 (백그라운드) -----")

        def work() -> None:
            ok_n = 0
            fail_n = pre_fail
            lines = list(pre_lines)
            try:
                results = publish_products(jobs, push_api=True) if jobs else []
                for result in results:
                    pid = int(result.get("productId") or 0)
                    if not result.get("ok"):
                        fail_n += 1
                        err = result.get("error") or "실패"
                        lines.append(f"#{pid} 실패: {err}")
                        self._log_q.put(f"등록 실패 #{pid}: {err}")
                        continue
                    try:
                        item = result["product"]
                        mall_id = str(item.get("id") or "")
                        archived = self.store.archive_published(
                            pid,
                            mall_id=mall_id,
                            note=result.get("priceLabel") or "",
                        )
                        ok_n += 1
                        name = item.get("name") or ""
                        lines.append(f"#{pid} → 등록 #{archived} / {name}")
                        self._log_q.put(
                            f"홈페이지 등록: {mall_id} / {result.get('priceLabel')} → 등록 목록 #{archived}"
                        )
                    except Exception as e:
                        fail_n += 1
                        lines.append(f"#{pid} 실패: {e}")
                        self._log_q.put(f"등록 실패 #{pid}: {e}")

                summary = "\n".join(lines[:12])
                if len(lines) > 12:
                    summary += f"\n… 외 {len(lines) - 12}건"
                ok_final, fail_final = ok_n, fail_n

                def finish() -> None:
                    self._mark_catalog_dirty()
                    self.current_id = next_id
                    self.current_published_id = None
                    self.refresh_list(preserve_yview=(y0, y1))
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
                self._log_q.put(f"등록 오류: {e}")
                self.after(0, lambda: messagebox.showerror("등록 실패", str(e)))
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
            "상품 관리 목록으로 되돌릴까요?\n"
            "(쇼핑몰에 올라간 상품은 그대로입니다)",
        ):
            return
        last_restored: int | None = None
        for pub_id in ids:
            restored_id = self.store.unpublish(pub_id)
            self._append(f"등록목록 제거 #{pub_id} → 상품관리 #{restored_id}")
            if restored_id is not None:
                last_restored = restored_id
        self._mark_catalog_dirty()
        self.current_published_id = None
        if last_restored is not None:
            self.current_id = last_restored
            self.list_mode.set("products")
            self._apply_mode_buttons()
            self.refresh_list()
        else:
            self.current_id = None
            self.refresh_list()

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
                self._log_q.put(f"ZIP: {zip_path} ({len(files)}개 파일)")
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
        win.geometry("560x680")
        win.configure(bg="#f3efe8")
        win.transient(self)
        win.grab_set()

        tk.Label(
            win,
            text="AI 모델 추천 코디",
            bg="#f3efe8",
            font=("Malgun Gothic", 14, "bold"),
            anchor="w",
        ).pack(fill="x", padx=16, pady=(16, 4))
        tk.Label(
            win,
            text="선택 상품을 확인한 뒤 모델 이미지를 올리고 적용하세요.\n제목·설명은 상품 정보로 자동 생성됩니다.",
            bg="#f3efe8",
            fg="#555",
            font=("Malgun Gothic", 9),
            anchor="w",
            wraplength=520,
            justify="left",
        ).pack(fill="x", padx=16)

        codes_box = scrolledtext.ScrolledText(
            win, height=7, font=("Malgun Gothic", 10), bg="#fffdf9", relief="solid", borderwidth=1
        )
        codes_box.pack(fill="x", padx=16, pady=8)
        lines = [
            f"{i}. NO {row['code']}  ·  {row.get('category','')}  ·  {row.get('name','')}"
            for i, row in enumerate(self.ai_style_items, 1)
        ]
        codes_box.insert("1.0", "\n".join(lines))
        codes_box.configure(state="disabled")

        img_var = tk.StringVar(value=str(self.ai_model_image or "(아직 없음)"))
        preview_label = tk.Label(win, bg="#ddd6ce")
        preview_photo: list[tk.PhotoImage | None] = [None]

        def show_preview(path: pathlib.Path) -> None:
            img_var.set(str(path))
            if Image is None:
                preview_label.configure(text=path.name, image="")
                return
            try:
                im = Image.open(path)
                im.thumbnail((320, 400))
                photo = ImageTk.PhotoImage(im)
                preview_photo[0] = photo
                preview_label.configure(image=photo, text="")
            except Exception as e:
                preview_label.configure(text=f"미리보기 실패: {e}", image="")

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

        img_row = tk.Frame(win, bg="#f3efe8")
        img_row.pack(fill="x", padx=16, pady=6)
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

        preview_label.pack(padx=16, pady=6)

        replace_var = tk.BooleanVar(value=False)
        tk.Checkbutton(
            win,
            text="기존 AI 코디를 지우고 이 룩만 남기기",
            variable=replace_var,
            bg="#f3efe8",
            font=("Malgun Gothic", 9),
            activebackground="#f3efe8",
        ).pack(anchor="w", padx=16, pady=(0, 4))

        if self.ai_model_image and self.ai_model_image.exists():
            show_preview(self.ai_model_image)

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

        zip_row = tk.Frame(win, bg="#f3efe8")
        zip_row.pack(fill="x", padx=16, pady=(8, 0))
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

        btn_row = tk.Frame(win, bg="#f3efe8")
        btn_row.pack(fill="x", padx=16, pady=12)
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

    def _run_google_for(self, product: Product, *, headless: bool = False) -> tuple[bool, str]:
        # Use UI field values when searching the selected product
        size = self.size_var.get().strip() if self.current_id == product.id else product.sizes
        color = self.color_var.get().strip() if self.current_id == product.id else product.colors
        if not size:
            size = product.sizes.strip()
        if not color:
            color = product.colors.strip()
        hint = " ".join(x for x in (product.title, product.tags, product.description) if x)
        # 상품 폴더 첫 이미지만 첨부 (커버 → image_paths[0])
        first_img = ""
        if product.cover_path and pathlib.Path(product.cover_path).exists():
            first_img = product.cover_path
        elif product.image_paths:
            for pth in product.image_paths:
                if pathlib.Path(pth).exists():
                    first_img = pth
                    break
        paths = [first_img] if first_img else list(product.image_paths[:1])
        self._log_q.put(
            f"구글 검색: 이미지 복사붙여넣기 → 「사이즈 … / 제품명, 컬러를 알려줘」"
            + (f" · {pathlib.Path(first_img).name}" if first_img else " · 이미지 없음!")
            + (f" · {size}" if size else "")
        )
        if not first_img and not product.image_urls:
            return False, "첨부할 이미지가 없습니다. 상품 폴더 첫 이미지를 확인하세요."
        result = search_product_images(
            paths,
            image_urls=product.image_urls if not paths else None,
            hint=hint,
            size=size,
            color="",  # 컬러는 질문에 넣지 않음 — 검색 결과로 받음
            headless=headless,
        )
        if result.error and not result.product_name:
            return False, result.error
        if not result.product_name:
            return False, "제품명을 찾지 못했습니다."
        category = result.category or extract_attrs(result.product_name, product.tags, hint).category
        raw_blob = "\n".join(result.raw_texts[:50]) if result.raw_texts else ""
        # 검색(AI) 컬러가 있으면 이미지 자동감지로 덮지 않음
        colors = (result.color or "").strip()
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
                colors = ", ".join(attrs.colors)
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
        self._mark_catalog_dirty()
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
        if product.cover_path and pathlib.Path(product.cover_path).exists():
            return product.cover_path
        for pth in product.image_paths or []:
            if pathlib.Path(pth).exists():
                return pth
        return ""

    def _apply_google_result_to_product(
        self, product: Product, result, *, size_fallback: str = ""
    ) -> tuple[bool, str]:
        if result.error and not result.product_name:
            return False, result.error
        if not result.product_name:
            return False, "제품명을 찾지 못했습니다."
        hint = " ".join(x for x in (product.title, product.tags, product.description) if x)
        category = result.category or extract_attrs(result.product_name, product.tags, hint).category
        colors = (result.color or "").strip()
        if not colors:
            # raw_texts 한 줄에서라도 컬러 후보 추출
            for ln in result.raw_texts or []:
                if "컬러" in ln or "색상" in ln or "번째 이미지" in ln or "이미지" in ln:
                    from multi_ai_parse import _color_from_chunk

                    colors = _color_from_chunk([ln], ln)
                    if colors:
                        break
        if not colors:
            attrs = extract_attrs(result.product_name, product.tags, hint)
            if attrs.colors:
                colors = ", ".join(attrs.colors)
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

    def _run_google_multi(self, products: list[Product]) -> tuple[int, int, str]:
        """Paste all selected product images once, apply per-image AI answers."""
        jobs: list[dict] = []
        ready: list[Product] = []
        for p in products:
            img = self._first_image_path(p)
            if not img:
                continue
            size = self.size_var.get().strip() if self.current_id == p.id else p.sizes
            if not size:
                size = p.sizes.strip()
            hint = " ".join(x for x in (p.title, p.tags, p.description) if x)
            jobs.append({"path": img, "size": size, "hint": hint})
            ready.append(p)
        if not jobs:
            return 0, len(products), "선택한 상품에 이미지가 없습니다."

        self._log_q.put(
            f"구글 다중 검색: 이미지 {len(jobs)}장 붙여넣기 → "
            f"「각 제품의 제품명과 컬러를 알려줘」"
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
                self._log_q.put(f"  #{p.id} → {msg}")
            else:
                fail_n += 1
                lines.append(f"#{p.id} 실패 — {msg}")
                self._log_q.put(f"  #{p.id} 실패: {msg}")
        # products without images
        fail_n += len(products) - len(ready)
        summary = "\n".join(lines[:16])
        if len(lines) > 16:
            summary += f"\n… 외 {len(lines) - 16}건"
        return ok_n, fail_n, summary

    def _on_google_one(self) -> None:
        if not self._job_start("search"):
            self._warn_job_busy("search")
            return
        if self.list_mode.get() != "products":
            self._job_end("search")
            messagebox.showwarning("선택", "상품 목록에서 선택하세요.")
            return
        ids = self._selected_product_ids()
        if not ids and self.current_id is not None:
            ids = [self.current_id]
        if not ids:
            self._job_end("search")
            messagebox.showwarning("선택", "상품을 먼저 선택하세요.")
            return

        # Soft-save current form before batch
        if self.current_id is not None and self.current_id in ids:
            self._soft_save_current(force=True)

        products: list[Product] = []
        for pid in ids:
            p = self.store.get(pid)
            if p:
                products.append(p)
        if not products:
            self._job_end("search")
            return

        if len(products) == 1:
            self._append(f"----- 이미지 검색 #{products[0].id} -----")
        else:
            self._append(f"----- 이미지 다중 검색 {len(products)}개 -----")

        def work() -> None:
            try:
                if len(products) == 1:
                    ok, msg = self._run_google_for(products[0], headless=False)
                    self._log_q.put(("OK: " if ok else "실패: ") + msg)

                    def after_one() -> None:
                        self.refresh_list(reload_detail=False)
                        if ok:
                            refreshed = self.store.get(products[0].id)
                            if refreshed and self.current_id == products[0].id:
                                self._show_product(refreshed)
                            messagebox.showinfo("이미지 검색", f"제품명 저장됨\n{msg}")
                        else:
                            messagebox.showwarning("이미지 검색", msg)

                    self.after(0, after_one)
                else:
                    ok_n, fail_n, summary = self._run_google_multi(products)

                    def after_multi() -> None:
                        self.refresh_list(reload_detail=False)
                        if self.current_id is not None:
                            refreshed = self.store.get(self.current_id)
                            if refreshed:
                                self._show_product(refreshed)
                        messagebox.showinfo(
                            "이미지 다중 검색",
                            f"완료: 성공 {ok_n} / 실패 {fail_n}\n\n{summary}",
                        )

                    self.after(0, after_multi)
            except Exception as e:
                self._log_q.put(f"오류: {e}")
                self.after(0, lambda: messagebox.showerror("오류", str(e)))
            finally:
                self.after(0, lambda: self._job_end("search"))

        threading.Thread(target=work, daemon=True).start()

    def _on_google_all(self) -> None:
        if not self._job_start("search"):
            self._warn_job_busy("search")
            return
        products = self.store.list_products(self.query.get(), category=self.filter_category.get())
        if not products:
            self._job_end("search")
            messagebox.showwarning("없음", "검색할 상품이 없습니다.")
            return
        # Prefer products without google_name first
        targets = [p for p in products if not p.google_name] or products
        if not messagebox.askyesno(
            "전체 이미지 검색",
            f"{len(targets)}개 상품 이미지를 검색할까요?\n"
            "시간이 꽤 걸릴 수 있습니다.\n"
            "(수집·등록과 동시에 진행할 수 있습니다)",
        ):
            self._job_end("search")
            return
        self._append(f"----- 전체 이미지 검색 {len(targets)}개 -----")

        def work() -> None:
            ok_n = 0
            fail_n = 0
            try:
                for i, p in enumerate(targets, start=1):
                    self._log_q.put(f"[{i}/{len(targets)}] #{p.id} 검색 중…")
                    ok, msg = self._run_google_for(p, headless=True)
                    if ok:
                        ok_n += 1
                        self._log_q.put(f"  → {msg}")
                    else:
                        fail_n += 1
                        self._log_q.put(f"  → 실패: {msg}")
                self.after(0, lambda: self.refresh_list(reload_detail=False))
                self.after(
                    0,
                    lambda: messagebox.showinfo(
                        "완료",
                        f"이미지 검색 완료\n성공 {ok_n} / 실패 {fail_n}\n목록은 카테고리순으로 정렬됩니다.",
                    ),
                )
            except Exception as e:
                self._log_q.put(f"오류: {e}")
                self.after(0, lambda: messagebox.showerror("오류", str(e)))
            finally:
                self.after(0, lambda: self._job_end("search"))

        threading.Thread(target=work, daemon=True).start()

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
        self.refresh_list(preserve_yview=(float(yview[0]), float(yview[1])))

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

        path = self._resolve_image_folder()
        path.mkdir(parents=True, exist_ok=True)
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
                self._log_q.put(msg)
                self.after(
                    0,
                    lambda: messagebox.showinfo("실행", msg)
                    if ok
                    else messagebox.showerror("실패", msg),
                )
            finally:
                self.after(0, lambda: self._job_end("launch"))

        threading.Thread(target=work, daemon=True).start()

    def _on_cancel_job(self) -> None:
        if self._job_running("collect") or self._job_running("import"):
            self._cancel_job.set()
            self._append("중지 요청… 수집/가져오기만 멈춥니다. (검색·등록은 계속됩니다)")
        elif self._jobs_status_text():
            self._append(
                f"중지 대상 없음 — 현재: {self._jobs_status_text()} "
                "(수집·가져오기만 중지 가능)"
            )
        else:
            self._append("진행 중인 작업이 없습니다.")

    def _on_auto_collect(self) -> None:
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
        if not messagebox.askyesno(
            "자동수집",
            "화면에 보이는 상품부터 한 화면씩 내려가며\n"
            "아직 없는 상품만 상세 수집합니다.\n\n"
            "· 이미 수집/제외 상품 → 클릭 안 함\n"
            "· 새 상품이 더 안 보이면 자동 종료\n"
            "· 1회 신규 최대 40개 (다시 누르면 마지막 인덱스 다음부터)\n\n"
            "수집 중에도 홈페이지 등록·이미지 검색을 사용할 수 있습니다.\n"
            "목록 화면을 연 상태로 두세요. 계속할까요?",
        ):
            self._job_end("collect")
            return
        self._cancel_job.clear()
        self._append("----- 목록→상세 자동수집 시작 (백그라운드) -----")
        self._append("수집 중에도 등록·이미지검색을 사용할 수 있습니다.")

        def work() -> None:
            ok_n = 0
            fail_n = 0

            def save_one(p) -> None:
                nonlocal ok_n, fail_n
                try:
                    pid = self.store.import_parsed(p, on_progress=lambda m: self._log_q.put(m))
                    if pid is not None and pid < 0:
                        return
                    ok_n += 1
                    self._mark_catalog_dirty()
                    # Soft refresh — do not wipe form being edited
                    self.after(0, lambda: self._schedule_list_refresh(reload_detail=False))
                except Exception as e:
                    fail_n += 1
                    self._log_q.put(f"저장 실패: {e}")

            try:
                products, msg = walk_list_details(
                    on_progress=lambda m: self._log_q.put(m),
                    on_product=save_one,
                    cancel=self._cancel_job,
                    excluded_goods_ids=self.store.excluded_goods_ids(),
                    excluded_search_codes=(
                        self.store.excluded_search_codes()
                        | self.store.published_search_codes()
                    ),
                    known_goods_ids=(
                        self.store.collected_goods_ids()
                        | self.store.published_goods_ids()
                    ),
                    max_items=40,
                    scroll_rounds=30,
                    get_cursor=self.store.get_list_cursor,
                    on_cursor=self.store.set_list_cursor,
                )
                self._log_q.put(msg)
                if not products and ok_n == 0:
                    self.after(
                        0,
                        lambda: messagebox.showwarning(
                            "없음",
                            msg or "수집된 상품이 없습니다.\n목록 화면인지, 디버그 연결인지 확인해 주세요.",
                        ),
                    )
                    return
                self.after(0, lambda: self.refresh_list(reload_detail=False))
                self.after(
                    0,
                    lambda: messagebox.showinfo(
                        "완료",
                        f"자동수집 완료\n상품 {len(products)}건\n저장 성공 {ok_n} / 실패 {fail_n}",
                    ),
                )
            except Exception as e:
                self._log_q.put(f"오류: {e}")
                self.after(0, lambda: messagebox.showerror("오류", str(e)))
            finally:
                self.after(0, lambda: self._job_end("collect"))

        threading.Thread(target=work, daemon=True).start()

    def _on_import(self) -> None:
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
                self._log_q.put(f"{msg} (source={source})")
                products = parse_products(html, text)
                if not products:
                    self._log_q.put("상품을 찾지 못했습니다. 상세 화면이거나 목록을 스크롤한 뒤 다시 시도하세요.")
                    self.after(0, lambda: messagebox.showwarning("없음", "상품을 찾지 못했습니다."))
                    return
                self._log_q.put(f"인식된 상품 {len(products)}개")
                # 목록만 잡힌 경우(커버 위주) → 자동 상세 순회 제안은 로그로
                if len(products) > 1:
                    avg_imgs = sum(len(p.image_urls) for p in products) / max(1, len(products))
                    if avg_imgs <= 1.5:
                        self._log_q.put(
                            "목록 커버만 감지됨. 전체 갤러리는 [목록→상세 자동수집]을 사용하세요."
                        )
                ok, fail = self.store.import_many(products, on_progress=lambda m: self._log_q.put(m))
                self._mark_catalog_dirty()
                self.after(0, lambda: self.refresh_list(reload_detail=False))
                self.after(
                    0,
                    lambda: messagebox.showinfo("완료", f"가져오기 완료\n성공 {ok} / 실패 {fail}"),
                )
            except Exception as e:
                self._log_q.put(f"오류: {e}")
                self.after(0, lambda: messagebox.showerror("오류", str(e)))
            finally:
                self.after(0, lambda: self._job_end("import"))

        threading.Thread(target=work, daemon=True).start()


def main() -> None:
    app = ManagerApp()
    app.mainloop()


if __name__ == "__main__":
    main()

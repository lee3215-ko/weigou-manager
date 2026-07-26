# -*- coding: utf-8 -*-
"""Reverse-image product identify (Google Lens → Baidu fallback) → Korean product name."""
from __future__ import annotations

import queue
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

from multi_ai_parse import parse_multi_image_answers
from product_name import build_product_name, extract_ai_labeled_fields, normalize_ai_color


@dataclass
class LensResult:
    product_name: str = ""
    name_en: str = ""
    candidates: list[str] = field(default_factory=list)
    category: str = ""
    raw_texts: list[str] = field(default_factory=list)
    error: str = ""
    source: str = ""
    color: str = ""


def _ensure_playwright():
    try:
        from playwright.sync_api import sync_playwright  # type: ignore

        return sync_playwright
    except ImportError as e:
        raise RuntimeError(
            "playwright 가 필요합니다. 실행.bat 을 다시 실행하세요.\n"
            "pip install playwright && playwright install chromium"
        ) from e


def _dismiss_google_consent(page) -> None:
    for sel in (
        'button:has-text("Accept all")',
        'button:has-text("I agree")',
        'button:has-text("모두 수락")',
        'button:has-text("동의")',
        "#L2AGLb",
        'form[action*="consent"] button',
    ):
        try:
            loc = page.locator(sel)
            if loc.count() > 0:
                loc.first.click(timeout=1500)
                page.wait_for_timeout(800)
                return
        except Exception:
            continue


def _collect_page_texts(page, *, max_lines: int = 200) -> list[str]:
    texts: list[str] = []
    for sel in (
        "body",
        "a",
        "[role='heading']",
        "h1",
        "h2",
        "h3",
        "[class*='title']",
        "[class*='Title']",
        "[class*='name']",
        "[class*='Name']",
    ):
        try:
            if sel == "body":
                body = page.inner_text("body")
                texts.extend([ln.strip() for ln in body.splitlines() if ln.strip()])
            else:
                locs = page.locator(sel)
                n = min(locs.count(), 100)
                for i in range(n):
                    try:
                        t = locs.nth(i).inner_text(timeout=300).strip()
                    except Exception:
                        continue
                    if t:
                        texts.append(t)
        except Exception:
            continue

    out: list[str] = []
    seen: set[str] = set()
    skip = re.compile(
        r"^(Accept all|Sign in|로그인|이미지 검색|Google|About|Privacy|"
        r"모두 수락|관련 제품|시각적 일치|Exact matches|Visual matches)$",
        re.I,
    )
    for t in texts:
        key = re.sub(r"\s+", " ", t).strip()
        if not key or key in seen or skip.match(key) or len(key) < 2:
            continue
        seen.add(key)
        out.append(key)
        if len(out) >= max_lines:
            break
    return out


def _build_ai_prompt(*, size: str = "", hint: str = "", color: str = "") -> str:
    """Short prompt after image paste (matches manual Google AI usage)."""
    size_s = (size or "").strip()
    if size_s:
        return f"사이즈 {size_s}\n제품명, 컬러를 알려줘"
    return "제품명, 컬러를 알려줘"


def _build_multi_ai_prompt(*, size: str = "", count: int = 0) -> str:
    """Prompt when several product images are pasted together."""
    size_s = (size or "").strip()
    base = "각 제품의 제품명과 컬러를 알려줘"
    if size_s:
        return f"{base} 사이즈 {size_s}"
    if count > 1:
        return f"{base} (이미지 {count}장 순서대로)"
    return base


def _copy_image_to_clipboard(path: Path) -> bool:
    """Put image on Windows clipboard for Ctrl+V paste (faster than file upload)."""
    path = Path(path)
    if not path.exists():
        return False

    # 1) Pillow → CF_DIB via ctypes (no extra deps)
    try:
        import ctypes
        import io

        from PIL import Image

        img = Image.open(path).convert("RGB")
        buf = io.BytesIO()
        img.save(buf, "BMP")
        dib = buf.getvalue()[14:]  # strip BITMAPFILEHEADER
        buf.close()

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        CF_DIB = 8
        GMEM_MOVEABLE = 0x0002

        if not user32.OpenClipboard(None):
            raise OSError("OpenClipboard failed")
        try:
            user32.EmptyClipboard()
            hmem = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(dib))
            if not hmem:
                raise OSError("GlobalAlloc failed")
            ptr = kernel32.GlobalLock(hmem)
            ctypes.memmove(ptr, dib, len(dib))
            kernel32.GlobalUnlock(hmem)
            if not user32.SetClipboardData(CF_DIB, hmem):
                raise OSError("SetClipboardData failed")
        finally:
            user32.CloseClipboard()
        return True
    except Exception:
        pass

    # 2) PowerShell STA clipboard (fallback)
    try:
        import subprocess

        ps_path = str(path).replace("'", "''")
        ps = (
            "Add-Type -AssemblyName System.Windows.Forms; "
            "Add-Type -AssemblyName System.Drawing; "
            f"$img = [System.Drawing.Image]::FromFile('{ps_path}'); "
            "[System.Windows.Forms.Clipboard]::SetImage($img); "
            "$img.Dispose()"
        )
        r = subprocess.run(
            ["powershell", "-NoProfile", "-STA", "-Command", ps],
            capture_output=True,
            timeout=20,
        )
        return r.returncode == 0
    except Exception:
        return False


def _focus_composer(page) -> bool:
    """Focus the AI Mode text input — avoid clicking image chips (that replaces them)."""
    for sel in (
        'textarea[aria-label]',
        'textarea[name="q"]',
        'div[role="textbox"]',
        '[contenteditable="true"]',
        'textarea',
        'input[name="q"]',
        'div.jUiaTd[role="presentation"]',
    ):
        try:
            loc = page.locator(sel)
            if loc.count() == 0:
                continue
            el = loc.last
            try:
                box = el.bounding_box(timeout=1200)
            except Exception:
                box = None
            if box and box.get("width", 0) > 8 and box.get("height", 0) > 8:
                # Click lower-center of the field (chips sit above the caret)
                page.mouse.click(
                    box["x"] + box["width"] * 0.55,
                    box["y"] + box["height"] * 0.75,
                )
            else:
                el.click(timeout=1500)
            return True
        except Exception:
            continue
    return False


def _image_attached(page) -> bool:
    """Loose check used by older upload helpers."""
    for sel in (
        'img[src^="blob:"]',
        'img[src^="data:image"]',
        'button[aria-label*="Remove"]',
        'button[aria-label*="제거"]',
    ):
        try:
            if page.locator(sel).count() > 0:
                return True
        except Exception:
            continue
    return False


def _quick_paste_image(page, path: Path) -> bool:
    """Clipboard → composer Ctrl+V. No upload menus, no long waits."""
    if not _copy_image_to_clipboard(path):
        return False
    _focus_composer(page)
    page.keyboard.press("Control+v")
    page.wait_for_timeout(350)
    return True


def _paste_images_one_by_one(page, paths: list[Path]) -> int:
    """Clipboard → Ctrl+V only. No file upload, no replace/retry, no chip counting."""
    attached = 0
    for p in paths:
        if not _copy_image_to_clipboard(p):
            continue
        _focus_composer(page)
        page.wait_for_timeout(250)
        page.keyboard.press("Control+v")
        attached += 1
        # Wait so the next paste adds another chip instead of racing the UI
        page.wait_for_timeout(750)
    return attached


def _attach_images_multi(page, paths: list[Path]) -> int:
    """Attach images by paste only — never open file chooser / set_input_files."""
    paths = [Path(p) for p in paths if Path(p).exists()]
    if not paths:
        return 0
    return _paste_images_one_by_one(page, paths)


def _type_prompt_and_enter(page, prompt: str) -> None:
    """Type question after image paste. Never fill('') — that wipes the image."""
    _focus_composer(page)
    page.wait_for_timeout(80)
    # insert_text keeps the pasted image chip
    page.keyboard.insert_text(prompt)
    page.wait_for_timeout(80)
    page.keyboard.press("Enter")


def _set_all_file_inputs(page, path: Path) -> bool:
    """Force set_input_files on every file input, including hidden ones."""
    try:
        # Reveal hidden inputs so Playwright can touch them more reliably
        page.evaluate(
            """() => {
              document.querySelectorAll('input[type="file"]').forEach((el) => {
                el.style.display = 'block';
                el.style.opacity = '1';
                el.style.visibility = 'visible';
                el.removeAttribute('hidden');
                el.removeAttribute('disabled');
              });
            }"""
        )
    except Exception:
        pass
    try:
        loc = page.locator('input[type="file"]')
        n = loc.count()
        for i in range(n):
            try:
                loc.nth(i).set_input_files(str(path), timeout=4000)
                page.wait_for_timeout(700)
                if _image_attached(page):
                    return True
                # even if preview heuristic fails, accept after set
                return True
            except Exception:
                continue
    except Exception:
        pass
    return False


def _click_upload_with_chooser(page, path: Path) -> bool:
    openers = (
        'button[aria-label*="이미지 업로드"]',
        'button[aria-label*="Upload image"]',
        'button[aria-label*="Upload a file"]',
        'button[aria-label*="Upload"]',
        'button[aria-label*="첨부"]',
        'button[aria-label*="Add photos"]',
        'button[aria-label*="Add files"]',
        'button[aria-label*="사진 추가"]',
        'button[aria-label*="파일"]',
        '[aria-label*="Upload an image"]',
        '[aria-label*="이미지로 검색"]',
        '[aria-label*="Search by image"]',
        'div[role="button"][aria-label*="이미지"]',
        'div[role="button"][aria-label*="Upload"]',
        'div[aria-label*="이미지로 검색"]',
        'div[aria-label*="Search by image"]',
        'span[aria-label*="Search by image"]',
        'span[aria-label*="이미지로 검색"]',
        # AI Mode "+" / tools menus
        'button[aria-label*="도구"]',
        'button[aria-label*="Tools"]',
        'button[aria-label*="Add"]',
        'button[aria-label*="추가"]',
        'button[aria-label="+"]',
    )
    for btn in openers:
        try:
            loc = page.locator(btn)
            if loc.count() == 0:
                continue
            try:
                with page.expect_file_chooser(timeout=3500) as fc_info:
                    loc.first.click(timeout=2500)
                fc_info.value.set_files(str(path))
                page.wait_for_timeout(800)
                return True
            except Exception:
                # maybe opened a menu — click nested upload item
                loc.first.click(timeout=1500)
                page.wait_for_timeout(400)
                for nested in (
                    'div[role="menuitem"]:has-text("이미지")',
                    'div[role="menuitem"]:has-text("Upload")',
                    'div[role="menuitem"]:has-text("사진")',
                    'div[role="option"]:has-text("이미지")',
                    'span:has-text("이미지 업로드")',
                    'span:has-text("Upload image")',
                    'span:has-text("Upload a file")',
                ):
                    try:
                        nloc = page.locator(nested)
                        if nloc.count() == 0:
                            continue
                        with page.expect_file_chooser(timeout=3500) as fc_info:
                            nloc.first.click(timeout=2000)
                        fc_info.value.set_files(str(path))
                        page.wait_for_timeout(800)
                        return True
                    except Exception:
                        continue
                if _set_all_file_inputs(page, path):
                    return True
        except Exception:
            continue
    return False


def _dnd_or_paste_image(page, path: Path) -> bool:
    """Drop / paste file onto composer (works on some Google UIs)."""
    import base64
    import mimetypes

    mime = mimetypes.guess_type(str(path))[0] or "image/jpeg"
    b64 = base64.b64encode(path.read_bytes()).decode("ascii")
    name = path.name
    try:
        ok = page.evaluate(
            """([name, mime, b64]) => {
              const binary = atob(b64);
              const arr = new Uint8Array(binary.length);
              for (let i = 0; i < binary.length; i++) arr[i] = binary.charCodeAt(i);
              const file = new File([arr], name, { type: mime });
              const dt = new DataTransfer();
              dt.items.add(file);
              const targets = [
                document.querySelector('textarea'),
                document.querySelector('[role="textbox"]'),
                document.querySelector('[contenteditable="true"]'),
                document.querySelector('form'),
                document.body,
              ].filter(Boolean);
              for (const el of targets) {
                for (const type of ['dragenter', 'dragover', 'drop']) {
                  el.dispatchEvent(new DragEvent(type, {
                    bubbles: true, cancelable: true, dataTransfer: dt
                  }));
                }
                try {
                  el.focus();
                  el.dispatchEvent(new ClipboardEvent('paste', {
                    bubbles: true, cancelable: true, clipboardData: dt
                  }));
                } catch (e) {}
              }
              return targets.length > 0;
            }""",
            [name, mime, b64],
        )
        page.wait_for_timeout(900)
        return bool(ok) and _image_attached(page)
    except Exception:
        return False


def _try_upload_image(page, path: Path) -> bool:
    path = Path(path)
    if not path.exists():
        return False
    # 1) Direct file inputs (hidden OK)
    if _set_all_file_inputs(page, path):
        return True
    # 2) Click upload / camera / + then file chooser
    if _click_upload_with_chooser(page, path):
        return True
    # 3) After menu clicks, file inputs may appear
    if _set_all_file_inputs(page, path):
        return True
    # 4) Drag-drop / paste onto composer
    if _dnd_or_paste_image(page, path):
        return True
    return False


def _upload_via_google_lens_home(page, path: Path) -> bool:
    """Most reliable: Google Images camera → file input."""
    try:
        page.goto("https://www.google.com/imghp?hl=ko", wait_until="domcontentloaded")
        _dismiss_google_consent(page)
        page.wait_for_timeout(900)
        for cam in (
            'div[aria-label*="이미지로 검색"]',
            'div[aria-label*="Search by image"]',
            'span[aria-label*="이미지로 검색"]',
            'span[aria-label*="Search by image"]',
            '[aria-label*="Search by image"]',
            '[aria-label*="이미지로 검색"]',
        ):
            try:
                loc = page.locator(cam)
                if loc.count() > 0:
                    loc.first.click(timeout=2000)
                    page.wait_for_timeout(500)
                    break
            except Exception:
                continue
        if _set_all_file_inputs(page, path):
            return True
        if _click_upload_with_chooser(page, path):
            return True
        # Lens home fallback
        page.goto("https://lens.google.com/?hl=ko", wait_until="domcontentloaded")
        _dismiss_google_consent(page)
        page.wait_for_timeout(900)
        if _set_all_file_inputs(page, path):
            return True
        return _click_upload_with_chooser(page, path)
    except Exception:
        return False


def _fill_prompt_and_submit(page, prompt: str) -> bool:
    """Legacy helper — prefer _type_prompt_and_enter (does not wipe pasted images)."""
    try:
        _type_prompt_and_enter(page, prompt)
        return True
    except Exception:
        return False


def _latest_ai_fingerprint(page) -> str:
    try:
        aim = page.locator('div[data-subtree="aimc"], [data-md], .markdown')
        n = aim.count()
        if n <= 0:
            return ""
        return (aim.last.inner_text(timeout=400) or "").strip()[:240]
    except Exception:
        return ""


def _ai_still_loading(page) -> bool:
    """True while Google AI shows skeleton / streaming placeholders."""
    try:
        return bool(
            page.evaluate(
                """() => {
                  // Skeleton bars while answering (user's loading DOM)
                  const skeletons = document.querySelectorAll('.wrqyud, .qaHYKd');
                  for (const el of skeletons) {
                    const r = el.getBoundingClientRect();
                    if (r.width > 8 && r.height > 2 && r.bottom > 0) {
                      if (r.top < window.innerHeight && r.top > 40) return true;
                    }
                  }
                  // Incomplete answer root
                  const roots = document.querySelectorAll(
                    '[data-complete="false"], [aria-busy="true"]'
                  );
                  for (const el of roots) {
                    const r = el.getBoundingClientRect();
                    if (r.width > 20 && r.height > 20) return true;
                  }
                  return false;
                }"""
            )
        )
    except Exception:
        return False


def _latest_answer_text(page) -> str:
    try:
        # Prefer the last completed markdown / AI block
        aim = page.locator('div[data-subtree="aimc"], [data-md], .markdown')
        n = aim.count()
        if n > 0:
            return (aim.last.inner_text(timeout=500) or "").strip()
    except Exception:
        pass
    return ""


def _answer_looks_ready(text: str, prev_fingerprint: str = "") -> bool:
    t = (text or "").strip()
    if len(t) < 50:
        return False
    if t[:240] == (prev_fingerprint or "")[:240]:
        return False
    # Reject skeleton-only / tiny placeholders
    if t.count("\n") < 1 and "제품명" not in t and "컬러" not in t:
        # allow longer prose without labels
        if len(t) < 80:
            return False
    return True


def _collect_ai_texts(page, *, prev_fingerprint: str = "") -> list[str]:
    """Wait until the NEW answer finishes loading — never reuse the previous one."""
    page.wait_for_timeout(600)
    stable = ""
    stable_hits = 0
    latest = ""

    # Up to ~45s for a full AI reply
    for _ in range(60):
        loading = _ai_still_loading(page)
        txt = _latest_answer_text(page)

        if loading:
            stable = ""
            stable_hits = 0
            page.wait_for_timeout(500)
            continue

        if not _answer_looks_ready(txt, prev_fingerprint):
            page.wait_for_timeout(500)
            continue

        # Require text to stay unchanged for 2 polls (streaming finished)
        if txt == stable:
            stable_hits += 1
        else:
            stable = txt
            stable_hits = 1

        if stable_hits >= 2:
            latest = txt
            break
        page.wait_for_timeout(450)

    if not latest:
        # Last chance: only accept if clearly new and not loading
        if not _ai_still_loading(page):
            txt = _latest_answer_text(page)
            if _answer_looks_ready(txt, prev_fingerprint):
                latest = txt

    if not latest:
        # Do NOT fall back to old page text / previous answer
        return []

    lines = [ln.strip() for ln in latest.splitlines() if ln.strip()]
    return lines or [latest]


_AI_MODE_URL = "https://www.google.com/search?udm=50&hl=ko&gl=kr&aep=42"


class _AiBrowserSession:
    """Keep one Chromium window alive; reopen only if the user closed it.

    Playwright sync API is thread-bound, so all browser ops run on this worker.
    """

    def __init__(self) -> None:
        self._q: queue.Queue = queue.Queue()
        self._thread = threading.Thread(
            target=self._loop, name="google-ai-browser", daemon=True
        )
        self._thread.start()
        self._pw = None
        self._browser = None
        self._context = None
        self._page = None
        self._headless: bool | None = None
        self._on_ai_mode = False  # already on AI Mode page (no reload)

    def search(
        self,
        path: Path | list[Path],
        prompt: str,
        *,
        headless: bool = False,
    ) -> list[str]:
        paths = [Path(path)] if not isinstance(path, list) else [Path(p) for p in path]
        box: dict = {}
        done = threading.Event()
        self._q.put(("search", paths, prompt, headless, box, done))
        if not done.wait(timeout=150):
            return []
        return list(box.get("texts") or [])

    def close(self) -> None:
        done = threading.Event()
        self._q.put(("close", done))
        done.wait(timeout=15)

    def _loop(self) -> None:
        while True:
            item = self._q.get()
            kind = item[0]
            if kind == "close":
                self._shutdown()
                item[1].set()
                continue
            if kind != "search":
                continue
            _, paths, prompt, headless, box, done = item
            try:
                box["texts"] = self._do_search(paths, prompt, headless)
            except Exception as e:  # noqa: BLE001
                box["texts"] = []
                box["error"] = str(e)
            finally:
                done.set()

    def _alive(self) -> bool:
        try:
            if self._browser is None or self._page is None:
                return False
            if not self._browser.is_connected():
                return False
            if self._page.is_closed():
                return False
            return True
        except Exception:
            return False

    def _shutdown(self) -> None:
        try:
            if self._browser is not None:
                self._browser.close()
        except Exception:
            pass
        try:
            if self._pw is not None:
                self._pw.stop()
        except Exception:
            pass
        self._browser = None
        self._context = None
        self._page = None
        self._pw = None
        self._headless = None
        self._on_ai_mode = False

    def _ensure(self, headless: bool) -> bool:
        """Return True if a brand-new browser/page was created."""
        if self._alive() and self._headless == headless:
            return False
        self._shutdown()
        sync_playwright = _ensure_playwright()
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(
            headless=headless,
            args=["--disable-blink-features=AutomationControlled"],
        )
        self._context = self._browser.new_context(
            locale="ko-KR",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
            permissions=["clipboard-read", "clipboard-write"],
        )
        self._page = self._context.new_page()
        self._page.set_default_timeout(60000)
        self._headless = headless
        self._on_ai_mode = False
        return True

    def _open_ai_mode(self, page) -> None:
        page.goto(_AI_MODE_URL, wait_until="domcontentloaded")
        _dismiss_google_consent(page)
        page.wait_for_timeout(500)
        self._on_ai_mode = True

    def _do_search(self, paths: list[Path], prompt: str, headless: bool) -> list[str]:
        """Paste image(s) with Ctrl+V → type prompt → Enter. No replace / re-upload."""
        paths = [p for p in paths if p.exists()]
        if not paths:
            return []
        for attempt in range(2):
            try:
                self._ensure(headless)
                page = self._page
                assert page is not None

                continuing = bool(self._on_ai_mode and self._alive())
                if not continuing:
                    self._open_ai_mode(page)
                else:
                    try:
                        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    except Exception:
                        pass

                prev_fp = _latest_ai_fingerprint(page)

                # 다중/단건 모두: 클립보드 붙여넣기만 (창 새로고침·파일교체 재시도 없음)
                pasted = _attach_images_multi(page, paths)
                if pasted == 0:
                    if attempt == 0 and not continuing:
                        self._shutdown()
                        continue
                    return []

                page.wait_for_timeout(300)
                _type_prompt_and_enter(page, prompt)
                texts = _collect_ai_texts(page, prev_fingerprint=prev_fp)
                self._on_ai_mode = True
                return texts
            except Exception:
                self._shutdown()
                if attempt == 0:
                    continue
                raise
        return []


# Headed / headless sessions are separate so batch search won't close the visible window
_AI_SESSION_HEADED = _AiBrowserSession()
_AI_SESSION_HEADLESS = _AiBrowserSession()


def _google_ai_mode_search(
    path: Path | list[Path],
    *,
    size: str = "",
    hint: str = "",
    color: str = "",
    headless: bool = False,
    multi: bool = False,
) -> list[str]:
    """Paste product image(s) into Google AI Mode. Reuses the same browser window."""
    paths = [Path(path)] if not isinstance(path, list) else [Path(p) for p in path]
    paths = [p for p in paths if p.exists()]
    if not paths:
        return []
    if multi or len(paths) > 1:
        prompt = _build_multi_ai_prompt(size=size, count=len(paths))
    else:
        prompt = _build_ai_prompt(size=size, hint=hint, color=color)
    session = _AI_SESSION_HEADLESS if headless else _AI_SESSION_HEADED
    return session.search(paths, prompt, headless=headless)


def _google_search_url(image_url: str) -> list[str]:
    sync_playwright = _ensure_playwright()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(
            locale="ko-KR",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
        )
        page.set_default_timeout(45000)
        url = (
            "https://lens.google.com/uploadbyurl"
            f"?url={quote(image_url, safe='')}&hl=ko&ep=gisbubu"
        )
        page.goto(url, wait_until="domcontentloaded")
        _dismiss_google_consent(page)
        page.wait_for_timeout(4500)
        texts = _collect_page_texts(page)
        browser.close()
    return texts


def _google_search_file(path: Path) -> list[str]:
    sync_playwright = _ensure_playwright()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(
            locale="ko-KR",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
        )
        page.set_default_timeout(45000)
        # Google Images camera upload → Lens results
        page.goto("https://www.google.com/imghp?hl=ko", wait_until="domcontentloaded")
        _dismiss_google_consent(page)
        page.wait_for_timeout(800)

        uploaded = False
        # Open camera / Lens upload UI
        for cam in (
            'div[aria-label*="이미지로 검색"]',
            'div[aria-label*="Search by image"]',
            'span[aria-label*="이미지로 검색"]',
            'span[aria-label*="Search by image"]',
            '[aria-label*="Search by image"]',
            '[aria-label*="이미지로 검색"]',
        ):
            try:
                loc = page.locator(cam)
                if loc.count() > 0:
                    loc.first.click(timeout=2000)
                    page.wait_for_timeout(600)
                    break
            except Exception:
                continue

        for sel in ('input[type="file"]', 'input[accept*="image"]'):
            loc = page.locator(sel)
            if loc.count() > 0:
                loc.first.set_input_files(str(path))
                uploaded = True
                break

        if not uploaded:
            # Fallback: Lens home page file input
            page.goto("https://lens.google.com/?hl=ko", wait_until="domcontentloaded")
            _dismiss_google_consent(page)
            page.wait_for_timeout(1000)
            for sel in ('input[type="file"]', 'input[accept*="image"]'):
                loc = page.locator(sel)
                if loc.count() > 0:
                    loc.first.set_input_files(str(path))
                    uploaded = True
                    break

        if not uploaded:
            browser.close()
            return []

        try:
            page.wait_for_url(re.compile(r"lens\.google\.com|google\.com/search"), timeout=25000)
        except Exception:
            pass
        page.wait_for_timeout(4500)
        texts = _collect_page_texts(page)
        browser.close()
    return texts


def _baidu_search_file(path: Path) -> list[str]:
    sync_playwright = _ensure_playwright()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(
            locale="zh-CN",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
        )
        page.set_default_timeout(45000)
        page.goto(
            "https://graph.baidu.com/pcpage/index?category=pcIndex",
            wait_until="domcontentloaded",
        )
        page.wait_for_timeout(1200)
        uploaded = False
        for sel in ('input[type="file"]', 'input[accept*="image"]'):
            loc = page.locator(sel)
            if loc.count() > 0:
                loc.first.set_input_files(str(path))
                uploaded = True
                break
        if not uploaded:
            browser.close()
            return []
        try:
            page.wait_for_url(re.compile(r"graph\.baidu\.com/s"), timeout=25000)
        except Exception:
            pass
        page.wait_for_timeout(4000)
        try:
            tip = page.get_by_text(re.compile(r"知道啦|关闭"))
            if tip.count() > 0:
                tip.first.click(timeout=1000)
        except Exception:
            pass
        texts = _collect_page_texts(page)
        browser.close()
    return texts


def _baidu_search_url(image_url: str) -> list[str]:
    sync_playwright = _ensure_playwright()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(locale="zh-CN")
        page.set_default_timeout(45000)
        page.goto(
            "https://graph.baidu.com/details?isfrom=pcsearch"
            f"&image={quote(image_url, safe='')}",
            wait_until="domcontentloaded",
        )
        page.wait_for_timeout(4000)
        if "pcpage/index" in page.url or page.locator('input[type="file"]').count() > 0:
            browser.close()
            return []
        body = page.inner_text("body")
        browser.close()
    return [ln.strip() for ln in body.splitlines() if ln.strip()]


def _download_temp(image_url: str) -> Path | None:
    import tempfile
    import urllib.request

    suffix = Path(image_url.split("?")[0]).suffix or ".jpg"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp_path = Path(tmp.name)
    try:
        req = urllib.request.Request(
            image_url,
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.szwego.com/"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            tmp_path.write_bytes(resp.read())
        return tmp_path
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass
        return None


def search_image(
    *,
    image_path: str | Path | None = None,
    image_url: str | None = None,
    hint: str = "",
    size: str = "",
    color: str = "",
    headless: bool = False,
) -> LensResult:
    path = Path(image_path) if image_path else None
    lines: list[str] = []
    source = ""
    tmp_path: Path | None = None

    try:
        if path and path.exists():
            # 1) Google AI Mode: open site, attach image + size, Enter
            lines = _google_ai_mode_search(
                path,
                size=size,
                hint=hint,
                color=color,
                headless=headless,
            )
            source = "google-ai"
            if not lines:
                lines = _google_search_file(path)
                source = "google-file"
            if not lines:
                lines = _baidu_search_file(path)
                source = "baidu-file"
        elif image_url:
            tmp_path = _download_temp(image_url) if image_url.startswith("http") else None
            if tmp_path:
                lines = _google_ai_mode_search(
                    tmp_path,
                    size=size,
                    hint=hint,
                    color=color,
                    headless=headless,
                )
                source = "google-ai-url"
            if not lines:
                lines = _google_search_url(image_url)
                source = "google-url"
            if not lines and image_url.startswith("http"):
                if not tmp_path:
                    tmp_path = _download_temp(image_url)
                if tmp_path:
                    lines = _google_search_file(tmp_path)
                    source = "google-url-file"
            if not lines and image_url.startswith("http"):
                lines = _baidu_search_url(image_url)
                source = "baidu-url"
            if not lines and image_url.startswith("http"):
                if not tmp_path:
                    tmp_path = _download_temp(image_url)
                if tmp_path:
                    lines = _baidu_search_file(tmp_path)
                    source = "baidu-url-file"
        else:
            return LensResult(error="검색할 이미지가 없습니다.")
    except Exception as e:
        msg = str(e)
        if "Executable doesn't exist" in msg or "BrowserType.launch" in msg:
            return LensResult(
                error="Chromium 미설치: `playwright install chromium` 후 다시 시도하세요."
            )
        return LensResult(error=f"이미지 검색 실패: {e}")
    finally:
        if tmp_path:
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception:
                pass

    if not lines:
        return LensResult(error="검색 결과를 받지 못했습니다.", source=source)

    named = build_product_name(lines, hint=hint)
    # AI 「제품명:」「컬러:」 라벨만 사용 — 문장/이미지 추정으로 덮지 않음
    _ai_name, ai_color = extract_ai_labeled_fields(lines)
    found_color = normalize_ai_color(ai_color) if ai_color else ""
    if not found_color:
        found_color = (named.color or "").strip()
        if found_color and re.search(r"문의|제품은|입니다|보입니다", found_color):
            found_color = ""
    if not found_color and color:
        found_color = normalize_ai_color(color)

    if not named.name:
        return LensResult(
            error="제품명을 찾지 못했습니다.",
            candidates=named.candidates,
            raw_texts=lines[:40],
            source=source,
            color=found_color,
        )

    return LensResult(
        product_name=named.name,
        name_en=named.name_en,
        candidates=named.candidates,
        category=named.category,
        raw_texts=lines[:40],
        source=source,
        color=found_color,
    )


def search_product_images(
    image_paths: list[str],
    image_urls: list[str] | None = None,
    hint: str = "",
    size: str = "",
    color: str = "",
    headless: bool = False,
) -> LensResult:
    paths = [p for p in image_paths if Path(p).exists()]
    urls = [u for u in (image_urls or []) if u.startswith("http")]

    # Prefer cover / first image with Google AI Mode (image + size)
    last = LensResult(error="검색 가능한 이미지가 없습니다.")
    for pth in paths[:1]:
        r = search_image(
            image_path=pth,
            hint=hint,
            size=size,
            color=color,
            headless=headless,
        )
        if r.product_name:
            if len(r.product_name.split()) >= 3:
                return r
            last = r
        elif r.error and "Chromium" in r.error:
            return r
        elif not last.product_name:
            last = r
    if last.product_name:
        return last
    if urls:
        return search_image(
            image_url=urls[0],
            hint=hint,
            size=size,
            color=color,
            headless=headless,
        )
    return last


def search_products_multi(
    jobs: list[dict],
    *,
    headless: bool = False,
) -> list[LensResult]:
    """Paste many product images at once, parse per-image 제품명/컬러.

    Each job: { "path": str, "size": str, "hint": str }
    Returns one LensResult per job (same order).
    """
    prepared: list[tuple[int, Path, str, str]] = []
    for i, job in enumerate(jobs):
        p = Path(job.get("path") or "")
        if p.exists():
            prepared.append(
                (i, p, (job.get("size") or "").strip(), (job.get("hint") or "").strip())
            )

    out: list[LensResult] = [
        LensResult(error="첨부할 이미지가 없습니다.") for _ in jobs
    ]
    if not prepared:
        return out

    size_vals = [s for _i, _p, s, _h in prepared if s]
    size_prompt = ""
    if size_vals:
        uniq = list(dict.fromkeys(size_vals))
        size_prompt = uniq[0] if len(uniq) == 1 else ", ".join(uniq[:3])

    try:
        lines = _google_ai_mode_search(
            [p for _i, p, _s, _h in prepared],
            size=size_prompt,
            headless=headless,
            multi=True,
        )
    except Exception as e:
        msg = str(e)
        if "Executable doesn't exist" in msg or "BrowserType.launch" in msg:
            err = LensResult(
                error="Chromium 미설치: `playwright install chromium` 후 다시 시도하세요."
            )
        else:
            err = LensResult(error=f"이미지 검색 실패: {e}")
        return [err for _ in jobs]

    if not lines:
        err = LensResult(error="검색 결과를 받지 못했습니다.", source="google-ai")
        for job_i, _p, _s, _h in prepared:
            out[job_i] = err
        return out

    parsed = parse_multi_image_answers(lines, len(prepared))
    for j, (job_i, _path, _size, hint) in enumerate(prepared):
        item = (
            parsed[j]
            if j < len(parsed)
            else {"name": "", "name_en": "", "color": "", "raw": ""}
        )
        name = (item.get("name") or "").strip()
        color = (item.get("color") or "").strip()
        name_en = (item.get("name_en") or "").strip()
        chunk_lines = [ln for ln in (item.get("raw") or "").splitlines() if ln.strip()]
        if name and not name_en:
            named = build_product_name(chunk_lines or [name], hint=hint)
            name_en = named.name_en
            if not color:
                color = named.color
        if not name:
            named = build_product_name(lines, hint=hint)
            name = named.name
            name_en = name_en or named.name_en
            color = color or named.color
        if not name:
            out[job_i] = LensResult(
                error="제품명을 찾지 못했습니다.",
                raw_texts=lines[:40],
                source="google-ai-multi",
                color=color,
            )
            continue
        cats = build_product_name([name], hint=hint)
        out[job_i] = LensResult(
            product_name=name,
            name_en=name_en,
            category=cats.category,
            color=color,
            raw_texts=chunk_lines or lines[:20],
            source="google-ai-multi",
            candidates=[name],
        )
    return out
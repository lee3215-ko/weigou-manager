# -*- coding: utf-8 -*-
"""Reverse-image product identify (Google Lens → Baidu fallback) → Korean product name."""
from __future__ import annotations

import queue
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

from multi_ai_parse import parse_multi_image_answers
from product_name import build_product_name, extract_ai_labeled_fields, normalize_ai_color


class AiGenerationFailed(Exception):
    """Google AI Mode returned the 'cannot generate answer' error UI."""

    def __init__(self, message: str = "") -> None:
        self.message = (
            (message or "").strip()
            or "문제가 발생하여 AI 대답을 생성할 수 없습니다."
        )
        super().__init__(self.message)


_AI_FAIL_RE = re.compile(
    r"문제가\s*발생하여\s*AI\s*대답을\s*생성할\s*수\s*없습니다"
    r"|AI\s*대답을\s*생성할\s*수\s*없습니다"
    r"|Unable to generate (an )?AI (response|answer)"
    r"|Something went wrong.*(AI|response|answer)"
    r"|Couldn'?t generate.*(response|answer)",
    re.I,
)


def _is_ai_fail_text(text: str) -> bool:
    return bool(_AI_FAIL_RE.search(text or ""))


def _ai_generation_failed(page) -> str:
    """Return fail message if Google AI showed the generation-error block."""
    try:
        found = page.evaluate(
            """() => {
              const nodes = document.querySelectorAll(
                '.Y3BBE, [data-complete="true"], [data-sfc-root], div[role="status"], body'
              );
              for (const el of nodes) {
                const t = (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
                if (!t) continue;
                if (/문제가\\s*발생하여\\s*AI\\s*대답을\\s*생성할\\s*수\\s*없습니다/.test(t)
                    || /AI\\s*대답을\\s*생성할\\s*수\\s*없습니다/.test(t)) {
                  const m = t.match(/문제가\\s*발생하여\\s*AI\\s*대답을\\s*생성할\\s*수\\s*없습니다[^\\n]*/);
                  return (m && m[0]) || '문제가 발생하여 AI 대답을 생성할 수 없습니다.';
                }
              }
              const body = (document.body && document.body.innerText) || '';
              if (/문제가\\s*발생하여\\s*AI\\s*대답을\\s*생성할\\s*수\\s*없습니다/.test(body)) {
                return '문제가 발생하여 AI 대답을 생성할 수 없습니다.';
              }
              return '';
            }"""
        )
        return str(found or "").strip()
    except Exception:
        return ""


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


def _build_ai_prompt(
    *,
    size: str = "",
    hint: str = "",
    color: str = "",
    brand: str = "",
    clothing: bool = False,
) -> str:
    """Short prompt after image paste (matches manual Google AI usage)."""
    brand_s = (brand or "").strip()
    size_s = (size or "").strip()
    lines: list[str] = []
    if brand_s and size_s:
        lines.append(f"{brand_s} 제품인데 사이즈 {size_s}")
    elif brand_s:
        lines.append(f"{brand_s} 제품인데")
    elif size_s:
        lines.append(f"사이즈 {size_s}")
    if clothing:
        lines.append("옷종류와 컬러를 알려줘")
        lines.append("예) 나시,니트,반팔티,긴팔티,가디건")
    else:
        lines.append("정확한 명칭과 공식 컬러명을 알려줘")
    return "\n".join(lines)


def _build_multi_ai_prompt(
    *, size: str = "", count: int = 0, brand: str = "", clothing: bool = False
) -> str:
    """Prompt when several product images are pasted together."""
    brand_s = (brand or "").strip()
    size_s = (size or "").strip()
    head = ""
    if brand_s and size_s:
        head = f"{brand_s} 제품인데 사이즈 {size_s}. "
    elif brand_s:
        head = f"{brand_s} 제품인데. "
    elif size_s:
        head = f"사이즈 {size_s}. "
    if clothing:
        base = (
            f"{head}각 제품의 옷종류와 컬러를 알려줘 "
            f"(예: 나시,니트,반팔티,긴팔티,가디건)"
        ).strip()
    else:
        base = f"{head}각 제품의 정확한 명칭과 공식 컬러명을 알려줘".strip()
    if count > 1:
        return f"{base} (이미지 {count}장 순서대로)"
    return base


def _copy_image_to_clipboard(path: Path) -> bool:
    """Put one image on Windows clipboard (bitmap) for Ctrl+V."""
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


def _copy_files_to_clipboard(paths: list[Path]) -> bool:
    """Copy image FILES as a file-drop list (CF_HDROP) — Ctrl+V attaches all at once."""
    files = [Path(p) for p in paths if Path(p).exists()]
    if not files:
        return False
    if len(files) == 1:
        return _copy_image_to_clipboard(files[0])
    try:
        import subprocess

        # PowerShell StringCollection → Clipboard.SetFileDropList
        ps_files = ", ".join(
            "'" + str(p.resolve()).replace("'", "''") + "'" for p in files
        )
        ps = (
            "Add-Type -AssemblyName System.Windows.Forms; "
            "$sc = New-Object System.Collections.Specialized.StringCollection; "
            f"@({ps_files}) | ForEach-Object {{ [void]$sc.Add($_) }}; "
            "[System.Windows.Forms.Clipboard]::SetFileDropList($sc)"
        )
        r = subprocess.run(
            ["powershell", "-NoProfile", "-STA", "-Command", ps],
            capture_output=True,
            timeout=30,
        )
        return r.returncode == 0
    except Exception:
        return False


def _focus_composer(page) -> bool:
    """JS focus only — never mouse-click (+ 버튼/썸네일 클릭 방지)."""
    try:
        return bool(
            page.evaluate(
                """() => {
                  const cands = [
                    ...document.querySelectorAll('textarea[aria-label], textarea[name="q"], textarea'),
                    ...document.querySelectorAll('[role="textbox"], [contenteditable="true"]'),
                  ];
                  const el = cands.find((n) => {
                    const r = n.getBoundingClientRect();
                    return r.width > 40 && r.height > 12;
                  });
                  if (!el) return false;
                  el.focus({ preventScroll: true });
                  try {
                    if (typeof el.setSelectionRange === 'function' && 'value' in el) {
                      const n = (el.value || '').length;
                      el.setSelectionRange(n, n);
                    }
                  } catch (e) {}
                  return true;
                }"""
            )
        )
    except Exception:
        return False


def _escape_dialogs(page) -> None:
    """Dismiss replace / multi-file error with keyboard only."""
    try:
        page.keyboard.press("Escape")
        page.wait_for_timeout(100)
    except Exception:
        pass


def _multi_file_search_blocked(page) -> bool:
    """Google shows: 여러 파일을 사용하여 검색할 수 없습니다."""
    try:
        loc = page.locator("text=여러 파일을 사용하여 검색할 수 없습니다")
        if loc.count() == 0:
            return False
        return bool(loc.first.is_visible(timeout=400))
    except Exception:
        return False


def _dismiss_multi_file_error(page) -> bool:
    if not _multi_file_search_blocked(page):
        return False
    # 닫기 button or Escape — do not click +
    try:
        btn = page.locator('button:has-text("닫기")')
        if btn.count() > 0 and btn.first.is_visible(timeout=400):
            btn.first.click(timeout=1200)
            page.wait_for_timeout(200)
            return True
    except Exception:
        pass
    _escape_dialogs(page)
    return True


def _composer_blob_count(page) -> int:
    try:
        return int(
            page.evaluate(
                """() => {
                  const h = window.innerHeight || 800;
                  let n = 0;
                  document.querySelectorAll('img[src^="blob:"], img[src^="data:image"]').forEach((img) => {
                    const r = img.getBoundingClientRect();
                    if (r.width >= 20 && r.height >= 20 && r.top >= h * 0.35) n += 1;
                  });
                  return n;
                }"""
            )
        )
    except Exception:
        return 0


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


def _paste_images_as_image_files_dom(page, paths: list[Path]) -> int:
    """Inject image/* File paste into composer (not CF_HDROP 'files' — that triggers Google block)."""
    import base64
    import mimetypes

    payload = []
    for path in paths:
        path = Path(path)
        if not path.exists():
            continue
        mime = mimetypes.guess_type(str(path))[0] or "image/jpeg"
        if not str(mime).startswith("image/"):
            mime = "image/jpeg"
        # Keep names looking like images
        name = path.name
        if not re.search(r"\.(jpe?g|png|webp|gif|bmp)$", name, re.I):
            name = f"{path.stem}.jpg"
        b64 = base64.b64encode(path.read_bytes()).decode("ascii")
        payload.append([name, mime, b64])
    if not payload:
        return 0

    before = _composer_blob_count(page)
    try:
        n = int(
            page.evaluate(
                """(files) => {
                  const dt = new DataTransfer();
                  for (const [name, mime, b64] of files) {
                    const binary = atob(b64);
                    const arr = new Uint8Array(binary.length);
                    for (let i = 0; i < binary.length; i++) arr[i] = binary.charCodeAt(i);
                    dt.items.add(new File([arr], name, { type: mime }));
                  }
                  const cands = [
                    ...document.querySelectorAll('[role="textbox"]'),
                    ...document.querySelectorAll('textarea'),
                    ...document.querySelectorAll('[contenteditable="true"]'),
                  ];
                  const el = cands.find((n) => {
                    const r = n.getBoundingClientRect();
                    return r.width > 40 && r.height > 12;
                  });
                  if (!el) return 0;
                  el.focus();
                  // Prefer drop (often accepted as image attach) then paste
                  for (const type of ['dragenter', 'dragover', 'drop']) {
                    el.dispatchEvent(new DragEvent(type, {
                      bubbles: true, cancelable: true, dataTransfer: dt
                    }));
                  }
                  try {
                    el.dispatchEvent(new ClipboardEvent('paste', {
                      bubbles: true, cancelable: true, clipboardData: dt
                    }));
                  } catch (e) {}
                  return dt.files.length;
                }""",
                payload,
            )
        )
    except Exception:
        n = 0
    for _ in range(15):
        page.wait_for_timeout(200)
        if _multi_file_search_blocked(page):
            _dismiss_multi_file_error(page)
            return 0
        after = _composer_blob_count(page)
        if after >= before + len(payload) or after >= len(payload):
            return len(payload)
        if after > before:
            return after - before
    after = _composer_blob_count(page)
    gained = max(0, after - before)
    return gained if gained else (n if _image_attached(page) else 0)


def _quick_paste_image(page, path: Path) -> bool:
    """저장된 이미지 → 클립보드(그림) → Ctrl+V."""
    if not _copy_image_to_clipboard(path):
        return False
    _focus_composer(page)
    page.keyboard.press("Control+v")
    page.wait_for_timeout(400)
    _escape_dialogs(page)
    return True


def _paste_all_files_once(page, paths: list[Path]) -> int:
    """Explorer-style file list + Ctrl+V (works in real Chrome; may fail in Chromium)."""
    if not _copy_files_to_clipboard(paths):
        return 0
    before = _composer_blob_count(page)
    _focus_composer(page)
    page.wait_for_timeout(150)
    page.keyboard.press("Control+v")
    for _ in range(20):
        page.wait_for_timeout(200)
        if _multi_file_search_blocked(page):
            _dismiss_multi_file_error(page)
            return 0
        after = _composer_blob_count(page)
        if after >= before + len(paths) or after >= len(paths):
            return len(paths)
    after = _composer_blob_count(page)
    gained = max(0, after - before)
    return gained


def _paste_images_one_by_one(page, paths: list[Path]) -> int:
    """Each image as bitmap Ctrl+V. Escape between pastes (no + button)."""
    _focus_composer(page)
    attached = 0
    for p in paths:
        before = _composer_blob_count(page)
        if not _copy_image_to_clipboard(p):
            continue
        _escape_dialogs(page)
        _focus_composer(page)
        page.wait_for_timeout(150)
        page.keyboard.press("Control+v")
        page.wait_for_timeout(600)
        if _multi_file_search_blocked(page):
            _dismiss_multi_file_error(page)
        _escape_dialogs(page)
        _focus_composer(page)
        after = _composer_blob_count(page)
        if after > before or (attached == 0 and _image_attached(page)):
            attached = max(attached + 1, after - before if after > before else attached + 1)
        page.wait_for_timeout(450)
    return attached


def _attach_images_multi(page, paths: list[Path]) -> int:
    """Paste images like a human — prefer image paste, avoid '여러 파일 검색 불가'."""
    paths = [Path(p) for p in paths if Path(p).exists()]
    if not paths:
        return 0
    if len(paths) == 1:
        return 1 if _quick_paste_image(page, paths[0]) else 0

    # 1) DOM image/* paste/drop (same as pasting pictures, not generic files)
    n = _paste_images_as_image_files_dom(page, paths)
    if n >= len(paths):
        return n

    # 2) Real clipboard file-list (user's Explorer multi-copy) — OK on real Chrome
    n = _paste_all_files_once(page, paths)
    if n >= len(paths):
        return n

    # 3) One image bitmap at a time
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
                  // Skeleton bars while answering
                  const skeletons = document.querySelectorAll(
                    '.wrqyud, .qaHYKd, [class*="skeleton"], [class*="Skeleton"]'
                  );
                  for (const el of skeletons) {
                    const r = el.getBoundingClientRect();
                    if (r.width > 8 && r.height > 2 && r.bottom > 0) {
                      if (r.top < window.innerHeight && r.top > 40) return true;
                    }
                  }
                  // Incomplete / busy answer root
                  const roots = document.querySelectorAll(
                    '[data-complete="false"], [aria-busy="true"]'
                  );
                  for (const el of roots) {
                    const r = el.getBoundingClientRect();
                    if (r.width > 20 && r.height > 20) return true;
                  }
                  // Stop / 중지 button while generating
                  const buttons = document.querySelectorAll('button, [role="button"]');
                  for (const b of buttons) {
                    const label = (
                      (b.getAttribute('aria-label') || '') + ' ' + (b.textContent || '')
                    ).trim();
                    if (/^(중지|Stop|Stop generating)$/i.test(label) ||
                        /응답 중지|생성 중지|Stop generating/i.test(label)) {
                      const r = b.getBoundingClientRect();
                      if (r.width > 8 && r.height > 8 && r.bottom > 0 &&
                          r.top < window.innerHeight) return true;
                    }
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
        aim = page.locator(
            'div[data-subtree="aimc"], [data-md], .markdown, .Y3BBE'
        )
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
    if _is_ai_fail_text(t):
        return False
    if t[:240] == (prev_fingerprint or "")[:240]:
        return False
    # Reject skeleton-only / tiny placeholders
    if t.count("\n") < 1 and "제품명" not in t and "컬러" not in t:
        # allow longer prose without labels
        if len(t) < 80:
            return False
    return True


def _collect_ai_texts(
    page,
    *,
    prev_fingerprint: str = "",
    timeout_sec: float = 60.0,
) -> list[str]:
    """Wait until the NEW answer finishes — empty only after timeout (no early skip)."""
    page.wait_for_timeout(600)
    stable = ""
    stable_hits = 0
    latest = ""
    deadline = time.monotonic() + max(15.0, float(timeout_sec))

    while time.monotonic() < deadline:
        fail = _ai_generation_failed(page)
        if fail:
            raise AiGenerationFailed(fail)

        loading = _ai_still_loading(page)
        txt = _latest_answer_text(page)
        if _is_ai_fail_text(txt):
            raise AiGenerationFailed(txt)

        if loading:
            # Still generating — never treat as done
            stable = ""
            stable_hits = 0
            page.wait_for_timeout(500)
            continue

        if not _answer_looks_ready(txt, prev_fingerprint):
            page.wait_for_timeout(500)
            continue

        # Require text unchanged across polls (streaming finished)
        if txt == stable:
            stable_hits += 1
        else:
            stable = txt
            stable_hits = 1

        if stable_hits >= 3:
            latest = txt
            break
        page.wait_for_timeout(400)

    # Final fail check (error UI often has data-complete=true → not "loading")
    fail = _ai_generation_failed(page)
    if fail:
        raise AiGenerationFailed(fail)

    if not latest:
        # Last chance only if loading finished with a new ready answer
        if not _ai_still_loading(page):
            txt = _latest_answer_text(page)
            if _is_ai_fail_text(txt):
                raise AiGenerationFailed(txt)
            if _answer_looks_ready(txt, prev_fingerprint):
                latest = txt

    if not latest:
        # Timed out or still stuck — caller will refresh / reopen
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
        # Allow wait(60s) × retries(refresh + browser restart + AI fail) + paste overhead
        if not done.wait(timeout=600):
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
        launch_args = ["--disable-blink-features=AutomationControlled"]
        # Use installed Google Chrome when possible — system clipboard paste
        # matches manual Explorer multi-copy + Ctrl+V (Playwright Chromium often does not).
        self._browser = None
        if not headless:
            for channel in ("chrome", "msedge"):
                try:
                    self._browser = self._pw.chromium.launch(
                        channel=channel,
                        headless=False,
                        args=launch_args,
                    )
                    break
                except Exception:
                    continue
        if self._browser is None:
            self._browser = self._pw.chromium.launch(
                headless=headless,
                args=launch_args,
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

    def _soft_refresh_ai(self) -> None:
        """Reload AI Mode page (stuck loading recovery)."""
        page = self._page
        if page is None or page.is_closed():
            self._shutdown()
            return
        try:
            page.goto(_AI_MODE_URL, wait_until="domcontentloaded", timeout=60000)
            _dismiss_google_consent(page)
            page.wait_for_timeout(800)
            self._on_ai_mode = True
        except Exception:
            self._shutdown()

    def _do_search(self, paths: list[Path], prompt: str, headless: bool) -> list[str]:
        """Paste → prompt → wait for NEW answer. Retry with refresh / browser restart."""
        paths = [p for p in paths if p.exists()]
        if not paths:
            return []

        # Extra attempts when Google shows "AI 대답을 생성할 수 없습니다"
        max_attempts = 5
        for attempt in range(max_attempts):
            try:
                # After AI fail / hard recoveries: always reopen a fresh window
                if attempt >= 2:
                    self._shutdown()

                self._ensure(headless)
                page = self._page
                assert page is not None

                if attempt == 1:
                    self._soft_refresh_ai()
                    if not self._alive():
                        self._ensure(headless)
                        page = self._page
                        assert page is not None
                        self._open_ai_mode(page)
                elif attempt >= 2 or not (self._on_ai_mode and self._alive()):
                    self._open_ai_mode(page)
                else:
                    try:
                        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    except Exception:
                        pass

                prev_fp = _latest_ai_fingerprint(page)

                pasted = _attach_images_multi(page, paths)
                if pasted == 0:
                    self._on_ai_mode = False
                    if attempt < max_attempts - 1:
                        if attempt == 0:
                            self._soft_refresh_ai()
                        else:
                            self._shutdown()
                            time.sleep(2.0)
                        continue
                    return []

                page.wait_for_timeout(300)
                _type_prompt_and_enter(page, prompt)

                # Must wait for a real new answer — do not advance while loading
                texts = _collect_ai_texts(
                    page,
                    prev_fingerprint=prev_fp,
                    timeout_sec=60.0,
                )
                if texts:
                    self._on_ai_mode = True
                    return texts

                # Empty result: if fail banner is up, close window and retry same image
                fail_msg = _ai_generation_failed(page)
                self._on_ai_mode = False
                if fail_msg:
                    self._shutdown()
                    time.sleep(4.0)
                    continue

                # 1분 이상 결과 없음 / 로딩 고착 → 새로고침 또는 창 재시작 후 같은 상품 재시도
                if attempt == 0:
                    self._soft_refresh_ai()
                else:
                    self._shutdown()
                    time.sleep(2.5)
            except AiGenerationFailed:
                # 「문제가 발생하여 AI 대답을 생성할 수 없습니다」
                # → 인터넷 창 종료 → 잠시 대기 → 같은 이미지부터 재시도
                self._on_ai_mode = False
                self._shutdown()
                time.sleep(4.0)
                continue
            except Exception:
                self._shutdown()
                if attempt >= max_attempts - 1:
                    raise
                time.sleep(2.0)
        return []


# Headed / headless sessions are separate so batch search won't close the visible window
_AI_SESSION_HEADED = _AiBrowserSession()
_AI_SESSION_HEADLESS = _AiBrowserSession()


def close_ai_browsers() -> None:
    """Force-close Google AI browser windows (call before retrying a failed image)."""
    try:
        _AI_SESSION_HEADED.close()
    except Exception:
        pass
    try:
        _AI_SESSION_HEADLESS.close()
    except Exception:
        pass


def _google_ai_mode_search(
    path: Path | list[Path],
    *,
    size: str = "",
    hint: str = "",
    color: str = "",
    brand: str = "",
    headless: bool = False,
    multi: bool = False,
    clothing: bool = False,
) -> list[str]:
    """Paste product image(s) into Google AI Mode. Reuses the same browser window."""
    paths = [Path(path)] if not isinstance(path, list) else [Path(p) for p in path]
    paths = [p for p in paths if p.exists()]
    if not paths:
        return []
    if multi or len(paths) > 1:
        prompt = _build_multi_ai_prompt(
            size=size, count=len(paths), brand=brand, clothing=clothing
        )
    else:
        prompt = _build_ai_prompt(
            size=size, hint=hint, color=color, brand=brand, clothing=clothing
        )
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
    brand: str = "",
    headless: bool = False,
    clothing: bool = False,
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
                brand=brand,
                headless=headless,
                clothing=clothing,
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
                    brand=brand,
                    headless=headless,
                    clothing=clothing,
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

    from product_name import (
        extract_ai_clothing_fields,
        extract_ai_labeled_fields,
        format_clothing_product_name,
        ko_name_to_en,
        normalize_ai_color,
    )

    if clothing:
        clothing_type, ai_color = extract_ai_clothing_fields(lines)
        found_color = normalize_ai_color(ai_color) if ai_color else ""
        if not found_color and color:
            found_color = normalize_ai_color(color)
        brand_s = (brand or "").strip()
        if not clothing_type:
            return LensResult(
                error="옷종류를 찾지 못했습니다.",
                raw_texts=lines[:40],
                source=source,
                color=found_color,
                category="여성옷",
            )
        name_ko = format_clothing_product_name(brand_s, clothing_type)
        name_en = ko_name_to_en(name_ko)
        return LensResult(
            product_name=name_ko,
            name_en=name_en,
            candidates=[clothing_type, name_ko],
            category="여성옷",
            raw_texts=lines[:40],
            source=source,
            color=found_color,
        )

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
    brand: str = "",
    headless: bool = False,
    clothing: bool = False,
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
            brand=brand,
            headless=headless,
            clothing=clothing,
        )
        if r.product_name:
            # Clothing names are short ("샤넬 니트"); bags need longer model names.
            if clothing or len(r.product_name.split()) >= 3:
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
            brand=brand,
            headless=headless,
            clothing=clothing,
        )
    return last


def search_products_multi(
    jobs: list[dict],
    *,
    headless: bool = False,
) -> list[LensResult]:
    """Paste many product images at once, parse per-image 제품명/컬러.

    Each job: { "path": str, "size": str, "hint": str, "brand": str, "clothing": bool }
    Returns one LensResult per job (same order).
    """
    from product_name import (
        extract_ai_clothing_fields,
        format_clothing_product_name,
        ko_name_to_en,
        normalize_ai_color,
    )

    prepared: list[tuple[int, Path, str, str, str, bool]] = []
    for i, job in enumerate(jobs):
        p = Path(job.get("path") or "")
        if p.exists():
            prepared.append(
                (
                    i,
                    p,
                    (job.get("size") or "").strip(),
                    (job.get("hint") or "").strip(),
                    (job.get("brand") or "").strip(),
                    bool(job.get("clothing")),
                )
            )

    out: list[LensResult] = [
        LensResult(error="첨부할 이미지가 없습니다.") for _ in jobs
    ]
    if not prepared:
        return out

    size_vals = [s for _i, _p, s, _h, _b, _c in prepared if s]
    size_prompt = ""
    if size_vals:
        uniq = list(dict.fromkeys(size_vals))
        size_prompt = uniq[0] if len(uniq) == 1 else ", ".join(uniq[:3])

    brand_vals = [b for _i, _p, _s, _h, b, _c in prepared if b]
    brand_prompt = ""
    if brand_vals:
        uniq_b = list(dict.fromkeys(brand_vals))
        brand_prompt = uniq_b[0] if len(uniq_b) == 1 else ", ".join(uniq_b[:4])

    clothing_mode = all(c for _i, _p, _s, _h, _b, c in prepared)

    try:
        lines = _google_ai_mode_search(
            [p for _i, p, _s, _h, _b, _c in prepared],
            size=size_prompt,
            brand=brand_prompt,
            headless=headless,
            multi=True,
            clothing=clothing_mode,
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
        for job_i, _p, _s, _h, _b, _c in prepared:
            out[job_i] = err
        return out

    parsed = parse_multi_image_answers(lines, len(prepared))
    for j, (job_i, _path, _size, hint, brand, clothing) in enumerate(prepared):
        item = (
            parsed[j]
            if j < len(parsed)
            else {"name": "", "name_en": "", "color": "", "raw": ""}
        )
        name = (item.get("name") or "").strip()
        color = (item.get("color") or "").strip()
        name_en = (item.get("name_en") or "").strip()
        chunk_lines = [ln for ln in (item.get("raw") or "").splitlines() if ln.strip()]
        if clothing or clothing_mode:
            ctype, ai_color = extract_ai_clothing_fields(
                chunk_lines or ([name] if name else []) or lines
            )
            if not ctype and name:
                ctype = name
            color = normalize_ai_color(ai_color or color)
            if not ctype:
                out[job_i] = LensResult(
                    error="옷종류를 찾지 못했습니다.",
                    raw_texts=chunk_lines or lines[:20],
                    source="google-ai-multi",
                    color=color,
                    category="여성옷",
                )
                continue
            name = format_clothing_product_name(brand, ctype)
            name_en = ko_name_to_en(name)
            out[job_i] = LensResult(
                product_name=name,
                name_en=name_en,
                category="여성옷",
                color=color,
                raw_texts=chunk_lines or lines[:20],
                source="google-ai-multi",
                candidates=[ctype, name],
            )
            continue
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
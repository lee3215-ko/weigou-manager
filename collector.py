# -*- coding: utf-8 -*-
"""Collect product image URLs from 微购相册 (CDP or clipboard HTML)."""
from __future__ import annotations

import json
import re
import socket
import urllib.error
import urllib.request
from typing import Iterable
from urllib.parse import urlparse

CDP_PORTS = (9222, 9223, 9229, 9333)
DEFAULT_CDP_PORT = 9222
IMG_HOST_MARKERS = (
    "xcimg.szwego.com/",
    "img.szwego.com/",
)
# /img/…, /imgHD/…, and dated /YYYYMMDD/… product paths
_RE_PRODUCT_PATH = re.compile(
    r"(?:/img(?:hd)?/\S+|/\d{8}/\S+\.(?:jpe?g|png|webp|gif))",
    re.I,
)
UI_NOISE = (
    "pc_client_poster",
    "static.szwego.com",
    "/normal/",
    ".svg",
    "coupon",
    "popup",
    "avatar",
    "shopimg",
    "minicode",
    "add_cart_default",
    "live_icon",
    "/product/",
)
# 샵 아바타 등: …jpg_160 / …jpg_80 (확장자 뒤 _숫자)
_RE_AVATAR_SUFFIX = re.compile(
    r"\.(?:jpe?g|png|webp|gif)_\d{2,4}(?:\D|$)",
    re.I,
)


def normalize_image_url(url: str) -> str | None:
    url = (
        url.replace("&quot;", '"')
        .replace("&amp;", "&")
        .strip()
        .rstrip(").,;]}\\\"'")
    )
    if not url.startswith("http"):
        return None
    low = url.lower()
    if not any(m in low for m in IMG_HOST_MARKERS):
        return None
    if any(n in low for n in UI_NOISE):
        return None
    # Prefer original file (drop imageMogr2 / thumbnail transforms)
    base = url.split("?")[0]
    # Shop/user avatar: https://…/i….jpg_160
    if _RE_AVATAR_SUFFIX.search(base):
        return None
    # Support /img/, /imgHD/, and dated paths like /20220815/i….jpg
    if "/img/" in base.lower() or "/imghd/" in base.lower():
        # still reject avatar-sized thumbs even under /img/
        if re.search(r"_\d{2,4}$", base):
            return None
        return base
    if re.search(r"xcimg\.szwego\.com/\d{8}/[^?\s]+\.(?:jpe?g|png|webp|gif)$", base, re.I):
        return base
    if "img.szwego.com/" in base.lower() and _RE_PRODUCT_PATH.search(base):
        return base
    return None


def extract_urls_from_text(text: str) -> list[str]:
    patterns = [
        r"https://xcimg\.szwego\.com/img(?:HD)?/[^\\\"'\\s<>]+",
        # dated path; do not stop early before avatar suffix _160
        r"https://xcimg\.szwego\.com/\d{8}/[^\\\"'\\s<>]+\.(?:jpe?g|png|webp|gif)(?!_\d)",
        r"https://img\.szwego\.com/[^\\\"'\\s<>]+",
    ]
    found: list[str] = []
    seen: set[str] = set()
    for pat in patterns:
        for raw in re.findall(pat, text, flags=re.I):
            u = normalize_image_url(raw)
            if u and u not in seen:
                seen.add(u)
                found.append(u)
    return found


def _port_open(port: int, timeout: float = 0.12) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def is_cdp_up(port: int = DEFAULT_CDP_PORT, timeout: float = 0.12) -> bool:
    return _port_open(port, timeout=timeout)


def _http_json(url: str, timeout: float = 0.6) -> object | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return None


def find_cdp_targets(ports: Iterable[int] = CDP_PORTS) -> list[dict]:
    targets: list[dict] = []
    for port in ports:
        if not _port_open(port):
            continue
        data = _http_json(f"http://127.0.0.1:{port}/json")
        if not isinstance(data, list):
            continue
        for item in data:
            if not isinstance(item, dict):
                continue
            item = dict(item)
            item["_port"] = port
            targets.append(item)
    return targets


def open_cdp_tab(url: str, ports: Iterable[int] = CDP_PORTS) -> tuple[bool, str]:
    """Open URL in a new tab on the CDP debug browser (微购相册)."""
    from urllib.parse import quote

    raw = (url or "").strip()
    if not raw:
        return False, "URL이 비어 있습니다."
    encoded = quote(raw, safe="")
    last_err = ""
    for port in ports:
        if not _port_open(port):
            continue
        api = f"http://127.0.0.1:{port}/json/new?{encoded}"
        for method in ("PUT", "GET"):
            try:
                req = urllib.request.Request(api, method=method)
                with urllib.request.urlopen(req, timeout=10) as resp:
                    if 200 <= int(getattr(resp, "status", 200) or 200) < 300:
                        return True, f"디버그 창에 탭을 열었습니다 (포트 {port})."
            except Exception as exc:
                last_err = str(exc)
    return False, last_err or "CDP 포트에 연결할 수 없습니다."


def pick_album_target(targets: list[dict]) -> dict | None:
    scored: list[tuple[int, dict]] = []
    for t in targets:
        url = (t.get("url") or "") + " " + (t.get("title") or "")
        typ = t.get("type") or ""
        score = 0
        if typ == "page":
            score += 2
        if "szwego" in url.lower() or "wego" in url.lower():
            score += 5
        if t.get("webSocketDebuggerUrl"):
            score += 1
        scored.append((score, t))
    scored.sort(key=lambda x: x[0], reverse=True)
    if not scored or scored[0][0] < 2:
        # still allow first page target
        for _, t in scored:
            if t.get("webSocketDebuggerUrl"):
                return t
        return None
    return scored[0][1]


def _cdp_call(ws_url: str, method: str, params: dict | None = None, timeout: float = 20.0) -> dict:
    try:
        import websocket  # type: ignore
    except ImportError as e:
        raise RuntimeError(
            "websocket-client 패키지가 필요합니다. 실행.bat 으로 다시 실행해 주세요."
        ) from e

    payload = {"id": 1, "method": method, "params": params or {}}
    ws = websocket.create_connection(ws_url, timeout=timeout)
    try:
        ws.send(json.dumps(payload))
        while True:
            raw = ws.recv()
            msg = json.loads(raw)
            if msg.get("id") == 1:
                if "error" in msg:
                    raise RuntimeError(str(msg["error"]))
                return msg.get("result") or {}
    finally:
        ws.close()


COLLECT_JS = r"""
(() => {
  const urls = new Set();
  const isAvatarUrl = (u) => /\.(jpe?g|png|webp|gif)_\d{2,4}(?:\?|$)/i.test(u);
  const add = (u) => {
    if (!u || typeof u !== 'string') return;
    const low = u.toLowerCase();
    if (low.indexOf('xcimg.szwego.com/') < 0 && low.indexOf('img.szwego.com/') < 0) return;
    if (/static\.szwego|pc_client_poster|coupon|avatar|minicode|\/product\/|\.svg/i.test(low)) return;
    const base = u.split('?')[0];
    if (isAvatarUrl(base)) return;
    // keep /img/, /imgHD/, or /YYYYMMDD/*.jpg style product images
    const ok =
      /\/img(?:hd)?\//i.test(base) ||
      /xcimg\.szwego\.com\/\d{8}\/.+\.(jpe?g|png|webp|gif)$/i.test(base) ||
      /img\.szwego\.com\//i.test(base);
    if (!ok) return;
    urls.add(base);
  };
  document.querySelectorAll('img').forEach((img) => {
    const cls = (img.className || '') + ' ' + (img.getAttribute('class') || '');
    if (/avatar/i.test(cls)) return;
    add(img.currentSrc);
    add(img.src);
    add(img.getAttribute('data-original'));
    add(img.getAttribute('data-src'));
  });
  document.querySelectorAll('[style*="xcimg"], [style*="szwego"]').forEach((el) => {
    const s = el.getAttribute('style') || '';
    const m = s.match(/url\((['"]?)(.*?)\1\)/i);
    if (m) add(m[2]);
  });
  try {
    performance.getEntriesByType('resource').forEach((e) => add(e.name));
  } catch (e) {}
  const html = document.documentElement ? document.documentElement.outerHTML : '';
  const text = document.body ? (document.body.innerText || '') : '';
  return { urls: Array.from(urls), html, text };
})()
"""


def collect_page_via_cdp() -> tuple[str, str, list[str], str]:
    """Return (html, text, urls, status_message)."""
    targets = find_cdp_targets()
    if not targets:
        return "", "", [], "CDP 연결 실패: 디버그 모드로 微购相册을 실행해 주세요."
    target = pick_album_target(targets)
    if not target:
        return "", "", [], "열린 페이지 탭을 찾지 못했습니다."
    ws_url = target.get("webSocketDebuggerUrl")
    if not ws_url:
        return "", "", [], "webSocketDebuggerUrl 없음"
    result = _cdp_call(
        ws_url,
        "Runtime.evaluate",
        {
            "expression": COLLECT_JS,
            "returnByValue": True,
            "awaitPromise": False,
        },
    )
    value = (result.get("result") or {}).get("value") or {}
    urls: list[str] = []
    seen: set[str] = set()
    for raw in value.get("urls") or []:
        u = normalize_image_url(str(raw))
        if u and u not in seen:
            seen.add(u)
            urls.append(u)
    html = value.get("html") or ""
    text = value.get("text") or ""
    if html:
        for u in extract_urls_from_text(html):
            if u not in seen:
                seen.add(u)
                urls.append(u)
    title = target.get("title") or urlparse(target.get("url") or "").path
    return html, text, urls, f"CDP 수집 완료 (탭: {title}, 이미지 {len(urls)}장)"


def collect_via_cdp() -> tuple[list[str], str]:
    """Return (urls, status_message)."""
    _html, _text, urls, msg = collect_page_via_cdp()
    return urls, msg


def collect_via_clipboard(text: str) -> tuple[list[str], str]:
    urls = extract_urls_from_text(text or "")
    if not urls:
        return [], "클립보드에서 상품 이미지 URL을 찾지 못했습니다."
    return urls, f"클립보드 HTML에서 {len(urls)}장 추출"


def collect_best(clipboard_text: str | None = None) -> tuple[list[str], str, str]:
    """
    Try CDP first, then clipboard.
    Returns (urls, source, message)
    """
    urls, msg = collect_via_cdp()
    if urls:
        return urls, "cdp", msg
    cdp_msg = msg
    if clipboard_text and ("xcimg.szwego.com" in clipboard_text or "<html" in clipboard_text.lower()):
        urls, msg = collect_via_clipboard(clipboard_text)
        if urls:
            return urls, "clipboard", msg
    return [], "none", cdp_msg


def collect_page_best(clipboard_text: str | None = None) -> tuple[str, str, str, str]:
    """
    Returns (html, text, source, message)
    """
    html, text, urls, msg = collect_page_via_cdp()
    if html or urls:
        return html, text, "cdp", msg
    cdp_msg = msg
    clip = clipboard_text or ""
    if "xcimg.szwego.com" in clip or "<html" in clip.lower() or "搜索码" in clip:
        return clip, clip, "clipboard", "클립보드 내용을 사용합니다."
    return "", "", "none", cdp_msg

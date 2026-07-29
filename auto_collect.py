# -*- coding: utf-8 -*-
"""Automate list → detail walk via CDP and collect full product galleries."""
from __future__ import annotations

import json
import threading
import time
from typing import Callable

from collector import (
    COLLECT_JS,
    find_cdp_targets,
    normalize_image_url,
    pick_album_target,
)
from product_parse import ParsedProduct, parse_detail_product, parse_list_products

ProgressCb = Callable[[str], None]

# 수집 속도 (서버 과부하·「服务器偷懒了」 완화)
AFTER_COLLECT_SEC = 1.8  # 상세 저장 후 다음 상품까지
AFTER_FAIL_SEC = 2.5  # 상세 실패·타임아웃 후
PAGE_DOWN_SETTLE_SEC = 1.2  # PageDown 후 최소 대기(스피너 없을 때)
PAGE_DOWN_KEY_SEC = 0.75
REST_EVERY_N = 8  # N건 신규 수집마다
REST_SEC = 7.0
SERVER_BUSY_WAIT_SEC = 12.0
# 하단 로딩(wgoo-loading-icon) 대기·회복
LOADING_WAIT_SEC = 20.0  # 스피너가 사라질 때까지 최대 대기
LOADING_POLL_SEC = 0.45
LOADING_STUCK_RECOVER_SEC = 22.0  # 이보다 길면 스크롤 넛지·새로고침
SESSION_MAX_NEW = 100  # UI 미지정 시 기본 상한 (manager에서 max_items로 덮음)


class CdpSession:
    """Persistent CDP websocket for repeated Runtime.evaluate calls."""

    def __init__(self, ws_url: str, timeout: float = 30.0) -> None:
        try:
            import websocket  # type: ignore
        except ImportError as e:
            raise RuntimeError(
                "websocket-client 패키지가 필요합니다. 실행.bat 으로 다시 실행해 주세요."
            ) from e
        self._ws = websocket.create_connection(ws_url, timeout=timeout)
        self._id = 0
        self._timeout = timeout

    def close(self) -> None:
        try:
            self._ws.close()
        except Exception:
            pass

    def __enter__(self) -> "CdpSession":
        return self

    def __exit__(self, *args) -> None:
        self.close()

    def call(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        req_id = self._id
        self._ws.send(json.dumps({"id": req_id, "method": method, "params": params or {}}))
        deadline = time.time() + self._timeout
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise TimeoutError(f"CDP timeout: {method}")
            self._ws.settimeout(max(0.5, remaining))
            raw = self._ws.recv()
            msg = json.loads(raw)
            if msg.get("id") != req_id:
                continue
            if "error" in msg:
                raise RuntimeError(str(msg["error"]))
            return msg.get("result") or {}

    def evaluate(self, expression: str, await_promise: bool = False):
        result = self.call(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": await_promise,
            },
        )
        return (result.get("result") or {}).get("value")


def open_album_session() -> tuple[CdpSession | None, str]:
    targets = find_cdp_targets()
    if not targets:
        return None, "CDP 연결 실패: 디버그 모드로 微购相册을 실행해 주세요."
    target = pick_album_target(targets)
    if not target or not target.get("webSocketDebuggerUrl"):
        return None, "열린 페이지 탭을 찾지 못했습니다."
    try:
        session = CdpSession(str(target["webSocketDebuggerUrl"]))
    except Exception as e:
        return None, f"CDP 연결 오류: {e}"
    title = target.get("title") or ""
    return session, f"CDP 연결됨 ({title})"


PAGE_STATE_JS = r"""
(() => {
  const detail = !!document.querySelector('[class*="GoodsDynamicDetails"]');
  const cards = document.querySelectorAll('[data-search-bury-info]');
  const text = document.body ? (document.body.innerText || '') : '';
  const hasSearch = text.indexOf('搜索码') >= 0;
  return {
    detail,
    listCount: cards.length,
    hasSearch,
    title: document.title || ''
  };
})()
"""

SERVER_BUSY_JS = r"""
(() => {
  const t = document.body ? (document.body.innerText || '') : '';
  return (
    t.indexOf('服务器偷懒') >= 0 ||
    t.indexOf('服务器开小差') >= 0 ||
    t.indexOf('网络异常') >= 0 ||
    t.indexOf('请求失败') >= 0 ||
    t.indexOf('稍后再试') >= 0 ||
    t.indexOf('加载失败') >= 0
  );
})()
"""

# 무한스크롤 하단 로딩 / 진짜 목록 끝
LIST_LOAD_STATE_JS = r"""
(() => {
  const visible = (el) => {
    if (!el) return false;
    try {
      const st = window.getComputedStyle(el);
      if (st.display === 'none' || st.visibility === 'hidden' || Number(st.opacity) === 0) return false;
      const r = el.getBoundingClientRect();
      return r.width > 2 && r.height > 2;
    } catch (e) { return false; }
  };
  const icon = document.querySelector('.wgoo-loading-icon');
  const footer = document.querySelector('.wgoo-footer');
  const circles = document.querySelectorAll('.loading-icon-circle, [class*="loading-icon"]');
  let loading = visible(icon);
  if (!loading && footer && visible(footer)) {
    loading = !!footer.querySelector('.wgoo-loading-icon, .loading-icon-circle');
  }
  if (!loading) {
    for (const c of circles) {
      if (visible(c)) { loading = true; break; }
    }
  }
  const t = document.body ? (document.body.innerText || '') : '';
  const endHint = (
    t.indexOf('没有更多了') >= 0 ||
    t.indexOf('没有更多') >= 0 ||
    t.indexOf('暂无更多') >= 0 ||
    t.indexOf('已經到底') >= 0 ||
    t.indexOf('已经到底') >= 0
  );
  return { loading: !!loading, endHint: !!endHint };
})()
"""

SCROLL_NUDGE_JS = r"""
((upPx) => {
  const candidates = [
    document.querySelector('.content'),
    document.querySelector('[class*="scroll"]'),
    document.scrollingElement,
    document.documentElement,
    document.body
  ].filter(Boolean);
  let best = candidates[0];
  let max = -1;
  for (const el of candidates) {
    const h = el.scrollHeight || 0;
    if (h > max) { max = h; best = el; }
  }
  if (!best) return { ok: false };
  const before = best.scrollTop || 0;
  best.scrollTop = Math.max(0, before - Math.abs(upPx || 240));
  return { ok: true, before, after: best.scrollTop || 0 };
})(%s)
"""

LIST_IDS_JS = r"""
(() => {
  const cards = Array.from(document.querySelectorAll('[data-search-bury-info]'));
  const rows = [];
  for (const el of cards) {
    const raw = el.getAttribute('data-search-bury-info') || '';
    let gid = '', sid = '';
    try {
      const j = JSON.parse(raw);
      gid = j.goods_id || '';
      sid = j.shop_id || '';
    } catch (e) {
      const m = raw.match(/goods_id["']?\s*[:=]\s*["']?([^"'&,}]+)/);
      if (m) gid = m[1];
      const s = raw.match(/shop_id["']?\s*[:=]\s*["']?([^"'&,}]+)/);
      if (s) sid = s[1];
    }
    if (!gid) continue;
    const r = el.getBoundingClientRect();
    if (r.width < 20 || r.height < 20) continue;
    let title = '';
    const t = el.querySelector('[class*="ellipsis-two"]');
    if (t) title = (t.textContent || '').trim();
    // templateList shopItem data-index = 목록 공식 순서 (0,1,2…)
    let idx = -1;
    const host = el.closest('[data-index]') || el.parentElement;
    if (host) {
      const di = host.getAttribute('data-index');
      if (di != null && di !== '' && !Number.isNaN(Number(di))) idx = Number(di);
    }
    rows.push({
      goods_id: gid,
      shop_id: sid,
      title,
      idx,
      top: r.top + (window.scrollY || 0),
      left: r.left + (window.scrollX || 0),
    });
  }
  // 1순위: data-index 0→1→2…  2순위: 화면 위→아래·왼→오
  rows.sort((a, b) => {
    if (a.idx >= 0 && b.idx >= 0 && a.idx !== b.idx) return a.idx - b.idx;
    if (a.idx >= 0 && b.idx < 0) return -1;
    if (b.idx >= 0 && a.idx < 0) return 1;
    const rowTol = 40;
    if (Math.abs(a.top - b.top) > rowTol) return a.top - b.top;
    return a.left - b.left;
  });
  const out = [];
  const seen = new Set();
  for (const it of rows) {
    if (seen.has(it.goods_id)) continue;
    seen.add(it.goods_id);
    out.push({
      goods_id: it.goods_id,
      shop_id: it.shop_id,
      title: it.title,
      index: it.idx,
    });
  }
  return out;
})()
"""

CLICK_CARD_JS = r"""
((gid) => {
  const cards = Array.from(document.querySelectorAll('[data-search-bury-info]'));
  const el = cards.find((n) => (n.getAttribute('data-search-bury-info') || '').indexOf(gid) >= 0);
  if (!el) return { ok: false, reason: 'not_found' };
  try { el.scrollIntoView({ block: 'center', inline: 'nearest' }); } catch (e) {}
  const target = el.querySelector('img') || el.querySelector('a') || el;
  const fire = (node, type) => {
    try {
      node.dispatchEvent(new MouseEvent(type, {
        bubbles: true, cancelable: true, view: window, buttons: 1
      }));
    } catch (e) {}
  };
  ['pointerdown', 'mousedown', 'mouseup', 'click'].forEach((t) => fire(target, t));
  if (target !== el) {
    ['pointerdown', 'mousedown', 'mouseup', 'click'].forEach((t) => fire(el, t));
  }
  try { if (typeof el.click === 'function') el.click(); } catch (e) {}
  return { ok: true };
})(%s)
"""

GO_BACK_JS = r"""
(() => {
  const sels = [
    '[class*="inline-cart-back"]',
    '[class*="GoodsDynamicDetails"] [class*="back"]',
    '[class*="NavBar"] [class*="back"]',
    '[class*="nav-back"]',
    '[class*="icon-back"]',
    '.wgoo-nav__left',
    '[class*="wego-iconfont"]'
  ];
  for (const sel of sels) {
    const nodes = Array.from(document.querySelectorAll(sel));
    for (const n of nodes) {
      const cls = (n.className || '') + ' ' + (n.parentElement ? (n.parentElement.className || '') : '');
      const looksBack = /back|返回|left/i.test(cls) || /back|返回/.test(n.getAttribute('aria-label') || '');
      if (!looksBack && sel.indexOf('wego-iconfont') >= 0) continue;
      const r = n.getBoundingClientRect();
      if (r.width < 8 || r.height < 8) continue;
      if (r.top > 120 || r.left > 80) continue;
      try {
        n.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window }));
        if (typeof n.click === 'function') n.click();
        return { ok: true, via: sel };
      } catch (e) {}
    }
  }
  try { history.back(); return { ok: true, via: 'history.back' }; } catch (e) {}
  return { ok: false };
})()
"""

# PageDown 한 번 분량 스크롤 (키 이벤트 실패 시 폴백)
SCROLL_PAGE_JS = r"""
(() => {
  const candidates = [
    document.querySelector('.content'),
    document.querySelector('[class*="scroll"]'),
    document.scrollingElement,
    document.documentElement,
    document.body
  ].filter(Boolean);
  let best = candidates[0];
  let max = -1;
  for (const el of candidates) {
    const h = el.scrollHeight || 0;
    if (h > max) { max = h; best = el; }
  }
  if (!best) return { moved: false, before: 0, after: 0 };
  try { best.focus({ preventScroll: true }); } catch (e) {}
  const before = best.scrollTop || 0;
  const step = Math.max(Math.floor((best.clientHeight || 640) * 0.92), 480);
  best.scrollTop = before + step;
  const after = best.scrollTop || 0;
  return {
    moved: after > before + 8,
    before,
    after,
    step,
    atEnd: after + (best.clientHeight || 0) >= (best.scrollHeight || 0) - 4,
  };
})()
"""

FOCUS_LIST_JS = r"""
(() => {
  const candidates = [
    document.querySelector('.content'),
    document.querySelector('[class*="scroll"]'),
    document.scrollingElement,
    document.documentElement,
    document.body
  ].filter(Boolean);
  let best = candidates[0];
  let max = -1;
  for (const el of candidates) {
    const h = el.scrollHeight || 0;
    if (h > max) { max = h; best = el; }
  }
  if (!best) return false;
  try {
    if (best.tabIndex < 0) best.tabIndex = -1;
    best.focus({ preventScroll: true });
  } catch (e) {}
  return true;
})()
"""

SCROLL_TOP_JS = r"""
(() => {
  const candidates = [
    document.querySelector('.content'),
    document.querySelector('[class*="scroll"]'),
    document.scrollingElement,
    document.documentElement,
    document.body
  ].filter(Boolean);
  let best = candidates[0];
  let max = -1;
  for (const el of candidates) {
    const h = el.scrollHeight || 0;
    if (h > max) { max = h; best = el; }
  }
  if (!best) return false;
  best.scrollTop = 0;
  return true;
})()
"""
SCROLL_DETAIL_JS = r"""
(() => {
  const root = document.querySelector('[class*="GoodsDynamicDetails"]') || document.scrollingElement || document.body;
  if (!root) return 0;
  const el = root.scrollHeight ? root : document.scrollingElement;
  if (!el) return 0;
  el.scrollTop = Math.min(el.scrollHeight, (el.scrollTop || 0) + Math.max(400, (el.clientHeight || 400)));
  return el.scrollTop || 0;
})()
"""


def _log(cb: ProgressCb | None, msg: str) -> None:
    if cb:
        cb(msg)


def _cancelled(cancel: threading.Event | None) -> bool:
    return bool(cancel and cancel.is_set())


def _wait_pause(
    pause: threading.Event | None,
    cancel: threading.Event | None,
    on_progress: ProgressCb | None = None,
) -> bool:
    """Block while paused. Returns False if cancelled during pause."""
    if not pause or not pause.is_set():
        return True
    _log(on_progress, "일시정지 — [수집 계속] 또는 [중지]를 누르세요")
    while pause.is_set():
        if _cancelled(cancel):
            return False
        time.sleep(0.25)
    _log(on_progress, "수집 재개")
    return not _cancelled(cancel)


def _sleep_interruptible(
    seconds: float,
    *,
    cancel: threading.Event | None = None,
    pause: threading.Event | None = None,
    on_progress: ProgressCb | None = None,
) -> bool:
    """Sleep in small slices; respect pause/cancel. False if cancelled."""
    end = time.time() + max(0.0, seconds)
    while time.time() < end:
        if not _wait_pause(pause, cancel, on_progress):
            return False
        if _cancelled(cancel):
            return False
        time.sleep(min(0.25, end - time.time()))
    return True


def _server_busy(session: CdpSession) -> bool:
    try:
        return bool(session.evaluate(SERVER_BUSY_JS))
    except Exception:
        return False


def _recover_server_busy(
    session: CdpSession,
    *,
    cancel: threading.Event | None = None,
    pause: threading.Event | None = None,
    on_progress: ProgressCb | None = None,
) -> bool:
    """
    Wait out Weigou overload screens. Returns False if cancelled.
    Reloads once if the error persists.
    """
    if not _server_busy(session):
        return True
    _log(
        on_progress,
        f"서버 응답 실패/과부하 감지 — {SERVER_BUSY_WAIT_SEC:.0f}초 대기 후 재시도",
    )
    if not _sleep_interruptible(
        SERVER_BUSY_WAIT_SEC, cancel=cancel, pause=pause, on_progress=on_progress
    ):
        return False
    if not _server_busy(session):
        _log(on_progress, "서버 복구됨 — 수집 계속")
        return True
    _log(on_progress, "여전히 오류 — 페이지 새로고침 후 목록 대기")
    try:
        session.evaluate("location.reload()")
    except Exception:
        pass
    if not _sleep_interruptible(4.0, cancel=cancel, pause=pause, on_progress=on_progress):
        return False
    _wait(
        session,
        "(() => document.querySelectorAll('[data-search-bury-info]').length > 2)()",
        timeout=20.0,
        cancel=cancel,
    )
    if _server_busy(session):
        _log(on_progress, "서버 오류 지속 — 수집을 일시적으로 더 느리게 진행")
        return _sleep_interruptible(
            SERVER_BUSY_WAIT_SEC, cancel=cancel, pause=pause, on_progress=on_progress
        )
    _log(on_progress, "새로고침 후 목록 복구 — 수집 계속")
    return True


def _list_load_state(session: CdpSession) -> dict:
    try:
        st = session.evaluate(LIST_LOAD_STATE_JS) or {}
        if isinstance(st, dict):
            return {
                "loading": bool(st.get("loading")),
                "endHint": bool(st.get("endHint")),
            }
    except Exception:
        pass
    return {"loading": False, "endHint": False}


def _nudge_scroll_up(session: CdpSession, px: int = 280) -> None:
    try:
        session.evaluate(SCROLL_NUDGE_JS % int(px))
    except Exception:
        pass


def _recover_list_loading(
    session: CdpSession,
    *,
    cancel: threading.Event | None = None,
    pause: threading.Event | None = None,
    on_progress: ProgressCb | None = None,
) -> bool:
    """
    Footer spinner stuck too long: nudge scroll, then reload once.
    Returns False if cancelled.
    """
    _log(on_progress, "목록 로딩이 오래 걸림 — 스크롤 재시도")
    _nudge_scroll_up(session, 320)
    if not _sleep_interruptible(0.8, cancel=cancel, pause=pause, on_progress=on_progress):
        return False
    _page_down(session)
    if not _sleep_interruptible(2.0, cancel=cancel, pause=pause, on_progress=on_progress):
        return False
    st = _list_load_state(session)
    if not st.get("loading"):
        _log(on_progress, "로딩 회복 — 수집 계속")
        return True
    _log(on_progress, "로딩 지속 — 목록 새로고침 후 대기")
    try:
        session.evaluate("location.reload()")
    except Exception:
        pass
    if not _sleep_interruptible(4.0, cancel=cancel, pause=pause, on_progress=on_progress):
        return False
    _wait(
        session,
        "(() => document.querySelectorAll('[data-search-bury-info]').length > 2)()",
        timeout=22.0,
        cancel=cancel,
    )
    # 새로고침 후 이전 위치로 다시 내려가도록 PageDown 몇 회 (ID 스킵으로 안전)
    for _ in range(3):
        if _cancelled(cancel):
            return False
        if not _wait_pause(pause, cancel, on_progress):
            return False
        _page_down(session)
        if not _sleep_interruptible(1.2, cancel=cancel, pause=pause, on_progress=on_progress):
            return False
        if not _list_load_state(session).get("loading"):
            break
    _log(on_progress, "새로고침 후 목록 복구 — 수집 계속")
    return True


def _wait_list_settle(
    session: CdpSession,
    *,
    seen_gids: set[str],
    cancel: threading.Event | None = None,
    pause: threading.Event | None = None,
    on_progress: ProgressCb | None = None,
) -> dict:
    """
    After PageDown: wait out footer loading spinner; do not treat as feed end.
    Returns keys: loading_seen, end_hint, timed_out, recovered.
    """
    start = time.time()
    loading_seen = False
    loading_since: float | None = None
    recovered = False

    # 최소 settle
    if not _sleep_interruptible(
        PAGE_DOWN_SETTLE_SEC, cancel=cancel, pause=pause, on_progress=on_progress
    ):
        return {
            "loading_seen": False,
            "end_hint": False,
            "timed_out": False,
            "recovered": False,
            "cancelled": True,
        }

    while True:
        if not _wait_pause(pause, cancel, on_progress):
            return {
                "loading_seen": loading_seen,
                "end_hint": False,
                "timed_out": False,
                "recovered": recovered,
                "cancelled": True,
            }
        if _cancelled(cancel):
            return {
                "loading_seen": loading_seen,
                "end_hint": False,
                "timed_out": False,
                "recovered": recovered,
                "cancelled": True,
            }

        st = _list_load_state(session)
        if st.get("endHint") and not st.get("loading"):
            return {
                "loading_seen": loading_seen,
                "end_hint": True,
                "timed_out": False,
                "recovered": recovered,
                "cancelled": False,
            }

        loading = bool(st.get("loading"))
        if loading:
            loading_seen = True
            if loading_since is None:
                loading_since = time.time()
                _log(on_progress, "목록 추가 로딩 중 — 스피너 사라질 때까지 대기")
            stuck_for = time.time() - loading_since
            if stuck_for >= LOADING_STUCK_RECOVER_SEC:
                if not _recover_list_loading(
                    session, cancel=cancel, pause=pause, on_progress=on_progress
                ):
                    return {
                        "loading_seen": True,
                        "end_hint": False,
                        "timed_out": True,
                        "recovered": False,
                        "cancelled": True,
                    }
                recovered = True
                loading_since = time.time()
            if not _sleep_interruptible(
                LOADING_POLL_SEC, cancel=cancel, pause=pause, on_progress=on_progress
            ):
                return {
                    "loading_seen": True,
                    "end_hint": False,
                    "timed_out": False,
                    "recovered": recovered,
                    "cancelled": True,
                }
            continue

        # 스피너 없음
        if loading_seen:
            return {
                "loading_seen": True,
                "end_hint": bool(st.get("endHint")),
                "timed_out": False,
                "recovered": recovered,
                "cancelled": False,
            }

        # 스피너가 안 보였어도 새 ID가 곧 들어올 수 있음 → 짧게만 추가 대기
        # (이미수집 패스 위주일 때 PageDown마다 길게 멈추지 않게)
        elapsed = time.time() - start
        visible = _list_items_now(session)
        new_n = sum(1 for it in visible if str(it.get("goods_id") or "") not in seen_gids)
        if new_n > 0 or elapsed >= PAGE_DOWN_SETTLE_SEC + 1.8:
            return {
                "loading_seen": False,
                "end_hint": bool(st.get("endHint")),
                "timed_out": False,
                "recovered": recovered,
                "cancelled": False,
            }
        if not _sleep_interruptible(
            LOADING_POLL_SEC, cancel=cancel, pause=pause, on_progress=on_progress
        ):
            return {
                "loading_seen": False,
                "end_hint": False,
                "timed_out": False,
                "recovered": recovered,
                "cancelled": True,
            }


def _page_down(session: CdpSession) -> dict:
    """Prefer real PageDown key (lazy-load friendly), fallback to scrollTop."""
    try:
        session.evaluate(FOCUS_LIST_JS)
    except Exception:
        pass
    before = -1
    try:
        before = int(
            session.evaluate(
                """(() => {
                  const el = document.scrollingElement || document.documentElement || document.body;
                  return el ? (el.scrollTop || 0) : 0;
                })()"""
            )
            or 0
        )
    except Exception:
        before = -1
    try:
        for typ in ("keyDown", "keyUp"):
            session.call(
                "Input.dispatchKeyEvent",
                {
                    "type": typ,
                    "windowsVirtualKeyCode": 34,
                    "nativeVirtualKeyCode": 34,
                    "code": "PageDown",
                    "key": "PageDown",
                },
            )
        time.sleep(PAGE_DOWN_KEY_SEC)
        after = int(
            session.evaluate(
                """(() => {
                  const el = document.scrollingElement || document.documentElement || document.body;
                  return el ? (el.scrollTop || 0) : 0;
                })()"""
            )
            or 0
        )
        if after > before + 8:
            return {"moved": True, "via": "PageDown", "before": before, "after": after}
    except Exception:
        pass
    try:
        scroll = session.evaluate(SCROLL_PAGE_JS) or {}
        scroll["via"] = "scroll"
        return scroll
    except Exception:
        return {"moved": False, "via": "none"}


def _wait(
    session: CdpSession,
    predicate_js: str,
    timeout: float,
    interval: float = 0.35,
    cancel: threading.Event | None = None,
) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _cancelled(cancel):
            return False
        try:
            if session.evaluate(predicate_js):
                return True
        except Exception:
            pass
        time.sleep(interval)
    return False


def _collect_page(session: CdpSession) -> tuple[str, str, list[str]]:
    value = session.evaluate(COLLECT_JS) or {}
    urls: list[str] = []
    seen: set[str] = set()
    for raw in value.get("urls") or []:
        u = normalize_image_url(str(raw))
        if u and u not in seen:
            seen.add(u)
            urls.append(u)
    html = value.get("html") or ""
    text = value.get("text") or ""
    return html, text, urls


def _list_items_now(session: CdpSession) -> list[dict]:
    items = session.evaluate(LIST_IDS_JS) or []
    out: list[dict] = []
    seen: set[str] = set()
    for it in items:
        if not isinstance(it, dict):
            continue
        gid = str(it.get("goods_id") or "")
        if not gid or gid in seen:
            continue
        seen.add(gid)
        out.append(
            {
                "goods_id": gid,
                "shop_id": str(it.get("shop_id") or ""),
                "title": str(it.get("title") or ""),
                "index": it.get("index", -1),
            }
        )
    if out:
        return out
    html, _text, _ = _collect_page(session)
    for p in parse_list_products(html):
        if p.goods_id and p.goods_id not in seen:
            seen.add(p.goods_id)
            out.append(
                {"goods_id": p.goods_id, "shop_id": p.shop_id, "title": p.title}
            )
    return out


def _open_collect_one(
    session: CdpSession,
    item: dict,
    *,
    on_progress: ProgressCb | None,
    cancel: threading.Event | None,
    open_wait: float,
    detail_settle: float,
    back_wait: float,
) -> ParsedProduct | None:
    gid = str(item.get("goods_id") or "")
    title = str(item.get("title") or "")
    click_js = CLICK_CARD_JS % json.dumps(gid)
    try:
        clicked = session.evaluate(click_js) or {}
    except Exception as e:
        _log(on_progress, f"  클릭 실패: {e}")
        return None
    if not clicked.get("ok"):
        _log(on_progress, "  클릭 실패 — 건너뜀")
        return None

    opened = _wait(
        session,
        r"""(() => {
          const d = !!document.querySelector('[class*="GoodsDynamicDetails"]');
          const t = document.body ? (document.body.innerText || '') : '';
          return d || t.indexOf('搜索码') >= 0;
        })()""",
        timeout=open_wait,
        cancel=cancel,
    )
    if not opened:
        _log(on_progress, "  상세 진입 시간 초과 — 뒤로 복귀")
        session.evaluate(GO_BACK_JS)
        _wait(
            session,
            "(() => document.querySelectorAll('[data-search-bury-info]').length > 2)()",
            timeout=4.0,
            cancel=cancel,
        )
        return None

    time.sleep(detail_settle)
    _wait(
        session,
        r"""(() => {
          const imgs = Array.from(document.querySelectorAll('img'));
          return imgs.some((img) => {
            const s = (img.currentSrc || img.src || '');
            return /xcimg\.szwego\.com\/(?:img|imghd|\d{8})\//i.test(s);
          });
        })()""",
        timeout=6.0,
        cancel=cancel,
    )
    try:
        session.evaluate(SCROLL_DETAIL_JS)
        time.sleep(0.3)
    except Exception:
        pass

    html, text, urls = _collect_page(session)
    detail = parse_detail_product(html, text)
    product: ParsedProduct | None = None
    if detail:
        if not detail.goods_id:
            detail.goods_id = gid
        if not detail.shop_id:
            detail.shop_id = str(item.get("shop_id") or "")
        if not detail.title and title:
            detail.title = title
        if urls and len(urls) > len(detail.image_urls):
            detail.image_urls = urls
        if detail.image_urls:
            product = detail
            _log(
                on_progress,
                f"  수집: 이미지 {len(detail.image_urls)}장"
                + (f", 搜索码 {detail.search_code}" if detail.search_code else ""),
            )
        else:
            _log(on_progress, "  이미지 URL 없음 — 저장 생략")
    else:
        _log(on_progress, "  상세 파싱 실패")

    session.evaluate(GO_BACK_JS)
    back_ok = _wait(
        session,
        r"""(() => {
          const detail = !!document.querySelector('[class*="GoodsDynamicDetails"]');
          const n = document.querySelectorAll('[data-search-bury-info]').length;
          return (!detail && n > 2) || n > 5;
        })()""",
        timeout=back_wait,
        cancel=cancel,
    )
    if not back_ok:
        try:
            session.evaluate("history.back()")
        except Exception:
            pass
        time.sleep(0.8)
    return product


def _item_index(item: dict) -> int:
    """Display-only list index (unstable when feed changes)."""
    try:
        return int(item.get("index", -1))
    except (TypeError, ValueError):
        return -1


def walk_list_details(
    *,
    on_progress: ProgressCb | None = None,
    on_product: Callable[[ParsedProduct], None] | None = None,
    cancel: threading.Event | None = None,
    pause: threading.Event | None = None,
    excluded_goods_ids: set[str] | None = None,
    excluded_search_codes: set[str] | None = None,
    known_goods_ids: set[str] | None = None,
    max_items: int = 0,
    open_wait: float = 12.0,
    detail_settle: float = 1.4,
    back_wait: float = 10.0,
    between_items: float = AFTER_COLLECT_SEC,  # 신규 수집 직후 대기(이미수집 패스에는 미적용)
    refresh_skips: Callable[[], tuple[set[str], set[str], set[str]]] | None = None,
    # Legacy kwargs ignored (index cursor removed — goods_id is the identity)
    scroll_rounds: int = 0,
    resume_after_index: int | None = None,
    get_cursor: Callable[[str], int] | None = None,
    on_cursor: Callable[[str, int], None] | None = None,
) -> tuple[list[ParsedProduct], str]:
    """
    Infinite list collect keyed by goods_id (not data-index):

      scroll to top → for each visible card:
        skip if already collected / excluded / published (by goods_id·搜索码)
        else open detail & save
      PageDown → repeat until feed end or user pause/stop

    data-index is only logged — new uploads / search reorder do not break skips.
    max_items > 0 → 해당 신규 건수만 수집 후 종료
    max_items <= 0 → 무제한 (목록 끝까지)
    """
    del scroll_rounds, resume_after_index, get_cursor, on_cursor  # unused legacy

    skip_excluded = set(excluded_goods_ids or set())
    skip_codes = set(excluded_search_codes or set())
    skip_known = set(known_goods_ids or set())
    if max_items and max_items > 0:
        unlimited = False
        max_new = int(max_items)
        session_cap = True
    else:
        unlimited = True
        max_new = 10**9
        session_cap = False

    session, msg = open_album_session()
    if not session:
        return [], msg

    products: list[ParsedProduct] = []
    skipped_excluded = 0
    skipped_known = 0
    # goods_ids already handled this run (collect or skip) — avoid re-click
    handled_gids: set[str] = set()
    # all goods_ids ever seen on screen this run (detect end of feed)
    ever_seen_gids: set[str] = set()
    stagnant_pages = 0
    page = 0

    def reload_skips() -> None:
        nonlocal skip_excluded, skip_codes, skip_known
        if not refresh_skips:
            return
        try:
            ex_g, ex_c, known = refresh_skips()
            skip_excluded = set(ex_g or set())
            skip_codes = set(ex_c or set())
            skip_known = set(known or set()) | {p.goods_id for p in products if p.goods_id}
        except Exception:
            pass

    try:
        _log(on_progress, msg)
        _log(
            on_progress,
            "방식: 상품ID(goods_id)·搜索码 기준 스킵 · PageDown 무한 수집 · "
            "인덱스 번호는 사용 안 함 (새 상품/검색어에도 안전)",
        )
        _log(
            on_progress,
            f"속도: 수집 후 {between_items:.1f}초 · PageDown 후 로딩대기(최대 {LOADING_WAIT_SEC:.0f}초) · "
            f"{REST_EVERY_N}건마다 {REST_SEC:.0f}초 휴식 · 서버오류/스피너 자동 회복",
        )
        if session_cap:
            _log(
                on_progress,
                f"세션 한도: 신규 {max_new}건 — 도달 후 종료, 다시 실행하면 이미 수집·제외는 패스하고 이어감",
            )
        state = session.evaluate(PAGE_STATE_JS) or {}
        if state.get("detail") and (state.get("hasSearch") or state.get("listCount", 0) <= 1):
            _log(on_progress, "현재가 상세 화면입니다. 상세 1건만 수집합니다.")
            html, text, urls = _collect_page(session)
            detail = parse_detail_product(html, text)
            if detail:
                if urls and len(urls) > len(detail.image_urls):
                    detail.image_urls = urls
                if not detail.image_urls:
                    return [], "상세에서 이미지를 찾지 못했습니다. 화면이 로드된 뒤 다시 시도하세요."
                if (detail.goods_id and detail.goods_id in skip_excluded) or (
                    detail.search_code and detail.search_code in skip_codes
                ):
                    return [], "제외 목록에 있는 상품이라 수집하지 않았습니다."
                if detail.goods_id and detail.goods_id in skip_known:
                    return [], "이미 수집된 상품이라 건너뛰었습니다."
                if on_product:
                    on_product(detail)
                return [detail], "상세 화면 1건 수집 완료"
            return [], "상세 화면에서 상품을 파싱하지 못했습니다."

        # Always start from top so newly uploaded items (shifted indices) are found
        try:
            session.evaluate(SCROLL_TOP_JS)
            time.sleep(0.45)
        except Exception:
            pass
        _log(on_progress, "목록 맨 위부터 · 이미 있는 상품은 클릭 없이 패스 · PageDown 계속")

        while True:
            if not _wait_pause(pause, cancel, on_progress):
                _log(on_progress, "사용자 중지로 중단됨")
                break
            if _cancelled(cancel):
                _log(on_progress, "사용자 중지로 중단됨")
                break
            if len(products) >= max_new:
                if session_cap:
                    _log(
                        on_progress,
                        f"세션 한도 {max_new}건 도달 — 이번 회차 종료 "
                        f"(다시 실행하면 이어서 수집)",
                    )
                else:
                    _log(on_progress, f"신규 한도 {max_new}개 도달 — 종료")
                break

            if not _recover_server_busy(
                session, cancel=cancel, pause=pause, on_progress=on_progress
            ):
                _log(on_progress, "사용자 중지로 중단됨")
                break

            visible = _list_items_now(session)
            if not visible and page == 0:
                return [], (
                    "목록에서 상품 카드를 찾지 못했습니다. "
                    "친구 앨범 목록을 연 뒤 다시 시도하세요."
                )

            new_on_screen = 0
            collected_this_page = 0
            for item in visible:
                if not _wait_pause(pause, cancel, on_progress):
                    break
                if _cancelled(cancel):
                    break

                gid = str(item.get("goods_id") or "")
                if not gid:
                    continue
                if gid not in ever_seen_gids:
                    ever_seen_gids.add(gid)
                    new_on_screen += 1
                if gid in handled_gids:
                    continue
                handled_gids.add(gid)

                title = str(item.get("title") or "")
                label = title[:40] or gid[:24]
                idx = _item_index(item)
                idx_tag = f"[화면#{idx}]" if idx >= 0 else ""

                if gid in skip_excluded:
                    skipped_excluded += 1
                    _log(on_progress, f"제외 패스{idx_tag}: {label}")
                    continue
                if gid in skip_known:
                    skipped_known += 1
                    if skipped_known <= 8 or skipped_known % 30 == 0:
                        _log(on_progress, f"이미수집 패스{idx_tag}: {label}")
                    continue

                n_show = len(products) + 1
                limit_txt = "∞" if unlimited else str(max_new)
                _log(on_progress, f"신규 열기 ({n_show}/{limit_txt}){idx_tag}: {label}")
                detail = _open_collect_one(
                    session,
                    item,
                    on_progress=on_progress,
                    cancel=cancel,
                    open_wait=open_wait,
                    detail_settle=detail_settle,
                    back_wait=back_wait,
                )
                if detail:
                    code = (detail.search_code or "").strip()
                    if (detail.goods_id and detail.goods_id in skip_excluded) or (
                        code and code in skip_codes
                    ):
                        skipped_excluded += 1
                        _log(on_progress, "  제외/등록됨 — 저장 생략")
                    else:
                        products.append(detail)
                        collected_this_page += 1
                        if detail.goods_id:
                            skip_known.add(detail.goods_id)
                        if code:
                            skip_codes.add(code)
                        if on_product:
                            try:
                                on_product(detail)
                            except Exception as e:
                                _log(on_progress, f"  저장 실패: {e}")
                        # Pick up skips from sync / other actions
                        if len(products) % 12 == 0:
                            reload_skips()
                        if not _sleep_interruptible(
                            between_items,
                            cancel=cancel,
                            pause=pause,
                            on_progress=on_progress,
                        ):
                            break
                        if len(products) % REST_EVERY_N == 0:
                            _log(
                                on_progress,
                                f"서버 부하 완화 — {REST_EVERY_N}건마다 {REST_SEC:.0f}초 휴식 "
                                f"(누적 신규 {len(products)})",
                            )
                            if not _sleep_interruptible(
                                REST_SEC,
                                cancel=cancel,
                                pause=pause,
                                on_progress=on_progress,
                            ):
                                break
                else:
                    if not _recover_server_busy(
                        session, cancel=cancel, pause=pause, on_progress=on_progress
                    ):
                        break
                    if not _sleep_interruptible(
                        AFTER_FAIL_SEC,
                        cancel=cancel,
                        pause=pause,
                        on_progress=on_progress,
                    ):
                        break
                if len(products) >= max_new:
                    break

            if _cancelled(cancel):
                break

            scroll = _page_down(session)
            settle = _wait_list_settle(
                session,
                seen_gids=ever_seen_gids,
                cancel=cancel,
                pause=pause,
                on_progress=on_progress,
            )
            if settle.get("cancelled"):
                break
            page += 1
            moved = bool(scroll.get("moved"))
            at_end = bool(scroll.get("atEnd"))
            loading_seen = bool(settle.get("loading_seen"))
            end_hint = bool(settle.get("end_hint"))

            # 로딩 중이었거나 회복했으면 '정체'로 세지 않음 (오판 종료 방지)
            if loading_seen or settle.get("recovered"):
                stagnant_pages = 0
            elif new_on_screen == 0 and collected_this_page == 0:
                stagnant_pages += 1
            else:
                stagnant_pages = 0

            _log(
                on_progress,
                f"PageDown #{page}: 화면신규ID {new_on_screen} · 수집 {collected_this_page} · "
                f"이동={moved}({scroll.get('via')}) · 로딩={'Y' if loading_seen else 'N'} · "
                f"정체 {stagnant_pages}/10 · 누적신규 {len(products)} · "
                f"확인ID {len(ever_seen_gids)}",
            )

            if end_hint and new_on_screen == 0 and collected_this_page == 0:
                _log(on_progress, "목록 끝 문구 감지 — 종료")
                break

            # End of feed: no new IDs for several downs and scroll stuck / at end
            # (스피너/로딩 직후에는 위에서 stagnant를 리셋함)
            if stagnant_pages >= 10 and (not moved or at_end):
                _log(on_progress, "목록 끝 — 더 이상 새 상품 ID가 없어 종료")
                break
            if stagnant_pages >= 14:
                _log(on_progress, "새 상품이 계속 안 보임 — 종료")
                break

        return (
            products,
            f"자동 수집 완료: 신규 {len(products)}건"
            f" (이미수집 패스 {skipped_known}, 제외 패스 {skipped_excluded},"
            f" 확인 상품ID {len(ever_seen_gids)}개, PageDown {page}회)",
        )
    finally:
        session.close()

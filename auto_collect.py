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

# 한 화면만 내려감 — 맨 끝까지 끝없이 밀지 않음
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
  const before = best.scrollTop || 0;
  const step = Math.max(Math.floor((best.clientHeight || 640) * 0.9), 500);
  best.scrollTop = before + step;
  const after = best.scrollTop || 0;
  return { moved: after > before + 10, before, after, step };
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
    try:
        return int(item.get("index", -1))
    except (TypeError, ValueError):
        return -1


def _scroll_after_index(
    session: CdpSession,
    after_index: int,
    *,
    on_progress: ProgressCb | None,
    cancel: threading.Event | None,
    max_scrolls: int = 80,
) -> int:
    """
    Page-down until a card with data-index > after_index is visible.
    Returns the highest visible index after seeking (or -1).
    """
    if after_index < 0:
        return -1

    _log(
        on_progress,
        f"이어서 수집: 마지막 인덱스 #{after_index} 다음부터 보이도록 스크롤…",
    )
    try:
        session.evaluate(SCROLL_TOP_JS)
        time.sleep(0.35)
    except Exception:
        pass

    best_seen = -1
    stagnant = 0
    for i in range(max_scrolls):
        if _cancelled(cancel):
            break
        visible = _list_items_now(session)
        idxs = [_item_index(it) for it in visible if _item_index(it) >= 0]
        max_i = max(idxs) if idxs else -1
        min_i = min(idxs) if idxs else -1
        if max_i > best_seen:
            best_seen = max_i
            stagnant = 0
        else:
            stagnant += 1

        # Target reached: next index is on screen
        if any(idx > after_index for idx in idxs):
            _log(
                on_progress,
                f"스크롤 완료 — 화면 인덱스 {min_i}~{max_i} (목표 > #{after_index})",
            )
            return max_i

        try:
            scroll = session.evaluate(SCROLL_PAGE_JS) or {}
        except Exception:
            scroll = {"moved": False}
        time.sleep(0.4)
        moved = bool(scroll.get("moved"))
        if not moved:
            _log(
                on_progress,
                f"스크롤 끝 — 최대 인덱스 #{best_seen} (목표 > #{after_index})",
            )
            break
        if stagnant >= 5:
            _log(on_progress, "인덱스가 더 이상 안 올라감 — 현재 위치에서 계속")
            break
        if (i + 1) % 10 == 0:
            _log(on_progress, f"  …스크롤 중 (현재 최대 #{max_i})")

    return best_seen


def walk_list_details(
    *,
    on_progress: ProgressCb | None = None,
    on_product: Callable[[ParsedProduct], None] | None = None,
    cancel: threading.Event | None = None,
    excluded_goods_ids: set[str] | None = None,
    excluded_search_codes: set[str] | None = None,
    known_goods_ids: set[str] | None = None,
    max_items: int = 40,
    scroll_rounds: int = 30,
    open_wait: float = 10.0,
    detail_settle: float = 1.1,
    back_wait: float = 8.0,
    between_items: float = 0.3,
    resume_after_index: int | None = None,
    get_cursor: Callable[[str], int] | None = None,
    on_cursor: Callable[[str, int], None] | None = None,
) -> tuple[list[ParsedProduct], str]:
    """
    Viewport walk (not scroll-to-absolute-end):
      resume after last data-index → visible cards → collect only new → page-down
    Stop when several pages yield no new goods_id, or 1-run new limit reached.
    Already collected / excluded goods_id are never clicked.
    """
    skip_excluded = set(excluded_goods_ids or set())
    skip_codes = set(excluded_search_codes or set())
    skip_known = set(known_goods_ids or set())
    max_new = max_items if max_items and max_items > 0 else 40
    max_pages = max(6, scroll_rounds)

    session, msg = open_album_session()
    if not session:
        return [], msg

    products: list[ParsedProduct] = []
    skipped_excluded = 0
    skipped_known = 0
    seen_gids: set[str] = set()
    empty_pages = 0
    cursor = -1
    shop_id = ""

    def bump_cursor(item: dict) -> None:
        nonlocal cursor, shop_id
        idx = _item_index(item)
        sid = str(item.get("shop_id") or shop_id or "")
        if sid and not shop_id:
            shop_id = sid
        if idx < 0:
            return
        if idx > cursor:
            cursor = idx
            if on_cursor and shop_id:
                try:
                    on_cursor(shop_id, cursor)
                except Exception:
                    pass

    try:
        _log(on_progress, msg)
        _log(
            on_progress,
            f"방식: data-index 순서(0→1→2…) · 커서 이어서 · 1회 신규 최대 {max_new}개 · "
            f"이미수집/제외는 클릭 안 함",
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

        # Peek shop_id → load saved cursor → scroll past it
        peek = _list_items_now(session)
        if peek:
            shop_id = str(peek[0].get("shop_id") or "")
        if resume_after_index is not None:
            cursor = int(resume_after_index)
        elif get_cursor and shop_id:
            try:
                cursor = int(get_cursor(shop_id))
            except Exception:
                cursor = -1
        if cursor >= 0:
            _scroll_after_index(
                session,
                cursor,
                on_progress=on_progress,
                cancel=cancel,
            )
        else:
            _log(on_progress, "첫 수집 — 인덱스 #0부터")

        page = 0
        while page < max_pages:
            if _cancelled(cancel):
                _log(on_progress, "사용자 중지로 중단됨")
                break
            if len(products) >= max_new:
                _log(
                    on_progress,
                    f"1회 신규 한도 {max_new}개 도달 — 종료 "
                    f"(다음엔 #{cursor + 1}부터 이어서)",
                )
                break

            visible = _list_items_now(session)
            if not visible and page == 0:
                return [], (
                    "목록에서 상품 카드를 찾지 못했습니다. "
                    "친구 앨범 목록을 연 뒤 다시 시도하세요."
                )

            if not shop_id and visible:
                shop_id = str(visible[0].get("shop_id") or "")

            # Only handle cards after remembered cursor
            queue = [
                it
                for it in visible
                if _item_index(it) < 0 or _item_index(it) > cursor
            ]
            # If all visible are <= cursor, just scroll further
            new_ids = 0
            for item in queue:
                gid = str(item.get("goods_id") or "")
                if not gid or gid in seen_gids:
                    continue
                seen_gids.add(gid)
                new_ids += 1
                title = str(item.get("title") or "")
                label = title[:40] or gid[:24]
                idx = _item_index(item)

                if gid in skip_excluded:
                    skipped_excluded += 1
                    _log(on_progress, f"제외 패스[#{idx}]: {label}")
                    bump_cursor(item)
                    continue
                if gid in skip_known:
                    skipped_known += 1
                    if skipped_known <= 5 or skipped_known % 25 == 0:
                        _log(on_progress, f"이미수집 패스[#{idx}]: {label}")
                    bump_cursor(item)
                    continue
                if len(products) >= max_new:
                    break

                _log(
                    on_progress,
                    f"신규 열기 ({len(products) + 1}/{max_new})[#{idx}]: {label}",
                )
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
                    if (detail.goods_id and detail.goods_id in skip_excluded) or (
                        detail.search_code and detail.search_code in skip_codes
                    ):
                        skipped_excluded += 1
                        _log(on_progress, "  제외 목록 — 저장 생략")
                    else:
                        products.append(detail)
                        skip_known.add(detail.goods_id)
                        if on_product:
                            try:
                                on_product(detail)
                            except Exception as e:
                                _log(on_progress, f"  저장 실패: {e}")
                bump_cursor(item)
                time.sleep(between_items)
                if _cancelled(cancel):
                    break

            try:
                scroll = session.evaluate(SCROLL_PAGE_JS) or {}
            except Exception:
                scroll = {"moved": False}
            time.sleep(0.45)
            page += 1
            moved = bool(scroll.get("moved"))
            if new_ids == 0 and not queue:
                # visible all behind cursor — count as empty progress toward more scroll
                empty_pages += 1
            elif new_ids == 0:
                empty_pages += 1
            else:
                empty_pages = 0

            _log(
                on_progress,
                f"화면 {page}: 처리 {new_ids} · 커서 #{cursor} · 스크롤={moved} · 연속빈 {empty_pages}/3",
            )
            if empty_pages >= 3:
                _log(on_progress, "새 상품이 더 안 보여 종료 (끝없이 스크롤하지 않음)")
                break
            if not moved and new_ids == 0:
                _log(on_progress, "스크롤 끝 — 종료")
                break

        return (
            products,
            f"자동 수집 완료: 신규 {len(products)}건"
            f" (이미수집 패스 {skipped_known}, 제외 패스 {skipped_excluded},"
            f" 확인 id {len(seen_gids)}개, 커서 #{cursor})",
        )
    finally:
        session.close()

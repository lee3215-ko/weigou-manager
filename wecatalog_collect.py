# -*- coding: utf-8 -*-
"""Collect products from wecatalog.cn / weshop (szwego shop front)."""
from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass
from typing import Callable
from urllib.parse import parse_qs, urlparse

from auto_collect import CdpSession, _cancelled, _wait_pause
from collector import find_cdp_targets, normalize_image_url
from product_parse import ParsedProduct
from wecatalog_browser import WECATALOG_CDP_PORTS, is_wecatalog_cdp_up

# 실제 스크롤 가능 요소 찾기.
# wecatalog: [data-virtuoso-scroller] 는 scrollHeight==clientHeight 라 스크롤 안 됨.
# 진짜 스크롤은 document.scrollingElement / window.scrollY.
_FIND_REAL_SCROLLER_JS = r"""
  const findScroller = () => {
    const candidates = [
      document.scrollingElement,
      document.documentElement,
      document.body,
      document.querySelector('[data-virtuoso-scroller]'),
      document.querySelector('[class*="goods-fs__content"]'),
      document.querySelector('[class*="goods-ts__content"]'),
      document.querySelector('[class*="templateList"]'),
      document.querySelector('.content'),
    ].filter(Boolean);
    let best = candidates[0] || document.documentElement;
    let bestScore = -1;
    for (const el of candidates) {
      const sh = el.scrollHeight || 0;
      const ch = el.clientHeight || 1;
      const can = sh > ch + 40;
      const score = (can ? 1e9 : 0) + (sh - ch);
      if (score > bestScore) {
        bestScore = score;
        best = el;
      }
    }
    return best;
  };
  const isWindowScroller = (el) =>
    !el ||
    el === document.scrollingElement ||
    el === document.documentElement ||
    el === document.body;
  const getScrollTop = (el) => {
    if (isWindowScroller(el)) return window.scrollY || document.documentElement.scrollTop || 0;
    return el.scrollTop || 0;
  };
  const setScrollTop = (el, y) => {
    const v = Math.max(0, Number(y) || 0);
    window.scrollTo(0, v);
    try { document.documentElement.scrollTop = v; } catch (e) {}
    try {
      if (document.body) document.body.scrollTop = v;
    } catch (e2) {}
    if (el && !isWindowScroller(el)) {
      try { el.scrollTop = v; } catch (e3) {}
    }
  };
  const scrollByPx = (el, dy) => setScrollTop(el, getScrollTop(el) + dy);
"""

ProgressCb = Callable[[str], None]
WECATALOG_PRICE = "가격문의하기"

_RE_SIZE = re.compile(
    r"(?:크기|尺寸|사이즈|size)\s*[:：]\s*([^\n]+)",
    re.I,
)
# 长53.8✖️高33✖️宽19.8cm / 长×宽×高
_RE_SIZE_CN_LHW = re.compile(
    r"长\s*(\d+(?:\.\d+)?)\s*(?:cm|CM)?\s*"
    r"(?:[×xX*✖\uFE0F]|✖️)\s*"
    r"高\s*(\d+(?:\.\d+)?)\s*(?:cm|CM)?\s*"
    r"(?:[×xX*✖\uFE0F]|✖️)\s*"
    r"宽\s*(\d+(?:\.\d+)?)\s*(?:cm|CM)?",
    re.I,
)
_RE_SIZE_CN_LWH = re.compile(
    r"长\s*(\d+(?:\.\d+)?)\s*(?:cm|CM)?\s*"
    r"(?:[×xX*✖\uFE0F]|✖️)\s*"
    r"宽\s*(\d+(?:\.\d+)?)\s*(?:cm|CM)?\s*"
    r"(?:[×xX*✖\uFE0F]|✖️)\s*"
    r"高\s*(\d+(?:\.\d+)?)\s*(?:cm|CM)?",
    re.I,
)
_RE_SIZE_DIM = re.compile(
    r"(?:长|길이)\s*(\d+(?:\.\d+)?)\s*(?:cm|CM)?\s*[×xX*✖\uFE0F]\s*"
    r"(?:宽|너비)\s*(\d+(?:\.\d+)?)\s*(?:cm|CM)?\s*[×xX*✖\uFE0F]\s*"
    r"(?:高|높이)\s*(\d+(?:\.\d+)?)\s*(?:cm|CM)?",
    re.I,
)


@dataclass
class WecatalogUrl:
    base_url: str
    album_id: str
    tag_id: str | None = None


RESOLVE_WESHOP_CONTEXT_JS = r"""
(() => {
  const qs = new URLSearchParams(location.search);
  const tagFromUrl = qs.get('tagId') || qs.get('tagid') || '';
  const resources = performance.getEntriesByType('resource')
    .map((e) => e.name)
    .filter((u) => u.includes('/album/personal/all') && u.includes('albumId='));
  let albumId = '';
  let tagId = tagFromUrl;
  let transLang = 'ko';
  let templateUrl = '';
  for (let i = resources.length - 1; i >= 0; i--) {
    try {
      const u = new URL(resources[i]);
      const aid = u.searchParams.get('albumId') || '';
      if (!aid) continue;
      albumId = aid;
      templateUrl = resources[i];
      tagId = u.searchParams.get('tagId') || u.searchParams.get('tagid') || tagId;
      transLang = u.searchParams.get('transLang') || transLang;
      break;
    } catch (e) {}
  }
  if (!albumId) {
    const m = (location.pathname || '').match(
      /\/weshop\/(?:goods_list|store|goods)\/([^/?#]+)/i
    );
    if (m) albumId = m[1];
  }
  if (!albumId || /^A\d{10,}$/i.test(albumId)) {
    const shopHref =
      (document.querySelector('a[href*="/weshop/store/"]') || {}).getAttribute?.('href') ||
      (document.querySelector('a[href*="/weshop/goods_list/"]') || {}).getAttribute?.('href') ||
      '';
    const sm = shopHref.match(/\/weshop\/(?:store|goods_list)\/([^/?#]+)/i);
    if (sm && sm[1].startsWith('_')) albumId = sm[1];
  }
  for (let i = resources.length - 1; i >= 0; i--) {
    try {
      const u = new URL(resources[i]);
      const aid = u.searchParams.get('albumId') || '';
      if (aid.startsWith('_')) {
        albumId = aid;
        templateUrl = resources[i];
        tagId = u.searchParams.get('tagId') || u.searchParams.get('tagid') || tagId;
        transLang = u.searchParams.get('transLang') || transLang;
        break;
      }
    } catch (e) {}
  }
  if (!albumId.startsWith('_')) {
    for (const a of document.querySelectorAll('a[href*="/weshop/goods/"]')) {
      const m = String(a.getAttribute('href') || '').match(
        /\/weshop\/goods\/(_d[^/?#]+)\//i
      );
      if (m) {
        albumId = m[1];
        break;
      }
    }
  }
  return {
    albumId,
    tagId: tagId || null,
    transLang,
    templateUrl,
    href: location.href,
  };
})()
"""


def _fetch_list_page_js(ctx: dict, page_ts: str = "") -> str:
    ctx_json = json.dumps(ctx)
    ts = json.dumps(page_ts or "")
    return f"""
(async () => {{
  const ctx = {ctx_json};
  const pageTs = {ts};
  const origin = location.origin;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 45000);
  try {{
    let qs;
    if (ctx.templateUrl) {{
      qs = new URL(ctx.templateUrl, origin).searchParams;
    }} else {{
      qs = new URLSearchParams({{
        searchValue: '',
        searchImg: '',
        startDate: '',
        endDate: '',
        transLang: ctx.transLang || 'ko',
        requestDataType: '',
        slipType: '0',
        timestamp: '',
        tagUnion: 'false',
        albumId: ctx.albumId || '',
      }});
    }}
    if (ctx.albumId) qs.set('albumId', String(ctx.albumId));
    if (ctx.tagId) qs.set('tagId', String(ctx.tagId));
    if (ctx.transLang) qs.set('transLang', String(ctx.transLang));
    qs.set('timestamp', pageTs || '');
    const r = await fetch(origin + '/album/personal/all?' + qs.toString(), {{
      credentials: 'include',
      signal: controller.signal,
    }});
    if (!r.ok) {{
      return {{ items: [], pageTimestamp: '', isLoadMore: false, err: 'HTTP ' + r.status }};
    }}
    const j = await r.json();
    const res = j.result || {{}};
    const pag = res.pagination || {{}};
    return {{
      items: res.items || [],
      pageTimestamp: String(pag.pageTimestamp || ''),
      isLoadMore: !!pag.isLoadMore,
      err: null,
    }};
  }} catch (e) {{
    return {{
      items: [],
      pageTimestamp: '',
      isLoadMore: false,
      err: String(e && e.message ? e.message : e),
    }};
  }} finally {{
    clearTimeout(timer);
  }}
}})()
"""


def _fetch_page_js(
    album_id: str,
    trans_lang: str,
    page_ts: str = "",
    tag_id: str | None = None,
) -> str:
    ctx: dict = {
        "albumId": album_id,
        "transLang": trans_lang or "ko",
        "tagId": tag_id,
        "templateUrl": "",
    }
    return _fetch_list_page_js(ctx, page_ts)


# footer 문구는 목록 맨 위에서도 DOM에 있음(화면 밖 top≈수만). 화면에 들어올 때만 끝.
_NO_MORE_VISIBLE_FN_JS = r"""
  const noMoreVisible = () => {
    const footers = [
      ...document.querySelectorAll('.wgoo-footer'),
      ...document.querySelectorAll('[class*="wgoo-footer"]'),
    ];
    const vh = window.innerHeight || 0;
    for (const footer of footers) {
      const t = ((footer.innerText || footer.textContent) || '').replace(/\s+/g, '');
      if (!t) continue;
      if (
        !/더이상데이터가없습니다/.test(t) &&
        !/没有更多数据/.test(t) &&
        !/没有更多/.test(t) &&
        !/沒有更多/.test(t)
      ) {
        continue;
      }
      const rect = footer.getBoundingClientRect();
      if (rect.height <= 0) continue;
      if (rect.top < vh + 120 && rect.bottom > -40) return true;
    }
    return false;
  };
"""

COLLECT_VISIBLE_CARDS_JS = (
    r"""
(async () => {
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const byGid = new Map();

  const setScrollTop = (y) => {
    const v = Math.max(0, y);
    window.scrollTo(0, v);
    try { document.documentElement.scrollTop = v; } catch (e) {}
  };
  const scrollByPx = (dy) => {
    window.scrollBy(0, dy);
  };

  const pickImgUrl = (img) => {
    const srcset = img.getAttribute('srcset') || '';
    const fromSet = srcset
      .split(',')
      .map((s) => (s.trim().split(/\s+/)[0] || '').trim())
      .find(Boolean);
    return (
      img.currentSrc ||
      img.src ||
      img.getAttribute('data-src') ||
      fromSet ||
      ''
    ).split('?')[0];
  };

  const isShopCard = (el) =>
    el &&
    el.getAttribute('data-index') != null &&
    (String(el.className || '').includes('shopItem') || !!el.querySelector('img'));
"""
    + _NO_MORE_VISIBLE_FN_JS
    + r"""
  const grab = () => {
    document.querySelectorAll('[data-index]').forEach((card) => {
      if (!isShopCard(card)) return;
      const di = String(card.getAttribute('data-index') || '');
      if (!di) return;
      const titleEl = card.querySelector(
        '[class*="ellipsis-two"], [class*="index_title"], [class*="title__"], [class*="EllipsisText"]'
      );
      const title = titleEl ? (titleEl.innerText || '').trim() : '';
      const imgs = [];
      card.querySelectorAll('img').forEach((img) => {
        const u = pickImgUrl(img);
        if (u && /xcimg|szwego|wecatalog/i.test(u)) imgs.push(u);
      });
      const key = `idx:${di}`;
      const prev = byGid.get(key);
      if (!prev || (imgs.length && !(prev.imgsSrc || []).length)) {
        byGid.set(key, {
          goods_id: '',
          shop_id: '',
          dataIndex: di,
          title: title || (prev && prev.title) || '',
          imgsSrc: imgs.length ? imgs : (prev && prev.imgsSrc) || [],
        });
      }
    });
  };

  try {
    setScrollTop(0);
  } catch (e) {}
  await sleep(400);
  grab();

  let stagnant = 0;
  let lastSize = byGid.size;
  let lastMax = -1;
  for (let i = 0; i < 400; i++) {
    if (noMoreVisible()) {
      grab();
      break;
    }
    const before = window.scrollY || 0;
    const step = Math.max((window.innerHeight || 640) * 0.92, 520);
    scrollByPx(step);
    if ((window.scrollY || 0) <= before + 8) {
      setScrollTop(before + step);
    }
    await sleep(480);
    let loading = document.querySelector(
      '.wgoo-loading-icon, [class*="loading-icon-circle"]'
    );
    for (let w = 0; w < 8 && loading; w++) {
      await sleep(350);
      loading = document.querySelector(
        '.wgoo-loading-icon, [class*="loading-icon-circle"]'
      );
    }
    grab();
    let curMax = -1;
    byGid.forEach((row) => {
      const n = Number(row.dataIndex);
      if (!Number.isNaN(n) && n > curMax) curMax = n;
    });
    if (byGid.size > lastSize || curMax > lastMax) {
      stagnant = 0;
      lastSize = byGid.size;
      lastMax = curMax;
    } else {
      stagnant += 1;
    }
    if (noMoreVisible()) break;
    if (stagnant >= 12 && !loading) break;
  }

  grab();
  const sawEnd = noMoreVisible();
  try {
    setScrollTop(0);
  } catch (e3) {}
  await sleep(400);

  return {
    cards: [...byGid.values()].sort((a, b) => {
      const ai = Number(a.dataIndex);
      const bi = Number(b.dataIndex);
      if (!Number.isNaN(ai) && !Number.isNaN(bi) && ai !== bi) return ai - bi;
      return String(a.dataIndex || '').localeCompare(String(b.dataIndex || ''));
    }),
    noMore: sawEnd,
    count: byGid.size,
  };
})()
"""
)

WAIT_STORE_LIST_JS = r"""
(() => {
  if (document.querySelector('[data-index]')) return true;
  if (document.querySelectorAll('[data-search-bury-info]').length > 0) return true;
  if (document.querySelector('[class*="shopItem"][data-index], [class*="shopItem-"][data-index]')) {
    return true;
  }
  return !!document.querySelector('a[href*="/weshop/goods/"], a[href*="/weshop/product/"]');
})()
"""

# 현재 DOM에 보이는 카드만 (스크롤/맨위복귀 없음)
GRAB_VISIBLE_CARDS_JS = r"""
(() => {
  const pickImgUrl = (img) => {
    const srcset = img.getAttribute('srcset') || '';
    const fromSet = srcset
      .split(',')
      .map((s) => (s.trim().split(/\s+/)[0] || '').trim())
      .find(Boolean);
    return (
      img.currentSrc ||
      img.src ||
      img.getAttribute('data-src') ||
      fromSet ||
      ''
    ).split('?')[0];
  };
  const isShopCard = (el) =>
    el &&
    el.getAttribute('data-index') != null &&
    (String(el.className || '').includes('shopItem') || !!el.querySelector('img'));
  const cards = [];
  document.querySelectorAll('[data-index]').forEach((card) => {
    if (!isShopCard(card)) return;
    const di = String(card.getAttribute('data-index') || '');
    if (!di) return;
    const titleEl = card.querySelector(
      '[class*="ellipsis-two"], [class*="index_title"], [class*="title__"], [class*="EllipsisText"]'
    );
    const imgs = [];
    card.querySelectorAll('img').forEach((img) => {
      const u = pickImgUrl(img);
      if (u && /xcimg|szwego|wecatalog/i.test(u)) imgs.push(u);
    });
    cards.push({
      goods_id: '',
      shop_id: '',
      dataIndex: di,
      title: titleEl ? (titleEl.innerText || '').trim() : '',
      imgsSrc: imgs,
    });
  });
  cards.sort((a, b) => {
    const ai = Number(a.dataIndex);
    const bi = Number(b.dataIndex);
    if (!Number.isNaN(ai) && !Number.isNaN(bi) && ai !== bi) return ai - bi;
    return String(a.dataIndex || '').localeCompare(String(b.dataIndex || ''));
  });
  return { cards, count: cards.length };
})()
"""

# footer 문구는 목록 맨 위에서도 DOM에 있음(화면 밖 top≈수만). 화면에 들어올 때만 끝.
_NO_MORE_VISIBLE_FN_JS = r"""
  const noMoreVisible = () => {
    const footers = [
      ...document.querySelectorAll('.wgoo-footer'),
      ...document.querySelectorAll('[class*="wgoo-footer"]'),
    ];
    const vh = window.innerHeight || 0;
    for (const footer of footers) {
      const t = ((footer.innerText || footer.textContent) || '').replace(/\s+/g, '');
      if (!t) continue;
      if (
        !/더이상데이터가없습니다/.test(t) &&
        !/没有更多数据/.test(t) &&
        !/没有更多/.test(t) &&
        !/沒有更多/.test(t)
      ) {
        continue;
      }
      const rect = footer.getBoundingClientRect();
      if (rect.height <= 0) continue;
      // 화면(또는 바로 아래)에 들어와야 끝 — top만 크면 아직 아래쪽
      if (rect.top < vh + 120 && rect.bottom > -40) return true;
    }
    return false;
  };
"""

HAS_NO_MORE_DATA_JS = (
    r"""
(() => {
"""
    + _NO_MORE_VISIBLE_FN_JS
    + r"""
  return noMoreVisible();
})()
"""
)

MAX_DATA_INDEX_JS = r"""
(() => {
  let max = -1;
  document.querySelectorAll('[data-index]').forEach((el) => {
    const cn = String(el.className || '');
    if (!cn.includes('shopItem') && !el.querySelector('img')) return;
    const n = Number(el.getAttribute('data-index'));
    if (!Number.isNaN(n) && n > max) max = n;
  });
  return max;
})()
"""

FOCUS_WECATALOG_LIST_JS = r"""
(() => {
  // Virtuoso 는 스크롤 불가 — 포커스는 document/body (창 스크롤) 우선
  const candidates = [
    document.body,
    document.documentElement,
    document.scrollingElement,
    document.querySelector('[data-index]'),
    document.querySelector('[class*="goods-fs__content"]'),
    document.querySelector('[class*="goods-ts__content"]'),
    document.querySelector('[class*="templateList"]'),
    document.querySelector('.content'),
  ].filter(Boolean);
  const el = candidates[0];
  if (!el) return false;
  try {
    if (el.tabIndex < 0) el.tabIndex = -1;
    el.focus({ preventScroll: true });
  } catch (e) {}
  return true;
})()
"""

SCROLL_DOWN_NEW_CARDS_JS = r"""
(async (seenJson) => {
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const seen = new Set(
    Array.isArray(seenJson)
      ? seenJson
      : JSON.parse(typeof seenJson === 'string' ? seenJson || '[]' : '[]')
  );
  const isShopCard = (el) =>
    el &&
    el.getAttribute('data-index') != null &&
    (String(el.className || '').includes('shopItem') || !!el.querySelector('img'));
  const findScroller = () => {
    const candidates = [
      document.scrollingElement,
      document.documentElement,
      document.body,
      document.querySelector('[data-virtuoso-scroller]'),
      document.querySelector('[class*="goods-fs__content"]'),
      document.querySelector('[class*="goods-ts__content"]'),
      document.querySelector('[class*="templateList"]'),
      document.querySelector('.content'),
      document.querySelector('[class*="scroll"]'),
    ].filter(Boolean);
    let best = candidates[0] || document.documentElement;
    let bestScore = -1;
    for (const el of candidates) {
      const sh = el.scrollHeight || 0;
      const ch = el.clientHeight || 1;
      const canScroll = sh > ch + 40 ? 1e9 : 0;
      const score = canScroll + (sh - ch);
      if (score > bestScore) {
        bestScore = score;
        best = el;
      }
    }
    return best;
  };
  const noMoreVisible = () => {
    const footers = [
      ...document.querySelectorAll('.wgoo-footer'),
      ...document.querySelectorAll('[class*="wgoo-footer"]'),
    ];
    const vh = window.innerHeight || 0;
    for (const footer of footers) {
      const t = ((footer.innerText || footer.textContent) || '').replace(/\s+/g, '');
      if (!t) continue;
      if (
        !/더이상데이터가없습니다/.test(t) &&
        !/没有更多数据/.test(t) &&
        !/没有更多/.test(t) &&
        !/沒有更多/.test(t)
      ) continue;
      const rect = footer.getBoundingClientRect();
      if (rect.height <= 0) continue;
      if (rect.top < vh + 120 && rect.bottom > -40) return true;
    }
    return false;
  };

  const maxVisibleIndex = () => {
    let max = -1;
    document.querySelectorAll('[data-index]').forEach((el) => {
      if (!isShopCard(el)) return;
      const n = Number(el.getAttribute('data-index'));
      if (!Number.isNaN(n) && n > max) max = n;
    });
    return max;
  };
  const grabNew = () => {
    const pickImg = (img) =>
      (
        img.currentSrc ||
        img.src ||
        img.getAttribute('data-src') ||
        ''
      ).split('?')[0];
    const out = [];
    document.querySelectorAll('[data-index]').forEach((el) => {
      if (!isShopCard(el)) return;
      const di = String(el.getAttribute('data-index') || '');
      if (!di || seen.has(di)) return;
      const titleEl = el.querySelector(
        '[class*="ellipsis-two"], [class*="index_title"], [class*="title__"]'
      );
      const imgs = [];
      el.querySelectorAll('img').forEach((img) => {
        const u = pickImg(img);
        if (u && /xcimg|szwego|wecatalog/i.test(u)) imgs.push(u);
      });
      out.push({
        goods_id: '',
        shop_id: '',
        dataIndex: di,
        title: titleEl ? (titleEl.innerText || '').trim() : '',
        imgsSrc: imgs,
      });
    });
    return out;
  };
  const doScroll = (scroller, step) => {
    const before = window.scrollY || document.documentElement.scrollTop || 0;
    try {
      window.focus();
    } catch (e) {}
    window.scrollBy(0, step);
    try {
      document.documentElement.scrollTop = before + step;
    } catch (e2) {}
    const after = window.scrollY || document.documentElement.scrollTop || 0;
    return after > before + 8;
  };

  const scroller = findScroller();
  const out = [];
  let stagnant = 0;
  let movedAny = false;
  let lastMax = maxVisibleIndex();
  // 한 번에 충분히 내려 새 data-index 확보 (중도 포기 방지)
  for (let round = 0; round < 20; round++) {
    if (noMoreVisible() && !grabNew().length) break;
    const step = Math.max((window.innerHeight || 640) * 0.95, 560);
    const moved = doScroll(scroller, step);
    if (moved) movedAny = true;
    await sleep(550);
    let loading = document.querySelector(
      '.wgoo-loading-icon, [class*="loading-icon-circle"]'
    );
    for (let w = 0; w < 10 && loading; w++) {
      await sleep(400);
      loading = document.querySelector(
        '.wgoo-loading-icon, [class*="loading-icon-circle"]'
      );
    }
    await sleep(300);
    const batch = grabNew();
    const curMax = maxVisibleIndex();
    if (batch.length) {
      out.push(...batch);
      batch.forEach((c) => seen.add(c.dataIndex));
      stagnant = 0;
    } else if (curMax > lastMax) {
      lastMax = curMax;
      stagnant = 0;
    } else {
      stagnant += 1;
    }
    lastMax = Math.max(lastMax, curMax);
    if (noMoreVisible()) break;
    // footer 없이 stagnant만으로 끝내지 않음 — 조금 더 시도
    if (stagnant >= 8 && !loading) break;
  }
  const atEnd = noMoreVisible();
  return {
    cards: out,
    atEnd,
    noMore: atEnd,
    moved: movedAny,
    maxIndex: lastMax,
    scrollTop: window.scrollY || document.documentElement.scrollTop || 0,
  };
})(%s)
"""

SCROLL_TO_CARD_INDEX_JS = (
    r"""
(async (dataIndex) => {
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
"""
    + _FIND_REAL_SCROLLER_JS
    + r"""
  const want = String(dataIndex);
  const targetIdx = Number(dataIndex);
  const isShopCard = (el) =>
    el &&
    el.getAttribute('data-index') != null &&
    (String(el.className || '').includes('shopItem') || !!el.querySelector('img'));
  const findCard = () => {
    const el = document.querySelector('[data-index="' + want + '"]');
    return isShopCard(el) ? el : null;
  };
  const visibleRange = () => {
    const nums = [];
    document.querySelectorAll('[data-index]').forEach((el) => {
      if (!isShopCard(el)) return;
      const n = Number(el.getAttribute('data-index'));
      if (!Number.isNaN(n)) nums.push(n);
    });
    if (!nums.length) return { min: -1, max: -1 };
    return { min: Math.min(...nums), max: Math.max(...nums) };
  };
  const avgRowHeight = () => {
    const el = document.querySelector('[class*="shopItem"][data-index], [data-index]');
    if (!el) return 280;
    return Math.max(180, (el.getBoundingClientRect().height || 0) + 12);
  };
  const colsPerRow = () => {
    const w = window.innerWidth || 800;
    if (w >= 1280) return 4;
    if (w >= 960) return 3;
    return 2;
  };

  const scroller = findScroller();
  let card = findCard();
  if (!card && !Number.isNaN(targetIdx)) {
    const row = Math.floor(Math.max(0, targetIdx) / colsPerRow());
    setScrollTop(scroller, Math.max(0, row * avgRowHeight() - 40));
    await sleep(400);
    card = findCard();
  }
  for (let attempt = 0; attempt < 40 && !card; attempt++) {
    card = findCard();
    if (card) break;
    const range = visibleRange();
    const step = Math.max((window.innerHeight || 640) * 0.85, avgRowHeight() * 2);
    if (range.max >= 0 && !Number.isNaN(targetIdx) && targetIdx > range.max) {
      scrollByPx(scroller, step);
    } else if (range.min >= 0 && !Number.isNaN(targetIdx) && targetIdx < range.min) {
      scrollByPx(scroller, -step);
    } else if (range.min < 0) {
      const row = Math.floor(Math.max(0, targetIdx) / colsPerRow());
      setScrollTop(scroller, Math.max(0, row * avgRowHeight()));
    } else {
      scrollByPx(scroller, step);
    }
    await sleep(280);
  }
  if (!card) return { ok: false, reason: 'no_card', dataIndex: want, range: visibleRange(), scrollTop: getScrollTop(scroller) };
  const pad = 80;
  const cardRect = card.getBoundingClientRect();
  setScrollTop(scroller, getScrollTop(scroller) + cardRect.top - pad);
  await sleep(300);
  return { ok: true, dataIndex: want, scrollTop: getScrollTop(scroller) };
})(%s)
"""
)

SCROLL_LIST_TOP_JS = (
    r"""
(async () => {
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
"""
    + _FIND_REAL_SCROLLER_JS
    + r"""
  setScrollTop(findScroller(), 0);
  await sleep(400);
  return { ok: true, scrollTop: getScrollTop(findScroller()) };
})()
"""
)

CLICK_CARD_SIMPLE_JS = r"""
(() => {
  const want = %s;
  const card = document.querySelector('[data-index="' + want + '"]');
  if (!card) return { ok: false, reason: 'no_card' };
  const target =
    card.querySelector('img') ||
    card.querySelector('[class*="imgBox"]') ||
    card.querySelector('[class*="main"]') ||
    card;
  const fire = (el) => {
    try {
      el.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true, cancelable: true, pointerId: 1, pointerType: 'mouse' }));
    } catch (e0) {}
    try {
      el.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true, view: window }));
    } catch (e1) {}
    try {
      el.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, cancelable: true, view: window }));
    } catch (e2) {}
    try {
      el.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window }));
    } catch (e3) {}
    try {
      el.click();
    } catch (e4) {}
  };
  fire(target);
  fire(card);
  const r = target.getBoundingClientRect();
  return {
    ok: true,
    dataIndex: want,
    x: Math.round(r.left + r.width / 2),
    y: Math.round(r.top + r.height / 2),
  };
})()
"""

CARD_BOUNDS_JS = r"""
(() => {
  const want = %s;
  const card = document.querySelector('[data-index="' + want + '"]');
  if (!card) return null;
  const target = card.querySelector('img') || card;
  const r = target.getBoundingClientRect();
  if (r.width < 8 || r.height < 8) return null;
  return {
    x: Math.round(r.left + r.width / 2),
    y: Math.round(r.top + r.height / 2),
    w: Math.round(r.width),
    h: Math.round(r.height),
  };
})()
"""

CLICK_CARD_BY_INDEX_JS = (
    r"""
(async (dataIndex) => {
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
"""
    + _FIND_REAL_SCROLLER_JS
    + r"""
  const want = String(dataIndex);
  const targetIdx = Number(dataIndex);
  const isShopCard = (el) =>
    el &&
    el.getAttribute('data-index') != null &&
    (String(el.className || '').includes('shopItem') || !!el.querySelector('img'));
  const findCard = () => {
    const el = document.querySelector('[data-index="' + want + '"]');
    return isShopCard(el) ? el : null;
  };
  const visibleRange = () => {
    const nums = [];
    document.querySelectorAll('[data-index]').forEach((el) => {
      if (!isShopCard(el)) return;
      const n = Number(el.getAttribute('data-index'));
      if (!Number.isNaN(n)) nums.push(n);
    });
    if (!nums.length) return { min: -1, max: -1 };
    return { min: Math.min(...nums), max: Math.max(...nums) };
  };
  const avgRowHeight = () => {
    const el = document.querySelector('[class*="shopItem"][data-index], [data-index]');
    if (!el) return 280;
    const h = el.getBoundingClientRect().height || 0;
    return Math.max(180, h + 12);
  };
  const colsPerRow = () => {
    const w = window.innerWidth || 800;
    if (w >= 1280) return 4;
    if (w >= 960) return 3;
    return 2;
  };
  const scroller = findScroller();
  let card = findCard();
  if (!card && !Number.isNaN(targetIdx)) {
    const row = Math.floor(Math.max(0, targetIdx) / colsPerRow());
    setScrollTop(scroller, Math.max(0, row * avgRowHeight() - 40));
    await sleep(400);
    card = findCard();
  }
  for (let attempt = 0; attempt < 30 && !card; attempt++) {
    card = findCard();
    if (card) break;
    const range = visibleRange();
    const step = Math.max((window.innerHeight || 640) * 0.9, avgRowHeight() * 2);
    if (range.max >= 0 && !Number.isNaN(targetIdx) && targetIdx > range.max) {
      scrollByPx(scroller, step);
    } else if (range.min >= 0 && !Number.isNaN(targetIdx) && targetIdx < range.min) {
      scrollByPx(scroller, -step);
    } else if (range.min < 0) {
      const row = Math.floor(Math.max(0, targetIdx) / colsPerRow());
      setScrollTop(scroller, Math.max(0, row * avgRowHeight()));
    } else {
      scrollByPx(scroller, step);
    }
    await sleep(260);
  }
  if (!card) {
    return { ok: false, reason: 'no_card', dataIndex: want, range: visibleRange(), scrollTop: getScrollTop(scroller) };
  }
  const pad = 80;
  const cardRect = card.getBoundingClientRect();
  setScrollTop(scroller, getScrollTop(scroller) + cardRect.top - pad);
  await sleep(250);
  const target = card.querySelector('img') || card.querySelector('[class*="imgBox"]') || card;
  try { target.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window })); } catch (e) {}
  try { if (typeof target.click === 'function') target.click(); } catch (e2) {}
  try { if (typeof card.click === 'function') card.click(); } catch (e3) {}
  const r = target.getBoundingClientRect();
  return {
    ok: true,
    dataIndex: want,
    x: Math.round(r.left + r.width / 2),
    y: Math.round(r.top + r.height / 2),
    scrollTop: getScrollTop(scroller),
  };
})(%s)
"""
)

SCREEN_DIAG_JS = r"""
(() => {
  const imgs = document.querySelectorAll('img').length;
  const bury = document.querySelectorAll('[data-search-bury-info]').length;
  const shopItems = document.querySelectorAll(
    '[class*="shopItem"][data-index], [class*="shopItem-"][data-index]'
  ).length;
  const goodsLinks = document.querySelectorAll('a[href*="/weshop/goods/"]').length;
  const title = (document.title || '').trim();
  return {
    href: location.href,
    title,
    bury,
    shopItems,
    goodsLinks,
    imgs,
  };
})()
"""

EXTRACT_DETAIL_JS = r"""
(() => {
  const rich = document.querySelector('[class*="RichText_RichText"]');
  const richText = rich ? (rich.innerText || '').trim() : '';
  const attrs = [];
  document.querySelectorAll('[class*="GoodsAttribute_GoodsAttribute"]').forEach((el) => {
    const label = ((el.querySelector('[class*="label"]') || {}).textContent || '').trim();
    const value = ((el.querySelector('[class*="value"]') || {}).textContent || '').trim();
    const clip = el.querySelector('[data-clipboard-text]');
    const clipText = clip ? (clip.getAttribute('data-clipboard-text') || '').trim() : '';
    if (label || value || clipText) attrs.push({ label, value, clip: clipText });
  });
  const goodsTags = [];
  document
    .querySelectorAll(
      '[class*="AttributeTags"] a[href*="tagId="], a[class*="GoodsTag"][href*="tagId="]'
    )
    .forEach((a) => {
      const name = (a.innerText || a.textContent || '').trim();
      const href = a.getAttribute('href') || '';
      let tagId = '';
      try {
        const u = new URL(href, location.origin);
        tagId = u.searchParams.get('tagId') || u.searchParams.get('tagid') || '';
      } catch (e) {}
      if (name) goodsTags.push({ name, tagId });
    });
  const imgs = [];
  const seen = new Set();
  const add = (u) => {
    if (!u || typeof u !== 'string') return;
    const low = u.toLowerCase();
    if (!low.includes('xcimg.szwego.com') && !low.includes('img.szwego.com')) return;
    const base = u.split('?')[0];
    if (seen.has(base)) return;
    seen.add(base);
    imgs.push(base);
  };
  const imgRoots = [
    ...document.querySelectorAll('[class*="GridMedias"] img'),
    ...document.querySelectorAll('[class*="ProductSourceList"] img'),
  ];
  if (imgRoots.length) {
    imgRoots.forEach((img) => add(img.currentSrc || img.src || ''));
  } else {
    document.querySelectorAll('img').forEach((img) => {
      add(img.currentSrc || img.src || '');
    });
  }
  document.querySelectorAll('[style*="background-image"]').forEach((el) => {
    const st = el.style.backgroundImage || '';
    const m = st.match(/url\(["']?([^"')]+)/);
    if (m) add(m[1]);
  });
  return { richText, attrs, imgs, goodsTags };
})()
"""


_RE_WESHOP_ALBUM = re.compile(
    r"/weshop/(?:goods_list|store|goods)/([^/?#&]+)",
    re.I,
)


def _href_route_blob(href: str) -> str:
    parsed = urlparse(href or "")
    path = parsed.path or ""
    frag = (parsed.fragment or "").lstrip("#/")
    if "/weshop/" in path.lower():
        return path
    if frag and "/weshop/" in frag.lower():
        return "/" + frag.split("?", 1)[0]
    return path


def _tab_href(target: dict, diag: dict) -> str:
    return str(diag.get("href") or target.get("url") or "").strip()


def _is_weshop_list_url(href: str) -> bool:
    blob = _href_route_blob(href).lower()
    return bool(re.search(r"/weshop/(?:store|goods_list)/", blob, re.I))


def _weshop_tab_rank(target: dict, diag: dict) -> tuple[int, int, int]:
    href = _tab_href(target, diag)
    is_list = 1 if _is_weshop_list_url(href) else 0
    is_weshop = 1 if "/weshop/" in _href_route_blob(href).lower() else 0
    return (is_list, is_weshop, _visible_card_count(diag))


def parse_wecatalog_url(url: str) -> WecatalogUrl | None:
    raw = (url or "").strip()
    if not raw:
        return None
    parsed = urlparse(raw)
    if not parsed.scheme or not parsed.netloc:
        return None
    base_url = f"{parsed.scheme}://{parsed.netloc}"
    route = _href_route_blob(raw)
    m = _RE_WESHOP_ALBUM.search(route) or _RE_WESHOP_ALBUM.search(raw)
    if not m:
        return None
    album_id = m.group(1)
    qs = parse_qs(parsed.query or "")
    if not qs and parsed.fragment and "?" in parsed.fragment:
        qs = parse_qs(parsed.fragment.split("?", 1)[1])
    tag_id = None
    for key in ("tagId", "tagid", "tag_id"):
        if qs.get(key):
            tag_id = str(qs[key][0]).strip() or None
            break
    return WecatalogUrl(base_url=base_url, album_id=album_id, tag_id=tag_id)


def _probe_target(target: dict) -> dict:
    ws = target.get("webSocketDebuggerUrl")
    if not ws:
        return {}
    session: CdpSession | None = None
    try:
        session = CdpSession(str(ws), timeout=15.0)
        _prepare_session(session)
        diag = session.evaluate(SCREEN_DIAG_JS)
        return diag if isinstance(diag, dict) else {}
    except Exception:
        return {}
    finally:
        if session:
            session.close()


def _scan_page_tabs() -> list[tuple[dict, dict]]:
    rows: list[tuple[dict, dict]] = []
    for t in find_cdp_targets(WECATALOG_CDP_PORTS):
        if t.get("type") != "page" or not t.get("webSocketDebuggerUrl"):
            continue
        rows.append((t, _probe_target(t)))
    rows.sort(key=lambda x: _weshop_tab_rank(x[0], x[1]), reverse=True)
    return rows


def _visible_card_count(diag: dict) -> int:
    bury = int(diag.get("bury") or 0)
    shop_items = int(diag.get("shopItems") or 0)
    return max(bury, shop_items)


def _has_visible_product_cards(diag: dict) -> bool:
    return _visible_card_count(diag) > 0


def _pick_weshop_list_tab(
    scanned: list[tuple[dict, dict]],
) -> tuple[dict | None, dict]:
    weshop_rows = [
        (t, d) for t, d in scanned if _is_weshop_list_url(_tab_href(t, d))
    ]
    if not weshop_rows:
        return None, {}
    weshop_rows.sort(key=lambda x: _visible_card_count(x[1]), reverse=True)
    return weshop_rows[0]


def _format_cdp_tab_lines(scanned: list[tuple[dict, dict]], limit: int = 10) -> str:
    lines: list[str] = []
    for t, d in scanned[:limit]:
        title = str(d.get("title") or t.get("title") or "?")[:26]
        href = _tab_href(t, d)
        short = href if len(href) <= 72 else href[:72] + "…"
        if _is_weshop_list_url(href):
            tag = "weshop 목록"
        elif "wecatalog" in href.lower():
            tag = "wecatalog"
        elif "szwego.com/static" in href.lower():
            tag = "微购相册 홈 (weshop 아님)"
        else:
            tag = "weshop 아님"
        cards = _visible_card_count(d)
        lines.append(f"· {title} [{tag}] 카드 {cards}\n  {short}")
    return "\n".join(lines) if lines else "(탭 없음)"


def _no_weshop_tab_message(scanned: list[tuple[dict, dict]]) -> str:
    ports = ", ".join(str(p) for p in WECATALOG_CDP_PORTS)
    return (
        "wecatalog Chrome에 상품 목록 탭이 없습니다.\n\n"
        "wecatalog 수집은 微购相册(목록-상세 자동수집)과 별개입니다.\n"
        f"· [wecatalog Chrome] 실행 후 목록 URL을 이 창에서 여세요\n"
        f"· CDP 포트 {ports} (微购相册 9222와 분리)\n"
        "· 일반 크롬(디버그 없음)은 수집되지 않습니다\n\n"
        f"현재 wecatalog CDP 탭 ({len(scanned)}개):\n{_format_cdp_tab_lines(scanned)}"
    )


def _format_tab_scan_hint(scanned: list[tuple[dict, dict]], limit: int = 8) -> str:
    lines: list[str] = []
    for t, d in scanned[:limit]:
        title = str(d.get("title") or t.get("title") or "?")[:28]
        cards = _visible_card_count(d)
        lines.append(f"· {title} — 카드 {cards}건")
    return "\n".join(lines) if lines else "(탭 없음)"


def _no_product_cards_message(scanned: list[tuple[dict, dict]], diag: dict) -> str:
    title = str(diag.get("title") or "?")
    hint = _format_tab_scan_hint(scanned)
    return (
        f"상품 카드가 없습니다 (현재 탭: {title}).\n\n"
        "wecatalog Chrome에서 브랜드/태그를 눌러\n"
        "상품 그리드가 보이는 「제품 목록」 화면을 연 뒤 다시 [wecatalog 수집]을 눌러 주세요.\n"
        "· [wecatalog Chrome]으로 연 창에서만 동작합니다 (微购相册와 별개)\n"
        "· 탭 제목이 「럭스엣지」인 스토어 홈은 상품 목록이 아닙니다\n\n"
        f"열린 탭:\n{hint}"
    )


def pick_wecatalog_target() -> dict | None:
    scanned = _scan_page_tabs()
    if not scanned:
        return None
    target, _ = _pick_weshop_list_tab(scanned)
    if target:
        return target
    for t, d in scanned:
        if _is_weshop_list_url(_tab_href(t, d)):
            return t
    return None


def _prepare_session(session: CdpSession) -> None:
    for domain in ("Page", "Runtime"):
        try:
            session.call(f"{domain}.enable")
        except Exception:
            pass


def _connect_wecatalog_session(ws_url: str, retries: int = 3) -> CdpSession:
    last_exc: Exception | None = None
    for attempt in range(max(1, retries)):
        try:
            session = CdpSession(ws_url, timeout=120.0)
            _prepare_session(session)
            return session
        except Exception as exc:
            last_exc = exc
            if attempt + 1 < retries:
                time.sleep(0.8)
    raise RuntimeError(f"CDP 연결 실패: {last_exc}")


def open_wecatalog_session() -> tuple[CdpSession | None, str]:
    scanned = _scan_page_tabs()
    target, diag = _pick_weshop_list_tab(scanned)
    if not target or not target.get("webSocketDebuggerUrl"):
        if scanned:
            return None, _no_weshop_tab_message(scanned)
        return None, (
            "wecatalog Chrome 탭을 찾지 못했습니다.\n"
            "[wecatalog Chrome] 실행 후 목록 페이지를 열어 주세요."
        )
    try:
        session = _connect_wecatalog_session(str(target["webSocketDebuggerUrl"]))
    except Exception as exc:
        return None, str(exc)
    title = str(diag.get("title") or target.get("title") or "")
    cards = _visible_card_count(diag)
    href = _tab_href(target, diag)
    return session, (
        f"wecatalog CDP 연결 ({title or '제품 목록'}) · 카드 {cards}건\n"
        f"{href[:90]}"
    )


def _current_href(session: CdpSession) -> str:
    try:
        href = session.evaluate("location.href")
        return str(href or "")
    except Exception:
        return ""


def _parse_product_href(href: str) -> tuple[str, str]:
    for pattern in (
        r"/weshop/product/([^/?#]+)/([^/?#]+)",
        r"/weshop/goods/([^/?#]+)/([^/?#]+)",
    ):
        m = re.search(pattern, href or "", re.I)
        if m:
            return m.group(1).strip(), m.group(2).strip()
    return "", ""


def _is_product_detail_href(href: str) -> bool:
    return bool(
        re.search(r"/weshop/product/[^/?#]+/[^/?#]+", href or "", re.I)
        or re.search(r"/weshop/goods/[^/?#]+/[^/?#]+", href or "", re.I)
    )


def _product_detail_url(base_url: str, shop_id: str, goods_id: str) -> str:
    shop = (shop_id or "").strip()
    gid = (goods_id or "").strip()
    if not gid:
        return ""
    if not shop:
        shop = "_"
    return f"{base_url.rstrip('/')}/weshop/product/{shop}/{gid}"


def _scroll_list_top(session: CdpSession) -> None:
    old_timeout = session._timeout
    session._timeout = max(old_timeout, 30.0)
    try:
        session.evaluate(SCROLL_LIST_TOP_JS, await_promise=True)
    except Exception:
        pass
    finally:
        session._timeout = old_timeout


def _normalize_list_href(
    href: str,
    parsed_url: WecatalogUrl | None = None,
    ctx: dict | None = None,
) -> str:
    """tagId 필터 URL이 navigate 시 빠지지 않도록 보정."""
    h = (href or "").strip()
    if not h:
        return h
    tag_id = ""
    if ctx and ctx.get("tagId"):
        tag_id = str(ctx["tagId"]).strip()
    elif parsed_url and parsed_url.tag_id:
        tag_id = str(parsed_url.tag_id).strip()
    if tag_id and "tagid=" not in h.lower():
        sep = "&" if "?" in h else "?"
        return f"{h}{sep}tagId={tag_id}"
    return h


def _return_to_list(
    session: CdpSession,
    list_href: str,
    *,
    wait_sec: float = 10.0,
    scroll_to_index: str | None = None,
) -> None:
    """상세→목록 복귀. back 후 다음 카드가 DOM에 없으면 navigate로 강제 복구."""
    if not list_href or not _is_weshop_list_url(list_href):
        return
    want = str(scroll_to_index or "").strip() or None

    def _settle_on_list() -> bool:
        _wait_store_list(session, wait_sec=min(wait_sec, 6.0))
        time.sleep(0.35)
        if not want:
            return True
        if _scroll_to_card_index(session, want):
            return True
        # Virtuoso가 카드를 안 그리면 PageDown으로 찾아보기
        for _ in range(6):
            _page_down_wecatalog(session)
            time.sleep(0.3)
            if _scroll_to_card_index(session, want):
                return True
        return False

    cur = _current_href(session)
    if _is_weshop_list_url(cur):
        if _settle_on_list():
            return
        _restore_list(session, list_href, wait_sec=wait_sec, scroll_to_index=want)
        return

    if _is_product_detail_href(cur):
        try:
            session.evaluate(
                """(() => {
                  const sels = [
                    '[class*="NavBar"] [class*="back"]',
                    '[class*="navbar"] [class*="back"]',
                    '[class*="icon-back"]',
                    '[class*="Back"]',
                    'button[aria-label*="back" i]',
                    'div[class*="back"]',
                  ];
                  for (const s of sels) {
                    const el = document.querySelector(s);
                    if (!el) continue;
                    try { el.click(); return true; } catch (e) {}
                  }
                  return false;
                })()"""
            )
        except Exception:
            pass
        try:
            session.evaluate("window.history.back()")
        except Exception:
            pass
        deadline = time.time() + max(3.0, wait_sec)
        while time.time() < deadline:
            cur = _current_href(session)
            if _is_weshop_list_url(cur):
                if _settle_on_list():
                    return
                break
            time.sleep(0.2)

    _restore_list(
        session,
        list_href,
        wait_sec=wait_sec,
        scroll_to_index=want,
        force_top=False,
    )


def _restore_list(
    session: CdpSession,
    list_href: str,
    *,
    wait_sec: float = 10.0,
    scroll_to_index: str | None = None,
    force_top: bool = False,
) -> None:
    if not list_href or not _is_weshop_list_url(list_href):
        return
    _navigate(session, list_href, wait_sec=5.0)
    _wait_store_list(session, wait_sec=wait_sec)
    time.sleep(0.4)
    if scroll_to_index is not None and str(scroll_to_index).strip():
        want = str(scroll_to_index).strip()
        if not _scroll_to_card_index(session, want):
            for _ in range(8):
                _page_down_wecatalog(session)
                time.sleep(0.3)
                if _scroll_to_card_index(session, want):
                    break
    elif force_top:
        _scroll_list_top(session)


def _scroll_to_card_index(session: CdpSession, data_index: str) -> bool:
    di = str(data_index or "").strip()
    if not di:
        return False
    old_timeout = session._timeout
    session._timeout = max(old_timeout, 45.0)
    try:
        res = session.evaluate(
            SCROLL_TO_CARD_INDEX_JS % json.dumps(di),
            await_promise=True,
        )
    except Exception:
        return False
    finally:
        session._timeout = old_timeout
    return isinstance(res, dict) and bool(res.get("ok"))


def _ensure_list_ready(
    session: CdpSession,
    list_href: str,
    *,
    scroll_to_index: str | None = None,
) -> None:
    cur = _current_href(session)
    if _is_product_detail_href(cur) or not _is_weshop_list_url(cur):
        _return_to_list(
            session, list_href, wait_sec=12.0, scroll_to_index=scroll_to_index
        )
        return
    try:
        n = int(session.evaluate("document.querySelectorAll('[data-index]').length") or 0)
    except Exception:
        n = 0
    if n > 0:
        if scroll_to_index:
            _scroll_to_card_index(session, scroll_to_index)
        return
    _restore_list(
        session, list_href, wait_sec=12.0, scroll_to_index=scroll_to_index
    )


def _wait_detail_href(
    session: CdpSession,
    before_href: str,
    *,
    timeout: float = 12.0,
) -> str:
    deadline = time.time() + max(2.0, timeout)
    while time.time() < deadline:
        href = _current_href(session)
        if href and href != before_href and _is_product_detail_href(href):
            return href
        time.sleep(0.2)
    return ""


def _cdp_click_xy(session: CdpSession, x: int, y: int) -> None:
    try:
        session.call(
            "Input.dispatchMouseEvent",
            {
                "type": "mousePressed",
                "x": x,
                "y": y,
                "button": "left",
                "clickCount": 1,
            },
        )
        session.call(
            "Input.dispatchMouseEvent",
            {
                "type": "mouseReleased",
                "x": x,
                "y": y,
                "button": "left",
                "clickCount": 1,
            },
        )
    except Exception:
        pass


def _click_card_by_index(session: CdpSession, data_index: str) -> bool:
    di = str(data_index or "").strip()
    if not di:
        return False

    def _try_once() -> bool:
        _scroll_to_card_index(session, di)
        old_timeout = session._timeout
        session._timeout = max(old_timeout, 40.0)
        try:
            res = session.evaluate(
                CLICK_CARD_BY_INDEX_JS % json.dumps(di),
                await_promise=True,
            )
        except Exception:
            res = None
        finally:
            session._timeout = old_timeout
        if not isinstance(res, dict) or not res.get("ok"):
            # simple click fallback
            try:
                res2 = session.evaluate(
                    CLICK_CARD_SIMPLE_JS % json.dumps(di),
                    await_promise=False,
                )
            except Exception:
                res2 = None
            if isinstance(res2, dict) and res2.get("ok"):
                res = res2
            else:
                return False
        x, y = res.get("x"), res.get("y")
        if isinstance(x, (int, float)) and isinstance(y, (int, float)):
            _cdp_click_xy(session, int(x), int(y))
        else:
            try:
                box = session.evaluate(CARD_BOUNDS_JS % json.dumps(di))
                if isinstance(box, dict) and box.get("x") is not None:
                    _cdp_click_xy(session, int(box["x"]), int(box["y"]))
            except Exception:
                pass
        return True

    if _try_once():
        return True
    # 한 번 더: PageDown으로 카드 노출 유도
    for _ in range(4):
        _page_down_wecatalog(session)
        time.sleep(0.25)
        if _try_once():
            return True
    return False


def _extract_detail_current(session: CdpSession) -> dict | None:
    detail = session.evaluate(EXTRACT_DETAIL_JS)
    return detail if isinstance(detail, dict) else None


def _open_detail_via_card_click(
    session: CdpSession,
    card: dict,
    list_href: str,
    *,
    on_progress: ProgressCb | None = None,
    parse_detail: bool = True,
) -> tuple[dict | None, str, str]:
    """목록 카드 클릭 → /weshop/product/{shop}/{goods} URL에서 ID 확인 → (선택) 상세 파싱."""
    di = str(card.get("dataIndex") or "").strip()
    if not di:
        if on_progress:
            on_progress("data-index 없음 — 카드 클릭 불가")
        return None, "", ""

    for attempt in range(2):
        _ensure_list_ready(session, list_href, scroll_to_index=di)
        before = _current_href(session)
        if not _is_weshop_list_url(before):
            _restore_list(session, list_href, wait_sec=12.0, scroll_to_index=di)
            before = _current_href(session)

        clicked = _click_card_by_index(session, di)
        if not clicked:
            if attempt == 0:
                if on_progress:
                    on_progress(f"카드 미발견 → 목록 새로고침 후 재시도 (index={di})")
                _restore_list(session, list_href, wait_sec=12.0, scroll_to_index=di)
                continue
            if on_progress:
                on_progress(f"카드 클릭 실패 (index={di})")
            return None, "", ""

        href = _wait_detail_href(session, before, timeout=10.0)
        if not href:
            # 클릭은 됐는데 이동 없음 — 좌표 클릭 재시도
            try:
                box = session.evaluate(CARD_BOUNDS_JS % json.dumps(di))
                if isinstance(box, dict) and box.get("x") is not None:
                    _cdp_click_xy(session, int(box["x"]), int(box["y"]))
                    href = _wait_detail_href(session, before, timeout=8.0)
            except Exception:
                pass
        if not href:
            if attempt == 0:
                if on_progress:
                    on_progress(f"상세 미진입 → 목록 복구 후 재시도 (index={di})")
                _restore_list(session, list_href, wait_sec=12.0, scroll_to_index=di)
                continue
            if on_progress:
                on_progress(
                    f"상세 URL 대기 실패 (index={di}) · 현재 {_current_href(session)[:70]}"
                )
            return None, "", ""

        shop_id, goods_id = _parse_product_href(href)
        if not goods_id:
            if on_progress:
                on_progress(f"URL에서 goods_id 없음 — {href[:80]}")
            return None, "", ""

        if not parse_detail:
            return None, goods_id, shop_id

        _wait_detail(session, wait_sec=12.0)
        detail = _extract_detail_current(session)
        return detail, goods_id, shop_id

    return None, "", ""


def _max_data_index_seen(seen_indices: set[str]) -> int:
    mx = -1
    for di in seen_indices:
        try:
            n = int(str(di).strip())
        except ValueError:
            continue
        if n > mx:
            mx = n
    return mx


def _visible_max_data_index(session: CdpSession) -> int:
    try:
        n = session.evaluate(MAX_DATA_INDEX_JS)
        return int(n) if n is not None else -1
    except Exception:
        return -1


def _has_no_more_data(session: CdpSession) -> bool:
    try:
        return bool(session.evaluate(HAS_NO_MORE_DATA_JS))
    except Exception:
        return False


def _page_down_wecatalog(session: CdpSession) -> dict:
    """PageDown key (lazy-load) + scroll/wheel fallback."""
    return _nav_key_wecatalog(
        session,
        vk=34,
        code="PageDown",
        key="PageDown",
    )


def _end_key_wecatalog(session: CdpSession) -> dict:
    """End key — jump/load toward list bottom (preferred for discovery)."""
    return _nav_key_wecatalog(
        session,
        vk=35,
        code="End",
        key="End",
    )


def _nav_key_wecatalog(
    session: CdpSession,
    *,
    vk: int,
    code: str,
    key: str,
) -> dict:
    """Dispatch a navigation key; fall back to window.scrollBy if it did not move."""
    try:
        session.evaluate(FOCUS_WECATALOG_LIST_JS)
    except Exception:
        pass
    before = -1
    try:
        before = int(
            session.evaluate(
                """(() => {
                  return window.scrollY || document.documentElement.scrollTop || 0;
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
                    "windowsVirtualKeyCode": int(vk),
                    "nativeVirtualKeyCode": int(vk),
                    "code": code,
                    "key": key,
                },
            )
        time.sleep(0.45)
        after = int(
            session.evaluate(
                """(() => {
                  return window.scrollY || document.documentElement.scrollTop || 0;
                })()"""
            )
            or 0
        )
        if after > before + 8:
            return {"moved": True, "via": key, "before": before, "after": after}
    except Exception:
        pass
    # End/PageDown이 안 먹으면 창 스크롤로 한 화면 분량 이동
    try:
        after2 = int(
            session.evaluate(
                """(() => {
                  const beforeY = window.scrollY || 0;
                  const step = Math.max(Math.floor(window.innerHeight * 0.9), 500);
                  window.scrollBy(0, step);
                  return window.scrollY || document.documentElement.scrollTop || beforeY;
                })()"""
            )
            or 0
        )
        return {
            "moved": after2 > before + 8,
            "via": "window.scrollBy",
            "before": before,
            "after": after2,
        }
    except Exception:
        return {"moved": False, "via": "none", "before": before, "after": before}


def _wait_list_loading(session: CdpSession, *, max_sec: float = 8.0) -> None:
    deadline = time.time() + max(1.0, max_sec)
    while time.time() < deadline:
        try:
            loading = session.evaluate(
                "!!document.querySelector('.wgoo-loading-icon,[class*=\"loading-icon-circle\"]')"
            )
        except Exception:
            loading = False
        if not loading:
            return
        time.sleep(0.35)


def _grab_visible_cards(session: CdpSession) -> list[dict]:
    """Current viewport DOM cards only — no scroll."""
    try:
        raw = session.evaluate(GRAB_VISIBLE_CARDS_JS)
    except Exception:
        return []
    rows = []
    if isinstance(raw, dict):
        rows = list(raw.get("cards") or [])
    elif isinstance(raw, list):
        rows = raw
    cards: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        di = str(row.get("dataIndex") or "").strip()
        if not di:
            continue
        cards.append(
            {
                "goods_id": str(row.get("goods_id") or "").strip(),
                "shop_id": str(row.get("shop_id") or "").strip(),
                "dataIndex": di,
                "title": str(row.get("title") or "").strip(),
                "imgsSrc": [
                    str(u).strip()
                    for u in (row.get("imgsSrc") or [])
                    if str(u).strip()
                ],
                "list_seq": _card_list_seq(row),
            }
        )
    return cards


def _scroll_down_new_cards(
    session: CdpSession,
    seen_indices: set[str],
) -> tuple[list[dict], bool]:
    """End 키로 아래로 확인 → 새 카드만 반환. 크게 건너뛰면 PageDown으로 메움."""
    prev_max = _max_data_index_seen(seen_indices)

    def _ingest_visible() -> list[dict]:
        out: list[dict] = []
        for card in _grab_visible_cards(session):
            di = str(card.get("dataIndex") or "").strip()
            if not di or di in seen_indices:
                continue
            out.append(card)
        return out

    # End 위주 (목록 끝 확인·추가 로드). 안 움직이면 PageDown.
    for _ in range(4):
        if _has_no_more_data(session):
            break
        moved = _end_key_wecatalog(session)
        if not moved.get("moved"):
            moved = _page_down_wecatalog(session)
        time.sleep(0.3)
        _wait_list_loading(session)
        batch = _ingest_visible()
        if batch:
            # End로 너무 많이 점프하면 중간 index 누락 → 직전 위치로 돌아와 PageDown
            nums = []
            for c in batch:
                di = str(c.get("dataIndex") or "").strip()
                if di.isdigit():
                    nums.append(int(di))
            if nums and prev_max >= 0 and min(nums) > prev_max + 8:
                if _scroll_to_card_index(session, str(prev_max)):
                    time.sleep(0.25)
                    filled: list[dict] = []
                    for _pd in range(12):
                        if _has_no_more_data(session):
                            break
                        _page_down_wecatalog(session)
                        time.sleep(0.3)
                        _wait_list_loading(session, max_sec=5.0)
                        more = _ingest_visible()
                        if more:
                            filled.extend(more)
                            break
                    if filled:
                        at_end = _has_no_more_data(session)
                        return filled, bool(at_end) and not filled
            at_end = _has_no_more_data(session)
            return batch, bool(at_end) and not batch
        if not moved.get("moved"):
            break

    # 보강: 기존 JS 스크롤 (한 화면씩)
    old_timeout = session._timeout
    session._timeout = max(old_timeout, 90.0)
    try:
        res = session.evaluate(
            SCROLL_DOWN_NEW_CARDS_JS % json.dumps(list(seen_indices)),
            await_promise=True,
        )
    except Exception:
        return [], _has_no_more_data(session)
    finally:
        session._timeout = old_timeout

    if not isinstance(res, dict):
        return [], _has_no_more_data(session)

    cards: list[dict] = []
    for row in res.get("cards") or []:
        if not isinstance(row, dict):
            continue
        di = str(row.get("dataIndex") or "").strip()
        if not di or di in seen_indices:
            continue
        cards.append(
            {
                "goods_id": "",
                "shop_id": "",
                "dataIndex": di,
                "title": str(row.get("title") or "").strip(),
                "imgsSrc": [
                    str(u).strip()
                    for u in (row.get("imgsSrc") or [])
                    if str(u).strip()
                ],
                "list_seq": _card_list_seq(row),
            }
        )

    at_end = bool(res.get("noMore") or res.get("atEnd")) or _has_no_more_data(session)
    if cards:
        at_end = False
    return cards, at_end


def _card_label(item: dict) -> str:
    gnum = str(item.get("goodsNum") or "").strip()
    gid = str(item.get("goods_id") or "").strip()
    if gnum:
        return gnum
    if gid:
        return gid
    di = str(item.get("dataIndex") or "").strip()
    return f"#{di}" if di else "?"


def _fetch_list_items(
    session: CdpSession,
    album_id: str,
    trans_lang: str,
    *,
    on_progress: ProgressCb | None = None,
    max_pages: int = 40,
) -> tuple[list[dict], str | None]:
    all_items: list[dict] = []
    ts = ""
    for page_no in range(1, max(1, max_pages) + 1):
        if on_progress:
            on_progress(f"목록 API {page_no}페이지 조회…")
        res = session.evaluate(
            _fetch_page_js(album_id, trans_lang, ts),
            await_promise=True,
        )
        if not isinstance(res, dict):
            return all_items, "목록 API 응답 형식 오류"
        err = (res.get("err") or "").strip()
        if err:
            return all_items, f"목록 API 오류: {err}"
        batch = list(res.get("items") or [])
        all_items.extend(batch)
        if on_progress:
            on_progress(f"목록 API {page_no}페이지 +{len(batch)}건 (누적 {len(all_items)}건)")
        if not res.get("isLoadMore"):
            break
        ts = str(res.get("pageTimestamp") or "").strip()
        if not ts:
            break
    return all_items, None


def _wait_store_list(session: CdpSession, wait_sec: float = 12.0) -> None:
    deadline = time.time() + max(2.0, wait_sec)
    while time.time() < deadline:
        try:
            if session.evaluate(WAIT_STORE_LIST_JS):
                return
        except Exception:
            pass
        time.sleep(0.35)
    time.sleep(0.5)


def _resolve_weshop_context(session: CdpSession, parsed_url: WecatalogUrl) -> dict:
    try:
        raw = session.evaluate(RESOLVE_WESHOP_CONTEXT_JS)
    except Exception:
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    album_id = str(raw.get("albumId") or parsed_url.album_id or "").strip()
    tag_id = raw.get("tagId") or parsed_url.tag_id
    trans_lang = str(raw.get("transLang") or "ko").strip() or "ko"
    template_url = str(raw.get("templateUrl") or "").strip()
    if tag_id is not None:
        tag_id = str(tag_id).strip() or None
    return {
        "albumId": album_id,
        "tagId": tag_id,
        "transLang": trans_lang,
        "templateUrl": template_url,
        "href": str(raw.get("href") or ""),
    }


def _card_sort_key(card: dict) -> tuple[int, int | str]:
    di = str(card.get("dataIndex") or "").strip()
    try:
        return (0, int(di))
    except ValueError:
        return (1, di)


def _normalize_title(title: str) -> str:
    return re.sub(r"[\s#*_\-·]+", "", (title or "").strip().lower())


def _zip_cards_with_api(cards: list[dict], api_items: list[dict]) -> list[dict]:
    cards_sorted = sorted(cards, key=_card_sort_key)
    out: list[dict] = []
    for i, card in enumerate(cards_sorted):
        if i >= len(api_items):
            break
        it = dict(api_items[i])
        gid = str(it.get("goods_id") or "").strip()
        if not gid:
            continue
        title = str(card.get("title") or "").strip()
        if title:
            it["title"] = title
        out.append(it)
    return out


def _api_ctx_for_fetch(ctx: dict) -> dict:
    """tagId가 있으면 templateUrl 무시 — 전체 상점 API 혼선 방지."""
    out = dict(ctx)
    if out.get("tagId"):
        out["templateUrl"] = ""
    album = str(out.get("albumId") or "").strip()
    if album.startswith("A") and len(album) > 18:
        out["templateUrl"] = ""
    return out


def _images_overlap(card: dict, api_item: dict) -> bool:
    c_imgs = list(card.get("imgsSrc") or [])
    a_imgs = list(api_item.get("imgsSrc") or api_item.get("imgs") or [])
    if not c_imgs or not a_imgs:
        return False
    c_keys: set[str] = set()
    for u in c_imgs:
        c_keys |= _image_match_keys(str(u))
    for u in a_imgs:
        if _image_match_keys(str(u)) & c_keys:
            return True
    return False


def _verify_zip_sample(
    cards: list[dict],
    api_items: list[dict],
    ctx: dict | None = None,
) -> bool:
    if not cards or not api_items:
        return False
    c0 = sorted(cards, key=_card_sort_key)[0]
    a0 = api_items[0]
    if _images_overlap(c0, a0):
        return True
    ct = _normalize_title(str(c0.get("title") or ""))
    at = _normalize_title(str(a0.get("title") or ""))
    if ct and at and len(ct) >= 8 and (ct in at or at in ct):
        return True
    return False


def _match_cards_by_title(cards: list[dict], api_items: list[dict]) -> list[dict]:
    by_title: dict[str, dict] = {}
    for it in api_items:
        key = _normalize_title(str(it.get("title") or ""))
        if key and key not in by_title:
            by_title[key] = it
    matched: list[dict] = []
    seen: set[str] = set()
    for card in sorted(cards, key=_card_sort_key):
        key = _normalize_title(str(card.get("title") or ""))
        if not key or len(key) < 4:
            continue
        hit = by_title.get(key)
        if not hit:
            for tkey, it in by_title.items():
                if key in tkey or tkey in key:
                    hit = it
                    break
        if not hit:
            continue
        gid = str(hit.get("goods_id") or "").strip()
        if not gid or gid in seen:
            continue
        seen.add(gid)
        merged = dict(hit)
        title = str(card.get("title") or "").strip()
        if title:
            merged["title"] = title
        matched.append(merged)
    return matched


def _fetch_list_items_for_context(
    session: CdpSession,
    ctx: dict,
    *,
    on_progress: ProgressCb | None = None,
    min_items: int = 0,
    max_pages: int = 40,
) -> list[dict]:
    all_items: list[dict] = []
    ts = ""
    album_id = str(ctx.get("albumId") or "")
    for page_no in range(1, max(1, max_pages) + 1):
        if on_progress:
            on_progress(
                f"목록 API {page_no}페이지 "
                f"(albumId={album_id[:18]}… · 누적 {len(all_items)}건)"
            )
        res = session.evaluate(
            _fetch_list_page_js(ctx, ts),
            await_promise=True,
        )
        if not isinstance(res, dict):
            break
        err = (res.get("err") or "").strip()
        if err:
            if on_progress:
                on_progress(f"목록 API 중단: {err}")
            break
        batch = list(res.get("items") or [])
        all_items.extend(batch)
        need = max(0, int(min_items or 0))
        if need and len(all_items) >= need:
            break
        if not res.get("isLoadMore"):
            break
        ts = str(res.get("pageTimestamp") or "").strip()
        if not ts:
            break
    return all_items


def _card_list_seq(card: dict) -> int | None:
    di = str(card.get("dataIndex") or "").strip()
    if not di:
        return None
    try:
        return int(di)
    except ValueError:
        return None


def _screen_item_from_card(card: dict, *, album_id: str, api_hit: dict | None = None) -> dict:
    base = dict(api_hit or {})
    gid = str(card.get("goods_id") or base.get("goods_id") or "").strip()
    sid = str(card.get("shop_id") or base.get("shop_id") or album_id or "").strip()
    title = str(card.get("title") or base.get("title") or "").strip()
    imgs = list(card.get("imgsSrc") or base.get("imgsSrc") or base.get("imgs") or [])
    seq = _card_list_seq(card)
    gnum = str(base.get("goodsNum") or base.get("mark_code") or "").strip()
    return {
        "goods_id": gid,
        "shop_id": sid,
        "title": title,
        "imgsSrc": imgs,
        "goodsNum": gnum,
        "dataIndex": str(card.get("dataIndex") or ""),
        "list_seq": seq,
    }


def _sort_screen_items(items: list[dict]) -> list[dict]:
    return sorted(
        items,
        key=lambda x: (
            x.get("list_seq") is None,
            x.get("list_seq") if x.get("list_seq") is not None else 10**9,
        ),
    )


def _match_cards_to_limited_api(
    session: CdpSession,
    ctx: dict,
    cards: list[dict],
    *,
    on_progress: ProgressCb | None = None,
    max_pages: int = 2,
) -> list[dict]:
    if not cards:
        return []
    album_id = str(ctx.get("albumId") or "")
    fetch_ctx = _api_ctx_for_fetch(ctx)
    api_items = _fetch_list_items_for_context(
        session,
        fetch_ctx,
        on_progress=on_progress,
        min_items=len(cards),
        max_pages=max(1, max_pages),
    )
    tag_id = str(ctx.get("tagId") or "").strip()
    if tag_id and api_items:
        tagged = _filter_by_tag(api_items, tag_id)
        if tagged:
            api_items = tagged
            if on_progress:
                on_progress(f"tagId={tag_id} 필터 {len(api_items)}건")
        elif len(api_items) > len(cards) + 5:
            if on_progress:
                on_progress(
                    f"tagId={tag_id} — API {len(api_items)}건이 화면({len(cards)})과 "
                    "맞지 않습니다. tagId 포함 URL에서 다시 시도해 주세요."
                )
            return []
    if not api_items:
        return []

    if (
        len(api_items) >= len(cards)
        and _verify_zip_sample(cards, api_items[: len(cards)], ctx)
    ):
        out: list[dict] = []
        for card, api in zip(
            sorted(cards, key=_card_sort_key), api_items[: len(cards)]
        ):
            if not _images_overlap(card, api):
                continue
            out.append(_screen_item_from_card(card, album_id=album_id, api_hit=api))
        if len(out) >= max(1, int(len(cards) * 0.6)):
            if on_progress:
                on_progress(f"화면 순서+이미지 매칭 {len(out)}/{len(cards)}건")
            return _sort_screen_items(out)

    image_index: dict[str, dict] = {}
    for it in api_items:
        _index_item_images(image_index, it)
    matched: list[dict] = []
    used: set[str] = set()
    for card in sorted(cards, key=_card_sort_key):
        hit = _match_card_to_api(card, image_index)
        if not hit or not _images_overlap(card, hit):
            continue
        gid = str(hit.get("goods_id") or "").strip()
        if not gid or gid in used:
            continue
        used.add(gid)
        matched.append(_screen_item_from_card(card, album_id=album_id, api_hit=hit))
    if matched and on_progress:
        on_progress(f"이미지 매칭 {len(matched)}/{len(cards)}건")
    return _sort_screen_items(matched)


def _image_match_keys(url: str) -> set[str]:
    if not url:
        return set()
    base = url.split("?")[0].strip()
    keys: set[str] = {base}
    name = base.rsplit("/", 1)[-1]
    if name:
        keys.add(name)
        keys.add(re.sub(r"\.[^.]+$", "", name))
    m = re.search(r"(cmp_i\d+_\d+_\d+_\d+|i\d+_\d+_\d+_\d+)", base, re.I)
    if m:
        keys.add(m.group(1))
    return keys


def _build_known_image_keys(urls: list[str] | set[str] | None) -> set[str]:
    out: set[str] = set()
    for raw in urls or []:
        out |= _image_match_keys(str(raw))
    return out


def _card_matches_known_images(card: dict, known_imgs: set[str]) -> bool:
    if not known_imgs:
        return False
    for raw in card.get("imgsSrc") or []:
        if _image_match_keys(str(raw)) & known_imgs:
            return True
    return False


def _remember_product_images(known_imgs: set[str], parsed: ParsedProduct) -> None:
    for u in parsed.image_urls or []:
        known_imgs |= _image_match_keys(str(u))


def _index_item_images(index: dict[str, dict], item: dict) -> None:
    gid = str(item.get("goods_id") or "").strip()
    if not gid:
        return
    for raw in list(item.get("imgsSrc") or []) + list(item.get("imgs") or []):
        for key in _image_match_keys(str(raw)):
            index.setdefault(key, item)


def _match_card_to_api(card: dict, index: dict[str, dict]) -> dict | None:
    for raw in card.get("imgsSrc") or []:
        for key in _image_match_keys(str(raw)):
            hit = index.get(key)
            if hit:
                merged = dict(hit)
                title = (card.get("title") or "").strip()
                if title:
                    merged["title"] = title
                return merged
    return None


def _log_screen_diag(
    session: CdpSession,
    *,
    on_progress: ProgressCb | None = None,
) -> None:
    if not on_progress:
        return
    try:
        diag = session.evaluate(SCREEN_DIAG_JS)
        if not isinstance(diag, dict):
            return
        on_progress(
            "화면 진단: "
            f"bury={diag.get('bury', 0)} "
            f"shopItem={diag.get('shopItems', 0)} "
            f"img={diag.get('imgs', 0)} "
            f"title={str(diag.get('title') or '')[:20]}"
        )
    except Exception:
        pass


def _scrape_visible_cards(
    session: CdpSession,
    *,
    on_progress: ProgressCb | None = None,
    cancel: threading.Event | None = None,
) -> tuple[list[dict], bool]:
    """Scroll list until footer; return (all cards with index+cover image, saw_footer)."""
    if on_progress:
        on_progress("목록 전체 파악 중 (더 이상 데이터가 없습니다 까지)…")
    _wait_store_list(session)
    _scroll_list_top(session)

    old_timeout = session._timeout
    session._timeout = max(old_timeout, 300.0)
    try:
        raw = session.evaluate(COLLECT_VISIBLE_CARDS_JS, await_promise=True)
    finally:
        session._timeout = old_timeout

    rows: list = []
    saw_footer = False
    if isinstance(raw, dict):
        rows = list(raw.get("cards") or [])
        saw_footer = bool(raw.get("noMore"))
    elif isinstance(raw, list):
        rows = raw

    # footer 미확인이면 PageDown으로 보강
    by_di: dict[str, dict] = {}

    def _ingest(batch: list) -> None:
        for row in batch:
            if not isinstance(row, dict):
                continue
            di = str(row.get("dataIndex") or "").strip()
            if not di:
                continue
            title = str(row.get("title") or "").strip()
            imgs = [str(u).strip() for u in (row.get("imgsSrc") or []) if str(u).strip()]
            goods_id = str(row.get("goods_id") or "").strip()
            shop_id = str(row.get("shop_id") or "").strip()
            if not (goods_id or title or imgs):
                continue
            prev = by_di.get(di)
            if prev and prev.get("imgsSrc") and not imgs:
                continue
            by_di[di] = {
                "goods_id": goods_id,
                "shop_id": shop_id,
                "dataIndex": di,
                "title": title or (prev or {}).get("title", ""),
                "imgsSrc": imgs or list((prev or {}).get("imgsSrc") or []),
            }

    _ingest(rows)

    if not saw_footer and not _cancelled(cancel):
        if on_progress:
            on_progress(
                f"목록 보강 스크롤… (현재 {len(by_di)}건, footer 미확인)"
            )
        stagnant = 0
        last_n = len(by_di)
        for round_i in range(120):
            if _cancelled(cancel):
                break
            if _has_no_more_data(session):
                saw_footer = True
                break
            _page_down_wecatalog(session)
            time.sleep(0.4)
            try:
                more = session.evaluate(
                    SCROLL_DOWN_NEW_CARDS_JS % json.dumps(list(by_di.keys())),
                    await_promise=True,
                )
            except Exception:
                more = {}
            if isinstance(more, dict):
                _ingest(list(more.get("cards") or []))
                if more.get("noMore") or more.get("atEnd"):
                    saw_footer = True
            if _has_no_more_data(session):
                saw_footer = True
                break
            if len(by_di) > last_n:
                stagnant = 0
                last_n = len(by_di)
                if on_progress and round_i % 5 == 0:
                    on_progress(f"목록 파악 {len(by_di)}건…")
            else:
                stagnant += 1
            if stagnant >= 15:
                break

    cards = sorted(by_di.values(), key=_card_sort_key)
    if on_progress:
        mx = _max_data_index_seen(set(by_di.keys()))
        foot = "footer확인" if saw_footer else "footer미확인"
        on_progress(
            f"목록 전체 파악 {len(cards)}건"
            + (f" (index 0~{mx})" if mx >= 0 else "")
            + f" · {foot}"
        )
    if not cards:
        _log_screen_diag(session, on_progress=on_progress)
    return cards, saw_footer


def _match_cards_via_api(
    session: CdpSession,
    ctx: dict,
    cards: list[dict],
    *,
    on_progress: ProgressCb | None = None,
) -> list[dict]:
    if not cards:
        return []
    album_id = str(ctx.get("albumId") or "")
    trans_lang = str(ctx.get("transLang") or "ko")
    image_index: dict[str, dict] = {}
    matched: list[dict] = []
    unmatched = list(cards)
    ts = ""
    for page_no in range(1, 41):
        if on_progress:
            on_progress(
                f"API 매칭 {page_no}페이지 "
                f"({len(matched)}/{len(cards)}건)"
            )
        res = session.evaluate(
            _fetch_list_page_js(ctx, ts),
            await_promise=True,
        )
        if not isinstance(res, dict):
            break
        err = (res.get("err") or "").strip()
        if err:
            if on_progress:
                on_progress(f"API 매칭 중단: {err}")
            break
        batch = list(res.get("items") or [])
        for it in batch:
            _index_item_images(image_index, it)
        still: list[dict] = []
        for card in unmatched:
            hit = _match_card_to_api(card, image_index)
            if hit and str(hit.get("goods_id") or "").strip():
                matched.append(hit)
            else:
                still.append(card)
        unmatched = still
        if not unmatched:
            break
        if not res.get("isLoadMore"):
            break
        ts = str(res.get("pageTimestamp") or "").strip()
        if not ts:
            break
    if unmatched and on_progress:
        on_progress(f"매칭 실패 {len(unmatched)}건 (화면과 API 불일치)")
    return matched


def _index_api_items(items: list[dict]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for it in items or []:
        gid = str(it.get("goods_id") or "").strip()
        if gid:
            out[gid] = it
    return out


def _merge_api_metadata(items: list[dict], api_items: list[dict]) -> list[dict]:
    api_by_id = _index_api_items(api_items)
    merged: list[dict] = []
    for it in items:
        gid = str(it.get("goods_id") or "").strip()
        base = api_by_id.get(gid) or {}
        merged.append({**base, **it, "goods_id": gid or base.get("goods_id", "")})
    return merged


def _collect_from_current_screen(
    session: CdpSession,
    parsed_url: WecatalogUrl,
    trans_lang: str,
    *,
    on_progress: ProgressCb | None = None,
    cancel: threading.Event | None = None,
) -> tuple[list[dict], dict, bool]:
    ctx = _resolve_weshop_context(session, parsed_url)
    list_href = _normalize_list_href(_current_href(session), parsed_url, ctx)
    ctx["listHref"] = list_href
    if on_progress:
        on_progress(f"현재 화면 수집: {list_href[:90]}")
    _wait_store_list(session, wait_sec=15.0)
    if trans_lang:
        ctx["transLang"] = trans_lang
    album_id = str(ctx.get("albumId") or parsed_url.album_id or "").strip()
    if on_progress:
        tag_hint = f" · tagId={ctx.get('tagId')}" if ctx.get("tagId") else ""
        on_progress(
            f"shop albumId={album_id[:28]}{'…' if len(album_id) > 28 else ''}{tag_hint}"
        )

    cards, saw_footer = _scrape_visible_cards(
        session, on_progress=on_progress, cancel=cancel
    )
    if not cards:
        return [], ctx, saw_footer

    items = [_screen_item_from_card(card, album_id=album_id) for card in cards]
    items = _sort_screen_items(items)
    seen: set[str] = set()
    unique: list[dict] = []
    for it in items:
        di = str(it.get("dataIndex") or "").strip()
        key = di if di else f"gid:{it.get('goods_id')}"
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append(it)

    if on_progress:
        on_progress(
            f"목록 확정 {len(unique)}건 · 이미지로 이미수집 판별 후 신규만 클릭"
        )
    return unique, ctx, saw_footer


def open_wecatalog_in_debug(url: str) -> tuple[bool, str]:
    """Open wecatalog list URL in the dedicated wecatalog Chrome (port 9223)."""
    from collector import open_cdp_tab

    raw = (url or "").strip()
    if not raw:
        return False, "URL이 비어 있습니다."
    if not _is_weshop_list_url(raw):
        return False, "weshop 목록 URL(store/goods_list)이 아닙니다."
    if not is_wecatalog_cdp_up():
        from wecatalog_browser import start_wecatalog_chrome

        ok, msg = start_wecatalog_chrome(raw)
        if not ok:
            return False, msg
        time.sleep(3.0)
        return True, msg
    ok, msg = open_cdp_tab(raw, ports=WECATALOG_CDP_PORTS)
    if not ok:
        return False, msg
    time.sleep(4.0)
    return True, msg


def preview_wecatalog_screen() -> tuple[str, WecatalogUrl | None, str]:
    """Return (href, parsed, error) for the wecatalog tab that has product cards."""
    scanned = _scan_page_tabs()
    if not scanned:
        return (
            "",
            None,
            "wecatalog Chrome 탭을 찾지 못했습니다.\n"
            "[wecatalog Chrome] 실행 후 목록 페이지를 열어 주세요.",
        )

    target, diag = _pick_weshop_list_tab(scanned)
    if not target:
        return "", None, _no_weshop_tab_message(scanned)

    href = _tab_href(target, diag)
    if not href:
        return "", None, "열린 wecatalog 탭 URL이 비어 있습니다."
    parsed = parse_wecatalog_url(href)
    if not parsed:
        return href, None, f"shop ID를 읽지 못했습니다.\n현재 URL: {href[:120]}"
    return href, parsed, ""


def _fetch_first_api_page(
    session: CdpSession,
    album_id: str,
    trans_lang: str,
) -> list[dict]:
    res = session.evaluate(
        _fetch_page_js(album_id, trans_lang, ""),
        await_promise=True,
    )
    if not isinstance(res, dict):
        return []
    return list(res.get("items") or [])


def _extract_goods_tags(detail: dict | None) -> tuple[str, str, str]:
    """Return (brand, product_tag, combined tags line) from AttributeTags."""
    rows = list((detail or {}).get("goodsTags") or [])
    names: list[str] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        names.append(name)
    brand = names[0] if names else ""
    product_tag = names[1] if len(names) > 1 else ""
    if len(names) > 2:
        product_tag = ", ".join(names[1:])
    combined = ", ".join(names)
    return brand, product_tag, combined


def _extract_search_code(attrs: list[dict], fallback: str = "") -> str:
    for row in attrs or []:
        label = (row.get("label") or "").strip()
        clip = (row.get("clip") or "").strip()
        value = (row.get("value") or "").strip()
        if re.search(
            r"검색\s*코드|搜索码|search\s*code|item\s*number|货号",
            label,
            re.I,
        ):
            if clip and re.fullmatch(r"\d+", clip):
                return clip
            m = re.search(r"\d{4,12}", value)
            if m:
                return m.group(0)
    for row in attrs or []:
        clip = (row.get("clip") or "").strip()
        if re.fullmatch(r"\d{4,12}", clip):
            return clip
    fb = (fallback or "").strip()
    if re.fullmatch(r"\d{4,12}", fb):
        return fb
    m = re.search(r"\d{4,12}", fb)
    return m.group(0) if m else fb


def _extract_size(text: str) -> str:
    blob = (text or "").strip()
    if not blob:
        return ""

    def _dim_triple(a: str, b: str, c: str) -> str:
        return f"{a}*{b}*{c}"

    m = _RE_SIZE.search(blob)
    if m:
        chunk = m.group(1).strip()
        m_lhw = _RE_SIZE_CN_LHW.search(chunk)
        if m_lhw:
            return _dim_triple(m_lhw.group(1), m_lhw.group(2), m_lhw.group(3))
        m_lwh = _RE_SIZE_CN_LWH.search(chunk)
        if m_lwh:
            return _dim_triple(m_lwh.group(1), m_lwh.group(2), m_lwh.group(3))
        m_old = _RE_SIZE_DIM.search(chunk)
        if m_old:
            return _dim_triple(m_old.group(1), m_old.group(2), m_old.group(3))
        return chunk

    m = _RE_SIZE_CN_LHW.search(blob)
    if m:
        return _dim_triple(m.group(1), m.group(2), m.group(3))
    m = _RE_SIZE_CN_LWH.search(blob)
    if m:
        return _dim_triple(m.group(1), m.group(2), m.group(3))
    m = _RE_SIZE_DIM.search(blob)
    if m:
        return _dim_triple(m.group(1), m.group(2), m.group(3))
    return ""


def _uniq_image_urls(urls: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in urls or []:
        u = normalize_image_url(raw) or raw.split("?")[0]
        if not u or u in seen:
            continue
        if "xcimg.szwego.com" not in u.lower() and "img.szwego.com" not in u.lower():
            continue
        seen.add(u)
        out.append(u)
    return out


def _title_from_text(text: str) -> str:
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    if not lines:
        return ""
    first = lines[0]
    if len(first) > 120:
        first = first[:120].rstrip()
    return first


def item_to_parsed(
    item: dict,
    detail: dict | None,
    *,
    album_id: str,
) -> ParsedProduct | None:
    goods_id = (item.get("goods_id") or item.get("selfGoodsId") or "").strip()
    goods_num = (item.get("goodsNum") or item.get("mark_code") or "").strip()
    shop_id = (item.get("shop_id") or album_id or "").strip()

    rich = ""
    attrs: list[dict] = []
    imgs: list[str] = []
    if detail:
        rich = (detail.get("richText") or "").strip()
        attrs = list(detail.get("attrs") or [])
        imgs = list(detail.get("imgs") or [])

    if not imgs:
        imgs = list(item.get("imgsSrc") or item.get("imgs") or [])
    if not rich:
        rich = (item.get("title") or "").strip()

    raw_code = _extract_search_code(attrs, goods_num)
    if not raw_code or not re.fullmatch(r"\d{4,12}", raw_code):
        return None

    brand, product_tag, tag_line = _extract_goods_tags(detail)
    size_line = _extract_size(rich)
    desc_parts: list[str] = []
    if rich:
        desc_parts.append(rich)
    if brand:
        desc_parts.append(f"브랜드: {brand}")
    if product_tag:
        desc_parts.append(f"상품태그: {product_tag}")
    desc_parts.append(f"搜索码：{raw_code}")
    if size_line:
        desc_parts.append(f"크기: {size_line}")

    title = _title_from_text(rich) or _title_from_text(item.get("title") or "")
    if not title:
        title = f"상품 {raw_code}"

    image_urls = _uniq_image_urls(imgs)
    if not image_urls:
        return None

    tags = tag_line or "wecatalog"

    list_seq = item.get("list_seq")
    if list_seq is not None:
        try:
            list_seq = int(list_seq)
        except (TypeError, ValueError):
            list_seq = None

    return ParsedProduct(
        goods_id=goods_id or f"wc-{raw_code}",
        shop_id=shop_id,
        title=title,
        search_code=f"A-{raw_code}",
        sku_no=WECATALOG_PRICE,
        tags=tags,
        description="\n".join(desc_parts).strip(),
        image_urls=image_urls,
        list_seq=list_seq,
    )


def _filter_by_tag(items: list[dict], tag_id: str | None) -> list[dict]:
    if not tag_id:
        return items
    want = str(tag_id).strip()
    if not want:
        return items
    out: list[dict] = []
    for it in items:
        tags = it.get("tags") or []
        if any(str(t.get("tagId") or "") == want for t in tags):
            out.append(it)
    return out


def _is_goods_detail_href(href: str) -> bool:
    return _is_product_detail_href(href)


def _fetch_goods_detail(
    session: CdpSession,
    detail_url: str,
    *,
    on_progress: ProgressCb | None = None,
) -> dict | None:
    _navigate(session, detail_url, wait_sec=5.0)
    _wait_detail(session, wait_sec=14.0)
    href = _current_href(session)
    if not _is_product_detail_href(href):
        if on_progress:
            on_progress(f"상세 페이지 아님 — {href[:90]}")
        return None
    detail = session.evaluate(EXTRACT_DETAIL_JS)
    return detail if isinstance(detail, dict) else None


def _navigate(session: CdpSession, url: str, wait_sec: float = 2.5) -> None:
    cur = _current_href(session)
    if cur.split("#")[0].rstrip("/") == url.split("#")[0].rstrip("/"):
        return
    session.call("Page.navigate", {"url": url})
    deadline = time.time() + max(1.0, wait_sec)
    while time.time() < deadline:
        try:
            ready = session.evaluate("document.readyState")
            if ready == "complete":
                break
        except Exception:
            pass
        time.sleep(0.2)
    time.sleep(0.8)


def _wait_detail(session: CdpSession, wait_sec: float = 8.0) -> None:
    deadline = time.time() + max(1.0, wait_sec)
    while time.time() < deadline:
        try:
            ok = session.evaluate(
                "!!document.querySelector('[class*=\"RichText_RichText\"],"
                "[class*=\"GoodsAttribute_GoodsAttribute\"]')"
            )
            if ok:
                return
        except Exception:
            pass
        time.sleep(0.25)
    time.sleep(0.4)


def format_wecatalog_error(exc: BaseException) -> str:
    msg = str(exc or "").strip()
    low = msg.lower()
    if "timed out" in low or "timeout" in low:
        return (
            "브라우저 연결 시간 초과입니다.\n\n"
            "· [wecatalog Chrome] 창에서 wecatalog 목록을 열었는지 확인\n"
            "· 微购相册(목록-상세 자동수집)과는 별개 브라우저입니다\n"
            "· 목록 페이지가 완전히 로드된 뒤 다시 [wecatalog 수집]\n"
            "· 탭이 멈춰 있으면 새로고침 후 재시도"
        )
    if "cdp" in low or "websocket" in low:
        return f"브라우저(CDP) 연결 오류: {msg}"
    return msg or "알 수 없는 오류"


def collect_wecatalog(
    list_url: str | None = None,
    *,
    on_progress: ProgressCb | None = None,
    on_product: Callable[[ParsedProduct], None] | None = None,
    cancel: threading.Event | None = None,
    pause: threading.Event | None = None,
    max_items: int = 0,
    known_goods_ids: set[str] | None = None,
    known_image_urls: list[str] | set[str] | None = None,
    trans_lang: str = "ko",
    fail_indices_out: list[str] | None = None,
) -> tuple[list[ParsedProduct], str]:
    """Collect products visible on the current wecatalog screen (no page navigation)."""

    def log(msg: str) -> None:
        if on_progress:
            on_progress(msg)

    session, conn_msg = open_wecatalog_session()
    if not session:
        return [], conn_msg
    log(conn_msg)

    try:
        href = _current_href(session)
        if not _is_weshop_list_url(href):
            return [], (
                "weshop 상품 목록 화면이 아닙니다.\n"
                f"현재 URL: {(href or '(없음)')[:120]}"
            )

        parsed_url = parse_wecatalog_url(href)
        if not parsed_url:
            return [], f"현재 페이지에서 shop ID를 읽지 못했습니다.\n{href[:120]}"

        log(f"현재 화면: {href[:90]}")
        list_href = href

        # 전체 목록을 끝까지 훑고 맨 위로 올리지 않음.
        # 맨 위 → 보이는 카드 수집 → End로 확인하며 신규만 이어서 수집.
        ctx = _resolve_weshop_context(session, parsed_url)
        list_href = _normalize_list_href(
            str(ctx.get("listHref") or list_href or href),
            parsed_url,
            ctx,
        )
        ctx["listHref"] = list_href
        if trans_lang:
            ctx["transLang"] = trans_lang
        album_id = str(ctx.get("albumId") or parsed_url.album_id or "").strip()
        tag_id = ctx.get("tagId") or parsed_url.tag_id
        _wait_store_list(session, wait_sec=15.0)
        log(
            f"shop albumId={album_id[:28]}{'…' if len(album_id) > 28 else ''}"
            + (f" · tagId={tag_id}" if tag_id else "")
        )
        log("맨 위부터 · End로 확인하며 신규만 수집 (전체 사전스캔 없음)")
        _scroll_list_top(session)
        time.sleep(0.35)

        known = {str(x).strip() for x in (known_goods_ids or set()) if str(x).strip()}
        known_imgs = _build_known_image_keys(known_image_urls)
        if known_imgs:
            log(f"이미지 스킵키 {len(known_imgs)}개 로드")

        def _cards_to_items(cards: list[dict]) -> list[dict]:
            items = [
                _screen_item_from_card(card, album_id=album_id) for card in cards
            ]
            items = _sort_screen_items(items)
            out: list[dict] = []
            seen_local: set[str] = set()
            for it in items:
                di = str(it.get("dataIndex") or "").strip()
                key = di if di else f"gid:{it.get('goods_id')}"
                if not key or key in seen_local:
                    continue
                seen_local.add(key)
                out.append(it)
            return out

        seed_cards = _grab_visible_cards(session)
        if not seed_cards:
            _log_screen_diag(session, on_progress=log)
            return [], (
                "수집할 상품이 없습니다.\n"
                "화면에 상품 카드가 보이는지 확인해 주세요."
            )

        seed_items = _cards_to_items(seed_cards)
        need_collect: list[dict] = []
        skipped_img = 0
        no_cover = 0
        for it in seed_items:
            if _card_matches_known_images(it, known_imgs):
                skipped_img += 1
            else:
                need_collect.append(it)
                if not (it.get("imgsSrc") or []):
                    no_cover += 1

        list_total = len(seed_items)
        max_di = _max_data_index_seen(
            {
                str(it.get("dataIndex") or "").strip()
                for it in seed_items
                if str(it.get("dataIndex") or "").strip()
            }
        )
        log(
            f"현재 화면 {list_total}건"
            + (f" (index ~{max_di})" if max_di >= 0 else "")
            + f" · 이미수집 {skipped_img} · 수집대상 {len(need_collect)}"
            + (f" · 썸네일없음 {no_cover}" if no_cover else "")
        )
        if not need_collect:
            log("현재 화면 신규 없음 — End로 아래쪽 추가 확인…")

        collected: list[ParsedProduct] = []
        limit = max(0, int(max_items or 0))
        done = 0
        skipped_known = skipped_img
        fail_n = 0
        parse_fail_n = 0
        fail_indices: list[str] = []
        queue: list[dict] = list(need_collect)
        seen_di: set[str] = {
            str(it.get("dataIndex") or "").strip()
            for it in seed_items
            if str(it.get("dataIndex") or "").strip()
        }
        pos = 0
        end_streak = 0
        fail_streak = 0
        end_need = 40
        last_max_di = max_di
        saw_footer = False
        weshop_ctx = ctx

        def _progress_suffix() -> str:
            base = (
                f"신규 {done}/{limit}"
                if limit > 0
                else f"신규 {done}/{max(len(queue), done)}"
            )
            return f"{base} · 확인 {list_total}건"

        def _stay_index(cur_item: dict) -> str:
            if pos < len(queue):
                nxt = str(queue[pos].get("dataIndex") or "").strip()
                if nxt:
                    return nxt
            return str(cur_item.get("dataIndex") or "").strip()

        while True:
            if _cancelled(cancel):
                log("사용자 중지")
                break
            if not _wait_pause(pause, cancel, on_progress):
                log("사용자 중지")
                break
            if limit > 0 and done >= limit:
                log(f"신규 {limit}건 수집 완료 (스킵 {skipped_known}건)")
                break

            if pos >= len(queue):
                if saw_footer or _has_no_more_data(session):
                    saw_footer = True
                    log(
                        f"수집 대상 처리 완료 — {_progress_suffix()}"
                        f" · 스킵 {skipped_known} · 실패 {fail_n}"
                    )
                    break
                new_cards, at_end = _scroll_down_new_cards(session, seen_di)
                added = 0
                if new_cards:
                    end_streak = 0
                    for card in new_cards:
                        di = str(card.get("dataIndex") or "").strip()
                        if not di or di in seen_di:
                            continue
                        seen_di.add(di)
                        if card.get("list_seq") is None:
                            card["list_seq"] = _card_list_seq(card)
                        list_total += 1
                        item = _screen_item_from_card(card, album_id=album_id)
                        if _card_matches_known_images(item, known_imgs):
                            skipped_known += 1
                            skipped_img += 1
                            continue
                        queue.append(item)
                        added += 1
                    last_max_di = max(last_max_di, _max_data_index_seen(seen_di))
                    if added:
                        log(
                            f"End 확인 +{added}건 신규대상 "
                            f"({_progress_suffix()} · 대기 {len(queue) - pos})"
                        )
                    elif new_cards:
                        log(
                            f"End 확인 · 새 카드 {len(new_cards)}건은 이미수집 "
                            f"({_progress_suffix()})"
                        )
                else:
                    end_streak += 1
                if at_end or _has_no_more_data(session):
                    saw_footer = True
                if pos >= len(queue):
                    if saw_footer or end_streak >= end_need:
                        log(
                            f"목록 끝 — {_progress_suffix()}"
                            f" · 스킵 {skipped_known} · 실패 {fail_n}"
                        )
                        break
                    continue

            item = queue[pos]
            pos += 1
            di = str(item.get("dataIndex") or "").strip()
            gid = (item.get("goods_id") or "").strip()
            label = _card_label(item)
            shop_id = str(item.get("shop_id") or "").strip()
            detail = None

            if _card_matches_known_images(item, known_imgs):
                skipped_known += 1
                skipped_img += 1
                continue

            try:
                detail, gid, shop_id = _open_detail_via_card_click(
                    session,
                    item,
                    list_href,
                    on_progress=log,
                    parse_detail=False,
                )
                if gid:
                    item = {**item, "goods_id": gid, "shop_id": shop_id}
            except Exception as exc:
                log(f"카드 확인 실패 {label}: {exc}")
                gid = ""

            if not gid:
                fail_streak += 1
                fail_n += 1
                if di:
                    fail_indices.append(di)
                log(f"goods_id 확인 실패 ({label}) · {_progress_suffix()}")
                _return_to_list(
                    session, list_href, scroll_to_index=_stay_index(item) or None
                )
                if fail_streak >= 5:
                    log(
                        f"카드 클릭 연속 실패 — 목록 복구 후 index={di} 재시도"
                    )
                    _restore_list(
                        session,
                        list_href,
                        wait_sec=12.0,
                        scroll_to_index=di or None,
                        force_top=False,
                    )
                    fail_streak = 0
                if fail_streak >= 15:
                    log("카드 클릭 연속 실패 — 수집 중단")
                    break
                continue

            fail_streak = 0

            if gid in known:
                skipped_known += 1
                for raw in item.get("imgsSrc") or []:
                    known_imgs |= _image_match_keys(str(raw))
                log(f"스킵(goods_id) {gid[:24]}… — {_progress_suffix()}")
                _return_to_list(
                    session, list_href, scroll_to_index=_stay_index(item) or None
                )
                continue

            if limit > 0 and done >= limit:
                _return_to_list(
                    session, list_href, scroll_to_index=_stay_index(item) or None
                )
                break

            try:
                log(f"신규 {done + 1}/{limit or max(len(queue), done + 1)} 수집 — {label}")
                _wait_detail(session, wait_sec=12.0)
                detail = _extract_detail_current(session)
            except Exception as exc:
                log(f"상세 실패 {label}: {exc}")
                detail = None

            parsed = item_to_parsed(item, detail, album_id=album_id)
            if not parsed:
                parse_fail_n += 1
                fail_n += 1
                if di:
                    fail_indices.append(di)
                log(f"파싱 실패 — 검색코드/이미지 없음 ({label})")
                _return_to_list(
                    session, list_href, scroll_to_index=_stay_index(item) or None
                )
                continue

            if on_product:
                on_product(parsed)
            collected.append(parsed)
            done += 1
            known.add(gid)
            _remember_product_images(known_imgs, parsed)
            for raw in item.get("imgsSrc") or []:
                known_imgs |= _image_match_keys(str(raw))
            if limit > 0:
                log(f"신규 {done}/{limit} 저장 ({gid[:20]}…)")
            else:
                log(f"신규 {done}건 저장 ({gid[:20]}…)")
            _return_to_list(
                session, list_href, scroll_to_index=_stay_index(item) or None
            )

        summary = (
            f"wecatalog 완료 · 확인 {list_total}건"
            f" · 신규저장 {len(collected)}"
            f" · 이미지스킵 {skipped_img}"
            f" · 실패 {fail_n}"
        )
        if parse_fail_n:
            summary += f" · 파싱실패 {parse_fail_n}"
        if fail_indices:
            uniq_fail = sorted(set(fail_indices), key=lambda x: int(x) if x.isdigit() else x)
            preview = ",".join(uniq_fail[:12])
            more = f" 외 {len(uniq_fail) - 12}" if len(uniq_fail) > 12 else ""
            summary += f" · 실패index[{preview}{more}]"
        if fail_indices_out is not None:
            uniq_fail = sorted(
                set(fail_indices), key=lambda x: int(x) if x.isdigit() else x
            )
            fail_indices_out.clear()
            fail_indices_out.extend(uniq_fail)
        return collected, summary
    finally:
        session.close()


def wecatalog_fail_setting_key(tag_id: str | None) -> str:
    tid = str(tag_id or "").strip() or "all"
    return f"wecatalog_fail_indices_{tid}"


def format_index_ranges(nums: list[int] | set[int], *, max_chars: int = 0) -> str:
    """연속 index를 0~5, 8, 10~12 형태로."""
    if not nums:
        return "(없음)"
    sorted_nums = sorted({int(n) for n in nums})
    ranges: list[tuple[int, int]] = []
    start = end = sorted_nums[0]
    for n in sorted_nums[1:]:
        if n == end + 1:
            end = n
        else:
            ranges.append((start, end))
            start = end = n
    ranges.append((start, end))
    parts = [str(a) if a == b else f"{a}~{b}" for a, b in ranges]
    text = ", ".join(parts)
    if max_chars > 0 and len(text) > max_chars:
        acc: list[str] = []
        length = 0
        for p in parts:
            add = len(p) + (2 if acc else 0)
            if length + add > max_chars - 12:
                rest = len(parts) - len(acc)
                if rest > 0:
                    acc.append(f"…외 {rest}구간")
                break
            acc.append(p)
            length += add
        text = ", ".join(acc)
    return text


@dataclass
class WecatalogAuditReport:
    href: str
    tag_id: str | None
    album_id: str
    saw_footer: bool
    max_index: int
    list_product_count: int
    site_image_total: int
    db_product_count: int
    db_list_seq_count: int
    db_image_keys: int
    db_goods_ids: int
    collected_indices: list[int]
    excluded_indices: list[int]
    pending_indices: list[int]
    no_cover_indices: list[int]
    failed_indices: list[int]
    gap_indices: list[int]
    report_path: str = ""
    text: str = ""


def build_wecatalog_audit_db(store) -> dict:
    """ProductStore → 대조용 인덱스 (이미지·goods_id·list_seq·제목)."""
    known_urls = store.catalog_image_urls()
    known_imgs = _build_known_image_keys(known_urls)
    goods_ids = store.collected_goods_ids() | store.published_goods_ids()
    excluded = store.excluded_goods_ids()
    list_seq_map: dict[int, dict] = {}
    title_map: dict[str, str] = {}
    product_count = 0
    with store._connect() as con:
        for table in ("products", "published"):
            try:
                rows = con.execute(
                    f"SELECT goods_id, title, list_seq, image_urls FROM {table}"
                ).fetchall()
            except Exception:
                continue
            for r in rows:
                product_count += 1
                gid = str(r["goods_id"] or "").strip()
                tn = _normalize_title(str(r["title"] or ""))
                img_keys: set[str] = set()
                try:
                    urls = json.loads(r["image_urls"] or "[]")
                except Exception:
                    urls = []
                if isinstance(urls, list):
                    for u in urls:
                        img_keys |= _image_match_keys(str(u))
                ls = r["list_seq"]
                if ls is not None:
                    try:
                        li = int(ls)
                        prev = list_seq_map.get(li)
                        if not prev or len(img_keys) > len(prev.get("image_keys") or set()):
                            list_seq_map[li] = {
                                "goods_id": gid,
                                "title_norm": tn,
                                "image_keys": img_keys,
                            }
                    except (TypeError, ValueError):
                        pass
                if tn and len(tn) >= 10 and gid and tn not in title_map:
                    title_map[tn] = gid
    return {
        "image_keys": known_imgs,
        "goods_ids": goods_ids,
        "excluded_goods_ids": excluded,
        "list_seq_map": list_seq_map,
        "title_map": title_map,
        "product_count": product_count,
        "list_seq_count": len(list_seq_map),
    }


def _audit_is_collected(
    card: dict,
    merged: dict,
    idx: int,
    audit_db: dict,
    *,
    known_imgs: set[str],
    known_gid: set[str],
) -> bool:
    if _card_matches_known_images(merged, known_imgs):
        return True
    if _card_matches_known_images(card, known_imgs):
        return True
    gid = str(merged.get("goods_id") or card.get("goods_id") or "").strip()
    if gid and gid in known_gid:
        return True
    rec = (audit_db.get("list_seq_map") or {}).get(idx)
    if rec:
        rg = str(rec.get("goods_id") or "").strip()
        if rg and rg in known_gid:
            return True
        card_keys: set[str] = set()
        for u in list(card.get("imgsSrc") or []) + list(merged.get("imgsSrc") or []):
            card_keys |= _image_match_keys(str(u))
        if card_keys & set(rec.get("image_keys") or set()):
            return True
        ct = _normalize_title(str(card.get("title") or merged.get("title") or ""))
        rt = str(rec.get("title_norm") or "")
        if ct and rt and len(ct) >= 10:
            if ct == rt or ct in rt or rt in ct:
                return True
    ct = _normalize_title(str(card.get("title") or merged.get("title") or ""))
    if ct and len(ct) >= 12 and ct in (audit_db.get("title_map") or {}):
        return True
    return False


def _count_item_images(item: dict) -> int:
    total = 0
    for key in ("imgs", "imgsSrc", "images"):
        arr = item.get(key)
        if isinstance(arr, list):
            total = len([str(u).strip() for u in arr if str(u).strip()])
            if total:
                return total
    return 0


def _index_int(item: dict) -> int | None:
    di = str(item.get("dataIndex") or "").strip()
    if not di.isdigit():
        return None
    return int(di)


def format_wecatalog_audit_report(report: WecatalogAuditReport) -> str:
    foot = "footer확인" if report.saw_footer else "footer미확인"
    tag = f"tagId={report.tag_id}" if report.tag_id else "tagId=(없음)"
    lines = [
        "=== wecatalog 목록 대조 (index 번호) ===",
        f"화면: {report.href[:100]}",
        f"{tag} · albumId={report.album_id[:32]}",
        f"목록 스캔: index 0~{report.max_index} · 상품 {report.list_product_count}건 · {foot}",
        "",
        f"[홈페이지 이 태그] 상품 {report.list_product_count}건 · 이미지 합계 {report.site_image_total}장",
        f"[프로그램 DB] 상품 {report.db_product_count}건 · list_seq기록 {report.db_list_seq_count}건 · "
        f"이미지키 {report.db_image_keys}개 · goods_id {report.db_goods_ids}개",
        "",
        f"수집완료 {len(report.collected_indices)}건: "
        + format_index_ranges(report.collected_indices),
        f"미수집 {len(report.pending_indices)}건: "
        + format_index_ranges(report.pending_indices),
        f"제외 {len(report.excluded_indices)}건: "
        + format_index_ranges(report.excluded_indices),
        f"실패기록 {len(report.failed_indices)}건: "
        + format_index_ranges(report.failed_indices),
        f"썸네일없음 {len(report.no_cover_indices)}건: "
        + format_index_ranges(report.no_cover_indices),
    ]
    if report.gap_indices:
        lines.append(
            f"목록 누락(스크롤 gap) {len(report.gap_indices)}건: "
            + format_index_ranges(report.gap_indices)
        )
    if not report.saw_footer:
        lines.append(
            "※ footer 미확인 — 아래 목록이 끝까지 스캔되지 않았을 수 있습니다."
        )
    if report.report_path:
        lines.append(f"\n전체 보고서: {report.report_path}")
    return "\n".join(lines)


def audit_wecatalog_list(
    *,
    on_progress: ProgressCb | None = None,
    cancel: threading.Event | None = None,
    known_goods_ids: set[str] | None = None,
    known_image_urls: list[str] | set[str] | None = None,
    excluded_goods_ids: set[str] | None = None,
    failed_indices: set[str] | None = None,
    audit_db: dict | None = None,
    report_dir: str | None = None,
    trans_lang: str = "ko",
) -> tuple[WecatalogAuditReport | None, str]:
    """현재 wecatalog 목록을 끝까지 스캔하고 index별 수집·제외·실패 상태를 대조."""

    def log(msg: str) -> None:
        if on_progress:
            on_progress(msg)

    session, conn_msg = open_wecatalog_session()
    if not session:
        return None, conn_msg
    log(conn_msg)

    if audit_db:
        known_gid = set(audit_db.get("goods_ids") or known_goods_ids or set())
        excluded_gid = set(
            audit_db.get("excluded_goods_ids") or excluded_goods_ids or set()
        )
        known_imgs = set(audit_db.get("image_keys") or set())
        if not known_imgs and known_image_urls:
            known_imgs = _build_known_image_keys(known_image_urls)
        db_product_count = int(audit_db.get("product_count") or 0)
        db_list_seq_count = int(audit_db.get("list_seq_count") or 0)
        log(
            f"DB 로드 · 상품 {db_product_count}건 · list_seq {db_list_seq_count}건 · "
            f"이미지키 {len(known_imgs)}개"
        )
    else:
        known_gid = {str(x).strip() for x in (known_goods_ids or set()) if str(x).strip()}
        excluded_gid = {
            str(x).strip() for x in (excluded_goods_ids or set()) if str(x).strip()
        }
        known_imgs = _build_known_image_keys(known_image_urls)
        audit_db = {
            "image_keys": known_imgs,
            "goods_ids": known_gid,
            "excluded_goods_ids": excluded_gid,
            "list_seq_map": {},
            "title_map": {},
            "product_count": 0,
            "list_seq_count": 0,
        }
        db_product_count = 0
        db_list_seq_count = 0

    fail_set = {
        str(x).strip()
        for x in (failed_indices or set())
        if str(x).strip().isdigit()
    }

    old_timeout = session._timeout
    session._timeout = max(old_timeout, 600.0)
    try:
        href = _current_href(session)
        if not _is_weshop_list_url(href):
            return None, (
                "weshop 상품 목록 화면이 아닙니다.\n"
                f"현재 URL: {(href or '(없음)')[:120]}"
            )

        parsed_url = parse_wecatalog_url(href)
        if not parsed_url:
            return None, f"shop ID를 읽지 못했습니다.\n{href[:120]}"

        log("목록 전체 스캔 중 (대조용)…")
        items, weshop_ctx, saw_footer = _collect_from_current_screen(
            session,
            parsed_url,
            trans_lang,
            on_progress=log,
            cancel=cancel,
        )
        if _cancelled(cancel):
            return None, "사용자 중지"

        album_id = str(weshop_ctx.get("albumId") or parsed_url.album_id or "").strip()
        tag_id = str(weshop_ctx.get("tagId") or parsed_url.tag_id or "").strip() or None
        list_href = _normalize_list_href(
            str(weshop_ctx.get("listHref") or href),
            parsed_url,
            weshop_ctx,
        )

        if not items:
            return None, "목록에서 상품 카드를 찾지 못했습니다."

        cards = [
            {
                "dataIndex": str(it.get("dataIndex") or ""),
                "title": str(it.get("title") or ""),
                "imgsSrc": list(it.get("imgsSrc") or []),
                "goods_id": str(it.get("goods_id") or ""),
                "shop_id": str(it.get("shop_id") or ""),
            }
            for it in items
        ]

        api_by_di: dict[str, dict] = {}
        log("goods_id 보강 (API 경량)…")
        fetch_ctx = _api_ctx_for_fetch(weshop_ctx)
        api_items = _fetch_list_items_for_context(
            session, fetch_ctx, on_progress=log, max_pages=10
        )
        if tag_id:
            tagged = _filter_by_tag(api_items, tag_id)
            if tagged:
                api_items = tagged
        image_index: dict[str, dict] = {}
        for it in api_items:
            _index_item_images(image_index, it)
        for card in cards:
            di = str(card.get("dataIndex") or "").strip()
            if not di or di in api_by_di:
                continue
            hit = _match_card_to_api(card, image_index)
            if hit:
                api_by_di[di] = hit

        collected: list[int] = []
        excluded: list[int] = []
        pending: list[int] = []
        no_cover: list[int] = []
        failed: list[int] = []
        seen_index: set[int] = set()
        site_images = 0

        for card in sorted(cards, key=_card_sort_key):
            di = str(card.get("dataIndex") or "").strip()
            if not di.isdigit():
                continue
            idx = int(di)
            seen_index.add(idx)
            api_hit = api_by_di.get(di) or {}
            merged = _screen_item_from_card(
                card,
                album_id=album_id,
                api_hit=api_hit if api_hit else None,
            )
            site_images += _count_item_images(merged)
            imgs = list(merged.get("imgsSrc") or card.get("imgsSrc") or [])
            gid = str(merged.get("goods_id") or "").strip()

            if not imgs:
                no_cover.append(idx)
                if fail_set and di in fail_set:
                    failed.append(idx)
                continue

            if gid and gid in excluded_gid:
                excluded.append(idx)
                continue

            if _audit_is_collected(
                card, merged, idx, audit_db, known_imgs=known_imgs, known_gid=known_gid
            ):
                collected.append(idx)
                continue

            if fail_set and di in fail_set:
                failed.append(idx)
                continue

            pending.append(idx)

        max_di = max(seen_index) if seen_index else -1
        gap: list[int] = []
        if max_di >= 0:
            for i in range(max_di + 1):
                if i not in seen_index:
                    gap.append(i)

        report = WecatalogAuditReport(
            href=list_href,
            tag_id=tag_id,
            album_id=album_id,
            saw_footer=saw_footer,
            max_index=max_di,
            list_product_count=len(seen_index),
            site_image_total=site_images,
            db_product_count=db_product_count,
            db_list_seq_count=db_list_seq_count,
            db_image_keys=len(known_imgs),
            db_goods_ids=len(known_gid),
            collected_indices=collected,
            excluded_indices=excluded,
            pending_indices=pending,
            no_cover_indices=no_cover,
            failed_indices=failed,
            gap_indices=gap,
        )
        report.text = format_wecatalog_audit_report(report)

        if report_dir:
            import pathlib

            root = pathlib.Path(report_dir)
            root.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y%m%d_%H%M%S")
            tid = tag_id or "notag"
            path = root / f"wecatalog_audit_{tid}_{stamp}.txt"
            detail_lines = [report.text, "", "--- index 상세 ---"]
            for card in sorted(cards, key=_card_sort_key):
                di = str(card.get("dataIndex") or "").strip()
                if not di.isdigit():
                    continue
                idx = int(di)
                api_hit = api_by_di.get(di) or {}
                merged = _screen_item_from_card(
                    card, album_id=album_id, api_hit=api_hit if api_hit else None
                )
                gid = str(merged.get("goods_id") or "").strip()
                gnum = str(merged.get("goodsNum") or "").strip()
                nimg = _count_item_images(merged)
                if idx in collected:
                    st = "수집완료"
                elif idx in excluded:
                    st = "제외"
                elif idx in failed:
                    st = "실패기록"
                elif idx in no_cover:
                    st = "썸네일없음"
                else:
                    st = "미수집"
                title = str(merged.get("title") or card.get("title") or "")[:40]
                detail_lines.append(
                    f"index {idx}: {st} · img {nimg} · "
                    f"goods={gid[:18] or '-'} · no={gnum or '-'} · {title}"
                )
            path.write_text("\n".join(detail_lines), encoding="utf-8")
            report.report_path = str(path)
            report.text = format_wecatalog_audit_report(report)

        log(
            f"대조 완료 · 수집 {len(collected)} · 미수집 {len(pending)} · "
            f"제외 {len(excluded)} · 실패 {len(failed)}"
        )
        return report, report.text
    finally:
        session._timeout = old_timeout
        session.close()

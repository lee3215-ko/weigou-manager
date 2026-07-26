# -*- coding: utf-8 -*-
"""Windows IME helpers — commit Hangul composition before focus loss."""
from __future__ import annotations

import sys
from typing import Any

_NI_COMPOSITIONSTR = 0x0015
_CPS_COMPLETE = 0x0001
_GCS_COMPSTR = 0x0008

_imm = None
if sys.platform == "win32":
    try:
        import ctypes

        _imm = ctypes.windll.imm32
        _imm.ImmGetContext.argtypes = [ctypes.c_void_p]
        _imm.ImmGetContext.restype = ctypes.c_void_p
        _imm.ImmReleaseContext.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        _imm.ImmReleaseContext.restype = ctypes.c_bool
        _imm.ImmNotifyIME.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint,
            ctypes.c_uint,
            ctypes.c_uint,
        ]
        _imm.ImmNotifyIME.restype = ctypes.c_bool
        _imm.ImmGetCompositionStringW.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint,
            ctypes.c_void_p,
            ctypes.c_uint,
        ]
        _imm.ImmGetCompositionStringW.restype = ctypes.c_long
    except Exception:  # noqa: BLE001
        _imm = None


def _hwnds(widget: Any) -> list[int]:
    """Candidate HWNDs for a Tk widget (child + toplevel)."""
    out: list[int] = []
    try:
        out.append(int(widget.winfo_id()))
    except Exception:
        pass
    try:
        top = int(widget.winfo_toplevel().winfo_id())
        if top not in out:
            out.append(top)
    except Exception:
        pass
    return out


def get_composition(widget: Any) -> str:
    """Return the current IME preedit string, or ''."""
    if _imm is None:
        return ""
    import ctypes

    for hwnd in _hwnds(widget):
        himc = _imm.ImmGetContext(hwnd)
        if not himc:
            continue
        try:
            nbytes = int(_imm.ImmGetCompositionStringW(himc, _GCS_COMPSTR, None, 0))
            if nbytes <= 0:
                continue
            buf = ctypes.create_unicode_buffer(nbytes // 2 + 1)
            _imm.ImmGetCompositionStringW(himc, _GCS_COMPSTR, buf, nbytes)
            return buf.value or ""
        finally:
            _imm.ImmReleaseContext(hwnd, himc)
    return ""


def commit_composition(widget: Any) -> str:
    """Force-commit IME composition into the widget. Returns committed preedit text."""
    if widget is None or _imm is None:
        return ""
    preedit = get_composition(widget)
    for hwnd in _hwnds(widget):
        himc = _imm.ImmGetContext(hwnd)
        if not himc:
            continue
        try:
            _imm.ImmNotifyIME(himc, _NI_COMPOSITIONSTR, _CPS_COMPLETE, 0)
        finally:
            _imm.ImmReleaseContext(hwnd, himc)
    return preedit


def snapshot_widget_text(widget: Any) -> str:
    """Best-effort full text including visible Hangul composition."""
    try:
        if widget.winfo_class() in ("Text", "TScrolledText"):
            return widget.get("1.0", "end-1c")
        return widget.get()
    except Exception:
        return ""


def restore_text_if_stripped(widget: Any, snapshot: str, preedit: str) -> bool:
    """If focus-loss cancelled composition, put the missing text back."""
    if not snapshot and not preedit:
        return False
    try:
        cls = widget.winfo_class()
        if cls in ("Text", "TScrolledText"):
            current = widget.get("1.0", "end-1c")
            if snapshot and len(snapshot) > len(current) and snapshot.startswith(current):
                widget.delete("1.0", "end")
                widget.insert("1.0", snapshot)
                return True
            if preedit and not current.endswith(preedit):
                # snapshot may already be stripped — append preedit at insert
                widget.insert("insert", preedit)
                return True
            return False

        current = widget.get()
        if snapshot and len(snapshot) > len(current) and snapshot.startswith(current):
            widget.delete(0, "end")
            widget.insert(0, snapshot)
            return True
        if preedit and not current.endswith(preedit):
            try:
                idx = widget.index("insert")
            except Exception:
                idx = len(current)
            widget.insert(idx, preedit)
            return True
    except Exception:
        return False
    return False

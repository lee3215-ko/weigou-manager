# -*- coding: utf-8 -*-
"""Launch 微购相册 with Chromium remote debugging enabled."""
from __future__ import annotations

import os
import pathlib
import subprocess
import time

DEFAULT_EXE = pathlib.Path(r"C:\Program Files\微购相册\WegoAlbum.exe")
DEFAULT_PORT = 9222


def find_exe() -> pathlib.Path | None:
    candidates = [
        DEFAULT_EXE,
        pathlib.Path(r"C:\Program Files (x86)\微购相册\WegoAlbum.exe"),
        pathlib.Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "微购相册" / "WegoAlbum.exe",
    ]
    for p in candidates:
        if p and p.exists():
            return p
    return None


def is_running() -> bool:
    """Fast process check without spawning tasklist every time."""
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            TH32CS_SNAPPROCESS = 0x00000002

            class PROCESSENTRY32W(ctypes.Structure):
                _fields_ = [
                    ("dwSize", wintypes.DWORD),
                    ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                    ("th32ModuleID", wintypes.DWORD),
                    ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", ctypes.c_long),
                    ("dwFlags", wintypes.DWORD),
                    ("szExeFile", wintypes.WCHAR * 260),
                ]

            CreateToolhelp32Snapshot = kernel32.CreateToolhelp32Snapshot
            Process32FirstW = kernel32.Process32FirstW
            Process32NextW = kernel32.Process32NextW
            CloseHandle = kernel32.CloseHandle

            snap = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
            if snap == wintypes.HANDLE(-1).value:
                raise OSError("snapshot failed")
            try:
                entry = PROCESSENTRY32W()
                entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
                if not Process32FirstW(snap, ctypes.byref(entry)):
                    return False
                while True:
                    if entry.szExeFile.lower() == "wegoalbum.exe":
                        return True
                    if not Process32NextW(snap, ctypes.byref(entry)):
                        break
                return False
            finally:
                CloseHandle(snap)
        except Exception:
            pass
    try:
        out = subprocess.check_output(
            ["tasklist", "/FI", "IMAGENAME eq WegoAlbum.exe", "/NH"],
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        return "WegoAlbum.exe" in out
    except Exception:
        return False


def stop_app(timeout: float = 12.0) -> bool:
    if not is_running():
        return True
    subprocess.run(
        ["taskkill", "/IM", "WegoAlbum.exe", "/F"],
        capture_output=True,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    # CEF subprocesses
    subprocess.run(
        ["taskkill", "/IM", "CefSharp.BrowserSubprocess.exe", "/F"],
        capture_output=True,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not is_running():
            return True
        time.sleep(0.3)
    return not is_running()


def start_debug(port: int = DEFAULT_PORT) -> tuple[bool, str]:
    exe = find_exe()
    if not exe:
        return False, "WegoAlbum.exe 를 찾지 못했습니다. 微购相册 설치 경로를 확인하세요."

    if is_running():
        if not stop_app():
            return False, "실행 중인 微购相册을 종료하지 못했습니다. 직접 종료 후 다시 시도하세요."

    args = [
        str(exe),
        f"--remote-debugging-port={port}",
        "--remote-allow-origins=*",
    ]
    try:
        subprocess.Popen(
            args,
            cwd=str(exe.parent),
            creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
            if os.name == "nt"
            else 0,
        )
    except OSError as e:
        return False, f"실행 실패: {e}"

    # Wait briefly for process
    time.sleep(2.0)
    if not is_running():
        return False, "프로세스가 바로 종료되었습니다. 관리자 권한/백신 차단을 확인해 주세요."
    return (
        True,
        f"디버그 모드로 실행했습니다.\n포트 {port}\n앨범 화면을 연 뒤 [일괄 다운로드]를 누르세요.",
    )

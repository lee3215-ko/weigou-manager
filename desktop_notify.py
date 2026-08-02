# -*- coding: utf-8 -*-
"""Windows sound + taskbar balloon / toast for Manager alerts."""
from __future__ import annotations

import subprocess
import sys
import threading


def play_alert_sound() -> None:
    """Short system notification sound (non-blocking)."""
    if sys.platform != "win32":
        return

    def _play() -> None:
        try:
            import winsound

            winsound.PlaySound(
                "SystemNotification",
                winsound.SND_ALIAS | winsound.SND_ASYNC | winsound.SND_NODEFAULT,
            )
        except Exception:
            try:
                import winsound

                winsound.MessageBeep(winsound.MB_ICONASTERISK)
            except Exception:
                pass

    threading.Thread(target=_play, daemon=True).start()


def show_tray_balloon(title: str, message: str, *, ms: int = 6000) -> None:
    """Show a balloon near the Windows taskbar tray (works without extra packages)."""
    if sys.platform != "win32":
        return
    t = (title or "알림").replace("'", "''")[:80]
    m = (message or "").replace("'", "''")[:220]
    wait_sec = max(3, min(12, int(ms / 1000) + 1))
    # NotifyIcon balloon — appears above the taskbar notification area
    ps = f"""
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$ni = New-Object System.Windows.Forms.NotifyIcon
$ni.Icon = [System.Drawing.SystemIcons]::Information
$ni.BalloonTipIcon = [System.Windows.Forms.ToolTipIcon]::Info
$ni.BalloonTipTitle = '{t}'
$ni.BalloonTipText = '{m}'
$ni.Text = 'Weigou Manager'
$ni.Visible = $true
$ni.ShowBalloonTip({int(ms)})
Start-Sleep -Seconds {wait_sec}
$ni.Visible = $false
$ni.Dispose()
"""
    try:
        subprocess.Popen(
            [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                ps,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception:
        pass


def alert(title: str, message: str) -> None:
    """Sound + taskbar balloon together."""
    play_alert_sound()
    show_tray_balloon(title, message)

# -*- coding: utf-8 -*-
"""E2E: detached updater must relaunch the app after the parent process exits."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from updater import (  # noqa: E402
    _write_update_script,
    extract_zip_to_staging,
    get_update_log_path,
    get_update_running_path,
    get_update_temp_dir,
    launch_detached_updater,
    wait_updater_started,
)

SLUG = "WeigouRestartTest"
EXE_NAME = "WeigouManager.exe"


def _compile_dummy_exe(dest: Path, tag: str) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    cs = r"""
using System;
using System.IO;
using System.Threading;
class P {
  static void Main() {
    var dir = AppDomain.CurrentDomain.BaseDirectory;
    File.WriteAllText(Path.Combine(dir, "relaunch_ok.txt"), File.ReadAllText(Path.Combine(dir, "version_tag.txt")));
    Thread.Sleep(30000);
  }
}
"""
    cmd = f"""
$src = @'
{cs}
'@
Add-Type -OutputType ConsoleApplication -OutputAssembly '{dest}' -TypeDefinition $src
"""
    r = subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command", cmd],
        capture_output=True,
        text=True,
    )
    if r.returncode != 0 or not dest.is_file():
        raise RuntimeError(f"dummy exe compile failed: {r.stderr or r.stdout}")
    (dest.parent / "version_tag.txt").write_text(tag, encoding="utf-8")


def main() -> int:
    temp = Path(tempfile.mkdtemp(prefix="weigou_upd_test_", dir=str(get_update_temp_dir())))
    install = temp / "WeigouManager"
    staging_src = temp / "payload" / "WeigouManager"
    zip_path = temp / "WeigouManager.zip"
    log_path = get_update_log_path(SLUG)
    running = get_update_running_path(SLUG)
    for p in (log_path, running, Path(str(running) + ".lock")):
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass

    _compile_dummy_exe(install / EXE_NAME, "OLD")
    (install / "data").mkdir(parents=True, exist_ok=True)
    (install / "data" / "keep.txt").write_text("keep-me", encoding="utf-8")
    _compile_dummy_exe(staging_src / EXE_NAME, "NEW")
    (staging_src / "new_file.txt").write_text("from-update", encoding="utf-8")

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in staging_src.rglob("*"):
            if path.is_file():
                zf.write(path, path.relative_to(staging_src.parent).as_posix())

    dummy = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(90)"],
        cwd=str(temp),
    )
    dummy_pid = dummy.pid
    print(f"dummy_pid={dummy_pid} install={install}", flush=True)

    staging_dir = temp / "staging"
    extract_zip_to_staging(zip_path, staging_dir)
    script_path = get_update_temp_dir() / f"{SLUG}_update_{os.getpid()}.ps1"
    _write_update_script(script_path, app_slug=SLUG)
    launch_detached_updater(
        script_path=script_path,
        staging_dir=staging_dir,
        install_dir=install,
        exe_path=install / EXE_NAME,
        inner="WeigouManager",
        wait_pid=dummy_pid,
        app_slug=SLUG,
    )
    started = wait_updater_started(SLUG, timeout=12.0)
    print(f"handshake={started}", flush=True)
    if not started:
        print("FAIL: updater handshake missing", flush=True)
        print("--- log ---", flush=True)
        print(log_path.read_text(encoding="utf-8", errors="replace") if log_path.is_file() else "(no log)", flush=True)
        dummy.kill()
        return 1

    dummy.terminate()
    try:
        dummy.wait(timeout=5)
    except subprocess.TimeoutExpired:
        dummy.kill()

    marker = install / "relaunch_ok.txt"
    ok = False
    tag = ""
    for _ in range(50):
        time.sleep(0.4)
        new_file = (install / "new_file.txt").is_file()
        keep = (install / "data" / "keep.txt").is_file()
        tag = (
            (install / "version_tag.txt").read_text(encoding="utf-8").strip()
            if (install / "version_tag.txt").is_file()
            else ""
        )
        relaunched = marker.is_file()
        if new_file and keep and tag == "NEW" and relaunched:
            ok = True
            print(f"relaunched tag={tag} marker={marker.read_text(encoding='utf-8').strip()}", flush=True)
            break

    subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-Command",
            "Get-Process -Name WeigouManager -ErrorAction SilentlyContinue | Stop-Process -Force",
        ],
        capture_output=True,
    )

    print("--- update log ---", flush=True)
    if log_path.is_file():
        text = log_path.read_text(encoding="utf-8-sig", errors="replace")
        sys.stdout.buffer.write((text + "\n").encode("utf-8", errors="replace"))
        sys.stdout.buffer.flush()
    else:
        print("(no log)", flush=True)

    if not ok:
        print(
            f"FAIL: app did not relaunch after parent exit (tag={tag!r} "
            f"new_file={(install / 'new_file.txt').is_file()} "
            f"keep={(install / 'data' / 'keep.txt').is_file()} "
            f"marker={marker.is_file()})",
            flush=True,
        )
        return 1
    print("PASS: update copied files, kept data/, relaunched exe", flush=True)
    shutil.rmtree(temp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

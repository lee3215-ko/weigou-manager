"""GitHub version.json check and Windows onedir auto-update."""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

_RAW_GITHUB_RE = re.compile(
    r"^https://raw\.githubusercontent\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/(?P<branch>[^/]+)/(?P<path>.+)$"
)

# Default release zip for this app (never leave NaverReport leftovers).
DEFAULT_RELEASE_ASSET = "WeigouManager.zip"
DEFAULT_APP_SLUG = "WeigouManager"


def _ca_bundle_path() -> str | None:
    if getattr(sys, "frozen", False):
        base = getattr(sys, "_MEIPASS", "")
        for candidate in (
            os.path.join(base, "certifi", "cacert.pem"),
            os.path.join(base, "cacert.pem"),
        ):
            if candidate and os.path.isfile(candidate):
                return candidate
    try:
        import certifi

        return certifi.where()
    except ImportError:
        return None


def _ssl_context() -> ssl.SSLContext:
    """PyInstaller exe에서도 HTTPS 검증이 되도록 certifi CA 번들 사용."""
    cafile = _ca_bundle_path()
    if cafile:
        return ssl.create_default_context(cafile=cafile)
    return ssl.create_default_context()


def _urlopen(request: urllib.request.Request, *, timeout: int):
    return urllib.request.urlopen(request, timeout=timeout, context=_ssl_context())


@dataclass(frozen=True)
class UpdateInfo:
    version: str
    url: str
    notes: str
    download_urls: tuple[str, ...] = ()


def parse_version(version: str) -> tuple[int, ...]:
    parts: list[int] = []
    for part in version.strip().split("."):
        digits = "".join(ch for ch in part if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts or (0,))


def is_newer(remote_version: str, local_version: str) -> bool:
    return parse_version(remote_version) > parse_version(local_version)


def _github_api_url(raw_url: str) -> str | None:
    match = _RAW_GITHUB_RE.match(raw_url.strip())
    if match is None:
        return None
    owner = match.group("owner")
    repo = match.group("repo")
    branch = match.group("branch")
    path = match.group("path")
    return (
        f"https://api.github.com/repos/{owner}/{repo}/contents/"
        f"{urllib.parse.quote(path)}?ref={urllib.parse.quote(branch)}"
    )


def _decode_json_bytes(raw: bytes) -> dict:
    text = raw.decode("utf-8-sig").strip()
    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise ValueError("version.json must be a JSON object")
    return payload


def _fetch_via_github_api(api_url: str, user_agent: str) -> dict | None:
    request = urllib.request.Request(
        api_url,
        headers={
            "User-Agent": user_agent,
            "Accept": "application/vnd.github+json",
        },
    )
    with _urlopen(request, timeout=15) as response:
        meta = json.loads(response.read().decode("utf-8-sig"))
    content = base64.b64decode(meta["content"]).decode("utf-8-sig")
    return _decode_json_bytes(content.encode("utf-8"))


def _fetch_via_raw_url(raw_url: str, user_agent: str) -> dict:
    parsed = urllib.parse.urlparse(raw_url.strip())
    query = urllib.parse.parse_qs(parsed.query)
    query["_"] = [str(int(time.time()))]
    busted_url = parsed._replace(query=urllib.parse.urlencode(query, doseq=True)).geturl()
    request = urllib.request.Request(
        busted_url,
        headers={"User-Agent": user_agent, "Cache-Control": "no-cache"},
    )
    with _urlopen(request, timeout=15) as response:
        return _decode_json_bytes(response.read())


def fetch_version_payload(version_url: str, user_agent: str) -> dict | None:
    url = version_url.strip()
    if not url:
        return None
    api_url = _github_api_url(url)
    if api_url:
        try:
            return _fetch_via_github_api(api_url, user_agent)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ValueError, KeyError):
            pass
    try:
        return _fetch_via_raw_url(url, user_agent)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ValueError):
        return None


def check_for_update(
    version_url: str,
    current_version: str,
    *,
    app_name: str = "App",
    release_asset: str = DEFAULT_RELEASE_ASSET,
) -> UpdateInfo | None:
    user_agent = f"{app_name}/{current_version}"
    payload = fetch_version_payload(version_url, user_agent)
    if payload is None:
        return None
    remote_version = str(payload.get("version", "")).strip()
    if not remote_version or not is_newer(remote_version, current_version):
        return None
    download_urls = collect_download_urls(
        payload,
        version_url=version_url,
        user_agent=user_agent,
        asset_name=release_asset or DEFAULT_RELEASE_ASSET,
    )
    primary = download_urls[0] if download_urls else str(payload.get("url", "")).strip()
    return UpdateInfo(
        version=remote_version,
        url=primary,
        notes=str(payload.get("notes", "")).strip(),
        download_urls=download_urls,
    )


def can_auto_update() -> bool:
    return getattr(sys, "frozen", False) and sys.platform == "win32"


def get_install_dir() -> Path:
    if can_auto_update():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def get_update_log_path(app_slug: str = DEFAULT_APP_SLUG) -> Path:
    return Path(tempfile.gettempdir()) / f"{app_slug}_update.log"


ProgressCallback = Callable[[int, int], None]


def validate_zip_file(zip_path: Path, min_bytes: int = 1024) -> None:
    if not zip_path.is_file():
        raise ValueError("다운로드 파일이 없습니다.")
    size = zip_path.stat().st_size
    if size < min_bytes:
        raise ValueError(f"다운로드 파일이 너무 작습니다 ({size} bytes).")
    with zip_path.open("rb") as handle:
        header = handle.read(4)
    if header[:2] != b"PK":
        raise ValueError("다운로드 파일이 zip 형식이 아닙니다 (GitHub 오류 페이지일 수 있습니다).")


def _github_repo_from_version_url(version_url: str) -> tuple[str, str] | None:
    match = _RAW_GITHUB_RE.match(version_url.strip())
    if match is None:
        return None
    return match.group("owner"), match.group("repo")


def _release_tag(version: str) -> str:
    version = version.strip()
    return version if version.startswith("v") else f"v{version}"


def _versioned_release_url(owner: str, repo: str, version: str, asset: str) -> str:
    return (
        f"https://github.com/{owner}/{repo}/releases/download/"
        f"{_release_tag(version)}/{asset}"
    )


def _latest_release_url(owner: str, repo: str, asset: str) -> str:
    return f"https://github.com/{owner}/{repo}/releases/latest/download/{asset}"


def _github_api_asset_url(owner: str, repo: str, asset_id: int) -> str:
    return f"https://api.github.com/repos/{owner}/{repo}/releases/assets/{asset_id}"


def _fetch_release_asset_id(
    owner: str,
    repo: str,
    version: str,
    asset_name: str,
    user_agent: str,
) -> int | None:
    tag = _release_tag(version)
    endpoints = (
        f"https://api.github.com/repos/{owner}/{repo}/releases/tags/{tag}",
        f"https://api.github.com/repos/{owner}/{repo}/releases/latest",
    )
    for endpoint in endpoints:
        try:
            request = urllib.request.Request(
                endpoint,
                headers={
                    "User-Agent": user_agent,
                    "Accept": "application/vnd.github+json",
                },
            )
            with _urlopen(request, timeout=20) as response:
                release = json.loads(response.read().decode("utf-8-sig"))
            for asset in release.get("assets") or []:
                if asset.get("name") == asset_name and asset.get("id"):
                    return int(asset["id"])
        except (
            urllib.error.URLError,
            TimeoutError,
            json.JSONDecodeError,
            ValueError,
            TypeError,
            KeyError,
        ):
            continue
    return None


def _dedupe_urls(urls: list[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    ordered: list[str] = []
    for raw in urls:
        url = raw.strip()
        if not url or url in seen:
            continue
        seen.add(url)
        ordered.append(url)
    return tuple(ordered)


def collect_download_urls(
    payload: dict,
    *,
    version_url: str = "",
    user_agent: str = "App",
    asset_name: str = DEFAULT_RELEASE_ASSET,
) -> tuple[str, ...]:
    """다운로드 URL 후보. api.github.com 자산 URL을 우선해 github.com DNS 오류를 우회."""
    urls: list[str] = []
    version = str(payload.get("version", "")).strip()
    asset = (asset_name or DEFAULT_RELEASE_ASSET).strip()

    for key in ("url", "download_url", "api_download_url"):
        value = str(payload.get(key, "")).strip()
        if value:
            urls.append(value)
    for item in payload.get("download_urls") or []:
        value = str(item).strip()
        if value:
            urls.append(value)

    owner_repo = _github_repo_from_version_url(version_url)
    if owner_repo and version:
        owner, repo = owner_repo
        asset_id = payload.get("asset_id")
        try:
            asset_id = int(asset_id) if asset_id is not None else None
        except (TypeError, ValueError):
            asset_id = None
        if asset_id is None:
            asset_id = _fetch_release_asset_id(owner, repo, version, asset, user_agent)
        if asset_id is not None:
            urls.insert(0, _github_api_asset_url(owner, repo, asset_id))
        urls.append(_versioned_release_url(owner, repo, version, asset))
        urls.append(_latest_release_url(owner, repo, asset))

    return _dedupe_urls(urls)


def format_network_error(exc: BaseException) -> str:
    message = str(exc).strip()
    lowered = message.lower()
    if (
        "getaddrinfo failed" in lowered
        or "11001" in message
        or "name or service not known" in lowered
    ):
        return (
            "인터넷 연결 또는 DNS 설정을 확인해 주세요.\n"
            "(GitHub 서버 주소를 찾지 못했습니다)\n\n"
            "· Wi-Fi/유선 연결 확인\n"
            "· 회사망·보안 프로그램이 GitHub 차단 여부 확인\n"
            "· 「아니오」로 브라우저에서 직접 받기"
        )
    if "timed out" in lowered or "timeout" in lowered:
        return "다운로드 시간이 초과되었습니다. 네트워크 상태를 확인한 뒤 다시 시도해 주세요."
    if "certificate" in lowered or "ssl" in lowered:
        return "보안 인증서(SSL) 오류입니다. PC 날짜/시간이 맞는지 확인해 주세요."
    return message or repr(exc)


def _download_request(url: str, user_agent: str) -> urllib.request.Request:
    headers = {"User-Agent": user_agent}
    if "api.github.com" in url and "/releases/assets/" in url:
        headers["Accept"] = "application/octet-stream"
    return urllib.request.Request(url.strip(), headers=headers)


def download_file(
    url: str,
    dest: Path,
    *,
    user_agent: str,
    on_progress: ProgressCallback | None = None,
    timeout: int = 600,
) -> None:
    request = _download_request(url, user_agent)
    with _urlopen(request, timeout=timeout) as response:
        total = int(response.headers.get("Content-Length", 0) or 0)
        downloaded = 0
        dest.parent.mkdir(parents=True, exist_ok=True)
        with dest.open("wb") as handle:
            while True:
                chunk = response.read(256 * 1024)
                if not chunk:
                    break
                handle.write(chunk)
                downloaded += len(chunk)
                if on_progress is not None:
                    on_progress(downloaded, total)


def download_file_with_fallbacks(
    urls: list[str] | tuple[str, ...],
    dest: Path,
    *,
    user_agent: str,
    on_progress: ProgressCallback | None = None,
    timeout: int = 600,
    retries: int = 1,
) -> str:
    candidates = _dedupe_urls(list(urls))
    if not candidates:
        raise ValueError("다운로드 URL이 없습니다.")

    errors: list[str] = []
    for url in candidates:
        for attempt in range(retries + 1):
            try:
                if attempt > 0:
                    time.sleep(1.5 * attempt)
                download_file(
                    url,
                    dest,
                    user_agent=user_agent,
                    on_progress=on_progress,
                    timeout=timeout,
                )
                return url
            except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
                errors.append(f"{url} → {format_network_error(exc)}")
    raise urllib.error.URLError("\n\n".join(errors))


def extract_zip_to_staging(zip_path: Path, staging_dir: Path) -> Path:
    """zip을 임시 폴더에 풀고 복사 원본 경로 반환."""
    if staging_dir.exists():
        shutil.rmtree(staging_dir, ignore_errors=True)
    staging_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "r") as archive:
        archive.extractall(staging_dir)
    return staging_dir


def _write_update_script(script_path: Path, *, app_slug: str) -> None:
    # UTF-8 BOM required so PowerShell -File parses correctly on Korean Windows.
    script_path.write_text(
        rf"""param(
    [Parameter(Mandatory=$true)][string]$Staging,
    [Parameter(Mandatory=$true)][string]$Install,
    [Parameter(Mandatory=$true)][string]$Exe,
    [Parameter(Mandatory=$true)][string]$Inner,
    [Parameter(Mandatory=$true)][int]$WaitPid
)
$ErrorActionPreference = "Continue"
$Log = Join-Path $env:TEMP "{app_slug}_update.log"
function Write-Log([string]$Message) {{
    Add-Content -Path $Log -Value ("[{{0}}] {{1}}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message)
}}

Write-Log "update start (powershell)"
Write-Log "Staging=$Staging"
Write-Log "Install=$Install"
Write-Log "Exe=$Exe"
Write-Log "Inner=$Inner"
Write-Log "WaitPid=$WaitPid"

$exeName = [IO.Path]::GetFileName($Exe)
$procName = [IO.Path]::GetFileNameWithoutExtension($Exe)

$deadline = (Get-Date).AddSeconds(90)
while ((Get-Date) -lt $deadline) {{
    if (-not (Get-Process -Id $WaitPid -ErrorAction SilentlyContinue)) {{ break }}
    Start-Sleep -Seconds 1
}}
if (Get-Process -Id $WaitPid -ErrorAction SilentlyContinue) {{
    Write-Log "force stop pid $WaitPid"
    Stop-Process -Id $WaitPid -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2
}}

# Also stop any leftover app instances locking files
Get-Process -Name $procName -ErrorAction SilentlyContinue | ForEach-Object {{
    Write-Log ("stop leftover pid " + $_.Id)
    Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue
}}
Start-Sleep -Seconds 2
Write-Log "process wait done"

$src = Join-Path $Staging $Inner
if (-not (Test-Path -LiteralPath $src)) {{
    Write-Log "inner folder missing, search for $exeName under staging"
    $hit = Get-ChildItem -LiteralPath $Staging -Recurse -Filter $exeName -File -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($hit) {{
        $src = $hit.Directory.FullName
        Write-Log "found exe under $src"
    }} else {{
        $src = $Staging
    }}
}} elseif (-not (Test-Path -LiteralPath (Join-Path $src $exeName))) {{
    $hit = Get-ChildItem -LiteralPath $Staging -Recurse -Filter $exeName -File -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($hit) {{
        $src = $hit.Directory.FullName
        Write-Log "relocated src to $src"
    }}
}}

Write-Log "robocopy `"$src`" -> `"$Install`" (data excluded)"
& robocopy $src $Install /E /IS /IT /XD data /R:10 /W:2 /NFL /NDL /NJH /NJS | Out-Null
$rc = $LASTEXITCODE
Write-Log "robocopy exit=$rc"
if ($rc -ge 8) {{
    Write-Log "robocopy failed code $rc — still attempting restart"
}}

Start-Sleep -Seconds 1
$workDir = Split-Path -Parent $Exe
if (-not (Test-Path -LiteralPath $Exe)) {{
    $fallback = Join-Path $src $exeName
    Write-Log "install exe missing, fallback=$fallback"
    if (Test-Path -LiteralPath $fallback) {{
        Start-Process -LiteralPath $fallback -WorkingDirectory (Split-Path -Parent $fallback)
        Write-Log "started fallback exe"
        Remove-Item -LiteralPath $Staging -Recurse -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $PSCommandPath -Force -ErrorAction SilentlyContinue
        exit 0
    }}
    Write-Log "FATAL: cannot find exe to restart"
    exit 1
}}

Write-Log "starting $Exe (cwd=$workDir)"
try {{
    Start-Process -LiteralPath $Exe -WorkingDirectory $workDir
}} catch {{
    Write-Log ("Start-Process failed: " + $_)
}}
Start-Sleep -Seconds 3
if (-not (Get-Process -Name $procName -ErrorAction SilentlyContinue)) {{
    Write-Log "process not up — retry via cmd start"
    $cmd = 'cmd.exe'
    $arg = '/c start "" "' + $Exe + '"'
    Start-Process -FilePath $cmd -ArgumentList $arg -WorkingDirectory $workDir -WindowStyle Hidden
    Start-Sleep -Seconds 2
}}
if (Get-Process -Name $procName -ErrorAction SilentlyContinue) {{
    Write-Log "update success — app running"
}} else {{
    Write-Log "WARNING: app still not running after restart attempts"
}}

Remove-Item -LiteralPath $Staging -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $PSCommandPath -Force -ErrorAction SilentlyContinue
exit 0
""",
        encoding="utf-8-sig",
    )


def schedule_apply_update(
    zip_path: Path,
    *,
    install_dir: Path | None = None,
    exe_name: str,
    zip_inner_folder: str | None = None,
    app_slug: str = DEFAULT_APP_SLUG,
) -> None:
    if not can_auto_update():
        raise RuntimeError("Auto-update works only in packaged exe builds.")

    validate_zip_file(zip_path)

    target_dir = install_dir or get_install_dir()
    inner = zip_inner_folder or target_dir.name
    exe_path = target_dir / exe_name
    slug = (app_slug or DEFAULT_APP_SLUG).strip() or DEFAULT_APP_SLUG
    staging_dir = Path(tempfile.gettempdir()) / f"{slug}_staging_{os.getpid()}"

    try:
        extract_zip_to_staging(zip_path, staging_dir)
    except (zipfile.BadZipFile, OSError) as exc:
        shutil.rmtree(staging_dir, ignore_errors=True)
        raise RuntimeError(f"업데이트 zip 풀기 실패: {exc}") from exc

    exe_hits = list(staging_dir.rglob(exe_name))
    if not exe_hits:
        shutil.rmtree(staging_dir, ignore_errors=True)
        raise RuntimeError(
            f"업데이트 zip 안에 {exe_name} 이(가) 없습니다. 배포 zip 구조를 확인하세요."
        )

    script_path = Path(tempfile.gettempdir()) / f"{slug}_update_{os.getpid()}.ps1"
    _write_update_script(script_path, app_slug=slug)

    # Detach from this process tree so os._exit does not kill the updater.
    flags = 0
    if hasattr(subprocess, "DETACHED_PROCESS"):
        flags |= subprocess.DETACHED_PROCESS
    if hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
        flags |= subprocess.CREATE_NEW_PROCESS_GROUP
    if hasattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB"):
        flags |= subprocess.CREATE_BREAKAWAY_FROM_JOB

    # Launcher .cmd avoids PowerShell being tied to the dying GUI process.
    cmd_path = Path(tempfile.gettempdir()) / f"{slug}_update_{os.getpid()}.cmd"
    cmd_path.write_text(
        "\r\n".join(
            [
                "@echo off",
                f'start "" /b powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "{script_path}" '
                f'-Staging "{staging_dir}" -Install "{target_dir}" -Exe "{exe_path}" '
                f'-Inner "{inner}" -WaitPid {os.getpid()}',
                "exit /b 0",
                "",
            ]
        ),
        encoding="utf-8",
    )

    subprocess.Popen(
        ["cmd.exe", "/c", str(cmd_path)],
        creationflags=flags,
        close_fds=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        cwd=str(tempfile.gettempdir()),
    )

    try:
        zip_path.unlink(missing_ok=True)
    except OSError:
        pass

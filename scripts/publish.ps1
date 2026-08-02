param(
    [string]$Notes = "업데이트",
    [ValidateSet("patch", "minor", "major", "none")]
    [string]$Bump = "",
    [switch]$SkipBuild
)

$ErrorActionPreference = "Stop"
$Root = Split-Path $PSScriptRoot -Parent
Set-Location $Root
. (Join-Path $PSScriptRoot "gh-env.ps1")

function Invoke-Git {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$GitArgs)
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    & git @GitArgs 2>&1 | Out-Null
    $code = $LASTEXITCODE
    $ErrorActionPreference = $prev
    if ($code -ne 0) { throw "git failed: git @GitArgs" }
}

function Write-TextNoBom([string]$Path, [string]$Text) {
    $utf8NoBom = New-Object System.Text.UTF8Encoding $false
    [System.IO.File]::WriteAllText($Path, $Text, $utf8NoBom)
}

function Read-DeployConfig {
    Get-Content (Join-Path $Root "deploy.json") -Raw | ConvertFrom-Json
}

function Read-AppVersion($cfg) {
    $path = Join-Path $Root $cfg.version.file
    $text = Get-Content $path -Raw
    $var = [regex]::Escape($cfg.version.variable)
    if ($text -match "${var}\s*=\s*`"([^`"]+)`"") {
        return $Matches[1]
    }
    throw "Version not found: $($cfg.version.variable) in $($cfg.version.file)"
}

function Set-AppVersion($cfg, [string]$Version) {
    $path = Join-Path $Root $cfg.version.file
    $text = Get-Content $path -Raw
    $var = [regex]::Escape($cfg.version.variable)
    $text = $text -replace "${var}\s*=\s*`"[^`"]+`"", "$($cfg.version.variable) = `"$Version`""
    Write-TextNoBom $path $text
}

function Bump-Version([string]$Version, [string]$Part) {
    $parts = $Version.Split(".")
    if ($parts.Count -lt 3) { throw "Invalid version: $Version" }
    [int]$major = $parts[0]
    [int]$minor = $parts[1]
    [int]$patch = $parts[2]
    switch ($Part) {
        "major" { $major++; $minor = 0; $patch = 0 }
        "minor" { $minor++; $patch = 0 }
        "patch" { $patch++ }
        "none" { }
    }
    return "$major.$minor.$patch"
}

function Write-VersionJson($cfg, [string]$Version, [string]$ReleaseNotes) {
    $tag = "v$Version"
    $owner = $cfg.github_owner
    $repo = $cfg.github_repo
    $asset = $cfg.release_asset
    $downloadUrlLatest = "https://github.com/$owner/$repo/releases/latest/download/$asset"
    $downloadUrlVersioned = "https://github.com/$owner/$repo/releases/download/$tag/$asset"
    $assetId = $null
    try {
        $assetId = gh api "repos/$owner/$repo/releases/tags/$tag" --jq ".assets[] | select(.name==`"$asset`") | .id" 2>$null
    } catch {
        $assetId = $null
    }
    if (-not $assetId) {
        try {
            $assetId = gh api "repos/$owner/$repo/releases/latest" --jq ".assets[] | select(.name==`"$asset`") | .id" 2>$null
        } catch {
            $assetId = $null
        }
    }
    $apiDownloadUrl = $null
    if ($assetId) {
        $apiDownloadUrl = "https://api.github.com/repos/$owner/$repo/releases/assets/$assetId"
    }
    $payload = [ordered]@{
        version      = $Version
        url          = if ($apiDownloadUrl) { $apiDownloadUrl } else { $downloadUrlVersioned }
        download_url = $downloadUrlVersioned
        notes        = $ReleaseNotes
    }
    if ($apiDownloadUrl) {
        $payload.api_download_url = $apiDownloadUrl
        $payload.asset_id = [int]$assetId
    }
    $payload.download_urls = @(
        $(if ($apiDownloadUrl) { $apiDownloadUrl }),
        $downloadUrlVersioned,
        $downloadUrlLatest
    ) | Where-Object { $_ }
    $json = $payload | ConvertTo-Json -Depth 4
    $path = Join-Path $Root "version.json"
    Write-TextNoBom $path ($json + "`n")
}

function Ensure-GitRemote($cfg) {
    if (-not (Test-Path (Join-Path $Root ".git"))) {
        git init | Out-Null
    }
    $branch = git branch --show-current 2>$null
    if ($branch -and $branch -ne "main") {
        git branch -M main | Out-Null
    } elseif (-not $branch) {
        git checkout -B main 2>$null | Out-Null
    }
    $remoteUrl = "https://github.com/$($cfg.github_owner)/$($cfg.github_repo).git"
    $hasOrigin = @(git remote 2>$null) -contains "origin"
    if (-not $hasOrigin) {
        git remote add origin $remoteUrl
        Write-Host "[git] origin: $remoteUrl"
    }
}

function Ensure-GhAuth {
    Invoke-Gh auth status *> $null
    if ($LASTEXITCODE -ne 0) {
        throw "Run .\scripts\setup-github.ps1 or gh auth login"
    }
}

$cfg = Read-DeployConfig
$bumpPart = if ($Bump) { $Bump } else { $cfg.default_bump }
$current = Read-AppVersion $cfg
$newVersion = Bump-Version $current $bumpPart
$tag = "v$newVersion"
$displayName = if ($cfg.app_display_name) { $cfg.app_display_name } else { $cfg.github_repo }

Write-Host "============================================"
Write-Host " $displayName deploy"
Write-Host " version: $current -> $newVersion"
Write-Host "============================================"

Set-AppVersion $cfg $newVersion
Write-VersionJson $cfg $newVersion $Notes

if (-not $SkipBuild) {
    Write-Host "[1/4] Building..."
    $buildScript = Join-Path $Root $cfg.build.script
    if (-not (Test-Path $buildScript)) { throw "Build script missing: $($cfg.build.script)" }
    & $buildScript
    if ($LASTEXITCODE -ne 0) { throw "Build failed" }
}

$distDir = Join-Path $Root ($cfg.build.dist_dir -replace "/", "\")
if (-not (Test-Path $distDir)) {
    throw "Build output missing: $($cfg.build.dist_dir)"
}

Write-Host "[2/4] Creating zip..."
$zipPath = Join-Path $Root "dist\$($cfg.release_asset)"
$distParent = Split-Path $zipPath -Parent
if (-not (Test-Path $distParent)) { New-Item -ItemType Directory -Path $distParent | Out-Null }
if (Test-Path $zipPath) { Remove-Item $zipPath -Force }
Compress-Archive -Path $distDir -DestinationPath $zipPath -Force

Ensure-GhInstalled
Ensure-GitRemote $cfg
Ensure-GhAuth

Write-Host "[3/4] Pushing to GitHub..."
$addArgs = @()
foreach ($item in $cfg.git_add) {
    $addArgs += $item
}
if ($addArgs.Count -gt 0) {
    Invoke-Git add @addArgs
}
Invoke-Git add deploy.json deploy.bat version.json scripts
Invoke-Git add -u

if (git status --porcelain) {
    Invoke-Git commit -m "Release $newVersion"
}

Invoke-Git push -u origin main
if ($LASTEXITCODE -ne 0) {
    Write-Host "[git] pull --rebase then push..."
    Invoke-Git pull origin main --rebase
    Invoke-Git push -u origin main
}

Write-Host "[4/4] GitHub Release..."
if (Test-GhRelease $tag) {
    Invoke-Gh release upload $tag $zipPath --clobber
    Invoke-Gh release edit $tag --notes $Notes --title $newVersion
} else {
    Invoke-Gh release create $tag $zipPath --title $newVersion --notes $Notes --latest
}

# Rewrite version.json with asset_id / api download URLs after release exists
Write-VersionJson $cfg $newVersion $Notes
if (git status --porcelain version.json) {
    Invoke-Git add version.json
    Invoke-Git commit -m "Update version.json download URLs for $newVersion"
    Invoke-Git push origin main
}

Write-Host ""
Write-Host "Done!"
Write-Host "  version: $newVersion"
Write-Host "  https://github.com/$($cfg.github_owner)/$($cfg.github_repo)/releases/tag/$tag"
Write-Host "  download: https://github.com/$($cfg.github_owner)/$($cfg.github_repo)/releases/latest/download/$($cfg.release_asset)"

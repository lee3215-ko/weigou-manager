param(
    [Parameter(Mandatory=$true)][string]$Staging,
    [Parameter(Mandatory=$true)][string]$Install,
    [Parameter(Mandatory=$true)][string]$Exe,
    [Parameter(Mandatory=$true)][string]$Inner,
    [Parameter(Mandatory=$true)][int]$WaitPid,
    [Parameter(Mandatory=$true)][string]$RunningFile,
    [Parameter(Mandatory=$true)][string]$LogFile,
    [Parameter(Mandatory=$false)][string]$DataLogFile = ""
)
$ErrorActionPreference = "Continue"
$Log = $LogFile
$Running = $RunningFile
$FailMarker = Join-Path $Install "data\update_failed.txt"

function Write-Log([string]$Message) {
    $line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
    foreach ($target in @($Log, $DataLogFile)) {
        if (-not $target) { continue }
        try {
            $parent = Split-Path -Parent $target
            if ($parent -and -not (Test-Path -LiteralPath $parent)) {
                New-Item -ItemType Directory -Path $parent -Force | Out-Null
            }
            Add-Content -LiteralPath $target -Value $line -Encoding UTF8
        } catch {}
    }
}

function Clear-FailMarker {
    if ($FailMarker -and (Test-Path -LiteralPath $FailMarker)) {
        Remove-Item -LiteralPath $FailMarker -Force -ErrorAction SilentlyContinue
    }
}

function Set-FailMarker([string]$Reason) {
    try {
        $parent = Split-Path -Parent $FailMarker
        if ($parent -and -not (Test-Path -LiteralPath $parent)) {
            New-Item -ItemType Directory -Path $parent -Force | Out-Null
        }
        Set-Content -LiteralPath $FailMarker -Value $Reason -Encoding UTF8
    } catch {}
}

try { Set-Content -LiteralPath $Running -Value $PID -Encoding ASCII } catch {
    Write-Log ("handshake write failed: " + $_)
}

Write-Log "update start pid=$PID"
Write-Log "Staging=$Staging"
Write-Log "Install=$Install"
Write-Log "Exe=$Exe"
Write-Log "Inner=$Inner"
Write-Log "WaitPid=$WaitPid"
Write-Log "RunningFile=$Running"
Write-Log "LogFile=$Log"
Write-Log "DataLogFile=$DataLogFile"

$exeName = [IO.Path]::GetFileName($Exe)
$procName = [IO.Path]::GetFileNameWithoutExtension($Exe)

$deadline = (Get-Date).AddSeconds(120)
while ((Get-Date) -lt $deadline) {
    if (-not (Get-Process -Id $WaitPid -ErrorAction SilentlyContinue)) { break }
    Start-Sleep -Seconds 1
}
if (Get-Process -Id $WaitPid -ErrorAction SilentlyContinue) {
    Write-Log "force stop pid $WaitPid"
    Stop-Process -Id $WaitPid -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2
}

Get-Process -Name $procName -ErrorAction SilentlyContinue | ForEach-Object {
    Write-Log ("stop leftover pid " + $_.Id)
    Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue
}

function Stop-ProcessesUnderInstall {
    param([string]$Root)
    if (-not $Root) { return }
    $rootNorm = $Root.TrimEnd('\')
    try {
        Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | ForEach-Object {
            $path = [string]$_.ExecutablePath
            if (-not $path) { return }
            if ($path.StartsWith($rootNorm, [System.StringComparison]::OrdinalIgnoreCase)) {
                Write-Log ("stop install-tree pid " + $_.ProcessId + " " + $path)
                Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
            }
        }
    } catch {
        Write-Log ("Stop-ProcessesUnderInstall failed: " + $_)
    }
}

Stop-ProcessesUnderInstall -Root $Install
Start-Sleep -Seconds 5
Write-Log "process wait done"

$src = Join-Path $Staging $Inner
if (-not (Test-Path -LiteralPath $src)) {
    Write-Log "inner folder missing, search for $exeName under staging"
    $hit = Get-ChildItem -LiteralPath $Staging -Recurse -Filter $exeName -File -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($hit) {
        $src = $hit.Directory.FullName
        Write-Log "found exe under $src"
    } else {
        $msg = "FATAL: $exeName not found in staging zip"
        Write-Log $msg
        Set-FailMarker $msg
        Remove-Item -LiteralPath $Running -Force -ErrorAction SilentlyContinue
        exit 1
    }
} elseif (-not (Test-Path -LiteralPath (Join-Path $src $exeName))) {
    $hit = Get-ChildItem -LiteralPath $Staging -Recurse -Filter $exeName -File -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($hit) {
        $src = $hit.Directory.FullName
        Write-Log "relocated src to $src"
    }
}

$srcExe = Join-Path $src $exeName
$srcSize = 0
try { if (Test-Path -LiteralPath $srcExe) { $srcSize = (Get-Item -LiteralPath $srcExe).Length } } catch {}
Write-Log "source exe size=$srcSize path=$srcExe"

$robocopyOk = $false
for ($attempt = 1; $attempt -le 6; $attempt++) {
    Write-Log "robocopy attempt $attempt `"$src`" -> `"$Install`" (data excluded)"
    & robocopy $src $Install /E /IS /IT /XD data /R:5 /W:3 /NFL /NDL /NJH /NJS | Out-Null
    $rc = $LASTEXITCODE
    Write-Log "robocopy exit=$rc"
    if ($rc -ge 8) {
        Write-Log "robocopy failed attempt $attempt - wait and retry"
        Stop-ProcessesUnderInstall -Root $Install
        Start-Sleep -Seconds 4
        continue
    }
    $destExe = Join-Path $Install $exeName
    if (-not (Test-Path -LiteralPath $destExe)) {
        Write-Log "dest exe missing after robocopy attempt $attempt"
        Start-Sleep -Seconds 3
        continue
    }
    $destSize = 0
    try { $destSize = (Get-Item -LiteralPath $destExe).Length } catch {}
    if ($srcSize -gt 0 -and $destSize -gt 0 -and $destSize -eq $srcSize) {
        $robocopyOk = $true
        Write-Log "copy verified size=$destSize"
        break
    }
    if ($srcSize -le 0 -and $destSize -gt 0) {
        $robocopyOk = $true
        Write-Log "copy verified dest size=$destSize (source size unknown)"
        break
    }
    Write-Log "size mismatch src=$srcSize dest=$destSize attempt $attempt"
    Start-Sleep -Seconds 3
}

if (-not $robocopyOk) {
    $msg = "robocopy failed after retries. Install folder may be read-only (Program Files) or files are locked."
    Write-Log $msg
    Set-FailMarker $msg
    Remove-Item -LiteralPath $Running -Force -ErrorAction SilentlyContinue
    exit 1
}

Clear-FailMarker

Start-Sleep -Seconds 2
$workDir = Split-Path -Parent $Exe
if (-not (Test-Path -LiteralPath $Exe)) {
    $fallback = Join-Path $src $exeName
    Write-Log "install exe missing, fallback=$fallback"
    if (Test-Path -LiteralPath $fallback) {
        $Exe = $fallback
        $workDir = Split-Path -Parent $fallback
    } else {
        $msg = "FATAL: cannot find exe to restart"
        Write-Log $msg
        Set-FailMarker $msg
        Remove-Item -LiteralPath $Running -Force -ErrorAction SilentlyContinue
        exit 1
    }
}

function Start-App {
    param([string]$Target, [string]$Dir)
    Write-Log "starting $Target (cwd=$Dir)"
    try {
        $p = Start-Process -FilePath $Target -WorkingDirectory $Dir -PassThru -WindowStyle Normal
        Write-Log ("Start-Process pid=" + $p.Id)
        return $true
    } catch {
        Write-Log ("Start-Process failed: " + $_)
    }
    $vbs = Join-Path $Dir "weigou_relaunch.vbs"
    $line1 = 'Set s=CreateObject("WScript.Shell")'
    $line2 = 's.CurrentDirectory="' + ($Dir -replace '"', '""') + '"'
    $line3 = 's.Run """' + ($Target -replace '"', '""') + '""",1,False'
    try {
        $vbsBody = $line1 + "`r`n" + $line2 + "`r`n" + $line3 + "`r`n"
        [System.IO.File]::WriteAllText($vbs, $vbsBody, [System.Text.Encoding]::Unicode)
        $wp = Start-Process -FilePath "wscript.exe" -ArgumentList $vbs -PassThru -WindowStyle Hidden
        Write-Log ("wscript pid=" + $wp.Id)
        return $true
    } catch {
        Write-Log ("wscript failed: " + $_)
    }
    $arg = '/c start "" /D "' + ($Dir -replace '"', '""') + '" "' + ($Target -replace '"', '""') + '"'
    try {
        Start-Process -FilePath "cmd.exe" -ArgumentList $arg
        Write-Log "cmd start issued"
        return $true
    } catch {
        Write-Log ("cmd start failed: " + $_)
    }
    return $false
}

$started = Start-App -Target $Exe -Dir $workDir
Start-Sleep -Seconds 5
if (-not (Get-Process -Name $procName -ErrorAction SilentlyContinue)) {
    Write-Log "process not up - retry Start-Process"
    try { Start-Process -FilePath $Exe -WorkingDirectory $workDir; $started = $true } catch { Write-Log ("retry failed: " + $_) }
    Start-Sleep -Seconds 4
}
if (-not (Get-Process -Name $procName -ErrorAction SilentlyContinue)) {
    Write-Log "process not up - retry explorer"
    Start-Process -FilePath "explorer.exe" -ArgumentList $Exe
    Start-Sleep -Seconds 4
}
if (Get-Process -Name $procName -ErrorAction SilentlyContinue) {
    Write-Log "update success - app running"
} else {
    $msg = "WARNING: app still not running after restart attempts"
    Write-Log $msg
    Set-FailMarker ($msg + " Files were updated. Please start WeigouManager.exe manually.")
}

Remove-Item -LiteralPath $Running -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $Staging -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath (Join-Path $workDir "weigou_relaunch.vbs") -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $PSCommandPath -Force -ErrorAction SilentlyContinue
exit 0

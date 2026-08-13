param(
    [Parameter(Mandatory=$true)][string]$Staging,
    [Parameter(Mandatory=$true)][string]$Install,
    [Parameter(Mandatory=$true)][string]$Exe,
    [Parameter(Mandatory=$true)][string]$Inner,
    [Parameter(Mandatory=$true)][int]$WaitPid,
    [Parameter(Mandatory=$true)][string]$RunningFile,
    [Parameter(Mandatory=$true)][string]$LogFile
)
$ErrorActionPreference = "Continue"
$Log = $LogFile
$Running = $RunningFile
function Write-Log([string]$Message) {
    try {
        Add-Content -LiteralPath $Log -Value ("[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message) -Encoding UTF8
    } catch {}
}

try { Set-Content -LiteralPath $Running -Value $PID -Encoding ASCII } catch {
    Write-Log ("handshake write failed: " + $_)
}
$Lock = $Running + ".lock"
if (Test-Path -LiteralPath $Lock) {
    $oldPid = 0
    try { $oldPid = [int]((Get-Content -LiteralPath $Lock -ErrorAction SilentlyContinue | Select-Object -First 1)) } catch {}
    if ($oldPid -gt 0 -and $oldPid -ne $PID -and (Get-Process -Id $oldPid -ErrorAction SilentlyContinue)) {
        $oldName = ""
        try { $oldName = [string](Get-Process -Id $oldPid -ErrorAction SilentlyContinue).ProcessName } catch {}
        if ($oldName -match "powershell|pwsh") {
            Write-Log "another updater pid=$oldPid already running - exit"
            exit 0
        }
    }
}
try { Set-Content -LiteralPath $Lock -Value $PID -Encoding ASCII } catch {}
Write-Log "update start pid=$PID"
Write-Log "Staging=$Staging"
Write-Log "Install=$Install"
Write-Log "Exe=$Exe"
Write-Log "Inner=$Inner"
Write-Log "WaitPid=$WaitPid"
Write-Log "RunningFile=$Running"

$exeName = [IO.Path]::GetFileName($Exe)
$procName = [IO.Path]::GetFileNameWithoutExtension($Exe)

$deadline = (Get-Date).AddSeconds(90)
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
Start-Sleep -Seconds 3
Write-Log "process wait done"

$src = Join-Path $Staging $Inner
if (-not (Test-Path -LiteralPath $src)) {
    Write-Log "inner folder missing, search for $exeName under staging"
    $hit = Get-ChildItem -LiteralPath $Staging -Recurse -Filter $exeName -File -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($hit) {
        $src = $hit.Directory.FullName
        Write-Log "found exe under $src"
    } else {
        $src = $Staging
    }
} elseif (-not (Test-Path -LiteralPath (Join-Path $src $exeName))) {
    $hit = Get-ChildItem -LiteralPath $Staging -Recurse -Filter $exeName -File -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($hit) {
        $src = $hit.Directory.FullName
        Write-Log "relocated src to $src"
    }
}

Write-Log "robocopy `"$src`" -> `"$Install`" (data excluded)"
& robocopy $src $Install /E /IS /IT /XD data /R:15 /W:2 /NFL /NDL /NJH /NJS | Out-Null
$rc = $LASTEXITCODE
Write-Log "robocopy exit=$rc"
if ($rc -ge 8) {
    Write-Log "robocopy failed code $rc - still attempting restart"
}

Start-Sleep -Seconds 2
$workDir = Split-Path -Parent $Exe
if (-not (Test-Path -LiteralPath $Exe)) {
    $fallback = Join-Path $src $exeName
    Write-Log "install exe missing, fallback=$fallback"
    if (Test-Path -LiteralPath $fallback) {
        $Exe = $fallback
        $workDir = Split-Path -Parent $fallback
    } else {
        Write-Log "FATAL: cannot find exe to restart"
        Remove-Item -LiteralPath $Running -Force -ErrorAction SilentlyContinue
        exit 1
    }
}

function Start-App {
    param([string]$Target, [string]$Dir)
    Write-Log "starting $Target (cwd=$Dir)"
    $vbs = Join-Path $Dir "weigou_relaunch.vbs"
    $line1 = 'Set s=CreateObject("WScript.Shell")'
    $line2 = 's.CurrentDirectory="' + $Dir + '"'
    $line3 = 's.Run """' + $Target + '""",1,False'
    try {
        Set-Content -LiteralPath $vbs -Value ($line1 + "`r`n" + $line2 + "`r`n" + $line3 + "`r`n") -Encoding ASCII
        $wp = Start-Process -FilePath "wscript.exe" -ArgumentList $vbs -PassThru -WindowStyle Hidden
        Write-Log ("wscript pid=" + $wp.Id)
        return
    } catch {
        Write-Log ("wscript failed: " + $_)
    }
    try {
        Start-Process -FilePath $Target -WorkingDirectory $Dir
        Write-Log "Start-Process ok"
        return
    } catch {
        Write-Log ("Start-Process failed: " + $_)
    }
    $arg = '/c start "" /D "' + $Dir + '" "' + $Target + '"'
    try {
        Start-Process -FilePath "cmd.exe" -ArgumentList $arg
        Write-Log "cmd start issued"
    } catch {
        Write-Log ("cmd start failed: " + $_)
    }
}

Start-App -Target $Exe -Dir $workDir
Start-Sleep -Seconds 4
if (-not (Get-Process -Name $procName -ErrorAction SilentlyContinue)) {
    Write-Log "process not up - retry Start-Process"
    try { Start-Process -FilePath $Exe -WorkingDirectory $workDir } catch { Write-Log ("retry failed: " + $_) }
    Start-Sleep -Seconds 3
}
if (-not (Get-Process -Name $procName -ErrorAction SilentlyContinue)) {
    Write-Log "process not up - retry explorer"
    Start-Process -FilePath "explorer.exe" -ArgumentList $Exe
    Start-Sleep -Seconds 3
}
if (Get-Process -Name $procName -ErrorAction SilentlyContinue) {
    Write-Log "update success - app running"
} else {
    Write-Log "WARNING: app still not running after restart attempts"
}

Remove-Item -LiteralPath $Running -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $Lock -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $Staging -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath (Join-Path $workDir "weigou_relaunch.vbs") -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $PSCommandPath -Force -ErrorAction SilentlyContinue
exit 0

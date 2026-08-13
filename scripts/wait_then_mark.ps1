param([int]$WaitPid, [string]$Marker)
$ErrorActionPreference = "Continue"
$deadline = (Get-Date).AddSeconds(30)
while ((Get-Date) -lt $deadline) {
    if (-not (Get-Process -Id $WaitPid -ErrorAction SilentlyContinue)) { break }
    Start-Sleep -Milliseconds 300
}
Set-Content -LiteralPath $Marker -Value "survived pid=$PID" -Encoding ASCII

param([string]$Marker)
Set-Content -LiteralPath $Marker -Value $PID -Encoding ASCII
Start-Sleep -Seconds 8

$ErrorActionPreference = 'Continue'
$log = Join-Path $PSScriptRoot 'watchdog.log'
$listening = Get-NetTCPConnection -LocalPort 8686 -State Listen -ErrorAction SilentlyContinue
if (-not $listening) {
    $ts = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
    try {
        & (Join-Path $PSScriptRoot 'jobhunter_start.ps1')
        "$ts  PORT 8686 未监听 -> 已重新拉起 web.py" | Out-File -Append -FilePath $log -Encoding utf8
    } catch {
        "$ts  拉起失败: $_" | Out-File -Append -FilePath $log -Encoding utf8
    }
}

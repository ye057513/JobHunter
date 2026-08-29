# JobHunter 看门狗 · 无窗口版
# 用 pythonw 拉起 web.py，避免 powershell.exe 每 5 分钟闪命令提示符窗口
$ErrorActionPreference = 'Continue'
$log = Join-Path $PSScriptRoot 'watchdog.log'

try {
    $pyw = 'E:\Program Files\Tencent\Marvis\MarvisAgent\1.0.1100.522\runtime\python311\pythonw.exe'
    $launcher = Join-Path $PSScriptRoot 'launch_web.py'
    $wd  = 'C:\Users\admin\AppData\Roaming\Tencent\Marvis\User\oAN1i2ceoM0vu22J0_ZM3Lb2YmCU\skills\custom\JobHunter'
    $inner = 'E:\Program Files\Tencent\Marvis\MarvisAgent\1.0.1100.522\_internal'
    $vendor = Join-Path $wd 'vendor'

    $listening = Get-NetTCPConnection -LocalPort 8686 -State Listen -ErrorAction SilentlyContinue
    if (-not $listening) {
        $ts = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
        $env:PYTHONPATH = "$vendor;$inner"
        Start-Process -FilePath $pyw -ArgumentList "`"$launcher`"" -WorkingDirectory $wd -WindowStyle Hidden
        "$ts  PORT 8686 未监听 -> 已用 pythonw 重新拉起 web.py（无窗口）" | Out-File -Append -FilePath $log -Encoding utf8
    }
} catch {
    $ts = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
    "$ts  看门狗异常: $_" | Out-File -Append -FilePath $log -Encoding utf8
}

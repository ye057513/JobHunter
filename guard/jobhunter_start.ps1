$ErrorActionPreference = 'Stop'
$pyw = 'E:\Program Files\Tencent\Marvis\MarvisAgent\1.0.1100.522\runtime\python311\pythonw.exe'
$launcher = 'C:\Users\admin\AppData\Roaming\Tencent\Marvis\User\oAN1i2ceoM0vu22J0_ZM3Lb2YmCU\skills\custom\JobHunter\guard\launch_web.py'
$wd  = 'C:\Users\admin\AppData\Roaming\Tencent\Marvis\User\oAN1i2ceoM0vu22J0_ZM3Lb2YmCU\skills\custom\JobHunter'
$inner = 'E:\Program Files\Tencent\Marvis\MarvisAgent\1.0.1100.522\_internal'
$vendor = Join-Path $wd 'vendor'
$busy = Get-NetTCPConnection -LocalPort 8686 -State Listen -ErrorAction SilentlyContinue
if ($busy) { exit 0 }
Set-Location $wd
$env:PYTHONPATH = "$vendor;$inner"
Start-Process -FilePath $pyw -ArgumentList "`"$launcher`"" -WorkingDirectory $wd

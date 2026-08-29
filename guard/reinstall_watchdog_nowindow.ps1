# JobHunter 看门狗改为无窗口运行（需管理员权限执行一次）
$ErrorActionPreference = 'Stop'
$pyw = 'E:\Program Files\Tencent\Marvis\MarvisAgent\1.0.1100.522\runtime\python311\pythonw.exe'
$script = 'C:\Users\admin\AppData\Roaming\Tencent\Marvis\User\oAN1i2ceoM0vu22J0_ZM3Lb2YmCU\skills\custom\JobHunter\guard\watchdog_nw.py'

if (-not (Test-Path $pyw))  { Write-Error "Cannot find pythonw.exe: $pyw"; exit 1 }
if (-not (Test-Path $script)) { Write-Error "Cannot find watchdog_nw.py: $script"; exit 1 }

$action = New-ScheduledTaskAction -Execute $pyw -Argument ('"{0}"' -f $script)
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(-1) -RepetitionInterval (New-TimeSpan -Minutes 5) -RepetitionDuration (New-TimeSpan -Days 3650)
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERNAME" -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName JobHunter_Watchdog -Action $action -Trigger $trigger -Principal $principal `
    -Description "JobHunter watchdog no-window pythonw version" -Force | Out-Null

$t = Get-ScheduledTask -TaskName JobHunter_Watchdog
Write-Host ("OK updated watchdog: Execute={0}" -f $t.Actions.Execute)
Write-Host ("    Args  ={0}" -f $t.Actions.Arguments)
Write-Host ("    Interval={0} State={1}" -f $t.Triggers.Repetition.Interval, $t.State)
Write-Host "No more CMD window popup."

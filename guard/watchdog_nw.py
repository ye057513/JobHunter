# -*- coding: utf-8 -*-
"""JobHunter 看门狗·无窗口版（用 pythonw 运行，不再弹命令提示符窗口）。

功能与原 guard/jobhunter_watchdog.ps1 一致：若 8686 未监听，则拉起 web.py。
本脚本由计划任务 JobHunter_Watchdog 每 5 分钟运行一次。
"""
import os
import socket
import subprocess
import sys
import time

HOST, PORT = "127.0.0.1", 8686
GUARD = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(GUARD)
LOG = os.path.join(GUARD, "watchdog.log")

PYW = r"E:\Program Files\Tencent\Marvis\MarvisAgent\1.0.1100.522\runtime\python311\pythonw.exe"
PYTHONPATH = os.path.join(ROOT, "vendor") + ";" + r"E:\Program Files\Tencent\Marvis\MarvisAgent\1.0.1100.522\_internal"


def _listening() -> bool:
    try:
        s = socket.create_connection((HOST, PORT), timeout=1)
        s.close()
        return True
    except OSError:
        return False


def _log(msg: str) -> None:
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write("%s  %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except OSError:
        pass


if _listening():
    sys.exit(0)

env = dict(os.environ)
env["PYTHONPATH"] = PYTHONPATH
launcher = os.path.join(GUARD, "launch_web.py")
try:
    subprocess.Popen(
        [PYW, launcher],
        cwd=ROOT,
        env=env,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    _log("PORT 8686 未监听 -> 已重新拉起 web.py(no-window)")
except Exception as e:  # noqa: BLE001
    _log("拉起失败: %s" % e)
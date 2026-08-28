# -*- coding: utf-8 -*-
"""JobHunter web.py 无窗口启动器：将 stdout/stderr 重定向到 guard 日志后运行 web.py。"""
import os
import sys

GUARD_DIR = os.path.dirname(os.path.abspath(__file__))
SKILL_ROOT = os.path.dirname(GUARD_DIR)
WEB_PY = os.path.join(SKILL_ROOT, 'src', 'web.py')

try:
    sys.stdout = open(os.path.join(GUARD_DIR, 'web.out.log'), 'a', encoding='utf-8')
except Exception:
    pass
try:
    sys.stderr = open(os.path.join(GUARD_DIR, 'web.err.log'), 'a', encoding='utf-8')
except Exception:
    pass

os.chdir(SKILL_ROOT)
sys.path.insert(0, SKILL_ROOT)
sys.path.insert(0, os.path.join(SKILL_ROOT, 'src'))

import runpy
runpy.run_path(WEB_PY, run_name='__main__')

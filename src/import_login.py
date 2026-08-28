"""一次性迁移：把用户日常 Edge(Default) 的 Boss直聘登录态导入 jobhunter 专用 profile。

前提：Edge 必须已完全退出（playwright 需独占 Default profile 才能解密其 cookie）。
执行完成后会自动重启 Edge 并打开岗位看板。

用法：
    python src/import_login.py
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PROFILE = Path.home() / "AppData" / "Local" / "Microsoft" / "Edge" / "User Data"
TARGET_PROFILE = SKILL_ROOT / "edge_profile"
LOG_FILE = SKILL_ROOT / "logs" / f"import_login_{datetime.now().strftime('%Y%m%d_%H%M')}.log"


def log(msg: str) -> None:
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def edge_running() -> bool:
    try:
        r = subprocess.run(["tasklist", "/FI", "IMAGENAME eq msedge.exe"], capture_output=True, text=True)
        return "msedge.exe" in r.stdout
    except Exception:
        return False


def restart_edge_with_dashboard() -> None:
    log("重新启动 Edge 并打开岗位看板...")
    try:
        subprocess.Popen(["cmd", "/c", "start", "", "http://127.0.0.1:8686/"], shell=True)
    except Exception as e:
        log(f"重启 Edge 失败：{e}")


def main() -> int:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

    if not DEFAULT_PROFILE.exists():
        log(f"未找到 Edge 用户数据：{DEFAULT_PROFILE}")
        return 1

    if edge_running():
        log("检测到 Edge 正在运行。本操作需要短暂关闭 Edge 以读取登录态。")
        log("请手动关闭所有 Edge 窗口（或先执行：taskkill /IM msedge.exe /F），再重新运行本脚本。")
        log("注意：关闭前请保存 Edge 中未完成的工作。")
        return 2

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log("缺少 playwright，请先：pip install playwright")
        return 1

    cookies = []
    with sync_playwright() as p:
        log("正在以 Default profile 启动 Edge（headless）...")
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(DEFAULT_PROFILE),
            channel="msedge",
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = ctx.new_page()
        page.goto("https://www.zhipin.com/", wait_until="domcontentloaded", timeout=60000)
        time.sleep(6)
        log(f"当前 URL：{page.url}")
        body = page.inner_text("body")[:400]
        log(f"页面开头：{body[:120]}")

        all_cookies = ctx.cookies(["https://www.zhipin.com", "https://www.zhipin.com/"])
        cookies = [c for c in all_cookies if "zhipin.com" in c.get("domain", "")]
        log(f"读取到 zhipin cookie {len(cookies)} 条")
        ctx.close()

    if not cookies:
        log("未读取到 zhipin cookie，可能未登录或页面结构变化")
        return 3

    # 写入目标 profile
    TARGET_PROFILE.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        ctx2 = p.chromium.launch_persistent_context(
            user_data_dir=str(TARGET_PROFILE),
            channel="msedge",
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        ctx2.add_cookies(cookies)
        page2 = ctx2.new_page()
        page2.goto("https://www.zhipin.com/web/geek/jobs", wait_until="domcontentloaded", timeout=60000)
        time.sleep(6)
        url2 = page2.url
        body2 = page2.inner_text("body")[:500]
        log(f"目标 profile 验证 URL：{url2}")
        log(f"目标 profile 页面：{body2[:150]}")
        ok = "login" not in url2 and "403" not in url2 and ("web/geek" in url2 or "职位" in body2 or "搜索" in body2)
        ctx2.close()

    if ok:
        log("迁移成功：jobhunter 专用 profile 已具备 Boss直聘 登录态")
    else:
        log("迁移后验证异常，可能 Boss直聘 对 cookie 迁移有风控，请人工登录一次 target profile")
        return 4

    restart_edge_with_dashboard()
    log("完成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

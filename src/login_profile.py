"""一次性准备某平台的登录态 profile。

用法：
    python src/login_profile.py <platform>
    <platform> = yingsheng | zhaopin | 51job | qiuzhifangzhou

作用：在本机以「有头浏览器」打开所选平台的登录页 / 首页，
你在弹出的浏览器窗口里手动登录（扫码 / 账号密码均可）。
**登录完成后直接关闭该浏览器窗口**，脚本检测到窗口关闭即自动保存登录态退出。
之后看板里对该平台的登录态采集会复用这份登录态。

保存位置：config.yaml 里 platforms.<platform>.profile 指定的路径；
留空时使用默认目录 ~/.config/jobhunter-platform/<platform>。
"""
from __future__ import annotations

import time
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import collectors  # noqa: E402
import config as _cfg  # noqa: E402

KEYS = {"yingsheng": "应届生求职", "zhaopin": "智联招聘",
        "51job": "前程无忧51Job", "qiuzhifangzhou": "求职方舟·AI找工作"}
TIMEOUT = 600  # 最长等待秒数


def main() -> int:
    if len(sys.argv) < 2:
        print("用法：python src/login_profile.py <platform>")
        print("可选平台：" + ", ".join(f"{k}({v})" for k, v in KEYS.items()))
        return 1
    key = sys.argv[1].strip()
    if key not in KEYS:
        print(f"未知平台：{key}。可选：" + ", ".join(KEYS))
        return 1

    pcfg = _cfg.get_platform_cfg(key)
    profile = (pcfg.get("profile") or "").strip()
    profile_dir = Path(profile) if profile else Path.home() / ".config" / "jobhunter-platform" / key
    profile_dir.mkdir(parents=True, exist_ok=True)

    col = collectors.get_collector(key, cfg={"headless": False},
                                   profile_dir=profile_dir)
    print(f"平台「{KEYS[key]}」登录态准备开始")
    print(f"profile 目录：{profile_dir}")
    print(f"登录站点：{col.home_url}")
    print("→ 浏览器窗口已打开，请在窗口里登录，登录成功后【关闭窗口】即可自动保存。")

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("缺少 playwright，请先 pip install playwright")
        return 1

    deadline = time.time() + TIMEOUT
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            channel=col.browser_channel,
            headless=False,
            # 关闭后台驻留，确保用户关闭最后一个窗口即真正退出浏览器，触发 close 事件保存
            args=["--disable-blink-features=AutomationControlled",
                  "--disable-background-mode"],
            locale="zh-CN",
        )
        page = ctx.new_page()
        try:
            page.goto(col.home_url, wait_until="domcontentloaded", timeout=60000)
        except Exception as e:  # noqa: BLE001
            print(f"打开首页警告（可忽略，继续等待登录）：{e}")

        closed = [False]

        def _on_close():
            closed[0] = True

        try:
            ctx.on("close", _on_close)
        except Exception:
            pass

        last_url = page.url
        while time.time() < deadline and not closed[0]:
            try:
                if page.is_closed() or not ctx.pages:
                    closed[0] = True
                    break
                if page.url != last_url:
                    last_url = page.url
                    if any(k in page.url for k in ("#!/login", "/login", "passport", "account/login")):
                        print(f"[状态] 当前在登录页：{page.url}")
                    else:
                        print(f"[状态] 已离开登录页（可能已登录）：{page.url}")
            except Exception:
                closed[0] = True
                break
            time.sleep(2)

        if not closed[0]:
            print(f"[超时] 等待 {TIMEOUT // 60} 分钟未检测到窗口关闭，强行保存当前登录态退出。")
        n_cookie = 0
        try:
            n_cookie = len(ctx.cookies())
        except Exception:
            pass
        try:
            ctx.close()
        except Exception:
            pass

    print(f"完成：已保存 {n_cookie} 条 cookie 到 {profile_dir}")
    print(f"现在可以在看板对「{KEYS[key]}」采集（登录态路径）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
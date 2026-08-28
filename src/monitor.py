"""JobHunter 回复监听模块。

抓取 Boss直聘未读消息，汇总 HR 回复。支持手动触发与定时轮询。

安全措施（保留不变）：
- Playwright 登录态访问，非高频 API 调用
- 页面加载后固定延迟，不密集请求
- 登录态失效检测 + 手动登录等待
- 仅读取消息，不发送任何消息（无投递/打招呼动作）
"""
from __future__ import annotations

import json
import random
import sys
import time
from typing import Any, Callable, Dict, List, Optional

import config

MESSAGES_PATH = config.DATA_DIR / "messages.json"


def load_messages() -> List[Dict[str, Any]]:
    """读取历史消息。"""
    if not MESSAGES_PATH.exists():
        return []
    with open(MESSAGES_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def save_messages(messages: List[Dict[str, Any]]) -> None:
    """保存消息。"""
    with open(MESSAGES_PATH, "w", encoding="utf-8") as f:
        json.dump(messages, f, ensure_ascii=False, indent=2)


def fetch_messages(page) -> List[Dict[str, Any]]:
    """抓取未读消息列表（仅读取，不发送任何消息）。"""
    messages: List[Dict[str, Any]] = []
    try:
        page.goto("https://www.zhipin.com/web/geek/chat", wait_until="domcontentloaded")
        time.sleep(random.uniform(3, 6))
        items = page.query_selector_all(".chat-item")
        for item in items:
            try:
                name_el = item.query_selector(".name")
                name = name_el.inner_text().strip() if name_el else ""
            except Exception:
                name = ""
            try:
                msg_el = item.query_selector(".last-msg")
                last_msg = msg_el.inner_text().strip() if msg_el else ""
            except Exception:
                last_msg = ""
            try:
                time_el = item.query_selector(".time")
                msg_time = time_el.inner_text().strip() if time_el else ""
            except Exception:
                msg_time = ""
            messages.append({
                "company": name,
                "last_msg": last_msg,
                "time": msg_time,
                "fetched_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            })
    except Exception:
        pass
    return messages


def monitor_web(
    on_progress: Optional[Callable[[str], None]] = None
) -> Dict[str, Any]:
    """Web 可调用入口：抓取并汇总 HR 回复。

    安全措施：
    - 使用 Playwright 浏览器访问（非高频 API 调用）
    - 页面加载后随机延迟 3-6 秒
    - 登录态检测，失效时等待手动登录
    - 仅读取消息列表，不执行任何发送/投递动作
    """
    config.ensure_dirs()

    def _log(msg):
        if on_progress:
            on_progress(msg)

    _log("启动 Playwright 浏览器…")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return {"ok": False, "message": "缺少 playwright，请执行：pip install playwright"}

    cfg = config.load_config()
    browser_cfg = cfg.get("browser", {})

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=browser_cfg.get("headless", False))
        context = browser.new_context()
        page = context.new_page()

        _log("打开 Boss直聘，检测登录态…")
        page.goto("https://www.zhipin.com/", wait_until="domcontentloaded")
        time.sleep(random.uniform(4, 7))
        if "login" in page.url or page.query_selector(".login-box"):
            _log("检测到需要登录，请在浏览器中手动完成登录…")
            for _ in range(24):
                time.sleep(5)
                if "login" not in page.url and not page.query_selector(".login-box"):
                    _log("登录成功。")
                    break
            else:
                browser.close()
                return {"ok": False, "message": "等待登录超时，请重新运行"}

        _log("正在抓取消息列表…")
        new_messages = fetch_messages(page)
        browser.close()

    if not new_messages:
        return {"ok": True, "added": 0, "total": len(load_messages()), "message": "未抓取到新消息"}

    existing = load_messages()
    existing_keys = {(m.get("company"), m.get("last_msg")) for m in existing}
    added = 0
    for m in new_messages:
        key = (m.get("company"), m.get("last_msg"))
        if key in existing_keys:
            continue
        existing.append(m)
        existing_keys.add(key)
        added += 1

    save_messages(existing)
    return {
        "ok": True,
        "added": added,
        "total": len(existing),
        "messages": new_messages[:20],
        "message": f"新增 {added} 条消息，累计 {len(existing)} 条",
    }


def run() -> None:
    """CLI 主入口。"""
    result = monitor_web(on_progress=lambda msg: print(msg))
    print(f"\n{result.get('message', '完成')}")
    if result.get("messages"):
        print("最新回复：")
        for m in result["messages"][:10]:
            print(f"  [{m.get('time')}] {m.get('company')}：{m.get('last_msg')}")


if __name__ == "__main__":
    try:
        run()
    except Exception as e:
        print(f"监听流程出错：{e}")
        sys.exit(1)

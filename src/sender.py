"""JobHunter 投递执行模块。

读取「待确认」岗位清单，通过 Playwright 发送招呼语。
注意：本模块只发送用户已确认的岗位，绝不自动投递。
"""
from __future__ import annotations

import json
import random
import sys
import time
from typing import Any, Dict, List

from . import config


def load_pending() -> List[Dict[str, Any]]:
    """读取状态为「待确认」的岗位。"""
    jobs_path = config.DATA_DIR / "jobs.json"
    if not jobs_path.exists():
        return []
    with open(jobs_path, "r", encoding="utf-8") as f:
        jobs = json.load(f)
    return [j for j in jobs if j.get("status") == "待确认"]


def save_jobs(jobs: List[Dict[str, Any]]) -> None:
    """保存岗位数据。"""
    with open(config.DATA_DIR / "jobs.json", "w", encoding="utf-8") as f:
        json.dump(jobs, f, ensure_ascii=False, indent=2)


def send_greeting(page, job: Dict[str, Any]) -> bool:
    """向单个岗位发送招呼语。"""
    link = job.get("link", "")
    if not link:
        return False
    url = f"https://www.zhipin.com{link}" if link.startswith("/") else link
    try:
        page.goto(url, wait_until="domcontentloaded")
        time.sleep(3)
        # 点击「立即沟通」按钮
        chat_btn = page.query_selector(".btn-startchat")
        if not chat_btn:
            chat_btn = page.query_selector("text=立即沟通")
        if not chat_btn:
            print(f"    未找到「立即沟通」按钮：{job.get('title')}")
            return False
        chat_btn.click()
        time.sleep(3)

        # 输入招呼语
        greeting = job.get("greeting", "")
        if not greeting:
            print(f"    无招呼语内容：{job.get('title')}")
            return False
        input_box = page.query_selector(".chat-input textarea")
        if not input_box:
            input_box = page.query_selector("textarea")
        if not input_box:
            print(f"    未找到输入框：{job.get('title')}")
            return False
        input_box.fill(greeting)

        # 点击发送
        send_btn = page.query_selector(".btn-send")
        if not send_btn:
            send_btn = page.query_selector("text=发送")
        if not send_btn:
            print(f"    未找到发送按钮：{job.get('title')}")
            return False
        send_btn.click()
        time.sleep(2)
        return True
    except Exception as e:
        print(f"    发送失败：{e}")
        return False


def run() -> None:
    """主入口：发送已确认的岗位招呼语。"""
    config.ensure_dirs()
    pending = load_pending()
    if not pending:
        print("没有「待确认」的岗位，请先执行评分和招呼语生成。")
        return

    print(f"待投递岗位 {len(pending)} 个：")
    for i, j in enumerate(pending, 1):
        print(f"  {i}. [{j.get('score')}] {j.get('title')} @ {j.get('company')}")

    # 人工确认：命令行交互确认
    print("\n请逐条确认是否投递（y=投递 / n=跳过 / q=退出）：")
    confirmed: List[Dict[str, Any]] = []
    for j in pending:
        answer = input(f"  投递「{j.get('title')} @ {j.get('company')}」？(y/n/q) ").strip().lower()
        if answer == "q":
            break
        if answer == "y":
            confirmed.append(j)

    if not confirmed:
        print("未确认任何岗位，退出。")
        return

    from playwright.sync_api import sync_playwright

    cfg = config.load_config()
    browser_cfg = cfg.get("browser", {})
    interval_min = browser_cfg.get("send_interval_min", 30)
    interval_max = browser_cfg.get("send_interval_max", 60)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=browser_cfg.get("headless", False))
        context = browser.new_context()
        page = context.new_page()

        print("打开 Boss直聘，请确认已登录...")
        page.goto("https://www.zhipin.com/", wait_until="domcontentloaded")
        time.sleep(5)
        if "login" in page.url or page.query_selector(".login-box"):
            print("检测到需要登录，请在浏览器中手动完成登录...")
            for _ in range(24):
                time.sleep(5)
                if "login" not in page.url and not page.query_selector(".login-box"):
                    print("登录成功。")
                    break
            else:
                print("等待登录超时，请重新运行。")
                browser.close()
                return

        success = 0
        for i, job in enumerate(confirmed, 1):
            print(f"[{i}/{len(confirmed)}] 投递中：{job.get('title')} @ {job.get('company')}")
            ok = send_greeting(page, job)
            if ok:
                job["status"] = "已投递"
                job["sent_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                success += 1
            else:
                job["status"] = "投递失败"
            # 随机间隔，降低风控风险
            if i < len(confirmed):
                wait = random.randint(interval_min, interval_max)
                print(f"  等待 {wait} 秒后继续...")
                time.sleep(wait)

        browser.close()

    # 保存状态
    jobs_path = config.DATA_DIR / "jobs.json"
    with open(jobs_path, "r", encoding="utf-8") as f:
        all_jobs = json.load(f)
    confirmed_links = {j.get("link") for j in confirmed}
    for j in all_jobs:
        if j.get("link") in confirmed_links:
            for c in confirmed:
                if c.get("link") == j.get("link"):
                    j["status"] = c["status"]
                    if "sent_at" in c:
                        j["sent_at"] = c["sent_at"]
    save_jobs(all_jobs)

    print(f"\n投递完成：成功 {success} 个，失败 {len(confirmed) - success} 个。")
    print("下一步：运行监听 `python -m src.monitor` 查看 HR 回复")


if __name__ == "__main__":
    try:
        run()
    except Exception as e:
        print(f"投递流程出错：{e}")
        sys.exit(1)

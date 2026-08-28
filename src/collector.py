"""JobHunter 岗位采集模块。

使用 Playwright 打开 Boss直聘，按关键词/城市搜索，抓取岗位列表与详情。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

from . import config

BOSS_BASE_URL = "https://www.zhipin.com/web/geek/job"


def _load_existing() -> List[Dict[str, Any]]:
    """读取已有岗位数据，避免重复采集。"""
    jobs_path = config.DATA_DIR / "jobs.json"
    if not jobs_path.exists():
        return []
    with open(jobs_path, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_jobs(jobs: List[Dict[str, Any]]) -> None:
    """保存岗位数据。"""
    with open(config.DATA_DIR / "jobs.json", "w", encoding="utf-8") as f:
        json.dump(jobs, f, ensure_ascii=False, indent=2)


def _parse_job_card(card) -> Dict[str, Any]:
    """解析单个岗位卡片元素。"""
    try:
        title_el = card.query_selector(".job-name")
        title = title_el.inner_text().strip() if title_el else ""
    except Exception:
        title = ""
    try:
        salary_el = card.query_selector(".salary")
        salary = salary_el.inner_text().strip() if salary_el else ""
    except Exception:
        salary = ""
    try:
        company_el = card.query_selector(".company-name")
        company = company_el.inner_text().strip() if company_el else ""
    except Exception:
        company = ""
    try:
        info_el = card.query_selector(".job-info")
        info = info_el.inner_text().strip() if info_el else ""
    except Exception:
        info = ""
    try:
        link_el = card.query_selector("a.job-card-left")
        link = link_el.get_attribute("href") if link_el else ""
    except Exception:
        link = ""
    return {
        "title": title,
        "salary": salary,
        "company": company,
        "info": info,
        "link": link,
        "description": "",
        "status": "待评分",
    }


def collect(page, keywords: List[str], city: str, max_pages: int = 3) -> List[Dict[str, Any]]:
    """执行采集，返回新采集的岗位列表。"""
    all_jobs: List[Dict[str, Any]] = []
    for keyword in keywords:
        print(f"搜索关键词：{keyword}（城市：{city}）")
        url = BOSS_BASE_URL
        if city:
            url += f"?city={city}"
        page.goto(url, wait_until="domcontentloaded")
        time.sleep(3)

        # 输入关键词搜索
        try:
            search_input = page.query_selector("input[name='query']")
            if search_input:
                search_input.fill(keyword)
                search_input.press("Enter")
                time.sleep(3)
        except Exception as e:
            print(f"  搜索输入失败：{e}")

        for page_no in range(1, max_pages + 1):
            print(f"  第 {page_no} 页...")
            try:
                cards = page.query_selector_all(".job-card-wrapper")
                if not cards:
                    cards = page.query_selector_all(".job-card-box")
            except Exception:
                cards = []
            if not cards:
                print("  未找到岗位卡片，可能已登录失效或页面结构变化。")
                break

            for card in cards:
                job = _parse_job_card(card)
                job["keyword"] = keyword
                job["city"] = city
                all_jobs.append(job)

            # 翻页
            try:
                next_btn = page.query_selector(".ui-icon-arrow-right")
                if not next_btn:
                    break
                next_btn.click()
                time.sleep(3)
            except Exception:
                break

    return all_jobs


def run() -> None:
    """主入口：执行采集。"""
    config.ensure_dirs()
    cfg = config.load_config()
    search = cfg.get("search", {})
    keywords = search.get("keywords", [])
    city = search.get("city", "")

    if not keywords:
        print("未配置搜索关键词，请先在配置面板 http://127.0.0.1:8686 中填写。")
        return

    from playwright.sync_api import sync_playwright

    existing = _load_existing()
    existing_links = {j.get("link") for j in existing}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=cfg.get("browser", {}).get("headless", False))
        context = browser.new_context()
        page = context.new_page()

        # 访问 Boss直聘，触发登录
        print("打开 Boss直聘，请确认已登录...")
        page.goto("https://www.zhipin.com/", wait_until="domcontentloaded")
        time.sleep(5)

        # 检测是否跳转登录页
        if "login" in page.url or page.query_selector(".login-box"):
            print("检测到需要登录，请在浏览器中手动完成登录...")
            # 等待用户手动登录（最多 120 秒）
            for _ in range(24):
                time.sleep(5)
                if "login" not in page.url and not page.query_selector(".login-box"):
                    print("登录成功，继续采集。")
                    break
            else:
                print("等待登录超时，请重新运行。")
                browser.close()
                return

        new_jobs = collect(page, keywords, city)
        browser.close()

    # 去重合并
    added = 0
    for job in new_jobs:
        if job.get("link") and job["link"] in existing_links:
            continue
        existing.append(job)
        if job.get("link"):
            existing_links.add(job["link"])
        added += 1

    _save_jobs(existing)
    print(f"\n采集完成，新增 {added} 个岗位，累计 {len(existing)} 个岗位。")
    print("下一步：运行评分 `python -m src.scorer`")


if __name__ == "__main__":
    try:
        run()
    except Exception as e:
        print(f"采集流程出错：{e}")
        sys.exit(1)

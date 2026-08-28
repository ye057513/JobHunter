"""JobHunter 后端静默采集脚本。

岗位采集支持两种引擎：
1. api（默认）：走 boss-cli 拆解出的 recommend 接口（非登录态搜索路径），
   列表采集仅携带最小 cookie（wt2），规避 search 接口的动态 stoken 风控（code=37）。
2. playwright（可选）：headless Edge + 独立登录 profile 采集搜索页（登录态，风控风险高）。

合并去重写入 jobs.json，保留已有 status 字段。

用法：
    python src/fetch_jobs.py                      # 默认 api 引擎，按 config.yaml 全量关键词采集
    python src/fetch_jobs.py --engine playwright  # 改用 playwright 登录态采集
    python src/fetch_jobs.py --keywords "Java开发工程师,Android开发工程师"  # 只采指定关键词（单轮轻量）
    python src/fetch_jobs.py --pages 2            # 每关键词翻页数（api 引擎下=推荐列表翻页）

依赖（api 引擎）：httpx；playwright 引擎还需 playwright + Edge + 登录 profile。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import sys
import time
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

SKILL_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL_ROOT / "src"))

import config  # noqa: E402
import boss_api  # noqa: E402

PROFILE_DIR = SKILL_ROOT / "edge_profile"
LOGS_DIR = SKILL_ROOT / "logs"
SEARCH_URL = "https://www.zhipin.com/web/geek/jobs"

# —— 最近一次 API 采集错误（供 web.py 采集弹窗透传报错类型/内容） ——
LAST_COLLECT_ERROR: Dict[str, Any] = {"type": "", "code": None, "message": ""}


def _set_collect_error(etype: str, code, message: str) -> None:
    global LAST_COLLECT_ERROR
    LAST_COLLECT_ERROR = {"type": etype, "code": code, "message": message}

# Boss直聘 403/登录拦截特征
BLOCK_MARKERS = ["访问受限", "暂时无法访问", "存在异常行为", "多次违规访问", "请登录后使用", "code=31"]
LOGIN_MARKERS = ["请先登录", "登录", "passport"]

# —— 风控：人类化随机延迟（秒），防止被识别为脚本高频访问 ——
DELAY_BOOT = (10, 30)     # 启动随机偏移，打散整点/固定节奏
DELAY_FIRST = (6, 10)     # 进入搜索页后首次停顿
DELAY_PAGE = (12, 20)     # 翻页间隔
DELAY_JD = (8, 15)        # 详情页间隔（如启用）
DELAY_KW = (25, 45)       # 关键词间冷却

# 岗位卡片/详情选择器（多套兜底）
CARD_SELS = [".job-card-wrapper", ".job-card-box", ".job-list-box li", ".job-primary"]
TITLE_SELS = [".job-name", ".job-title", "a.job-card-left .job-name"]
SALARY_SELS = [".salary", ".job-salary"]
COMPANY_SELS = [".company-name", ".company-text .name"]
LINK_SELS = ["a.job-card-left", "a.job-card-right", ".job-primary .info-primary a"]
INFO_SELS = [".job-info", ".job-desc", ".info-primary .job-card-footer"]
JD_SELS = [".job-sec-text", ".detail-content .job-sec-text", ".text", ".job-detail-content", ".description"]


def _log(msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def _sleep(lo: float, hi: float) -> None:
    time.sleep(random.uniform(lo, hi))


def _detect_block(page) -> str:
    """检测 403/登录拦截，返回 'blocked' / 'login' / ''。"""
    try:
        url = page.url
        if "passport/zp/403" in url or "code=31" in url or "wz/passport" in url:
            return "blocked"
        if "passport" in url and "login" in url:
            return "login"
        body = page.inner_text("body")[:2000]
        if any(m in body for m in BLOCK_MARKERS):
            return "blocked"
        if "请先登录" in body and ("登录后" in body or "立即登录" in body):
            return "login"
    except Exception:
        pass
    return ""


def _extract_job_from_card(card, keyword: str, city: str) -> Dict[str, Any]:
    def qtext(selectors):
        for sel in selectors:
            try:
                el = card.query_selector(sel)
                if el:
                    t = el.inner_text().strip()
                    if t:
                        return t
            except Exception:
                pass
        return ""

    def qhref(selectors):
        for sel in selectors:
            try:
                el = card.query_selector(sel)
                if el:
                    h = el.get_attribute("href") or ""
                    if h:
                        return h
            except Exception:
                pass
        return ""

    title = qtext(TITLE_SELS)
    salary = qtext(SALARY_SELS)
    company = qtext(COMPANY_SELS)
    info = qtext(INFO_SELS)
    link = qhref(LINK_SELS)

    # 链接转绝对 URL
    if link:
        if link.startswith("//"):
            link = "https:" + link
        elif link.startswith("/"):
            link = "https://www.zhipin.com" + link

    education, experience = "", ""
    # 从 info 文本提取学历/经验（Boss 信息串：如 "厦门 1-3年 本科"）
    if info:
        parts = [x.strip() for x in re.split(r"[\s·|]", info) if x.strip()]
        for p in parts:
            if p in ("本科", "大专", "硕士", "博士", "学历不限", "不限"):
                education = p
            if re.search(r"年经验|\d+-\d+年|应届|在校|无需经验|经验不限", p):
                experience = p

    return {
        "title": title,
        "salary": salary,
        "company": company,
        "info": info,
        "url": link,
        "link": link,
        "keyword": keyword,
        "city": city,
        "education": education,
        "experience": experience,
        "description": "",
        "status": "待处理",
    }


def _fetch_jd(page, url: str) -> str:
    """抓取岗位详情页的职位描述。"""
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        _sleep(3, 6)
        for sel in JD_SELS:
            try:
                el = page.query_selector(sel)
                if el:
                    text = el.inner_text().strip()
                    if len(text) > 30:
                        return text
            except Exception:
                pass
    except Exception:
        pass
    return ""


def collect_keyword(page, keyword: str, city: str, max_pages: int, max_jd: int = 0) -> List[Dict[str, Any]]:
    jobs: List[Dict[str, Any]] = []
    # 城市编码映射（Boss直聘 city code）
    city_code = {"厦门": "101230200", "福州": "101230100", "泉州": "101230500"}.get(city, "101230200")
    base = f"{SEARCH_URL}?query={keyword}&city={city_code}"
    _log(f"关键词[{keyword}] 城市[{city}] 搜索：{base}")
    try:
        page.goto(base, wait_until="domcontentloaded", timeout=40000)
        _sleep(*DELAY_FIRST)
    except Exception as e:
        _log(f"  goto 失败：{e}")
        return jobs

    blk = _detect_block(page)
    if blk:
        _log(f"  拦截：{blk}")
        return jobs

    for page_no in range(1, max_pages + 1):
        if page_no > 1:
            # 点击下一页
            try:
                nxt = page.query_selector(".ui-icon-arrow-right")
                if not nxt:
                    nxt = page.query_selector("a.next, .pagination .next")
                if not nxt:
                    _log("  无下一页")
                    break
                nxt.click()
                _sleep(*DELAY_PAGE)
            except Exception:
                break
        _log(f"  第 {page_no} 页")
        blk = _detect_block(page)
        if blk:
            _log(f"  翻页后拦截：{blk}")
            break

        cards = []
        for sel in CARD_SELS:
            try:
                cards = page.query_selector_all(sel)
                if cards:
                    break
            except Exception:
                continue
        if not cards:
            _log("  未找到岗位卡片")
            break
        _log(f"  解析 {len(cards)} 张卡片")
        for card in cards:
            job = _extract_job_from_card(card, keyword, city)
            if job["title"]:
                jobs.append(job)

        # 滚动模拟（随机 2~3 次、随机步长，模拟真人浏览）
        try:
            for _ in range(random.randint(2, 3)):
                page.mouse.wheel(0, random.randint(1500, 3500))
                _sleep(1.5, 3.5)
        except Exception:
            pass

    # 抓 JD 详情（默认不抓：详情页请求最易触发风控；仅当显式指定 --max-jd N 时抓前 N 条）
    if max_jd > 0:
        _log(f"  抓取前 {max_jd} 条 JD 详情（风控提示：详情页请求密集，谨慎使用）")
        for job in jobs[:max_jd]:
            if job["url"] and "job_detail" in job["url"]:
                jd = _fetch_jd(page, job["url"])
                if jd:
                    job["description"] = jd
                _sleep(*DELAY_JD)
    return jobs


def _content_fp(text: str, n: int = 200) -> str:
    """取文本前 n 个字符（去除空白）的 MD5 摘要，用于内容相似度比较。"""
    raw = re.sub(r"\s+", "", text)[:n]
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def _is_closed(job: Dict[str, Any]) -> bool:
    """判断岗位是否已停招：无薪资描述 / 薪资乱码 / 薪资未披露 / 标题含停招关键词。"""
    import re
    salary = (job.get("salary") or "").strip()
    title = (job.get("title") or "").lower()
    if not salary:
        return True
    # 薪资含 Unicode 私有区乱码字符（Boss API 编码异常，视为无效薪资）
    if re.search(r'[\ue000-\uf8ff]', salary):
        return True
    # 薪资为"未披露"/"面议"等无具体范围，视为可疑停招
    if salary in ("未披露", "面议", ""):
        return True
    closed_kw = ["已停招", "已关闭", "已下架", "temporarily inactive"]
    if any(kw in title for kw in closed_kw):
        return True
    return False


def merge_jobs(new_jobs: List[Dict[str, Any]]) -> Dict[str, int]:
    """合并新采集岗位到 jobs.json。

    去重与清理规则：
    1. URL 完全相同 → 保留已有记录，仅补充描述。
    2. 同一家公司 + 内容描述高度相似（MD5 前 200 字符指纹相同）→ 保留 match_score/tech_matches 更高者。
    3. 新采集或已有岗位检测为"已停招"（无薪资描述 / 标题含停招关键词）→ 标记 status="已停招"，不纳入展示。
    """
    jobs_file = config.DATA_DIR / "jobs.json"
    existing: List[Dict[str, Any]] = []
    if jobs_file.exists():
        try:
            existing = json.loads(jobs_file.read_text(encoding="utf-8"))
        except Exception:
            existing = []
    by_url: Dict[str, Dict[str, Any]] = {
        (j.get("url") or j.get("link")): j
        for j in existing
        if j.get("url") or j.get("link")
    }
    # 新一轮入库：清空上一批 is_newest 标志
    for j in existing:
        j["is_newest"] = False

    added = 0
    dup_count = 0
    closed_count = 0

    for nj in new_jobs:
        key = nj.get("url") or nj.get("link")
        if not key:
            continue

        # 停招检测：新采集岗位已停招 → 直接丢弃
        if _is_closed(nj):
            closed_count += 1
            continue

        if key in by_url:
            # URL 完全匹配：已有，补充 JD 描述，保留原 status
            old = by_url[key]
            if nj.get("description") and not old.get("description"):
                old["description"] = nj["description"]
        else:
            # URL 未出现过：检查同公司内是否有内容高度相似岗位
            company = nj.get("company", "").strip()
            fp = _content_fp(nj.get("description") or "")
            best_same_company_key: str | None = None
            best_same_company_score: int = -1
            for u, old in by_url.items():
                if not u:
                    continue
                if (old.get("company", "").strip() == company
                        and _content_fp(old.get("description") or "") == fp
                        and not _is_closed(old)):
                    # 同公司同内容，取 tech_matches / match_score 更高者
                    old_score = max(old.get("tech_matches") or 0, old.get("match_score") or 0)
                    new_score = max(nj.get("tech_matches") or 0, nj.get("match_score") or 0)
                    if new_score > old_score:
                        best_same_company_key = u
                        best_same_company_score = new_score
                    elif best_same_company_key is None:
                        best_same_company_key = u
                        best_same_company_score = old_score
                    break  # 只比较第一个匹配（同公司同内容极罕见）

            if best_same_company_key is not None:
                # 同公司同内容已存在，跳过新增
                dup_count += 1
                continue

            nj["is_newest"] = True
            by_url[key] = nj
            added += 1

    # 清理已停招的存量岗位（已收集/已投递但现已停招）
    for j in existing:
        if j.get("url") or j.get("link"):
            if _is_closed(j) and j.get("status") not in ("已停招", "暂不考虑已结束"):
                j["status"] = "已停招"
                j["is_newest"] = False
                closed_count += 1

    # 写出
    merged = list(by_url.values())
    jobs_file.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    _log(f"merge: 新增 {added} 条，同公司重复跳过 {dup_count} 条，已停招清理 {closed_count} 条，累计 {len(merged)} 条")
    return {"added": added, "total": len(merged), "dup": dup_count, "closed": closed_count}


def write_round_log(stage: str, data: Dict[str, Any]) -> None:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    fname = LOGS_DIR / f"round_{datetime.now().strftime('%Y%m%d_%H%M')}.json"
    data["stage"] = stage
    data["time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    fname.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    _log(f"轮次日志：{fname.name}")


def collect_via_api(keywords: List[str], cities: List[str], max_pages: int,
                    minimal_list: bool = True, on_page=None) -> List[Dict[str, Any]]:
    """API 引擎：走 boss-cli 拆解出的 recommend 接口采集推荐岗位列表。

    非登录态路径：列表采集仅携带最小 cookie（wt2），不触发 search 接口的动态
    stoken 风控。推荐列表不带搜索关键词，按标题/标签命中关键词的技术词元 +
    城市匹配做粗筛，粗筛后的岗位再交由 smart_filter 技术栈过滤。
    """
    # 用户关键词 → 技术词元（用于匹配推荐流标题的缩写形式，如 "java开发实习生"）
    KW_TERMS = {
        "java开发工程师": ["java", "j2ee", "spring", "后端", "后端开发"],
        "android开发工程师": ["android", "安卓"],
        "ai应用开发工程师": ["ai", "aigc", "chatgpt", "大模型", "rag", "llm", "python"],
        "前端开发工程师": ["前端", "vue", "react", "javascript", "web前端"],
        "全栈开发工程师": ["全栈", "fullstack", "web开发"],
        "软件测试工程师": ["测试", "qa", "test"],
        "ai公司": ["ai公司", "人工智能", "大模型", "aigc", "智能"],
        "数字化创新岗": ["数字化", "创新", "数字化转型", "数字化运营"],
        "软件行业": ["软件", "软件公司", "软件研发", "软件工程师"],
    }

    def _match_keyword(title: str, tags: List[str]) -> str:
        """标题/标签命中任一关键词的技术词元，返回命中的关键词，否则 ''。"""
        text = (title + " " + " ".join(tags or [])).lower()
        for kw, terms in KW_TERMS.items():
            for t in terms:
                if t.lower() in text:
                    return kw
        return ""

    jobs: List[Dict[str, Any]] = []
    api = boss_api.BossApi(minimal_list=minimal_list)
    city_codes = {boss_api.resolve_city(c) for c in cities}
    city_names = set(cities)
    _set_collect_error("", None, "")

    try:
        for page_no in range(1, max_pages + 1):
            _log(f"推荐列表 第 {page_no} 页")
            cards = api.list_recommend_jobs(page=page_no)
            if not cards:
                _log("  该页无岗位")
                break
            _log(f"  返回 {len(cards)} 条")
            for card in cards:
                title = card.get("title") or ""
                tags = card.get("tags") or []
                card_city = card.get("city") or ""
                # 粗筛1：城市匹配（空城市放行，交由后续人工判断）
                hit_city = (not card_city) or card_city in city_names or card_city in city_codes
                if not hit_city:
                    continue
                # 粗筛2：标题/标签命中关键词技术词元
                card["keyword"] = _match_keyword(title, tags)
                jobs.append(card)
            # 翻页间隔，避免被识别为高频采集
            if page_no < max_pages:
                _sleep(*DELAY_PAGE)
            # 通知前端进度
            if on_page:
                on_page(page_no, max_pages)
    except boss_api.BossApiError as e:
        _log(f"API 采集失败：{e}")
        _set_collect_error("api", e.code, str(e))
    except Exception as e:  # noqa: BLE001
        _log(f"API 采集异常：{e}")
        _set_collect_error("internal", -1, str(e))
    return jobs


def open_dashboard() -> None:
    """打开 JobHunter 岗位看板页面。"""
    try:
        webbrowser.open("http://127.0.0.1:8686/jobs")
    except Exception as e:
        _log(f"打开看板失败：{e}")


def run() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--engine", choices=["api", "authed", "playwright"], default="api",
                        help="采集引擎：api=recommend接口非登录态最小cookie（默认）；authed=完整登录cookie安全采集（降频限量）；playwright=登录态浏览器")
    parser.add_argument("--keywords", default="")
    parser.add_argument("--pages", type=int, default=1, help="每关键词翻页数（风控建议 1，勿超过 2）")
    parser.add_argument("--no-jd", action="store_true", help="跳过 JD 详情抓取（轻量模式，与 --max-jd 0 等价）")
    parser.add_argument("--max-jd", type=int, default=0, help="最多抓取前 N 条 JD 详情（默认 0=不抓详情；详情页请求最易触发风控，建议保持 0）")
    parser.add_argument("--max-keywords", type=int, default=1, help="本轮最多采集关键词数（风控建议 1，单轮轻量）")
    args = parser.parse_args()

    cfg = config.load_config()
    search = cfg.get("search", {})
    if args.keywords:
        keywords = [k.strip() for k in args.keywords.split(",") if k.strip()]
    else:
        keywords = list(search.get("keywords", []))
    cities = [c.strip() for c in re.split(r"[,，、]", search.get("city", "")) if c.strip()]
    if not cities:
        cities = ["厦门"]

    if not keywords:
        _log("未配置关键词")
        return 1

    # ── API 引擎（默认）：非登录态 recommend 接口采集 ──────────────────
    if args.engine == "api":
        _log("使用 API 引擎：recommend 接口非登录态采集（最小 cookie，规避 search 风控）")
        # 单轮轻量：限制本轮关键词数量（风控建议 1）
        if len(keywords) > args.max_keywords:
            kept = keywords[: args.max_keywords]
            _log(f"单轮轻量：关键词从 {len(keywords)} 截断为 {len(kept)}（{','.join(kept)}）")
            keywords = kept
        result = {"keywords": keywords, "cities": cities, "blocked": False, "login_lost": False, "collected": 0}
        _log(f"启动随机冷却 {DELAY_BOOT[0]}~{DELAY_BOOT[1]} 秒…")
        _sleep(*DELAY_BOOT)
        all_jobs = collect_via_api(keywords, cities, args.pages)
        result["collected"] = len(all_jobs)
        _log(f"API 采集合计 {len(all_jobs)} 条")
        if all_jobs:
            # 智能过滤：复用 smart_filter（年限/学历排除始终生效）
            # - 标题命中关键词的岗位（keyword 非空）为明确意向，直接保留；
            #   仅当命中年限/学历排除时才剔除。
            # - 未命中关键词的岗位按 smart_filter 技术匹配（≥2 项）决定。
            #   recommend 推荐流无 JD 描述，技术匹配对关键词命中岗位放宽，避免误杀。
            try:
                from smart_filter import match_job

                before = len(all_jobs)
                kept = []
                for j in all_jobs:
                    r = match_job(j)
                    if r["pass"]:
                        kept.append(j)
                        continue
                    # 失败原因：仅"技术匹配不足"可因关键词命中而豁免；年限/学历排除不豁免
                    if r["reason"].startswith("技术匹配不足") and j.get("keyword"):
                        kept.append(j)
                all_jobs = kept
                _log(f"智能过滤：采集 {before} 条 → 通过 {len(all_jobs)} 条")
            except Exception as e:
                _log(f"智能过滤异常（跳过，仍按原样合并）：{e}")
        if all_jobs:
            merged = merge_jobs(all_jobs)
            result.update(merged)
            _log(f"新增 {merged['added']} 条，累计 {merged['total']} 条")
        write_round_log("collect", result)
        # 本轮无新增岗位：直接跳回看板
        if result.get("added", 0) == 0:
            _log("本轮无新增岗位，直接跳回看板")
            open_dashboard()
        return 0

    # ── authed 引擎：登录态安全采集（完整 cookie，降频+单轮限量+遇风控即停） ─
    if args.engine == "authed":
        _log("使用 authed 引擎：登录态安全采集（完整 cookie，降频+单轮限量，遇 code=37/7 即停）")
        _log(f"登录态启动冷却 {boss_api.BossApi.AUTH_DELAY_BOOT[0]}~{boss_api.BossApi.AUTH_DELAY_BOOT[1]} 秒…")
        api = boss_api.BossApi(minimal_list=False)
        try:
            cards = api.collect_authed(max_pages=args.pages)
        except boss_api.BossApiError as e:
            _log(f"登录态安全采集失败：{e}")
            result = {"keywords": keywords, "cities": cities, "blocked": False,
                      "login_lost": False, "collected": 0, "error": str(e)}
            write_round_log("collect", result)
            return 1
        # 风险中断信息（如有）
        risk = getattr(api, "_last_risk", None)
        if risk:
            _log(f"采集因风控提前停止：{risk}")
        all_jobs = []
        city_codes = {boss_api.resolve_city(c) for c in cities}
        city_names = set(cities)
        for card in cards:
            card_city = card.get("city") or ""
            if card_city and card_city not in city_names and card_city not in city_codes:
                continue
            all_jobs.append(card)
        result = {"keywords": keywords, "cities": cities, "blocked": bool(risk),
                  "login_lost": False, "collected": len(all_jobs), "risk": risk or ""}
        _log(f"authed 采集合计 {len(all_jobs)} 条（风控中断：{bool(risk)}）")
        if all_jobs:
            # 智能过滤（复用 smart_filter，链路不变）
            try:
                from smart_filter import match_job

                before = len(all_jobs)
                kept = []
                for j in all_jobs:
                    r = match_job(j)
                    if r["pass"]:
                        kept.append(j)
                        continue
                    if r["reason"].startswith("技术匹配不足") and j.get("keyword"):
                        kept.append(j)
                all_jobs = kept
                _log(f"智能过滤：采集 {before} 条 → 通过 {len(all_jobs)} 条")
            except Exception as e:
                _log(f"智能过滤异常（跳过，仍按原样合并）：{e}")
        if all_jobs:
            merged = merge_jobs(all_jobs)
            result.update(merged)
            _log(f"新增 {merged['added']} 条，累计 {merged['total']} 条")
        write_round_log("collect", result)
        if result.get("added", 0) == 0:
            _log("本轮无新增岗位，直接跳回看板")
            open_dashboard()
        return 0

    # ── playwright 引擎（可选）：登录态浏览器采集 ─────────────────────
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        _log("缺少 playwright，请执行：pip install playwright")
        return 1

    # 单轮轻量：限制本轮关键词数量（风控建议 1）
    if len(keywords) > args.max_keywords:
        kept = keywords[: args.max_keywords]
        _log(f"单轮轻量：关键词从 {len(keywords)} 截断为 {len(kept)}（{','.join(kept)}）")
        keywords = kept

    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    _log(f"使用独立登录 profile：{PROFILE_DIR}")
    # 启动随机冷却，打散定时触发的整点节奏
    _log(f"启动随机冷却 {DELAY_BOOT[0]}~{DELAY_BOOT[1]} 秒…")
    _sleep(*DELAY_BOOT)
    result = {"keywords": keywords, "cities": cities, "blocked": False, "login_lost": False, "collected": 0}
    all_jobs: List[Dict[str, Any]] = []

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            channel="chrome",
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
            locale="zh-CN",
        )
        page = ctx.new_page()
        # 先访问首页探测登录态
        try:
            page.goto("https://www.zhipin.com/", wait_until="domcontentloaded", timeout=40000)
            _sleep(3, 5)
        except Exception:
            pass
        blk = _detect_block(page)
        if blk == "blocked":
            result["blocked"] = True
            _log("IP 或账号被 Boss直聘 拦截（code31），本轮停止")
        elif blk == "login":
            result["login_lost"] = True
            _log("登录态失效，需要重新登录 jobhunter profile")
        else:
            body = page.inner_text("body")
            logged_in = ("登录" not in body[:300]) or ("极速沟通" in body)
            _log(f"首页加载完成，url={page.url}，登录态={logged_in}")

        # 登录态无效则不采集
        if not result["blocked"] and not result["login_lost"]:
            try:
                if "web/geek" not in page.url and "login" in page.url:
                    result["login_lost"] = True
                    _log("检测到未登录，跳过采集")
                else:
                    for city in cities:
                        for idx, kw in enumerate(keywords):
                            if idx > 0:
                                _log(f"关键词间冷却 {DELAY_KW[0]}~{DELAY_KW[1]} 秒…")
                                _sleep(*DELAY_KW)
                            collected = collect_keyword(page, kw, city, args.pages, args.max_jd)
                            _log(f"  关键词[{kw}] 城市[{city}] 采得 {len(collected)} 条")
                            all_jobs.extend(collected)
                            blk2 = _detect_block(page)
                            if blk2 == "blocked":
                                result["blocked"] = True
                                _log("中途触发拦截，停止后续采集")
                                break
                            if blk2 == "login":
                                result["login_lost"] = True
                                _log("中途登录态失效，停止后续采集")
                                break
                            _sleep(5, 9)
                        if result["blocked"] or result["login_lost"]:
                            break
            except Exception as e:
                _log(f"采集异常：{e}")
        ctx.close()

    result["collected"] = len(all_jobs)
    if all_jobs:
        # 智能过滤新增岗位：技术匹配≥2项、排除年限/硕士博士要求（复用 smart_filter）
        try:
            from smart_filter import match_job

            before = len(all_jobs)
            all_jobs = [j for j in all_jobs if match_job(j)["pass"]]
            _log(f"智能过滤：采集 {before} 条 → 通过 {len(all_jobs)} 条")
        except Exception as e:
            _log(f"智能过滤异常（跳过，仍按原样合并）：{e}")
        if all_jobs:
            merged = merge_jobs(all_jobs)
            result.update(merged)
            _log(f"新增 {merged['added']} 条，累计 {merged['total']} 条")
    write_round_log("collect", result)
    # 本轮无新增岗位：直接跳回看板
    if result.get("added", 0) == 0:
        _log("本轮无新增岗位，直接跳回看板")
        open_dashboard()
    return 0


if __name__ == "__main__":
    sys.exit(run())

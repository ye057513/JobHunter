"""JobHunter —— 多平台采集框架。

原则（与项目反爬/安全边界一致）：
- **上游只采岗位列表**，绝不自动投递/打招呼；投递仍需上层人工确认。
- 每个平台一个采集器，统一实现 `JobCollector.collect(...)` → 返回岗位 dict 列表
  （每份岗位均已打好 `source` 来源标签，写库时不混平台）。
- 已接入：
  * `boss`        —— 复用现有 recommend API 采集（BossApi / fetch_jobs.collect_via_api）。
  * `qiuzhifangzhou` —— 打真实站点 qiuzhifangzhou.com/job 的浏览器采集（需先在该站点登录 profile）。
- 框架占位：`yingsheng` / `zhaopin` / `51job` —— 建立在统一的浏览器采集上，
  各自提供站点专用选择器与搜索链接模板；登录态不足或无匹配时返回清晰提示，待逐个调通。

注册：`COLLECTORS` 字典 key ↔ 采集器类。CLI / Web 端通过 `get_collector(key)` 分发。
"""
from __future__ import annotations

import json
import logging
import random
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 人类化随机延迟（防风控，与 fetch_jobs 保持同一量级）
# ---------------------------------------------------------------------------
def _sleep(lo: float, hi: float) -> None:
    time.sleep(random.uniform(lo, hi))


def profile_msg(profile_dir: Optional[Path], key: str) -> str:
    """返回登录态 profile 的展示路径（用户可据此准备登录态）。"""
    p = profile_dir or Path.home() / ".config" / "jobhunter-platform" / key
    return str(p)


_BOOT = (6, 12)
_FIRST = (3, 6)
_PAGE = (4, 8)
_DETAIL = (6, 10)


# ---------------------------------------------------------------------------
# 采集器基类
# ---------------------------------------------------------------------------
class JobCollector:
    key: str = ""
    source: str = ""
    label: str = ""
    home_url: str = ""

    def __init__(self, cfg: Optional[Dict[str, Any]] = None, on_page: Optional[Callable] = None,
                 profile_dir: Optional[Path] = None):
        self.cfg = cfg or {}
        self.on_page = on_page or (lambda p, t: None)
        self.profile_dir = profile_dir
        self.last_api_error: Dict[str, Any] = {}
        self._collect_error: Dict[str, Any] = {}
        # engine_mode: "anonymous"=非登录态匿名浏览器；"authed"=登录态用户 profile
        self.engine_mode = "anonymous" if self.cfg.get("engine", "api") in ("api", "anonymous") else "authed"
        self.api_used = self.engine_mode == "authed"  # 二进制：本轮最终是否走登录态

    @property
    def last_error(self) -> Dict[str, Any]:
        return self._collect_error

    def collect(self, keywords: List[str], cities: List[str], max_pages: int = 1) -> List[Dict[str, Any]]:
        raise NotImplementedError

    # ── Boss 参照的通用后过滤：城市/停招已按卡片处理，这里补技术栈过滤 ──
    def _post_filter(self, jobs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """与 Boss.collect() 完全一致的技术过滤：
        通过 smart_filter 直接保留；仅当失败原因=「技术匹配不足」且该岗位命中搜索关键词时豁免。"""
        try:
            from smart_filter import match_job
        except Exception:
            return jobs
        kept: List[Dict[str, Any]] = []
        for j in jobs:
            r = match_job(j)
            if r["pass"]:
                kept.append(j)
                continue
            if r["reason"].startswith("技术匹配不足") and j.get("keyword"):
                kept.append(j)
        return kept

    # ── 免登录态（无账号/最小cookie）JSON 接口采集 —— 非登录态优先，不足则登录态兜底 ──
    api_available: bool = False          # 平台是否可能提供可免登录的 JSON 接口
    api_endpoint: str = ""
    _api_delay = (0.8, 2.5)              # 人类化请求间隔（与 Boss api 同量级）

    def api_collect(self, keywords: List[str], cities: List[str], max_pages: int = 1) -> List[Dict[str, Any]]:
        """免登录态 API 采集。默认未实现；子类覆写。返回岗位 dict 列表。"""
        self.last_api_error = {
            "type": "api_unavailable",
            "message": f"平台「{self.label}」暂未提供免登录 JSON 接口，本次自动转入登录态采集。",
        }
        return []

    @staticmethod
    def _api_ua() -> str:
        return ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36")

    @staticmethod
    def _uuid() -> str:
        import uuid
        return str(uuid.uuid4())

    def _api_get_json(self, url: str, params: Dict[str, Any], headers: Dict[str, str],
                      action: str, retries: int = 2, cookies: Optional[Dict[str, str]] = None,
                      timeout: float = 25.0) -> Dict[str, Any]:
        """人类化随机延迟 + 有限重试 + HTML重定向检测。返回 JSON dict。"""
        import httpx
        last_exc: Optional[Exception] = None
        for attempt in range(retries + 1):
            try:
                _sleep(*self._api_delay)
                resp = httpx.get(url, params=params, headers=headers, cookies=cookies or {},
                                 timeout=timeout, follow_redirects=True)
                text = resp.text
                if not text or text.lstrip().startswith("<"):
                    raise RuntimeError(f"{action}: 收到 HTML 而非 JSON（可能被重定向/验证码拦截）")
                return resp.json()
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_exc = exc
                time.sleep(2 ** attempt)
        raise RuntimeError(f"{action}: 请求失败: {last_exc}")

    def _api_post_json(self, url: str, params: Dict[str, Any], headers: Dict[str, str],
                       json_body: Dict[str, Any], action: str, retries: int = 2,
                       cookies: Optional[Dict[str, str]] = None, timeout: float = 25.0) -> Dict[str, Any]:
        """POST JSON（智联类接口用）：人类化延迟 + 重试 + HTML 检测。"""
        import httpx
        last_exc: Optional[Exception] = None
        for attempt in range(retries + 1):
            try:
                _sleep(*self._api_delay)
                resp = httpx.post(url, params=params, headers=headers, json=json_body,
                                  cookies=cookies or {}, timeout=timeout, follow_redirects=True)
                text = resp.text
                if not text or text.lstrip().startswith("<"):
                    raise RuntimeError(f"{action}: 收到 HTML 而非 JSON（可能被重定向/验证码拦截）")
                return resp.json()
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_exc = exc
                time.sleep(2 ** attempt)
        raise RuntimeError(f"{action}: 请求失败: {last_exc}")

    # 日志/进度辅助
    def _page(self, page_no: int, total: int) -> None:
        try:
            self.on_page(page_no, total)
        except Exception:
            pass

    def _tag(self, job: Dict[str, Any]) -> Dict[str, Any]:
        job.setdefault("source", self.source)
        job.setdefault("status", job.get("status") or "待处理")
        return job


# ---------------------------------------------------------------------------
# Boss：复用现有 recommend API 采集
# ---------------------------------------------------------------------------
class BossCollector(JobCollector):
    key = "boss"
    source = "Boss直聘"
    label = "Boss直聘"
    home_url = "https://www.zhipin.com"

    @staticmethod
    def _kw_terms() -> Dict[str, List[str]]:
        # 用户关键词 → 技术词元（用于匹配 recommend 推荐流标题的缩写/英文代号）
        return {
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

    def collect(self, keywords: List[str], cities: List[str], max_pages: int = 1) -> List[Dict[str, Any]]:
        import sys
        from pathlib import Path as _P
        sys.path.insert(0, str(_P(__file__).resolve().parent))
        import boss_api

        terms = self._kw_terms()
        kw_low = {k.lower(): v for k, v in terms.items()}

        def _match_keyword(title: str, tags: List[str]) -> str:
            text = (title + " " + " ".join(tags or [])).lower()
            for kw, tlist in kw_low.items():
                if any(t.lower() in text for t in tlist):
                    return kw
            return ""

        jobs: List[Dict[str, Any]] = []
        api = boss_api.BossApi(minimal_list=bool(self.cfg.get("minimal_list", True)))
        city_codes = {boss_api.resolve_city(c) for c in cities}
        city_names = set(cities)
        self._collect_error = {}
        try:
            for page_no in range(1, max_pages + 1):
                cards = api.list_recommend_jobs(page=page_no)
                if not cards:
                    break
                for card in cards:
                    title = card.get("title") or ""
                    card_city = card.get("city") or ""
                    if card_city and card_city not in city_names and card_city not in city_codes:
                        continue
                    card["keyword"] = _match_keyword(title, card.get("tags") or [])
                    self._tag(card)
                    jobs.append(card)
                if page_no < max_pages:
                    _sleep(*_PAGE)
                self._page(page_no, max_pages)
            # 智能过滤（复用 smart_filter，与基类一致）
            jobs = self._post_filter(jobs)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Boss 采集异常: %s", exc)
            self._collect_error = {"type": "boss", "code": None, "message": str(exc)}
        return jobs


# ---------------------------------------------------------------------------
# 通用浏览器采集器（Playwright）：供未走专用 API 的平台使用
# ---------------------------------------------------------------------------
class PlaywrightJobCollector(JobCollector):
    """基于持久化浏览器 profile 的通用采集器。

    子类需定义：
      - `site_selectors`：卡片 / 标题 / 薪资 / 公司 / 链接 / 学历经验 选择器（多套兜底）
      - `search_url_template(keyword, city)`：返回搜索页 URL
      - `requires_login`：该站点是否强制登录
    采集时复用持久化 profile（首次需在该站点登录一次，之后带登录态采集）。
    """

    key: str = ""
    source: str = ""
    label: str = ""
    home_url: str = ""
    browser_channel: str = "chrome"       # 或 "msedge"
    requires_login: bool = True

    # 站点专用选择器（多套兜底）
    site_selectors: Dict[str, List[str]] = {}
    # 搜索链接模板，子类覆写
    def search_url_template(self, keyword: str, city: str) -> str:  # noqa: ARG002
        return self.home_url

    # Boss 停招关键词复用
    CLOSED_KEYWORDS = ["已停招", "已关闭", "已下架", "temporarily inactive"]

    def _is_closed(self, job: Dict[str, Any]) -> bool:
        salary = (job.get("salary") or "").strip()
        title = (job.get("title") or "").lower()
        if not salary:
            return True
        if re.search(r"[\ue000-\uf8ff]", salary):  # Unicode 私有区乱码
            return True
        if salary in ("未披露", "面议", ""):
            return True
        return any(k in title for k in self.CLOSED_KEYWORDS)

    def _build_job(self, card, keyword: str, city: str) -> Dict[str, Any]:
        sel = self.site_selectors
        title = self._qtext(card, sel.get("title"))
        if not title:
            return {}
        href = self._resolve_url(self._qhref(card, sel.get("link")))
        if not href:
            # 部分平台(应届生等)检索列表卡片是 Vue 组件、无 <a href>，从 JS 数据读岗位ID/跳转链接
            href = self._read_spa_job_url(card)
        job: Dict[str, Any] = {
            "title": title,
            "salary": self._qtext(card, sel.get("salary")),
            "company": self._qtext(card, sel.get("company")),
            "info": self._qtext(card, sel.get("info")),
            "region": self._qtext(card, sel.get("location")) or self._qtext(card, sel.get("info")) or "",
            "url": href,
            "link": href,
            "keyword": keyword,
            "city": city,
            "education": "",
            "experience": "",
            "description": "",
            "status": "待处理",
        }
        info = job["info"] or ""
        for part in re.split(r"[\s·|,，]", info):
            if not part:
                continue
            if part in ("本科", "大专", "硕士", "博士", "学历不限"):
                job["education"] = part
            elif re.search(r"年经验|\d+-\d+年|应届|在校|无需经验|经验不限", part):
                job["experience"] = part
        return self._tag(job)

    @staticmethod
    def _resolve_url(href: str) -> str:
        if not href:
            return ""
        if href.startswith("//"):
            href = "https:" + href
        return href

    def _qtext(self, card, selectors: List[str]) -> str:
        if not selectors:
            return ""
        for s in selectors:
            try:
                el = card.query_selector(s)
                if el:
                    t = el.inner_text().strip()
                    if t:
                        return t
            except Exception:
                continue
        return ""

    def _qhref(self, card, selectors: List[str]) -> str:
        if not selectors:
            return ""
        for s in selectors:
            try:
                el = card.query_selector(s)
                if el:
                    h = el.get_attribute("href") or ""
                    if h:
                        return h
            except Exception:
                continue
        return ""

    def _read_spa_job_url(self, card) -> str:
        """SPA 平台(应届生等)卡片无 <a href>，从卡片及其祖先 __vue__ 组件数据读岗位ID/跳转链接。

        返回稳定的岗位详情链接（用于入库判重与用户点开详情）；读不到返回空串。
        """
        try:
            d = card.evaluate("""el => {
                const probe = n => {
                    if(!n) return null;
                    const v = n.__vue__ || (n._vnode && n._vnode.context);
                    if(!v) return null;
                    const jd = (v.jobData || (v.$data && v.$data.jobData)) || {};
                    const st = JSON.stringify(jd).slice(0,120);
                    return {tag: (n.className||n.tagName||'').toString().slice(0,30), jobId: jd.jobId||null, jump: jd.jumpUrlHttp||jd.url||null, hasJobData: !!(v.jobData||v.$data), sample: st};
                };
                const nodes=[el];
                while(nodes.length){ const cur=nodes.shift(); const p=probe(cur); if(p) return p; if(cur && cur.querySelectorAll){ for(const k of cur.querySelectorAll('*')) nodes.push(k);} }
                return null;
            }""")
            if not d or not (d.get("jump") or d.get("jobId")):
                return ""
            jump = d.get("jump") or ""
            if jump:
                return str(jump)
            base = getattr(self, "detail_base", "") or self.home_url
            return f"{str(base).rstrip('/')}/jobdetail/{d['jobId']}.html"
        except Exception:
            pass
        return ""

    def _detect_login_required(self, page) -> str:
        """检测是否被要求登录 / 被 WAF / 验证码 拦截。返回 '' 正常 / 'login' / 'blocked'。"""
        try:
            url = (page.url or "") if page else ""
            if any(k in url for k in ("passport", "login")):
                return "login"
            title = ""
            body = ""
            try:
                title = (page.title() or "") if page else ""
                body = page.inner_text("body")[:2000] if page else ""
            except Exception:
                pass
            # WAF 挑战页：body 极短、含 JS 混淆 / 标题为安全校验
            if any(k in body for k in ("请扫码登录", "请先登录", "登录后查看", "立即登录", "点击登录", "登录后投递")):
                return "login"
            if any(k in body for k in ("访问受限", "存在异常行为", "human verification", "Just a moment")):
                return "blocked"
            if any(k in title for k in ("验证", "安全", "Just a moment", "访问")):
                if len(body) < 60:
                    return "blocked"
            # 站点特有 WAF / 滑块验证关键字
            if any(k in body for k in ("滑块", "拖动", "完成验证", "geetest", "captcha", "按住")):
                return "blocked"
            # 空 body 且 URL 落回首页（未能进入检索）视为待登录/被拦
            if page and len(body.strip()) < 8:
                return "login"
        except Exception:
            pass
        return ""

    # ---------- 主流程（按 engine_mode 分发：非登录态匿名 / 登录态 profile） ----------
    def collect(self, keywords: List[str], cities: List[str], max_pages: int = 1) -> List[Dict[str, Any]]:
        """按 engine_mode 采集：
        - "anonymous"：临时匿名无痕 profile（非登录态，浏览器执行 WAF 挑战后抓渲染 DOM）；
          若被要求登录 / 触发验证码则设置 last_error 并返回 []，由调用方决定是否转入登录态。
        - "authed"：复用用户持久化 profile（需已在该平台登录）；未登录给出引导并抛错。
        """
        if not keywords:
            keywords = [""]
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            logger.error("缺少 playwright，请执行 pip install playwright")
            raise RuntimeError("缺少 playwright 依赖，请先安装 pip install playwright")

        anonymous = self.engine_mode == "anonymous"
        if anonymous:
            import tempfile
            profile = Path(tempfile.mkdtemp(prefix=f"jobhunter_anon_{self.key}_"))
        else:
            profile = self.profile_dir or Path.home() / ".config" / "jobhunter-platform" / self.key
            profile.mkdir(parents=True, exist_ok=True)

        _sleep(*_BOOT)
        with sync_playwright() as p:
            ctx = p.chromium.launch_persistent_context(
                user_data_dir=str(profile),
                channel=self.browser_channel,
                headless=bool(self.cfg.get("headless", True)),
                args=["--disable-blink-features=AutomationControlled",
                      "--no-first-run",
                      "--disable-background-mode"],  # 防止 GUI 关窗后 Chrome 后台驻留占用 profile 锁
                locale="zh-CN",
            )
            try:
                jobs = self._run_search_session(ctx, keywords, cities, max_pages, anonymous)
                return self._post_filter(jobs)
            finally:
                ctx.close()

    def collect_non_login(self, keywords: List[str], cities: List[str], max_pages: int = 1) -> List[Dict[str, Any]]:
        """显式非登录态采集入口（调用方已确定要走匿名模式）。"""
        self.engine_mode = "anonymous"
        return self.collect(keywords, cities, max_pages)

    def _wait_waf_settle(self, page) -> str:
        """WAF（Cloudflare/Aliyun 等）挑战页会自运行若干秒后放行。
        切到「验证」类挑战页时稍等并重载，等挑战结算后再判定；避免把还在结算中的挑战误判为永久封禁。"""
        for _ in range(3):
            state = self._detect_login_required(page)
            if state != "blocked":
                return state  # '' 正常 或 'login' 需登录
            _sleep(2.5, 4.5)
            try:
                page.reload(wait_until="domcontentloaded", timeout=30000)
            except Exception:
                pass
            _sleep(2.5, 4.5)
        return self._detect_login_required(page)

    def _run_search_session(self, ctx, keywords: List[str], cities: List[str],
                            max_pages: int, anonymous: bool) -> List[Dict[str, Any]]:
        jobs: List[Dict[str, Any]] = []
        page = ctx.new_page()
        for kw in keywords or [""]:
            for city in cities or [""]:
                url = self.search_url_template(kw, city)
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=45000)
                    _sleep(*_FIRST)
                except Exception as exc:
                    logger.warning("检索页跳转失败 %s: %s", url, exc)
                    continue
                state = self._wait_waf_settle(page)
                if state == "login":
                    msg = (f"平台「{self.label}」需要登录：请在浏览器打开 {self.home_url} 登录后采集。"
                           if anonymous else
                           f"平台「{self.label}」需要登录：请在浏览器打开 {self.home_url} "
                           f"并登录该平台后，再回到看板采集。\n登录态保存在 profile：{profile_msg(self.profile_dir, self.key)}")
                    self._collect_error = {"type": "login", "code": None, "message": msg}
                    if not anonymous:
                        ctx.close()
                        raise RuntimeError(msg)
                    return jobs  # 非登录态：停，交由调用方转入登录态
                if state == "blocked":
                    msg = (f"平台「{self.label}」触发访问限制/验证码：请放慢频率稍后重试，或先登录该平台再采。")
                    self._collect_error = {"type": "blocked", "code": None, "message": msg}
                    if not anonymous:
                        ctx.close()
                        raise RuntimeError(msg)
                    return jobs  # 非登录态：停，交由调用方转入登录态
                jobs.extend(self._scrape_pool(page, kw, city, max_pages))
                _sleep(3, 6)
        return jobs

    def _scrape_pool(self, page, keyword: str, city: str, max_pages: int) -> List[Dict[str, Any]]:
        sel = self.site_selectors
        jobs: List[Dict[str, Any]] = []
        card_sels = sel.get("card")
        seen = set()
        for page_no in range(1, max_pages + 1):
            if page_no > 1:
                paginated = self._next_page(page)
                if not paginated:
                    # 无「下一页」元素：51job 等 SPA 检索页为无限滚动，改用滚动到底加载更多
                    self._scroll_to_bottom(page, rounds=2)
            self._page(page_no, max_pages)
            cards = []
            for s in card_sels or []:
                try:
                    cards = page.query_selector_all(s)
                    if cards:
                        break
                except Exception:
                    continue
            if not cards:
                break
            found_new = False
            for card in cards:
                link = self._qhref(card, sel.get("link")) or ""
                if link and link in seen:
                    continue
                j = self._build_job(card, keyword, city)
                if j and j.get("title") and not self._is_closed(j):
                    jobs.append(j)
                    if link:
                        seen.add(link)
                        found_new = True
            if not jobs and not found_new:
                # 首轮一张也没解析出：可能选择器不匹配，给一次滚动机会
                if page_no == 1:
                    self._scroll_to_bottom(page, rounds=2)
                    cards = []
                    for s in card_sels or []:
                        try:
                            cards = page.query_selector_all(s)
                            if cards:
                                break
                        except Exception:
                            continue
                    for card in cards:
                        j = self._build_job(card, keyword, city)
                        if j and j.get("title") and not self._is_closed(j):
                            jobs.append(j)
                    if jobs:
                        continue
                break
            # 模拟滚动（随机、人类化）
            try:
                for _ in range(random.randint(1, 2)):
                    page.mouse.wheel(0, random.randint(1000, 2500))
                    _sleep(1.2, 2.5)
            except Exception:
                pass
        return jobs

    def _scroll_to_bottom(self, page, rounds: int = 2) -> None:
        """模拟真人滚动到底多次，触发 SPA 无限滚动加载更多列表。"""
        try:
            from playwright.sync_api import sync_playwright as sw
            _ = sw
        except Exception:
            pass
        try:
            for _ in range(max(1, rounds)):
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                _sleep(1.5, 3.0)
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                _sleep(1.5, 3.0)
        except Exception:
            pass

    def _next_page(self, page) -> bool:
        click_sels = self.site_selectors.get("next") or [
            "a.next", ".pagination .next", "button.next",
            ".pagination__item--next", ".mypage .p_next", "[class*='next']",
        ]
        for s in click_sels:
            try:
                el = page.query_selector(s)
                if el:
                    el.click()
                    _sleep(*_PAGE)
                    return True
            except Exception:
                continue
        return False


# ---------------------------------------------------------------------------
# 远程方舟（求职方舟 · AI找工作）—— 真实站点
# ---------------------------------------------------------------------------
class QiuzhifangzhouCollector(PlaywrightJobCollector):
    key = "qiuzhifangzhou"
    source = "求职方舟·AI找工作"
    label = "求职方舟·AI找工作"
    home_url = "https://www.qiuzhifangzhou.com/job"
    requires_login = True
    browser_channel = "chrome"

    site_selectors = {
        "card": [".job-card", ".position-item", "li.job-item", "[class*='jobCard']", "[class*='job-card']"],
        "title": ["[class*='title'] h3", "[class*='job-title']", "[class*='position-name']", "a[class*='title']"],
        "salary": ["[class*='salary']", "[class*='salary-range']", "span[name='salary']"],
        "company": ["[class*='company']", "[class*='corp-name']", ".company-name"],
        "info": ["[class*='meta']", "[class*='tags']", "[class*='job-info']"],
        "link": ["a[href*='job']", "a[href*='position']"],
        "next": ["a.next", ".pagination .next", "button.next"],
    }

    def search_url_template(self, keyword: str, city: str) -> str:
        import urllib.parse
        q = urllib.parse.urlencode([k for k in [("kw", keyword)] if keyword] + ([("city", city)] if city else []))
        return self.home_url + ("?" + q if q else "")


# ---------------------------------------------------------------------------
# 应届生求职 / 智联 / 51job
# 说明：三平台免登录 JSON 接口均被阿里云 WAF / 站点风控硬拦截（返回加密 JS 或占位空结果），
#       非登录态无法调通。唯一可行路径 = 登录态 Playwright（真实浏览器渲染 + 已登录 profile，
#       浏览器执行 WAF 挑战脚本事后可正常访问检索页）。
#       因此三个采集器以「登录态浏览器采集」为主，统一继承 PlaywrightJobCollector 的人类化
#       延迟 / 登录失效探测 / 被迫跳转(验证码)检测 / 有限重试，对齐 Boss 的防封与字段约定。
# ---------------------------------------------------------------------------
class YingshengCollector(PlaywrightJobCollector):
    key = "yingsheng"
    source = "应届生求职"
    label = "应届生求职"
    home_url = "https://www.yingjiesheng.com"
    browser_channel = "chrome"
    requires_login = True

    # 应届生职位检索 q.yingjiesheng.com 真实结构（实测 2026-08）：
    # 卡片 .search-list-item，标题 left-title-name，公司 left-detail-company，
    # 薪资 right-salary，标签/学历 left-tag-item，链接 search-list-href。
    site_selectors = {
        "card": [".search-list-item", ".search-list-item-wrapper"],
        "title": [".left-title-name", "[class*='title-name'] a", ".left-title-name a"],
        "salary": [".right-salary"],
        "company": [".left-detail-company", "[class*='company']"],
        "info": [".left-tag-item"],
        "location": [".left-tag-item"],
        "link": [".search-list-href", "a.search-list-href", ".left-title-name a",
                 "a[href*='/jobs/']", "a[href*='/job/']"],
        "next": [".search-list-pagination [class*='next']", "a.next",
                 ".pagination .next"],
    }

    def search_url_template(self, keyword: str, city: str) -> str:
        import urllib.parse
        # 应届生职位搜索引擎 q.yingjiesheng.com；city 可选（工作地点）
        q = urllib.parse.quote(keyword or "")
        base = f"https://q.yingjiesheng.com/jobs/search/{q or ''}"
        if city and not city.startswith("全国"):
            base += f"?cp=0&jobarea={urllib.parse.quote(city)}"
        return base


class ZhaopinCollector(PlaywrightJobCollector):
    key = "zhaopin"
    source = "智联招聘"
    label = "智联招聘"
    home_url = "https://www.zhaopin.com"
    browser_channel = "chrome"
    requires_login = True

    # 智联已无可用免登录检索接口（base/data 仅返回筛选条件；sou 返回占位空结果）。
    # 统一走登录态浏览器采集检索页 sou.zhaopin.com。
    # 智联真实检索页会在 sou.zhaopin.com 校验后重定向到 www.zhaopin.com/jobs，
    # 岗位卡片为 BEM 结构 .job-card（实测 2026-08，必须真实浏览器过 EdgeOne）。
    site_selectors = {
        "card": [".job-card"],
        "title": [".job-card__title-main", ".job-card__title-clamp", "a[class*='job-card__title']"],
        "salary": [".job-card__salary"],
        "company": [".job-card__company-name", ".job-card__company"],
        "info": [".job-card__skill-tags", "[class*='job-card__info']"],
        "location": [".job-card__location"],
        "link": ["a.job-card__title-main", "a[class*='job-card__company-name--link']",
                 "a[href*='/jobs/']", "a[href*='JobDetail']"],
        "next": [".jobs-split-page [class*='next']", ".pagination__item--next", "button.next"],
    }

    def search_url_template(self, keyword: str, city: str) -> str:
        import urllib.parse
        # 直接进入 www.zhaopin.com/jobs 检索页，规避 sou 校验再跳转；kt=3 全职
        # jl 支持城市中文名：优先用已配置的城市码，无码时直接用城市名限定检索区域，
        # 避免默认全国检索导致岗位落入其它城市、被「目标城市过滤」误删。
        city_code = ""
        if city and not city.startswith("全国"):
            city_code = (self.cfg.get("city_codes") or {}).get(city) or city
        params = urllib.parse.urlencode(
            [("kw", keyword or ""), ("jl", city_code or ""), ("kt", "3")])
        return f"https://www.zhaopin.com/jobs?{params}"


class Job51Collector(PlaywrightJobCollector):
    key = "51job"
    source = "前程无忧51Job"
    label = "前程无忧51Job"
    home_url = "https://www.51job.com"
    browser_channel = "chrome"
    requires_login = True

    # we.51job.com 检索接口被 WAF 拦截（返回 HTML/验证码），统一走登录态浏览器采集。
    # we.51job.com 检索页真实结构（实测 2026-08）：
    # 卡片 .joblist-item；标题 .jname；薪资 .sal；公司 .cname；标签/学历 .joblist-item-tags。
    site_selectors = {
        "card": [".joblist-item"],
        "title": [".jname", ".job-info"],
        "salary": [".sal"],
        "company": [".cname", "[class*='company-name']"],
        "info": [".joblist-item-tags", ".tags"],
        "location": [".area"],
        "link": ["a[href*='jobs.51job.com']", ".joblist-item a[href]", "a[href*='job']"],
        "next": [".el-pagination .next", "a.next", "[class*='pagination'] .next"],
    }

    def search_url_template(self, keyword: str, city: str) -> str:
        import urllib.parse
        # 51job 检索页参数：keyword / jobArea(6 位城市码) / searchType=2 全职
        area = ""
        if city:
            area = (self.cfg.get("city_codes") or {}).get(city, "")
        params = urllib.parse.urlencode(
            [("keyword", keyword or ""), ("jobArea", area or ""),
             ("searchType", "2"), ("sortType", "0"), ("metro", "")])
        return f"https://we.51job.com/pc/search?{params}"


# ---------------------------------------------------------------------------
# 注册表
# ---------------------------------------------------------------------------
COLLECTORS: Dict[str, type] = {
    "boss": BossCollector,
    "qiuzhifangzhou": QiuzhifangzhouCollector,
    "yingsheng": YingshengCollector,
    "zhaopin": ZhaopinCollector,
    "51job": Job51Collector,
}


def get_collector(key: str, cfg: Optional[Dict[str, Any]] = None,
                  on_page: Optional[Callable] = None,
                  profile_dir: Optional[Path] = None) -> JobCollector:
    """按平台 key 拿到采集器实例；未知 key 抛异常。"""
    cls = COLLECTORS.get(key)
    if cls is None:
        raise ValueError(f"未知平台 key：{key}")
    return cls(cfg=cfg, on_page=on_page, profile_dir=profile_dir)
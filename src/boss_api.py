"""JobHunter —— boss-cli 集成层（拆解自 kabi-boss-cli 的 BossClient）。

职责：
1. 岗位【列表】采集：走 recommend 接口（/wapi/zprelation/interaction/geekGetJob tag=5），
   规避 search 接口（/wapi/zpgeek/search/joblist.json）的动态 __zp_stoken__ 风控（code=37）。
2. 列表采集默认使用【最小 cookie（仅 wt2）】——recommend 接口实测仅需 wt2 即可成功，
   不携带完整登录态 cookie（wbg / zp_at / bst），降低登录态接口触发风控的风险。
3. 登录态动作（岗位详情 get_job_detail / 打招呼 add_friend）单独提供，使用完整 credential cookie。

安全边界：add_friend（打招呼/投递）绝不在本模块内自动调用；必须由上层在人工确认后显式调用。

字段映射（recommend cardList → JobHunter job dict）：
    encryptJobId → url       jobName → title      brandName → company
    salaryDesc   → salary    cityName → city      jobExperience → experience
    jobDegree    → education jobLabels → tags     jobId/securityId/lid → 保留（供详情/投递）
"""
from __future__ import annotations

import json
import logging
import random
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

# ── 端点（与 boss-cli constants 对齐） ─────────────────────────────
BASE_URL = "https://www.zhipin.com"
WEB_GEEK_JOB_URL = f"{BASE_URL}/web/geek/job"
WEB_GEEK_RECOMMEND_URL = f"{BASE_URL}/web/geek/recommend"
GEEK_GET_JOB_URL = "/wapi/zprelation/interaction/geekGetJob"
JOB_DETAIL_URL = "/wapi/zpgeek/job/detail.json"
JOB_SEARCH_URL = "/wapi/zpgeek/search/joblist.json"
FRIEND_ADD_URL = "/wapi/zpgeek/friend/add.json"

CREDENTIAL_FILE = Path.home() / ".config" / "boss-cli" / "credential.json"

# 列表接口最小必需 cookie（实测：仅 wt2 即可让 recommend 返回 code=0）
MINIMAL_LIST_COOKIES = ("wt2",)
# 完整登录态所需 cookie（boss-cli REQUIRED_COOKIES + bst）
FULL_AUTH_COOKIES = ("__zp_stoken__", "wt2", "wbg", "zp_at", "bst")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/145.0.0.0 Safari/537.36"
    ),
    "sec-ch-ua": '"Chromium";v="145", "Not(A:Brand";v="99", "Google Chrome";v="145"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"macOS"',
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "DNT": "1",
    "Priority": "u=1, i",
    "Origin": BASE_URL,
    "Referer": f"{BASE_URL}/",
}

# 城市编码（与 boss-cli CITY_CODES 对齐，仅列常见）
CITY_CODES: Dict[str, str] = {
    "全国": "100010000",
    "北京": "101010100", "上海": "101020100", "广州": "101280100", "深圳": "101280600",
    "杭州": "101210100", "成都": "101270100", "南京": "101190100", "武汉": "101200100",
    "西安": "101110100", "苏州": "101190400", "长沙": "101250100", "天津": "101030100",
    "重庆": "101040100", "郑州": "101180100", "东莞": "101281600", "佛山": "101280800",
    "合肥": "101220100", "青岛": "101120200", "宁波": "101210400", "沈阳": "101070100",
    "昆明": "101290100", "大连": "101070200", "厦门": "101230200", "珠海": "101280700",
    "无锡": "101190200", "福州": "101230100", "济南": "101120100", "哈尔滨": "101050100",
    "长春": "101060100", "南昌": "101240100", "贵阳": "101260100", "南宁": "101300100",
    "石家庄": "101090100", "太原": "101100100", "兰州": "101160100", "海口": "101310100",
    "常州": "101191100", "温州": "101210700", "嘉兴": "101210300", "徐州": "101190800",
    "香港": "101320100",
}

# 岗位详情页 URL 模板（匿名可访问，供看板跳转）
JOB_DETAIL_PAGE_URL = BASE_URL + "/job_detail/{job_id}.html"


def resolve_city(name: str) -> str:
    """城市名 → 编码；已编码则透传。"""
    if name.isdigit() and len(name) >= 6:
        return name
    return CITY_CODES.get(name, CITY_CODES["全国"])


def load_credentials() -> Dict[str, str]:
    """读取 boss-cli 登录凭证 cookie。缺失时返回空 dict。"""
    if not CREDENTIAL_FILE.exists():
        return {}
    try:
        data = json.loads(CREDENTIAL_FILE.read_text(encoding="utf-8"))
        cookies = data.get("cookies") or {}
        return {k: v for k, v in cookies.items() if v}
    except Exception as e:  # noqa: BLE001
        logger.warning("读取 boss-cli 凭证失败: %s", e)
        return {}


def _filter_cookies(cookies: Dict[str, str], keys) -> Dict[str, str]:
    return {k: cookies[k] for k in keys if k in cookies}


class BossApiError(Exception):
    def __init__(self, message: str, code: int = -1, response=None):
        super().__init__(message)
        self.code = code
        self.response = response


class BossApi:
    """JobHunter 的 boss-cli 拆解集成层。

    Args:
        minimal_list: 列表采集仅携带最小 cookie（wt2），不携带完整登录态。默认 True。
    """

    def __init__(self, minimal_list: bool = True, timeout: float = 30.0):
        self.minimal_list = minimal_list
        self.timeout = timeout
        self._cookies = load_credentials()

    # ── 内部请求 ────────────────────────────────────────────────────
    def _headers_for(self, url: str, params: Optional[Dict[str, Any]]) -> Dict[str, str]:
        headers = dict(HEADERS)
        if url == GEEK_GET_JOB_URL and params and params.get("tag") == 5:
            headers["Referer"] = WEB_GEEK_RECOMMEND_URL
        elif url == GEEK_GET_JOB_URL:
            headers["Referer"] = WEB_GEEK_RECOMMEND_URL
        elif url in (JOB_DETAIL_URL, JOB_SEARCH_URL):
            headers["Referer"] = WEB_GEEK_JOB_URL
        elif url == FRIEND_ADD_URL:
            headers["Referer"] = WEB_GEEK_RECOMMEND_URL
        return headers

    def _get(self, url: str, params: Dict[str, Any], cookies: Dict[str, str], action: str,
            retries: int = 2) -> Dict[str, Any]:
        """GET 请求 + 人类化随机延迟 + 有限重试。返回 JSON。"""
        last_exc = None
        for attempt in range(retries + 1):
            try:
                # 人类化随机延迟（0.8~2.5s），降低被识别为脚本的频率
                time.sleep(random.uniform(0.8, 2.5))
                resp = httpx.get(
                    BASE_URL + url,
                    params=params,
                    headers=self._headers_for(url, params),
                    cookies=cookies,
                    timeout=self.timeout,
                    follow_redirects=True,
                )
                text = resp.text
                if not text or text.lstrip().startswith("<"):
                    raise BossApiError(f"{action}: 收到 HTML 而非 JSON（可能被重定向）", code=-1)
                return resp.json()
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_exc = exc
                logger.warning("%s 网络异常(%s)，重试 %d/%d", action, exc, attempt + 1, retries)
                time.sleep(2 ** attempt)
        raise BossApiError(f"{action}: 请求失败: {last_exc}")

    def _handle(self, data: Dict[str, Any], action: str) -> Dict[str, Any]:
        code = data.get("code", -1)
        if code == 0:
            return data.get("zpData", {})
        msg = data.get("message", "未知错误")
        if code == 37:
            raise BossApiError(f"{action}: 环境异常(code=37)，请稍后重试或降低频率", code=code)
        if code == 7:
            raise BossApiError(f"{action}: 登录态失效(code=7)，需重新登录 boss-cli", code=code)
        raise BossApiError(f"{action}: {msg} (code={code})", code=code)

    # ── 列表采集（非登录态路径） ────────────────────────────────────
    def list_recommend_jobs(self, page: int = 1) -> List[Dict[str, Any]]:
        """采集推荐岗位列表（recommend 接口，tag=5）。

        使用最小 cookie（默认仅 wt2），不携带完整登录态，规避 search 接口的
        动态 stoken 风控（code=37）。
        """
        cookies = _filter_cookies(self._cookies, MINIMAL_LIST_COOKIES) if self.minimal_list else dict(self._cookies)
        if not cookies:
            raise BossApiError("未找到 boss-cli 登录凭证（credential.json），无法采集岗位列表")

        data = self._get(
            GEEK_GET_JOB_URL,
            params={"page": page, "tag": 5, "isActive": "true"},
            cookies=cookies,
            action="推荐岗位列表",
        )
        zp = self._handle(data, "推荐岗位列表")
        cards = zp.get("cardList") or []
        jobs = [self._card_to_job(c) for c in cards]
        return [j for j in jobs if j]

    def _card_to_job(self, card: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """recommend card → JobHunter job dict（字段对齐 smart_filter / web.py）。"""
        title = card.get("jobName") or ""
        if not title:
            return None
        encrypt_id = card.get("encryptJobId") or card.get("jobId") or ""
        return {
            "title": title,
            "salary": card.get("salaryDesc") or "",
            "company": card.get("brandName") or "",
            "city": card.get("cityName") or "",
            "info": " ".join(
                x for x in [
                    card.get("cityName") or "",
                    card.get("jobExperience") or "",
                    card.get("jobDegree") or "",
                ] if x
            ),
            "education": card.get("jobDegree") or "",
            "experience": card.get("jobExperience") or "",
            "tags": card.get("jobLabels") or [],
            "description": "",
            "url": JOB_DETAIL_PAGE_URL.format(job_id=encrypt_id) if encrypt_id else "",
            "link": JOB_DETAIL_PAGE_URL.format(job_id=encrypt_id) if encrypt_id else "",
            "keyword": "",
            "status": "待处理",
            # 保留给后续详情/投递
            "_boss": {
                "encryptJobId": encrypt_id,
                "securityId": card.get("securityId") or "",
                "lid": card.get("lid") or "",
                "encryptBossId": card.get("encryptBossId") or "",
            },
        }

    # ── 登录态安全采集（降频 + 单轮限量 + 遇风控即停） ──────────────
    AUTH_DELAY_BOOT = (15, 40)      # 登录态采集启动冷却（更保守）
    AUTH_DELAY_PAGE = (8, 18)       # 登录态翻页间隔（更保守）
    AUTH_DELAY_MAX_PER_ROUND = 40   # 单轮最多采集条数（降频，避免触发风控）
    AUTH_MAX_PAGES = 3              # 单轮最多翻页数

    def collect_authed(self, max_pages: int = 3, max_per_round: int = 40,
                       boot_delay: bool = True) -> List[Dict[str, Any]]:
        """登录态安全采集推荐岗位列表（完整 cookie）。

        与 list_recommend_jobs 的区别（登录态安全边界）：
        1. 使用完整登录 cookie（wt2/wbg/zp_at/bst），可获取更贴合登录账号的推荐流；
        2. 更保守的人类化随机延迟（启动 15~40s、翻页 8~18s）；
        3. 单轮限量（默认最多 40 条）与翻页上限（默认 3 页），降频防风控；
        4. 遇 code=37（环境异常）/ code=7（登录失效）立即停止，返回已采集部分。

        仅采集岗位【列表】，不包含任何投递/打招呼动作（投递仍需上层人工确认）。
        """
        cookies = _filter_cookies(self._cookies, FULL_AUTH_COOKIES)
        if not cookies:
            raise BossApiError("未找到完整 boss-cli 登录凭证（credential.json），登录态安全采集不可用")
        if boot_delay:
            time.sleep(random.uniform(*self.AUTH_DELAY_BOOT))

        jobs: List[Dict[str, Any]] = []
        max_pages = max(1, min(int(max_pages), self.AUTH_MAX_PAGES))
        max_per_round = max(1, min(int(max_per_round), self.AUTH_DELAY_MAX_PER_ROUND))

        for page_no in range(1, max_pages + 1):
            if page_no > 1:
                time.sleep(random.uniform(*self.AUTH_DELAY_PAGE))
            try:
                data = self._get(
                    GEEK_GET_JOB_URL,
                    params={"page": page_no, "tag": 5, "isActive": "true"},
                    cookies=cookies,
                    action="登录态推荐岗位列表",
                )
            except BossApiError as exc:
                # 遇风控（code=37/7）或网络异常：立即停止，返回已采部分
                self._last_risk = str(exc)
                break
            zp = self._handle(data, "登录态推荐岗位列表")
            cards = zp.get("cardList") or []
            page_jobs = [self._card_to_job(c) for c in cards]
            page_jobs = [j for j in page_jobs if j]
            jobs.extend(page_jobs)
            if len(jobs) >= max_per_round:
                jobs = jobs[:max_per_round]
                break
            if not zp.get("hasMore", True):
                break
        return jobs

    # ── 登录态动作（必须登录） ──────────────────────────────────────
    def _auth_cookies(self) -> Dict[str, str]:
        cookies = _filter_cookies(self._cookies, FULL_AUTH_COOKIES)
        if not cookies:
            raise BossApiError("未找到完整 boss-cli 登录凭证，登录态动作不可用")
        return cookies

    def get_job_detail(self, security_id: str, lid: str = "") -> Dict[str, Any]:
        """获取岗位详情（登录态）。"""
        params: Dict[str, str] = {"securityId": security_id}
        if lid:
            params["lid"] = lid
        data = self._get(JOB_DETAIL_URL, params=params, cookies=self._auth_cookies(), action="岗位详情")
        return self._handle(data, "岗位详情")

    def send_greeting(self, security_id: str, lid: str = "", confirm: bool = False) -> Dict[str, Any]:
        """打招呼/投递（登录态）。

        ⚠️ 安全边界：必须 confirm=True 才会真正发送。调用方（人工确认后）负责传 True。
        本方法绝不自动投递。
        """
        if not confirm:
            raise BossApiError("send_greeting 需要人工确认（confirm=True）后才可调用")
        params: Dict[str, str] = {"securityId": security_id}
        if lid:
            params["lid"] = lid
        data = self._get(FRIEND_ADD_URL, params=params, cookies=self._auth_cookies(), action="打招呼")
        return self._handle(data, "打招呼")

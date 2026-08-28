"""JobHunter 七步法动态求职管理系统（FastAPI 前后端一体 · 增强版）。

功能：
- 动态看板（SPA）：10 栏七步法看板，支持拖拽流转、暗色模式、图表可视化、日历视图
- 全流程 Web 化：采集 → AI评分 → 招呼语生成 → HR监听 → 简历定制，全部从看板一键触发
- 批量操作、岗位对比、招呼语在线编辑、数据导出、WebSocket实时推送
- Boss直聘安全边界：采集保留随机延迟/最小cookie/限量/拦截检测；监听仅读取不发送；投递需人工确认
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import random
import re
import sys
import threading
import time
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Set

from fastapi import FastAPI, File, Form, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, Response, StreamingResponse
import httpx
import uvicorn

try:
    from . import config
except ImportError:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import config

app = FastAPI(title="JobHunter 七步法动态求职管理系统")

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

STATUSES = [
    "新发现", "待评估", "简历待优化", "待投递", "已投递",
    "笔试测评", "面试中", "等待结果", "Offer", "暂不考虑已结束", "已停招",
]

STATUS_MAP = {
    "待处理": "新发现", "已收藏": "待评估", "已打招呼": "已投递",
    "已回复": "面试中", "已评分": "待评估", "待确认": "待投递",
    "未获得面试机会": "暂不考虑已结束", "不符合要求": "暂不考虑已结束",
    "投递失败": "待投递",
}

DEFAULT_JOB: Dict[str, Any] = {
    "title": "", "company": "", "industry": "", "company_intro": "",
    "city": "", "location": "", "salary": "", "source": "Boss直聘",
    "url": "", "link": "", "jd_publish_time": "",
    "responsibilities": "", "requirements": "", "plus_points": "",
    "freq_keywords": [], "match_points": "", "skill_gaps": "",
    "match_score": 0, "priority": "C", "apply_time": "", "apply_reason": "",
    "status": "新发现", "next_action": "", "action_deadline": "",
    "resume_version": "", "interview_feedback": "", "follow_up_date": "",
    "followups": [], "keyword": "", "education": "", "experience": "",
    "info": "", "description": "", "tags": [], "tech_matches": [],
    "greeting": "", "created_at": "", "updated_at": "",
}


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def normalize_job(job: Dict[str, Any]) -> Dict[str, Any]:
    """补齐缺失字段，兼容旧数据；迁移旧状态与 score→match_score。"""
    n = dict(DEFAULT_JOB)
    for k, v in job.items():
        n[k] = v
    if n["status"] in STATUS_MAP:
        n["status"] = STATUS_MAP[n["status"]]
    if n["status"] not in STATUSES:
        n["status"] = "新发现"
    if not n.get("source"):
        n["source"] = "Boss直聘"
    if not n.get("link") and n.get("url"):
        n["link"] = n["url"]
    if not n.get("url") and n.get("link"):
        n["url"] = n["link"]
    if not n.get("location") and n.get("city"):
        n["location"] = n["city"]
    # score → match_score 迁移
    if not n.get("match_score") and n.get("score"):
        try:
            n["match_score"] = int(n["score"])
        except (TypeError, ValueError):
            pass
    try:
        n["match_score"] = int(n.get("match_score") or 0)
    except (TypeError, ValueError):
        n["match_score"] = 0
    n["match_score"] = max(0, min(100, n["match_score"]))
    if n["priority"] not in ("A", "B", "C"):
        n["priority"] = "C"
    if isinstance(n.get("freq_keywords"), str):
        n["freq_keywords"] = [x.strip() for x in re.split(r"[,，、]", n["freq_keywords"]) if x.strip()]
    if not isinstance(n.get("followups"), list):
        n["followups"] = []
    if not n.get("created_at"):
        n["created_at"] = _now()
    return n


def load_jobs() -> List[Dict[str, Any]]:
    jobs_file = config.DATA_DIR / "jobs.json"
    if not jobs_file.exists():
        return []
    try:
        raw = json.loads(jobs_file.read_text(encoding="utf-8"))
    except Exception:
        return []
    if not isinstance(raw, list):
        return []
    return [normalize_job(j) for j in raw]


def save_jobs(jobs: List[Dict[str, Any]]) -> None:
    jobs_file = config.DATA_DIR / "jobs.json"
    jobs_file.write_text(json.dumps(jobs, ensure_ascii=False, indent=2), encoding="utf-8")


def find_job(jobs: List[Dict[str, Any]], url: str) -> Dict[str, Any]:
    for j in jobs:
        if j.get("url") == url:
            return j
    return {}


# ---------------------------------------------------------------------------
# WebSocket 广播管理
# ---------------------------------------------------------------------------

class WSBroadcast:
    def __init__(self):
        self.connections: Set[WebSocket] = set()
        self._loop = None

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.connections.add(ws)
        await ws.send_json({"type": "connected"})

    def disconnect(self, ws: WebSocket):
        self.connections.discard(ws)

    def broadcast(self, message: dict):
        if not self._loop or not self.connections:
            return
        try:
            asyncio.run_coroutine_threadsafe(self._do_broadcast(message), self._loop)
        except Exception:
            pass

    async def _do_broadcast(self, message: dict):
        dead = set()
        for ws in self.connections:
            try:
                await ws.send_json(message)
            except Exception:
                dead.add(ws)
        self.connections -= dead


ws_mgr = WSBroadcast()


@app.on_event("startup")
async def _capture_loop():
    ws_mgr._loop = asyncio.get_running_loop()


# ---------------------------------------------------------------------------
# 后台任务状态管理
# ---------------------------------------------------------------------------

def _new_state() -> Dict[str, Any]:
    return {
        "running": False, "done": False, "ok": False, "message": "",
        "percent": 0, "stage": "idle", "started_at": "", "finished_at": "",
        "result": {}, "error_type": "", "error_message": "",
    }


def _set_state(state, lock, **kw):
    with lock:
        state.update(kw)


_collect_state = _new_state()
_collect_lock = threading.Lock()
_score_state = _new_state()
_score_lock = threading.Lock()
_greet_state = _new_state()
_greet_lock = threading.Lock()
_monitor_state = _new_state()
_monitor_lock = threading.Lock()
_resume_state = _new_state()
_resume_lock = threading.Lock()

# ── 扫码登录状态（BOSS 直聘 二维码登录） ────────────────────────────
_login_state = _new_state()
_login_lock = threading.Lock()
_login_state.update({"phase": "idle", "progress": 0})


def _classify_collect_error(exc: Exception, last: Dict[str, Any]) -> tuple:
    """把采集异常归类为（报错类型, 报错内容），供采集弹窗展示。"""
    code = None
    msg = ""
    if last and last.get("type"):
        code = last.get("code")
        msg = last.get("message") or ""
    if code is None:
        code = getattr(exc, "code", None)
        msg = msg or str(exc)
    msg = msg or str(exc)
    if code == 7 or "登录态失效" in msg:
        return ("登录态失效",
                "BOSS 直聘登录态已失效（code=7）。请点击弹窗内「🔐 扫码登录」或右上角「🔐 登录」重新扫码登录后再采集。")
    if code == 37 or "code=37" in msg:
        return ("风控拦截",
                "触发 BOSS 直聘环境异常风控（code=37）。请降低采集频率，等待一段时间后再试；频繁触发可能被临时封禁。")
    if code == -1 or "网络" in msg:
        return ("网络异常", msg)
    if "未找到 boss-cli 登录凭证" in msg:
        return ("未配置登录", "未找到 boss-cli 登录凭证（credential.json）。请点击右上角「🔐 登录」扫码登录后再采集。")
    return ("采集失败", msg)


# ---------------------------------------------------------------------------
# 后台任务 Worker
# ---------------------------------------------------------------------------

def _run_collect_worker() -> None:
    """采集线程：api 引擎轻量采集 → 智能过滤 → 合并入库 → 打轮次日志。

    安全措施（保留不变）：
    - 启动随机冷却（DELAY_BOOT 10-30s）
    - 最小 cookie（仅 wt2），不携带完整登录态
    - 单关键词、单页采集（风控限量）
    - smart_filter 技术栈过滤
    """
    started = _now()
    _set_state(_collect_state, _collect_lock, running=True, done=False, ok=False,
               message="采集中…", collected=0, added=0, total=0, percent=0,
               stage="读取配置…", started_at=started, finished_at="")
    ws_mgr.broadcast({"type": "progress", "task": "collect", "percent": 0, "stage": "读取配置…"})
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import fetch_jobs

        cfg = config.load_config()
        search = cfg.get("search", {})
        keywords = [k.strip() for k in search.get("keywords", []) if k.strip()]
        cities = [c.strip() for c in re.split(r"[,，、]", search.get("city", "") or "") if c.strip()]
        if not cities:
            cities = ["厦门"]
        if len(keywords) > 1:
            keywords = keywords[:1]
        if not keywords:
            _set_state(_collect_state, _collect_lock, done=True, ok=False,
                       message="未配置搜索关键词", stage="未配置关键词", finished_at=_now(),
                       error_type="配置缺失", error_message="未配置搜索关键词，请到「配置」页填写关键词后再采集。")
            return
        _set_state(_collect_state, _collect_lock, percent=8, stage="准备启动（冷却防风控）…")

        boot_lo, boot_hi = fetch_jobs.DELAY_BOOT
        boot_total = random.uniform(boot_lo, boot_hi)
        boot_steps = 8
        for _i in range(1, boot_steps + 1):
            time.sleep(boot_total / boot_steps)
            pct = 8 + int(_i / boot_steps * 32)
            _set_state(_collect_state, _collect_lock, percent=pct, stage="启动冷却中…")
            ws_mgr.broadcast({"type": "progress", "task": "collect", "percent": pct, "stage": "启动冷却中…"})

        _set_state(_collect_state, _collect_lock, percent=42, stage="正在采集岗位…")
        ws_mgr.broadcast({"type": "progress", "task": "collect", "percent": 42, "stage": "正在采集岗位…"})
        collect_pages = int(cfg.get("search", {}).get("collect_pages", 35))
        all_jobs = fetch_jobs.collect_via_api(keywords, cities, max_pages=collect_pages,
                                              on_page=lambda p, total: ws_mgr.broadcast({
                                                  "type": "progress", "task": "collect",
                                                  "percent": 42 + int(p / max(1, total) * 35),
                                                  "stage": f"正在采集岗位… 第 {p}/{total} 页"
                                              }))

        _set_state(_collect_state, _collect_lock, percent=70, stage="智能过滤…")
        ws_mgr.broadcast({"type": "progress", "task": "collect", "percent": 70, "stage": "智能过滤…"})
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
        except Exception:
            pass

        result = {"keywords": keywords, "cities": cities, "blocked": False,
                  "login_lost": False, "collected": len(all_jobs)}
        if all_jobs:
            _set_state(_collect_state, _collect_lock, percent=85, stage="合并入库…")
            ws_mgr.broadcast({"type": "progress", "task": "collect", "percent": 85, "stage": "合并入库…"})
            merged = fetch_jobs.merge_jobs(all_jobs)
            result.update(merged)
        try:
            fetch_jobs.write_round_log("collect_manual", result)
        except Exception:
            pass

        added = result.get("added", 0)
        total = result.get("total", 0)
        dup = result.get("dup", 0)
        closed = result.get("closed", 0)
        parts = [f"采得 {len(all_jobs)} 条，新增 {added} 条"]
        if dup:
            parts.append(f"，同内容重复跳过 {dup} 条")
        if closed:
            parts.append(f"，已停招清理 {closed} 条")
        if total:
            parts.append(f"，累计 {total} 条")
        msg = "，".join(parts)
        _set_state(_collect_state, _collect_lock, done=True, ok=True, message=msg,
                   collected=len(all_jobs), added=added, total=total,
                   percent=100, stage="完成", result=result,
                   error_type="", error_message="")
        ws_mgr.broadcast({"type": "task_done", "task": "collect", "ok": True, "message": msg})
    except Exception as e:
        import traceback
        traceback.print_exc()
        _last = None
        if "fetch_jobs" in sys.modules:
            _last = getattr(sys.modules["fetch_jobs"], "LAST_COLLECT_ERROR", None)
        err_type, err_msg = _classify_collect_error(e, _last)
        _set_state(_collect_state, _collect_lock, done=True, ok=False, message=f"采集失败：{e}",
                   stage="采集失败", error_type=err_type, error_message=err_msg)
        ws_mgr.broadcast({"type": "task_done", "task": "collect", "ok": False, "message": str(e)})
    finally:
        _set_state(_collect_state, _collect_lock, running=False, finished_at=_now())


def _run_login_worker() -> None:
    """BOSS 直聘 二维码登录线程：randkey→生成二维码→等待扫码→确认→写 credential.json。"""
    started = _now()
    _set_state(_login_state, _login_lock, running=True, done=False, ok=False,
               phase="generating", progress=5, message="正在生成登录二维码…",
               started_at=started, finished_at="", error_type="", error_message="")

    def _cancelled() -> bool:
        with _login_lock:
            return _login_state.get("phase") == "cancelled"

    def _finish(phase, ok, message, error_type="", error_message=""):
        _set_state(_login_state, _login_lock, phase=phase, done=True, ok=ok,
                   message=message, error_type=error_type, error_message=error_message)

    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import boss_api
        BASE = boss_api.BASE_URL
        HDR = dict(boss_api.HEADERS)
        qr_file = config.DATA_DIR / "login_qr.jpg"
        qr_file.parent.mkdir(parents=True, exist_ok=True)

        with httpx.Client(headers=HDR, base_url=BASE, timeout=40, follow_redirects=True) as client:
            # 1) randkey 获取 qrId
            try:
                r = client.post("/wapi/zppassport/captcha/randkey")
                data = r.json()
            except Exception as e:
                raise RuntimeError(f"获取登录 randkey 失败：{e}")
            if data.get("code") != 0:
                raise RuntimeError(f"获取登录 randkey 失败：{data}")
            qr_id = data["zpData"]["qrId"]

            # 2) 官方 getqrcode 生成二维码图片（App 可识别）
            r = client.get("/wapi/zpweixin/qrcode/getqrcode", params={"content": qr_id}, timeout=30)
            body = r.content
            is_img = body[:3] == b"\xff\xd8\xff" or body[:8] == b"\x89PNG\r\n\x1a\n"
            if r.status_code != 200 or not is_img or len(body) <= 500:
                raise RuntimeError("二维码图片生成失败，请稍后重试")
            qr_file.write_bytes(body)
            _set_state(_login_state, _login_lock, phase="ready", progress=20,
                       message="二维码已生成，请用 BOSS 直聘 App 扫码")

            # 3) 等待扫码（约 12 分钟）
            scanned = False
            for _ in range(25):
                if _cancelled():
                    return
                try:
                    d = client.get("/wapi/zppassport/qrcode/scan", params={"uuid": qr_id}, timeout=35).json()
                    if d.get("scaned"):
                        scanned = True
                        break
                except Exception:
                    pass
                _set_state(_login_state, _login_lock, progress=20, message="等待扫码中…")
            if not scanned:
                if not _cancelled():
                    _finish("expired", False, "二维码已过期，请重新生成")
                return
            _set_state(_login_state, _login_lock, phase="scanned", progress=55,
                       message="已扫码，请在手机上确认登录")

            # 4) 等待手机端确认
            confirmed = False
            for _ in range(25):
                if _cancelled():
                    return
                try:
                    r = client.get("/wapi/zppassport/qrcode/scanLogin", params={"qrId": qr_id}, timeout=35)
                    if r.status_code == 200:
                        confirmed = True
                        break
                except Exception:
                    pass
            if not confirmed:
                if not _cancelled():
                    _finish("expired", False, "手机端未确认已超时，请重新生成二维码")
                return
            _set_state(_login_state, _login_lock, phase="confirmed", progress=75,
                       message="已确认，正在写入登录凭证…")

            # 5) dispatcher + warmup 收集完整 cookie
            cookies: Dict[str, str] = {}
            r = client.get("/wapi/zppassport/qrcode/dispatcher", params={"qrId": qr_id, "pk": "header-login"})
            for name, value in r.cookies.items():
                cookies[name] = value
            for name, value in client.cookies.items():
                cookies[name] = value
            try:
                w = client.get("/", timeout=15)
                w.raise_for_status()
                for name, value in w.cookies.items():
                    cookies[name] = value
                for name, value in client.cookies.items():
                    cookies[name] = value
            except Exception:
                pass
            if not cookies:
                raise RuntimeError("登录后未获取到有效 cookie，请重试")

            # 6) 写入 credential.json（结构对齐 boss_api.load_credentials）
            cred_file = boss_api.CREDENTIAL_FILE
            cred_file.parent.mkdir(parents=True, exist_ok=True)
            cred_file.write_text(json.dumps(
                {"cookies": {k: v for k, v in cookies.items() if v},
                 "saved_at": _now()}, ensure_ascii=False, indent=2), encoding="utf-8")

        _finish("success", True, "登录成功，登录凭证已更新")
        ws_mgr.broadcast({"type": "login_done", "ok": True, "message": "BOSS 直聘 登录成功"})
    except Exception as e:
        import traceback
        traceback.print_exc()
        _finish("error", False, f"登录失败：{e}", error_type="登录失败", error_message=str(e))
    finally:
        _set_state(_login_state, _login_lock, running=False, finished_at=_now())


def _run_score_worker() -> None:
    """AI 评分线程：调用硅基流动 API 评分，不接触 Boss直聘，无封号风险。"""
    started = _now()
    _set_state(_score_state, _score_lock, running=True, done=False, ok=False,
               message="评分中…", percent=0, stage="读取数据…", started_at=started)
    ws_mgr.broadcast({"type": "progress", "task": "score", "percent": 0, "stage": "读取数据…"})
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import scorer

        def on_progress(i, total, msg):
            pct = int(i / total * 90) + 5 if total else 5
            _set_state(_score_state, _score_lock, percent=pct, stage=msg)
            ws_mgr.broadcast({"type": "progress", "task": "score", "percent": pct, "stage": msg})

        result = scorer.score_jobs_web(on_progress=on_progress)
        msg = f"评分完成：{result['scored']} 成功，{result['failed']} 失败"
        _set_state(_score_state, _score_lock, done=True, ok=True, message=msg,
                   percent=100, stage="完成", result=result)
        ws_mgr.broadcast({"type": "task_done", "task": "score", "ok": True, "message": msg})
    except Exception as e:
        import traceback
        traceback.print_exc()
        _set_state(_score_state, _score_lock, done=True, ok=False, message=f"评分失败：{e}", stage="失败")
        ws_mgr.broadcast({"type": "task_done", "task": "score", "ok": False, "message": str(e)})
    finally:
        _set_state(_score_state, _score_lock, running=False, finished_at=_now())


def _run_greet_worker() -> None:
    """招呼语生成线程：调用硅基流动 API，不接触 Boss直聘。"""
    started = _now()
    _set_state(_greet_state, _greet_lock, running=True, done=False, ok=False,
               message="生成招呼语中…", percent=0, stage="读取数据…", started_at=started)
    ws_mgr.broadcast({"type": "progress", "task": "greet", "percent": 0, "stage": "读取数据…"})
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import greeter

        def on_progress(i, total, msg):
            pct = int(i / total * 90) + 5 if total else 5
            _set_state(_greet_state, _greet_lock, percent=pct, stage=msg)
            ws_mgr.broadcast({"type": "progress", "task": "greet", "percent": pct, "stage": msg})

        result = greeter.generate_greetings_web(min_score=60, on_progress=on_progress)
        msg = f"招呼语生成完成：{result['generated']} 成功，{result['failed']} 失败"
        _set_state(_greet_state, _greet_lock, done=True, ok=True, message=msg,
                   percent=100, stage="完成", result=result)
        ws_mgr.broadcast({"type": "task_done", "task": "greet", "ok": True, "message": msg})
    except Exception as e:
        import traceback
        traceback.print_exc()
        _set_state(_greet_state, _greet_lock, done=True, ok=False, message=f"生成失败：{e}", stage="失败")
        ws_mgr.broadcast({"type": "task_done", "task": "greet", "ok": False, "message": str(e)})
    finally:
        _set_state(_greet_state, _greet_lock, running=False, finished_at=_now())


def _run_monitor_worker() -> None:
    """HR 回复监听线程：Playwright 浏览器访问，仅读取消息不发送，保留安全延迟。

    安全措施：
    - Playwright 浏览器访问（非高频 API 调用）
    - 页面加载后随机延迟 3-6 秒
    - 登录态检测，失效时等待手动登录
    - 仅读取消息列表，不执行任何发送/投递动作
    """
    started = _now()
    _set_state(_monitor_state, _monitor_lock, running=True, done=False, ok=False,
               message="监听中…", percent=0, stage="启动浏览器…", started_at=started)
    ws_mgr.broadcast({"type": "progress", "task": "monitor", "percent": 0, "stage": "启动浏览器…"})
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import monitor

        def on_progress(msg):
            _set_state(_monitor_state, _monitor_lock, stage=msg, percent=50)
            ws_mgr.broadcast({"type": "progress", "task": "monitor", "percent": 50, "stage": msg})

        result = monitor.monitor_web(on_progress=on_progress)
        _set_state(_monitor_state, _monitor_lock, done=True, ok=result.get("ok", False),
                   message=result.get("message", ""), percent=100, stage="完成", result=result)
        ws_mgr.broadcast({"type": "task_done", "task": "monitor",
                          "ok": result.get("ok", False), "message": result.get("message", "")})
    except Exception as e:
        import traceback
        traceback.print_exc()
        _set_state(_monitor_state, _monitor_lock, done=True, ok=False, message=f"监听失败：{e}", stage="失败")
        ws_mgr.broadcast({"type": "task_done", "task": "monitor", "ok": False, "message": str(e)})
    finally:
        _set_state(_monitor_state, _monitor_lock, running=False, finished_at=_now())


def _run_resume_worker(job_url: str) -> None:
    """简历定制线程：调用硅基流动 API，不接触 Boss直聘。"""
    started = _now()
    _set_state(_resume_state, _resume_lock, running=True, done=False, ok=False,
               message="生成简历中…", percent=0, stage="读取数据…", started_at=started)
    ws_mgr.broadcast({"type": "progress", "task": "resume", "percent": 0, "stage": "读取数据…"})
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import resume as resume_mod

        _set_state(_resume_state, _resume_lock, percent=20, stage="读取岗位信息…")
        job = resume_mod.load_job(job_url)
        if not job:
            _set_state(_resume_state, _resume_lock, done=True, ok=False, message="未找到岗位", stage="失败")
            ws_mgr.broadcast({"type": "task_done", "task": "resume", "ok": False, "message": "未找到岗位"})
            return

        _set_state(_resume_state, _resume_lock, percent=40, stage="读取简历素材…")
        resume_text = resume_mod.load_resume()
        client = config.get_llm_client()

        _set_state(_resume_state, _resume_lock, percent=60, stage="AI 生成中…")
        ws_mgr.broadcast({"type": "progress", "task": "resume", "percent": 60, "stage": "AI 生成中…"})
        content = resume_mod.generate_resume(client, job, resume_text)

        _set_state(_resume_state, _resume_lock, percent=90, stage="保存文件…")
        safe_title = "".join(c for c in job.get("title", "岗位") if c not in '\\/:*?"<>|').strip()
        out_path = config.EXPORT_DIR / f"定制简历_{safe_title}.md"
        out_path.write_text(content, encoding="utf-8")

        msg = f"简历已生成：{out_path.name}"
        _set_state(_resume_state, _resume_lock, done=True, ok=True, message=msg,
                   percent=100, stage="完成", result={"path": str(out_path), "content": content[:500]})
        ws_mgr.broadcast({"type": "task_done", "task": "resume", "ok": True, "message": msg})
    except Exception as e:
        import traceback
        traceback.print_exc()
        _set_state(_resume_state, _resume_lock, done=True, ok=False, message=f"生成失败：{e}", stage="失败")
        ws_mgr.broadcast({"type": "task_done", "task": "resume", "ok": False, "message": str(e)})
    finally:
        _set_state(_resume_state, _resume_lock, running=False, finished_at=_now())


# ---------------------------------------------------------------------------
# 页面路由
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def root():
    return jobs_page()


@app.get("/jobs", response_class=HTMLResponse)
def jobs_page():
    return HTMLResponse(content=DASHBOARD_HTML, headers={
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache",
        "Expires": "0",
    })


FAVICON_SVG = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
               '<text y="26" font-size="26">\U0001F3AF</text></svg>')


@app.get("/favicon.ico")
def favicon():
    return Response(content=FAVICON_SVG, media_type="image/svg+xml",
                    headers={"Cache-Control": "public, max-age=86400"})


@app.get("/config", response_class=HTMLResponse)
def config_page():
    cfg = config.load_config()
    search = cfg.get("search", {})
    monitor = cfg.get("monitor", {})

    def _join(v):
        return "、".join(str(x) for x in v) if isinstance(v, list) else str(v or "")

    html = CONFIG_HTML
    html = html.replace("__V_KW__", _join(search.get("keywords", [])))
    html = html.replace("__V_CITY__", _join(search.get("city", "")))
    html = html.replace("__V_SALMIN__", str(search.get("salary_min", 0)))
    html = html.replace("__V_SALMAX__", str(search.get("salary_max", 0)))
    html = html.replace("__V_VETO__", _join(search.get("veto_words", [])))
    html = html.replace("__V_MODEL__", str(cfg.get("llm", {}).get("model", "deepseek-ai/DeepSeek-V3")))
    html = html.replace("__V_INTERVAL__", str(monitor.get("interval_minutes", 30)))
    on = " selected" if monitor.get("enabled") else ""
    off = " selected" if not monitor.get("enabled") else ""
    html = html.replace("__SEL_MON_ON__", on).replace("__SEL_MON_OFF__", off)
    return html


@app.post("/save")
async def save(
    resume_files: List[UploadFile] = File(None),
    resume_info: str = Form(""),
    keywords: str = Form(""),
    city: str = Form(""),
    salary_min: int = Form(0),
    salary_max: int = Form(0),
    veto_words: str = Form(""),
    api_key: str = Form(""),
    model: str = Form("deepseek-ai/DeepSeek-V3"),
    monitor_enabled: str = Form("false"),
    interval_minutes: int = Form(30),
):
    cfg = config.load_config()

    if resume_files:
        resume_dir = config.DATA_DIR / "resumes"
        resume_dir.mkdir(parents=True, exist_ok=True)
        saved = []
        for f in resume_files:
            if not f or not f.filename:
                continue
            suffix = Path(f.filename).suffix.lower()
            if suffix not in (".docx", ".pdf"):
                return {"ok": False, "message": f"简历 {f.filename} 仅支持 .docx 或 .pdf 格式"}
            dest = resume_dir / f.filename
            dest.write_bytes(await f.read())
            saved.append(str(dest))
        if saved:
            cfg["resume"]["files"] = saved
            cfg["resume"]["file"] = ""
    if resume_info:
        cfg["resume"]["info"] = resume_info

    def _split_list(s: str):
        return [x.strip() for x in re.split(r"[,，、;；]", s) if x.strip()]

    cfg["search"]["keywords"] = _split_list(keywords)
    cfg["search"]["city"] = city.strip()
    cfg["search"]["salary_min"] = salary_min
    cfg["search"]["salary_max"] = salary_max
    cfg["search"]["veto_words"] = _split_list(veto_words)

    if api_key:
        cfg["llm"]["api_key"] = api_key.strip()
    if model:
        cfg["llm"]["model"] = model.strip()

    cfg["monitor"]["enabled"] = monitor_enabled == "true"
    cfg["monitor"]["interval_minutes"] = interval_minutes

    config.save_config(cfg)
    return {"ok": True, "message": "配置已保存！现在可以关闭本页面，回到对话中继续。"}


# ---------------------------------------------------------------------------
# Jobs API
# ---------------------------------------------------------------------------

@app.get("/api/jobs")
def api_jobs():
    return load_jobs()


@app.post("/api/jobs/status")
async def api_jobs_status(request: Request):
    try:
        body = await request.json()
    except Exception:
        return {"ok": False, "message": "请求体不是合法 JSON"}
    url = (body or {}).get("url", "")
    status = (body or {}).get("status", "")
    if not url or not status:
        return {"ok": False, "message": "缺少 url 或 status"}
    if status in STATUS_MAP:
        status = STATUS_MAP[status]
    if status not in STATUSES:
        return {"ok": False, "message": f"非法状态：{status}"}
    jobs = load_jobs()
    for j in jobs:
        if j.get("url") == url:
            j["status"] = status
            j["updated_at"] = _now()
            save_jobs(jobs)
            return {"ok": True, "message": f"已流转为「{status}」"}
    return {"ok": False, "message": "未找到对应岗位"}


@app.post("/api/jobs/update")
async def api_jobs_update(request: Request):
    try:
        body = await request.json()
    except Exception:
        return {"ok": False, "message": "请求体不是合法 JSON"}
    url = (body or {}).get("url", "")
    fields = (body or {}).get("fields", {})
    if not url or not isinstance(fields, dict):
        return {"ok": False, "message": "缺少 url 或 fields"}
    if not fields:
        return {"ok": True, "message": "无字段更新"}
    jobs = load_jobs()
    for j in jobs:
        if j.get("url") == url:
            for k, v in fields.items():
                if v is None:
                    continue
                j[k] = v
            j["status"] = STATUS_MAP.get(j.get("status", ""), j.get("status", "新发现"))
            j["updated_at"] = _now()
            save_jobs(jobs)
            return {"ok": True, "message": "字段已更新"}
    return {"ok": False, "message": "未找到对应岗位"}


@app.post("/api/jobs/add")
async def api_jobs_add(request: Request):
    try:
        body = await request.json()
    except Exception:
        return {"ok": False, "message": "请求体不是合法 JSON"}
    body = body or {}
    title = (body.get("title") or "").strip()
    company = (body.get("company") or "").strip()
    if not title or not company:
        return {"ok": False, "message": "公司名称与岗位名称为必填"}
    url = (body.get("url") or "").strip()
    if not url:
        url = "manual://" + str(len(load_jobs()) + 1) + "-" + title
    jobs = load_jobs()
    if find_job(jobs, url):
        return {"ok": False, "message": "该链接已存在（重复岗位）"}
    new_job = normalize_job({
        "title": title, "company": company,
        "city": body.get("city") or "", "location": body.get("city") or "",
        "salary": body.get("salary") or "", "source": body.get("source") or "手动录入",
        "url": url, "link": url, "description": body.get("description") or "",
        "responsibilities": body.get("description") or "",
        "status": "新发现", "created_at": _now(), "updated_at": _now(),
    })
    jobs.append(new_job)
    save_jobs(jobs)
    return {"ok": True, "message": f"已新增岗位「{title}」", "url": url}


@app.post("/api/jobs/delete")
async def api_jobs_delete(request: Request):
    try:
        body = await request.json()
    except Exception:
        return {"ok": False, "message": "请求体不是合法 JSON"}
    url = (body or {}).get("url", "")
    if not url:
        return {"ok": False, "message": "缺少 url"}
    jobs = load_jobs()
    new_jobs = [j for j in jobs if j.get("url") != url]
    if len(new_jobs) == len(jobs):
        return {"ok": False, "message": "未找到对应岗位"}
    save_jobs(new_jobs)
    return {"ok": True, "message": "岗位已删除"}


@app.post("/api/jobs/followup")
async def api_jobs_followup(request: Request):
    try:
        body = await request.json()
    except Exception:
        return {"ok": False, "message": "请求体不是合法 JSON"}
    url = (body or {}).get("url", "")
    note = (body or {}).get("note", "").strip()
    ftype = (body or {}).get("type", "跟进")
    if not url or not note:
        return {"ok": False, "message": "缺少 url 或 note"}
    jobs = load_jobs()
    for j in jobs:
        if j.get("url") == url:
            if not isinstance(j.get("followups"), list):
                j["followups"] = []
            j["followups"].append({"date": _now(), "type": ftype, "note": note})
            j["follow_up_date"] = _today()
            j["updated_at"] = _now()
            save_jobs(jobs)
            return {"ok": True, "message": "跟进已记录"}
    return {"ok": False, "message": "未找到对应岗位"}


@app.post("/api/jobs/review")
async def api_jobs_review(request: Request):
    try:
        body = await request.json()
    except Exception:
        return {"ok": False, "message": "请求体不是合法 JSON"}
    url = (body or {}).get("url", "")
    feedback = (body or {}).get("feedback", "").strip()
    status = (body or {}).get("status", "")
    if not url or not feedback:
        return {"ok": False, "message": "缺少 url 或 feedback"}
    jobs = load_jobs()
    for j in jobs:
        if j.get("url") == url:
            j["interview_feedback"] = feedback
            j["follow_up_date"] = _today()
            if status and status in STATUSES:
                j["status"] = status
            if not isinstance(j.get("followups"), list):
                j["followups"] = []
            j["followups"].append({"date": _now(), "type": "复盘", "note": feedback})
            j["updated_at"] = _now()
            save_jobs(jobs)
            return {"ok": True, "message": "面试复盘已保存"}
    return {"ok": False, "message": "未找到对应岗位"}


@app.post("/api/jobs/batch")
async def api_jobs_batch(request: Request):
    """批量操作：批量改状态 / 批量改优先级 / 批量删除。"""
    try:
        body = await request.json()
    except Exception:
        return {"ok": False, "message": "请求体不是合法 JSON"}
    urls = (body or {}).get("urls", [])
    action = (body or {}).get("action", "")
    if not urls or not action:
        return {"ok": False, "message": "缺少 urls 或 action"}
    url_set = set(urls)
    jobs = load_jobs()
    if action == "delete":
        jobs = [j for j in jobs if j.get("url") not in url_set]
        save_jobs(jobs)
        return {"ok": True, "message": f"已删除 {len(urls)} 个岗位"}
    elif action == "status":
        status = (body or {}).get("status", "")
        if status in STATUS_MAP:
            status = STATUS_MAP[status]
        if status not in STATUSES:
            return {"ok": False, "message": f"非法状态：{status}"}
        cnt = 0
        for j in jobs:
            if j.get("url") in url_set:
                j["status"] = status
                j["updated_at"] = _now()
                cnt += 1
        save_jobs(jobs)
        return {"ok": True, "message": f"已批量更新 {cnt} 个岗位状态为「{status}」"}
    elif action == "priority":
        pri = (body or {}).get("priority", "C")
        if pri not in ("A", "B", "C"):
            return {"ok": False, "message": "非法优先级"}
        cnt = 0
        for j in jobs:
            if j.get("url") in url_set:
                j["priority"] = pri
                j["updated_at"] = _now()
                cnt += 1
        save_jobs(jobs)
        return {"ok": True, "message": f"已批量更新 {cnt} 个岗位优先级为「{pri}」"}
    return {"ok": False, "message": f"未知操作：{action}"}


@app.post("/api/jobs/compare")
async def api_jobs_compare(request: Request):
    """获取多个岗位的对比数据。"""
    try:
        body = await request.json()
    except Exception:
        return {"ok": False, "message": "请求体不是合法 JSON"}
    urls = (body or {}).get("urls", [])
    if len(urls) < 2:
        return {"ok": False, "message": "请至少选择 2 个岗位"}
    url_set = set(urls)
    jobs = [j for j in load_jobs() if j.get("url") in url_set]
    return {"ok": True, "jobs": jobs}


# ---------------------------------------------------------------------------
# Collect API
# ---------------------------------------------------------------------------

@app.post("/api/collect")
def api_collect():
    with _collect_lock:
        if _collect_state.get("running"):
            return {"ok": True, "running": True, "message": "正在采集中，请稍候"}
        threading.Thread(target=_run_collect_worker, daemon=True).start()
    return {"ok": True, "running": True, "message": "已开始采集（约需 30 秒）"}


@app.get("/api/collect/status")
def api_collect_status():
    with _collect_lock:
        return dict(_collect_state)


# ---------------------------------------------------------------------------
# Login API（BOSS 直聘 扫码登录）
# ---------------------------------------------------------------------------

@app.post("/api/login/qr")
def api_login_qr():
    with _login_lock:
        if _login_state.get("running"):
            return {"ok": True, "running": True, "message": "登录流程进行中，请查看二维码"}
        threading.Thread(target=_run_login_worker, daemon=True).start()
    return {"ok": True, "running": True, "message": "已生成二维码，请用 BOSS 直聘 App 扫码"}


@app.post("/api/login/cancel")
def api_login_cancel():
    with _login_lock:
        if _login_state.get("running"):
            _login_state["phase"] = "cancelled"
            _login_state["message"] = "已取消登录"
    return {"ok": True}


@app.get("/api/login/qr/img")
def api_login_qr_img():
    f = config.DATA_DIR / "login_qr.jpg"
    if f.exists():
        return FileResponse(str(f), media_type="image/jpeg", headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        })
    return {"ok": False, "message": "二维码尚未生成"}


@app.get("/api/login/status")
def api_login_status():
    """返回登录状态；二维码就绪时内嵌 base64 data URI，规避浏览器对单独图片请求的兼容/缓存问题。"""
    with _login_lock:
        st = dict(_login_state)
    if st.get("phase") in ("ready", "scanned", "confirmed") and not st.get("qr_img"):
        f = config.DATA_DIR / "login_qr.jpg"
        if f.exists():
            try:
                b64 = base64.b64encode(f.read_bytes()).decode("ascii")
                st["qr_img"] = "data:image/jpeg;base64," + b64
            except Exception:
                st["qr_img"] = ""
    return st


# ---------------------------------------------------------------------------
# Score API（AI 评分，不接触 Boss直聘）
# ---------------------------------------------------------------------------

@app.post("/api/score")
def api_score():
    with _score_lock:
        if _score_state.get("running"):
            return {"ok": True, "running": True, "message": "正在评分中，请稍候"}
        threading.Thread(target=_run_score_worker, daemon=True).start()
    return {"ok": True, "running": True, "message": "已开始 AI 评分"}


@app.get("/api/score/status")
def api_score_status():
    with _score_lock:
        return dict(_score_state)


# ---------------------------------------------------------------------------
# Greet API（招呼语生成，不接触 Boss直聘）
# ---------------------------------------------------------------------------

@app.post("/api/greet")
def api_greet():
    with _greet_lock:
        if _greet_state.get("running"):
            return {"ok": True, "running": True, "message": "正在生成招呼语，请稍候"}
        threading.Thread(target=_run_greet_worker, daemon=True).start()
    return {"ok": True, "running": True, "message": "已开始生成招呼语"}


@app.get("/api/greet/status")
def api_greet_status():
    with _greet_lock:
        return dict(_greet_state)


@app.post("/api/greet/single")
async def api_greet_single(request: Request):
    """为单个岗位同步生成招呼语（调用硅基流动 API，不接触 Boss直聘）。"""
    try:
        body = await request.json()
    except Exception:
        return {"ok": False, "message": "请求体不是合法 JSON"}
    url = (body or {}).get("url", "")
    if not url:
        return {"ok": False, "message": "缺少 url"}
    jobs = load_jobs()
    job = find_job(jobs, url)
    if not job:
        return {"ok": False, "message": "未找到对应岗位"}
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import greeter
        resume_text = config.load_resume_text()
        client = config.get_llm_client()
        greeting = greeter.generate_greeting(client, job, resume_text)
        job["greeting"] = greeting
        if greeting and not greeting.startswith("（"):
            job["status"] = "待投递"
        job["updated_at"] = _now()
        save_jobs(jobs)
        return {"ok": True, "greeting": greeting, "message": "招呼语已生成"}
    except Exception as e:
        return {"ok": False, "message": f"生成失败：{e}"}


# ---------------------------------------------------------------------------
# Monitor API（HR 回复监听，Playwright 安全读取）
# ---------------------------------------------------------------------------

@app.post("/api/monitor")
def api_monitor():
    with _monitor_lock:
        if _monitor_state.get("running"):
            return {"ok": True, "running": True, "message": "正在监听中，请稍候"}
        threading.Thread(target=_run_monitor_worker, daemon=True).start()
    return {"ok": True, "running": True, "message": "已开始监听 HR 回复"}


@app.get("/api/monitor/status")
def api_monitor_status():
    with _monitor_lock:
        return dict(_monitor_state)


@app.get("/api/messages")
def api_messages():
    """读取本地 messages.json 中的 HR 回复消息。"""
    messages_path = config.DATA_DIR / "messages.json"
    if not messages_path.exists():
        return []
    try:
        return json.loads(messages_path.read_text(encoding="utf-8"))
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Resume API
# ---------------------------------------------------------------------------

@app.get("/api/resume/versions")
def api_resume_versions():
    resume_dir = config.DATA_DIR / "resumes"
    versions = []
    if resume_dir.exists():
        for f in sorted(resume_dir.glob("*.docx")):
            name = f.name
            direction = ""
            parts = name.replace(".docx", "").split("_")
            if len(parts) >= 3:
                direction = parts[1]
            rtype = "原版基准" if name.endswith("_user.docx") else "在投版本"
            size = f.stat().st_size
            versions.append({
                "name": name, "direction": direction, "type": rtype,
                "path": str(f), "size": size, "size_fmt": _fmt_size(size),
                "mtime": datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
            })
    return versions


def _fmt_size(n: int) -> str:
    if n >= 1048576:
        return f"{n / 1048576:.2f} MB"
    if n >= 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n} B"


@app.get("/api/resume/download")
def api_resume_download(path: str = ""):
    resume_dir = (config.DATA_DIR / "resumes").resolve()
    try:
        target = Path(path).resolve()
    except Exception:
        return {"ok": False, "message": "非法路径"}
    if resume_dir not in target.parents or target.suffix.lower() != ".docx":
        return {"ok": False, "message": "仅允许下载 data/resumes 目录内的 docx 简历"}
    if not target.exists() or not target.is_file():
        return {"ok": False, "message": "文件不存在"}
    return FileResponse(str(target), filename=target.name)


@app.post("/api/resume/generate")
async def api_resume_generate(request: Request):
    """触发 AI 简历定制（后台线程，不接触 Boss直聘）。"""
    try:
        body = await request.json()
    except Exception:
        body = {}
    job_url = (body or {}).get("url", "")
    with _resume_lock:
        if _resume_state.get("running"):
            return {"ok": True, "running": True, "message": "正在生成简历，请稍候"}
        threading.Thread(target=_run_resume_worker, args=(job_url,), daemon=True).start()
    return {"ok": True, "running": True, "message": "已开始生成定制简历"}


@app.get("/api/resume/generate/status")
def api_resume_generate_status():
    with _resume_lock:
        return dict(_resume_state)


# ---------------------------------------------------------------------------
# Export API
# ---------------------------------------------------------------------------

@app.get("/api/export")
def api_export(format: str = "xlsx", search: str = "", city: str = "",
               source: str = "", priority: str = "", score: str = ""):
    """导出当前筛选结果为 Excel 或 CSV。"""
    jobs = load_jobs()
    q = search.lower()
    if q:
        jobs = [j for j in jobs if q in (j.get("title", "") + " " + j.get("company", "") + " " + j.get("city", "")).lower()]
    if city:
        jobs = [j for j in jobs if j.get("city") == city]
    if source:
        jobs = [j for j in jobs if j.get("source") == source]
    if priority:
        jobs = [j for j in jobs if j.get("priority") == priority]
    if score:
        try:
            sc = int(score)
            jobs = [j for j in jobs if (j.get("match_score", 0) or 0) >= sc]
        except ValueError:
            pass

    headers = ["岗位", "公司", "城市", "薪资", "匹配度", "优先级", "状态",
               "招聘链接", "核心职责", "必备条件", "加分项", "匹配点",
               "能力缺口", "招呼语", "下一步", "截止日期", "面试反馈"]

    if format == "csv":
        import csv
        import io
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(headers)
        for j in jobs:
            writer.writerow([
                j.get("title"), j.get("company"), j.get("city"), j.get("salary"),
                j.get("match_score", 0), j.get("priority"), j.get("status"),
                j.get("url", ""), j.get("responsibilities", ""), j.get("requirements", ""),
                j.get("plus_points", ""), j.get("match_points", ""), j.get("skill_gaps", ""),
                j.get("greeting", ""), j.get("next_action", ""),
                j.get("action_deadline", ""), j.get("interview_feedback", ""),
            ])
        return StreamingResponse(
            iter([output.getvalue()]),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=jobs_export.csv"},
        )

    try:
        import openpyxl
    except ImportError:
        return {"ok": False, "message": "缺少 openpyxl，请执行 pip install openpyxl"}
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "岗位看板"
    ws.append(headers)
    for j in jobs:
        ws.append([
            j.get("title"), j.get("company"), j.get("city"), j.get("salary"),
            j.get("match_score", 0), j.get("priority"), j.get("status"),
            j.get("url", ""), j.get("responsibilities", ""), j.get("requirements", ""),
            j.get("plus_points", ""), j.get("match_points", ""), j.get("skill_gaps", ""),
            j.get("greeting", ""), j.get("next_action", ""),
            j.get("action_deadline", ""), j.get("interview_feedback", ""),
        ])
    for cell in ws[1]:
        cell.font = cell.font.copy(bold=True)
    out = config.EXPORT_DIR / f"求职看板_{_today()}.xlsx"
    wb.save(str(out))
    return FileResponse(str(out), filename=out.name)


# ---------------------------------------------------------------------------
# Status API
# ---------------------------------------------------------------------------

@app.get("/api/status")
def api_status():
    cfg = config.load_config()
    jobs = load_jobs()
    by_status: Dict[str, int] = {s: 0 for s in STATUSES}
    for j in jobs:
        by_status[j["status"]] = by_status.get(j["status"], 0) + 1
    return {
        "total_jobs": len(jobs),
        "statuses": by_status,
        "config": {
            "keywords": cfg.get("search", {}).get("keywords", []),
            "city": cfg.get("search", {}).get("city", ""),
        },
        "jobhunter_dir": str(Path(__file__).resolve().parent.parent),
    }


# ---------------------------------------------------------------------------
# WebSocket
# ---------------------------------------------------------------------------

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws_mgr.connect(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        ws_mgr.disconnect(ws)
    except Exception:
        ws_mgr.disconnect(ws)


# ---------------------------------------------------------------------------
# HTML 模板
# ---------------------------------------------------------------------------

CONFIG_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>JobHunter 配置面板</title>
<style>
:root{--canvas:#f6f5f4;--card:#fff;--line:#e9e9e7;--ink:#37352f;--ink-2:#787774;--ink-3:#9b9a97;--blue:#0075de;--blue-soft:#e6f3fe;--green:#0f7b6c;--coral:#c43e2d;}
[data-theme="dark"]{--canvas:#1f1f1d;--card:#2a2a28;--line:#3a3a38;--ink:#e8e6e3;--ink-2:#a0a09e;--ink-3:#6b6a67;--blue:#5290ff;--blue-soft:#1a3a5c;--green:#4db6a9;--coral:#e85d4c;}
*{margin:0;padding:0;box-sizing:border-box;}
body{font-family:"Inter","Microsoft YaHei",sans-serif;min-height:100vh;background:var(--canvas);color:var(--ink);padding:40px 20px;transition:background .3s,color .3s;}
.wrap{max-width:760px;margin:0 auto;}
header{text-align:center;margin-bottom:32px;}
header .logo{display:inline-flex;align-items:center;gap:12px;background:var(--card);border:1px solid var(--line);border-radius:12px;padding:10px 22px;}
header .logo .icon{font-size:24px;}
header h1{font-size:22px;font-weight:700;}
header p{color:var(--ink-2);font-size:13px;margin-top:10px;}
.card{background:var(--card);border-radius:12px;padding:24px 26px;margin-bottom:16px;border:1px solid var(--line);transition:background .3s,border-color .3s;}
.card h3{display:flex;align-items:center;gap:10px;font-size:16px;font-weight:600;margin-bottom:16px;padding-bottom:10px;border-bottom:1px solid var(--line);}
.card h3 .step{display:inline-flex;align-items:center;justify-content:center;width:24px;height:24px;border-radius:50%;background:var(--blue);color:#fff;font-size:13px;font-weight:700;}
label{display:block;margin:12px 0 6px;font-weight:600;font-size:14px;}
input[type=text],input[type=number],input[type=password],textarea,select{width:100%;padding:9px 12px;border:1px solid var(--line);border-radius:8px;font-size:14px;color:var(--ink);background:var(--card);transition:border-color .2s,box-shadow .2s;font-family:inherit;}
input:focus,textarea:focus,select:focus{outline:none;border-color:var(--blue);box-shadow:0 0 0 3px rgba(0,117,222,.12);}
textarea{min-height:80px;resize:vertical;}
.hint{color:var(--ink-3);font-size:12px;margin-top:6px;}
.row{display:flex;gap:12px;}
.row>div{flex:1;}
.file-zone{border:2px dashed #d3d1cb;border-radius:10px;padding:18px;text-align:center;background:rgba(255,255,255,.4);cursor:pointer;transition:border-color .2s,background .2s;}
.file-zone:hover{border-color:var(--blue);background:var(--blue-soft);}
.file-zone .fz-icon{font-size:28px;display:block;margin-bottom:6px;}
.file-zone .fz-text{color:var(--ink-2);font-size:13px;}
.file-zone .fz-text b{color:var(--blue);}
.file-zone input[type=file]{display:none;}
#fileName{margin-top:8px;font-size:12px;color:var(--blue);font-weight:600;}
.btn-row{text-align:center;margin-top:8px;}
button[type=submit]{background:var(--blue);color:#fff;border:none;padding:11px 40px;border-radius:10px;font-size:15px;font-weight:600;cursor:pointer;transition:background .2s;}
button[type=submit]:hover{background:#0a64c4;}
#msg{margin-top:14px;font-weight:600;font-size:14px;text-align:center;padding:10px;border-radius:8px;display:none;}
#msg.ok{color:var(--green);background:rgba(15,123,108,.1);display:block;}
#msg.err{color:var(--coral);background:rgba(196,62,45,.1);display:block;}
footer{text-align:center;color:var(--ink-3);font-size:12px;margin-top:24px;}
.theme-toggle{position:fixed;top:20px;right:20px;width:40px;height:40px;border-radius:50%;border:1px solid var(--line);background:var(--card);color:var(--ink);cursor:pointer;font-size:18px;transition:all .2s;}
.theme-toggle:hover{border-color:var(--blue);color:var(--blue);}
</style>
</head>
<body>
<script>
if(localStorage.getItem('jh_theme')==='dark')document.body.setAttribute('data-theme','dark');
</script>
<button class="theme-toggle" onclick="const c=document.body.getAttribute('data-theme');const n=c==='dark'?'light':'dark';document.body.setAttribute('data-theme',n);localStorage.setItem('jh_theme',n);">🎨</button>
<div class="wrap">
<header>
<div style="display:flex;justify-content:center;align-items:center;gap:16px">
<div class="logo"><span class="icon">🎯</span><h1>JobHunter 配置面板</h1></div>
<a href="/jobs" style="font-size:13px;color:var(--blue);text-decoration:none;font-weight:600;padding:7px 14px;border:1px solid var(--blue);border-radius:8px;">← 返回看板</a>
</div>
<p>表单已预填当前配置，修改后点击保存即可；所有投递均需你确认后发送</p>
</header>
<form id="cfgForm" enctype="multipart/form-data">
<div class="card">
<h3><span class="step">1</span>简历</h3>
<label>上传简历（.docx / .pdf 格式，可多选）</label>
<div class="file-zone" id="fileZone">
<span class="fz-icon">📄</span>
<div class="fz-text">点击选择文件，支持 <b>.docx</b> / <b>.pdf</b>，可一次选择多份</div>
<input type="file" name="resume_files" id="resumeFile" accept=".docx,.pdf" multiple>
</div>
<div id="fileName"></div>
<div class="hint">或直接在下方填写简历信息库（结构化文本）</div>
<label>简历信息库</label>
<textarea name="resume_info" placeholder="姓名 / 技能 / 项目经历 / 教育背景..."></textarea>
</div>
<div class="card">
<h3><span class="step">2</span>搜索配置</h3>
<label>搜索关键词（多个用逗号分隔）</label>
<input type="text" name="keywords" value="__V_KW__" placeholder="如：Java后端开发, AI应用开发">
<label>目标城市</label>
<input type="text" name="city" value="__V_CITY__" placeholder="如：厦门、福州、泉州">
<label>期望薪资范围（K）</label>
<div class="row">
<div><input type="number" name="salary_min" value="__V_SALMIN__" placeholder="最低"></div>
<div><input type="number" name="salary_max" value="__V_SALMAX__" placeholder="最高"></div>
</div>
<label>一票否决词（多个用逗号分隔）</label>
<input type="text" name="veto_words" value="__V_VETO__" placeholder="如：外包, 996, 驻场">
</div>
<div class="card">
<h3><span class="step">3</span>AI 模型（硅基流动）</h3>
<label>API Key</label>
<input type="password" name="api_key" placeholder="sk-...">
<label>模型</label>
<input type="text" name="model" value="__V_MODEL__">
</div>
<div class="card">
<h3><span class="step">4</span>回复监听</h3>
<label>定时轮询</label>
<select name="monitor_enabled">
<option value="false"__SEL_MON_OFF__>关闭</option>
<option value="true"__SEL_MON_ON__>开启</option>
</select>
<label>轮询间隔（分钟）</label>
<input type="number" name="interval_minutes" value="__V_INTERVAL__">
</div>
<div class="btn-row">
<button type="submit">保存配置</button>
</div>
<div id="msg"></div>
</form>
<footer>JobHunter · 数据仅保存在本地</footer>
</div>
<script>
const fileZone=document.getElementById('fileZone');
const resumeFile=document.getElementById('resumeFile');
fileZone.addEventListener('click',()=>resumeFile.click());
resumeFile.addEventListener('change',()=>{
const files=resumeFile.files;
document.getElementById('fileName').textContent=files.length?'已选择 '+files.length+' 份：'+Array.from(files).map(f=>f.name).join('、'):'';
});
document.getElementById('cfgForm').addEventListener('submit',async(e)=>{
e.preventDefault();
const fd=new FormData(e.target);
const resp=await fetch('/save',{method:'POST',body:fd});
const data=await resp.json();
const msg=document.getElementById('msg');
msg.textContent=data.message;
msg.className=data.ok?'ok':'err';
});
</script>
</body>
</html>
"""


DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">
<meta http-equiv="Pragma" content="no-cache">
<meta http-equiv="Expires" content="0">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>JobHunter 七步法求职看板</title>
<style>
:root{--canvas:#f6f5f4;--card:#fff;--line:#e9e9e7;--line-soft:#f0efed;--ink:#37352f;--ink-2:#787774;--ink-3:#9b9a97;--blue:#0075de;--blue-soft:#e6f3fe;--blue-border:#b3dbf7;--green:#0f7b6c;--green-soft:#e8f3f0;--coral:#c43e2d;--coral-soft:#fce8e6;--amber:#cb9d06;--amber-soft:#fef5e3;--shadow:0 1px 3px rgba(0,0,0,.08),0 1px 2px rgba(0,0,0,.04);--shadow-lg:0 4px 12px rgba(0,0,0,.12);--radius:8px;--radius-lg:12px;}
[data-theme="dark"]{--canvas:#1f1f1d;--card:#2a2a28;--line:#3a3a38;--line-soft:#333331;--ink:#e8e6e3;--ink-2:#a0a09e;--ink-3:#6b6a67;--blue:#5290ff;--blue-soft:#1a3a5c;--blue-border:#2a4a6c;--green:#4db6a9;--green-soft:#1a3a36;--coral:#e85d4c;--coral-soft:#3a1a1a;--amber:#e8c44e;--amber-soft:#3a2f1a;--shadow:0 1px 3px rgba(0,0,0,.3);--shadow-lg:0 4px 12px rgba(0,0,0,.4);}
*{margin:0;padding:0;box-sizing:border-box;}
body{font-family:"Inter","Microsoft YaHei",sans-serif;background:var(--canvas);color:var(--ink);overflow-x:auto;transition:background .3s,color .3s;}
a{color:var(--blue);text-decoration:none;}
.topbar{position:sticky;top:0;z-index:100;display:flex;align-items:center;gap:8px;padding:10px 20px;background:var(--card);border-bottom:1px solid var(--line);box-shadow:var(--shadow);}
.brand{font-size:16px;font-weight:700;white-space:nowrap;}
.brand .logo{font-size:20px;}
.nav{display:flex;gap:2px;margin-left:16px;}
.nav a{padding:6px 12px;border-radius:6px;font-size:13px;font-weight:500;color:var(--ink-2);cursor:pointer;transition:all .15s;}
.nav a:hover{background:var(--line-soft);color:var(--ink);}
.nav a.active{background:var(--blue-soft);color:var(--blue);font-weight:600;}
.spacer{flex:1;}
.btn{display:inline-flex;align-items:center;gap:4px;padding:6px 12px;border:1px solid var(--line);border-radius:6px;background:var(--card);color:var(--ink);font-size:13px;font-weight:500;cursor:pointer;transition:all .15s;white-space:nowrap;}
.btn:hover{border-color:var(--blue);color:var(--blue);}
.btn.primary{background:var(--blue);color:#fff;border-color:var(--blue);}
.btn.primary:hover{background:#0a64c4;}
.btn.danger{color:var(--coral);border-color:var(--coral);}
.btn.danger:hover{background:var(--coral);color:#fff;}
.btn:disabled{opacity:.5;cursor:not-allowed;}
.btn .spin{display:inline-block;width:14px;height:14px;border:2px solid var(--line);border-top-color:var(--blue);border-radius:50%;animation:spin .6s linear infinite;}
@keyframes spin{to{transform:rotate(360deg);}}
.task-bar{display:none;align-items:center;gap:10px;padding:8px 20px;background:var(--card);border-bottom:1px solid var(--line);}
.task-bar.show{display:flex;}
.task-track{flex:1;height:6px;background:var(--line-soft);border-radius:3px;overflow:hidden;}
.task-fill{height:100%;background:var(--blue);border-radius:3px;transition:width .3s;}
.task-stage{font-size:12px;color:var(--ink-2);white-space:nowrap;}
.statsbar{display:flex;align-items:center;gap:8px;padding:10px 20px;background:var(--card);border-bottom:1px solid var(--line);overflow-x:auto;}
.stat-chip{display:inline-flex;align-items:center;gap:4px;padding:4px 10px;border-radius:16px;font-size:12px;font-weight:600;white-space:nowrap;}
.stat-chip b{font-size:14px;}
.stat-chip.s-total{background:var(--blue-soft);color:var(--blue);}
.stat-chip.s-A{background:var(--coral-soft);color:var(--coral);}
.stat-chip.s-score{background:var(--green-soft);color:var(--green);}
.stat-chip.s-interview{background:var(--amber-soft);color:var(--amber);}
.stat-chip.s-offer{background:var(--green-soft);color:var(--green);}
.stat-charts{display:flex;gap:16px;margin-left:auto;align-items:center;}
.stat-chart-box{display:flex;flex-direction:column;align-items:center;gap:2px;}
.stat-chart-box .label{font-size:10px;color:var(--ink-3);font-weight:600;}
.filterbar{display:flex;align-items:center;gap:8px;padding:8px 20px;background:var(--card);border-bottom:1px solid var(--line);flex-wrap:wrap;}
.filterbar input,.filterbar select{padding:5px 10px;border:1px solid var(--line);border-radius:6px;font-size:13px;color:var(--ink);background:var(--card);transition:border-color .2s;}
.filterbar input:focus,.filterbar select:focus{outline:none;border-color:var(--blue);box-shadow:0 0 0 2px rgba(0,117,222,.1);}
.filterbar .count{font-size:12px;color:var(--ink-3);margin-left:auto;}
.board-wrap{overflow-x:auto;padding:12px 20px;}
.board{display:flex;gap:12px;min-width:max-content;}
.column{width:280px;flex-shrink:0;background:var(--card);border:1px solid var(--line);border-radius:var(--radius-lg);box-shadow:var(--shadow);display:flex;flex-direction:column;max-height:calc(100vh - 200px);}
.col-head{display:flex;align-items:center;justify-content:space-between;padding:10px 12px;border-bottom:1px solid var(--line);font-size:13px;font-weight:600;}
.col-head .col-name{display:flex;align-items:center;gap:6px;}
.col-head .col-dot{width:8px;height:8px;border-radius:50%;}
.col-head .col-count{font-size:11px;color:var(--ink-3);font-weight:400;}
.col-body{flex:1;overflow-y:auto;padding:8px;display:flex;flex-direction:column;gap:6px;min-height:40px;transition:background .2s,border-radius .2s;}
.col-body.drop-target{background:var(--blue-soft);border-radius:var(--radius);}
.col-body.drop-target::after{content:"放开以流转到此栏";display:block;text-align:center;font-size:12px;color:var(--blue);padding:8px;}
.job-card{position:relative;background:var(--card);border:1px solid var(--line);border-radius:var(--radius);padding:8px 10px;cursor:grab;box-shadow:var(--shadow);transition:box-shadow .2s,border-color .2s,transform .15s;animation:cardIn .3s ease;}
@keyframes cardIn{from{opacity:0;transform:translateY(8px);}to{opacity:1;transform:translateY(0);}}
.job-card:hover{box-shadow:var(--shadow-lg);border-color:var(--blue-border);}
.job-card.dragging{opacity:.4;cursor:grabbing;}
.jc-head{display:flex;align-items:center;justify-content:space-between;margin-bottom:4px;}
.jc-pri{font-size:10px;font-weight:700;padding:1px 5px;border-radius:4px;}
.jc-pri.pri-A{background:var(--coral-soft);color:var(--coral);}
.jc-pri.pri-B{background:var(--amber-soft);color:var(--amber);}
.jc-pri.pri-C{background:var(--line-soft);color:var(--ink-3);}
.jc-score{font-size:14px;font-weight:700;}
.jc-title{font-size:13px;font-weight:600;line-height:1.3;margin-bottom:2px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.jc-meta{display:flex;gap:6px;font-size:11px;color:var(--ink-2);overflow:hidden;}
.jc-meta span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.jc-detail{max-height:0;overflow:hidden;transition:max-height .3s ease;}
.job-card:hover .jc-detail{max-height:100px;}
.jc-mp{font-size:10px;color:var(--green);margin-top:4px;line-height:1.3;overflow:hidden;text-overflow:ellipsis;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;}
.jc-gap{font-size:10px;color:var(--coral);margin-top:2px;line-height:1.3;overflow:hidden;text-overflow:ellipsis;display:-webkit-box;-webkit-line-clamp:1;-webkit-box-orient:vertical;}
.jc-deadline{font-size:10px;color:var(--coral);margin-top:3px;font-weight:600;}
.jc-greeting{font-size:10px;color:var(--blue);margin-top:3px;font-weight:600;}
.jc-check{position:absolute;top:8px;right:8px;width:16px;height:16px;cursor:pointer;}
body.batch-mode .job-card{cursor:default;}
body.batch-mode .job-card:hover .jc-detail{max-height:0;}
.skeleton-card{background:var(--line-soft);border-radius:var(--radius);padding:10px;height:80px;animation:shimmer 1.5s infinite;}
@keyframes shimmer{0%{background-position:200% 0;}100%{background-position:-200% 0;}}
.calendar-wrap{padding:20px;}
.calendar{max-width:1000px;margin:0 auto;background:var(--card);border:1px solid var(--line);border-radius:var(--radius-lg);overflow:hidden;}
.cal-header{display:grid;grid-template-columns:repeat(7,1fr);border-bottom:1px solid var(--line);}
.cal-header span{text-align:center;padding:8px;font-size:12px;font-weight:600;color:var(--ink-2);}
.cal-body{display:grid;grid-template-columns:repeat(7,1fr);}
.cal-day{text-align:center;padding:8px;min-height:64px;border:1px solid var(--line);font-size:13px;position:relative;cursor:pointer;transition:background .15s;}
.cal-day:hover{background:var(--blue-soft);}
.cal-day.empty{background:var(--line-soft);}
.cal-day.today{background:var(--blue-soft);font-weight:700;color:var(--blue);}
.cal-day .cal-dot{display:inline-block;width:6px;height:6px;border-radius:50%;margin-left:2px;}
.cal-day .cal-dot.deadline{background:var(--coral);}
.cal-day .cal-dot.interview{background:var(--amber);}
.cal-day .cal-dot.followup{background:var(--green);}
.cal-day .cal-events{font-size:9px;color:var(--ink-3);margin-top:2px;line-height:1.2;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.compare-wrap{padding:20px;}
.compare-table{width:100%;border-collapse:collapse;background:var(--card);border-radius:var(--radius-lg);overflow:hidden;box-shadow:var(--shadow);}
.compare-table th,.compare-table td{padding:8px 12px;border:1px solid var(--line);font-size:13px;text-align:left;}
.compare-table th{background:var(--blue-soft);font-weight:600;color:var(--blue);}
.compare-table .ck{background:var(--line-soft);font-weight:600;color:var(--ink-2);width:120px;white-space:nowrap;}
.resume-wrap{padding:20px;max-width:900px;margin:0 auto;}
.resume-item{display:flex;align-items:center;justify-content:space-between;padding:12px 16px;background:var(--card);border:1px solid var(--line);border-radius:var(--radius);margin-bottom:8px;box-shadow:var(--shadow);}
.resume-item .ri-info{display:flex;flex-direction:column;gap:2px;}
.resume-item .ri-name{font-size:14px;font-weight:600;}
.resume-item .ri-meta{font-size:12px;color:var(--ink-3);}
.messages-wrap{padding:20px;max-width:900px;margin:0 auto;}
.msg-item{display:flex;align-items:center;gap:10px;padding:10px 14px;background:var(--card);border:1px solid var(--line);border-radius:var(--radius);margin-bottom:6px;box-shadow:var(--shadow);}
.msg-item .mi-company{font-size:13px;font-weight:600;min-width:100px;}
.msg-item .mi-text{font-size:12px;color:var(--ink-2);flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.msg-item .mi-time{font-size:11px;color:var(--ink-3);white-space:nowrap;}
.empty-state{text-align:center;padding:40px;color:var(--ink-3);font-size:14px;}
.batch-toolbar{position:fixed;bottom:0;left:0;right:0;display:none;align-items:center;justify-content:center;gap:10px;padding:10px 20px;background:var(--card);border-top:1px solid var(--line);box-shadow:0 -2px 8px rgba(0,0,0,.1);z-index:200;}
.batch-toolbar.show{display:flex;}
.batch-toolbar .bt-count{font-size:13px;font-weight:600;color:var(--ink-2);}
.modal-overlay{position:fixed;inset:0;background:rgba(0,0,0,.4);display:none;z-index:1000;align-items:flex-start;justify-content:center;padding:30px 16px;overflow-y:auto;}
.modal-overlay.show{display:flex;}
.modal{background:var(--card);border-radius:var(--radius-lg);box-shadow:var(--shadow-lg);width:100%;max-width:680px;margin:auto;animation:modalIn .2s ease;}
@keyframes modalIn{from{opacity:0;transform:scale(.96);}to{opacity:1;transform:scale(1);}}
.modal-head{display:flex;align-items:center;justify-content:space-between;padding:16px 20px;border-bottom:1px solid var(--line);}
.modal-head h2{font-size:16px;font-weight:700;}
.modal-close{font-size:22px;cursor:pointer;color:var(--ink-3);background:none;border:none;line-height:1;padding:4px;transition:color .15s;}
.modal-close:hover{color:var(--coral);}
.modal-body{padding:16px 20px;max-height:calc(100vh - 140px);overflow-y:auto;}
.m-sec{margin-bottom:14px;padding-bottom:14px;border-bottom:1px solid var(--line-soft);}
.m-sec:last-child{border-bottom:none;margin-bottom:0;padding-bottom:0;}
.m-sec h4{font-size:13px;font-weight:600;color:var(--ink-2);margin-bottom:8px;display:flex;align-items:center;gap:4px;}
.m-sec .kv{display:grid;grid-template-columns:80px 1fr;gap:6px 10px;font-size:13px;}
.m-sec .kv .k{color:var(--ink-3);font-weight:500;}
.m-sec .kv .v{color:var(--ink);word-break:break-all;}
.m-sec textarea{width:100%;min-height:80px;padding:8px 10px;border:1px solid var(--line);border-radius:6px;font-size:13px;color:var(--ink);background:var(--card);resize:vertical;font-family:inherit;line-height:1.5;}
.m-sec textarea:focus{outline:none;border-color:var(--blue);box-shadow:0 0 0 2px rgba(0,117,222,.1);}
.edit-row{display:flex;gap:8px;margin-top:6px;}
.timeline{margin-top:8px;}
.timeline-item{padding:6px 10px;background:var(--line-soft);border-radius:6px;margin-bottom:4px;font-size:12px;}
.timeline-item .ti-date{color:var(--ink-3);font-size:11px;}
.timeline-item .ti-type{font-weight:600;color:var(--blue);margin:0 4px;}
.modal-foot{display:flex;justify-content:flex-end;gap:8px;padding:12px 20px;border-top:1px solid var(--line);}
.score-bar{display:inline-flex;align-items:center;gap:2px;}
.score-bar .sb-track{width:50px;height:6px;background:var(--line-soft);border-radius:3px;overflow:hidden;}
.score-bar .sb-fill{height:100%;border-radius:3px;}
.toast{position:fixed;bottom:60px;left:50%;transform:translateX(-50%);background:var(--ink);color:var(--card);padding:10px 20px;border-radius:8px;font-size:13px;z-index:2000;opacity:0;transition:opacity .2s,bottom .2s;pointer-events:none;}
.toast.show{opacity:1;bottom:80px;}
.form-row{display:flex;gap:10px;}
.form-row>div{flex:1;}
.form-row input,.form-row select,.form-row textarea{width:100%;padding:8px 10px;border:1px solid var(--line);border-radius:6px;font-size:13px;color:var(--ink);background:var(--card);font-family:inherit;}
.form-row input:focus,.form-row select:focus,.form-row textarea:focus{outline:none;border-color:var(--blue);}
.field{margin-bottom:10px;}
.field label{display:block;font-size:12px;font-weight:600;color:var(--ink-2);margin-bottom:4px;}
.add-modal{max-width:520px;}
@media(max-width:768px){.nav{display:none;}.board{padding-bottom:60px;}.statsbar{flex-wrap:wrap;}.stat-charts{display:none;}}
</style>
</head>
<body>
<div class="topbar">
<span class="brand"><span class="logo">🎯</span> JobHunter</span>
<div class="nav">
<a class="active" data-view="board">看板</a>
<a data-view="calendar">日历</a>
<a data-view="compare">对比</a>
<a data-view="resume">简历库</a>
<a data-view="messages">消息</a>
<a href="/config">配置</a>
</div>
<div class="spacer"></div>
<button class="btn" id="btnLogin">🔐 登录</button>
<button class="btn" id="btnScore">📊 评分</button>
<button class="btn" id="btnGreet">💬 招呼语</button>
<button class="btn" id="btnMonitor">🔔 监听</button>
<button class="btn" id="btnCollect">📡 采集</button>
<button class="btn" id="btnExport">📥 导出</button>
<button class="btn" id="btnBatch">☑ 批量</button>
<button class="btn primary" id="btnAdd">＋ 新增</button>
<button class="btn" id="btnDark">🌙</button>
</div>
<div class="task-bar" id="taskBar">
<div class="task-track"><div class="task-fill" id="taskFill" style="width:0"></div></div>
<span class="task-stage" id="taskStage">准备中…</span>
</div>
<div class="statsbar" id="statsbar"></div>
<div class="filterbar" id="filterbar">
<input type="text" id="fSearch" placeholder="搜索岗位/公司/城市…" style="width:200px">
<select id="fCity"><option value="">全部城市</option></select>
<select id="fSource"><option value="">全部来源</option></select>
<select id="fPriority"><option value="">全部优先级</option><option value="A">A</option><option value="B">B</option><option value="C">C</option></select>
<select id="fScore"><option value="">全部匹配度</option><option value="80">≥80</option><option value="60">≥60</option><option value="40">≥40</option></select>
<span class="count" id="fCount"></span>
</div>
<div id="viewContainer">
<div class="board-wrap" id="boardView"><div class="board" id="board"></div></div>
<div class="calendar-wrap" id="calendarView" style="display:none"><div class="calendar" id="calendar"></div></div>
<div class="compare-wrap" id="compareView" style="display:none"><div class="empty-state" id="compareEmpty">在看板上勾选岗位后点击「对比」查看</div><div id="compareContent"></div></div>
<div class="resume-wrap" id="resumeView" style="display:none"><div id="resumeContent"></div></div>
<div class="messages-wrap" id="messagesView" style="display:none"><div id="messagesContent"></div></div>
</div>
<div class="batch-toolbar" id="batchToolbar">
<span class="bt-count" id="btCount">已选 0 项</span>
<button class="btn" onclick="batchSetStatus()">改状态</button>
<button class="btn" onclick="batchSetPriority()">改优先级</button>
<button class="btn" onclick="openCompareFromBatch()">对比选中</button>
<button class="btn danger" onclick="batchDelete()">删除</button>
<button class="btn" onclick="toggleBatch()">退出批量</button>
</div>
<div class="modal-overlay" id="detailOverlay">
<div class="modal" id="detailModal">
<div class="modal-head">
<h2 id="dmTitle">岗位详情</h2>
<button class="modal-close" onclick="closeDetail()">&times;</button>
</div>
<div class="modal-body" id="dmBody"></div>
<div class="modal-foot">
<button class="btn" id="dmGenResume">📝 AI 简历</button>
<button class="btn primary" onclick="saveDetail()">保存修改</button>
</div>
</div>
</div>
<div class="modal-overlay" id="addOverlay">
<div class="modal add-modal">
<div class="modal-head">
<h2>新增岗位</h2>
<button class="modal-close" onclick="closeAdd()">&times;</button>
</div>
<div class="modal-body">
<div class="field"><label>岗位名称 *</label><input id="amTitle"></div>
<div class="field"><label>公司名称 *</label><input id="amCompany"></div>
<div class="form-row">
<div class="field"><label>城市</label><input id="amCity"></div>
<div class="field"><label>薪资</label><input id="amSalary"></div>
</div>
<div class="field"><label>招聘链接</label><input id="amUrl" placeholder="可留空，自动生成"></div>
<div class="field"><label>岗位描述</label><textarea id="amDesc" rows="3"></textarea></div>
</div>
<div class="modal-foot">
<button class="btn" onclick="closeAdd()">取消</button>
<button class="btn primary" onclick="submitAdd()">确认新增</button>
</div>
</div>
</div>
<div class="modal-overlay" id="collectOverlay">
<div class="modal add-modal">
<div class="modal-head">
<h2>采集结果</h2>
<button class="modal-close" onclick="closeCollect()">&times;</button>
</div>
<div class="modal-body">
<div class="field"><label>状态</label><div id="cmStatus" style="font-size:14px;font-weight:600;">—</div></div>
<div class="field" id="cmErrWrap" style="display:none;">
<label>报错类型</label>
<div id="cmErrType" style="font-size:13px;color:var(--coral);font-weight:600;word-break:break-all;">—</div>
<label style="margin-top:10px;">报错内容</label>
<div id="cmErrMsg" style="font-size:13px;color:var(--ink-2);word-break:break-all;white-space:pre-wrap;">—</div>
</div>
</div>
<div class="modal-foot">
<button class="btn" id="cmLoginBtn" style="display:none;">🔐 扫码登录</button>
<button class="btn primary" onclick="closeCollect()">知道了</button>
</div>
</div>
</div>
<div class="modal-overlay" id="loginOverlay">
<div class="modal add-modal" style="max-width:420px;text-align:center;">
<div class="modal-head">
<h2>BOSS 直聘 扫码登录</h2>
<button class="modal-close" onclick="closeLogin()">&times;</button>
</div>
<div class="modal-body">
<div style="font-size:12px;color:var(--ink-3);margin-bottom:10px;">请用 BOSS 直聘 App 扫描下方二维码，并在手机上确认登录</div>
<img id="loginQrImg" src="" alt="二维码加载中…" style="width:240px;height:240px;border:1px solid var(--line);border-radius:8px;object-fit:contain;background:#fff;display:none;">
<div id="loginStatus" style="margin-top:12px;font-size:13px;color:var(--ink-2);min-height:18px;word-break:break-all;">正在生成二维码…</div>
</div>
<div class="modal-foot" style="justify-content:center;">
<button class="btn" id="btnLoginRegen" style="display:none;">重新生成二维码</button>
<button class="btn primary" onclick="closeLogin()">关闭</button>
</div>
</div>
</div>
<div class="toast" id="toast"></div>
<script>
// ===== State =====
let JOBS=[],MESSAGES=[],currentView='board',batchMode=false;
let selectedUrls=new Set(),currentDetailUrl='',detailModified={};
const STATUS_COLORS={'新发现':'#9b9a97','待评估':'#0075de','简历待优化':'#cb9d06','待投递':'#0f7b6c','已投递':'#6c5ce7','笔试测评':'#e17055','面试中':'#e84393','等待结果':'#fdcb6e','Offer':'#00b894','暂不考虑已结束':'#636e72','已停招':'#555555'};
const STATUSES=['新发现','待评估','简历待优化','待投递','已投递','笔试测评','面试中','等待结果','Offer','暂不考虑已结束','已停招'];

// ===== Utilities =====
const $=id=>document.getElementById(id);
const esc=s=>String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
const fmtDate=d=>{if(!d)return'';try{return d.slice(0,10)}catch{return''}};
function scoreColor(s){s=parseInt(s)||0;return s>=80?'var(--green)':s>=60?'var(--blue)':s>=40?'var(--amber)':'var(--coral)';}
function toast(msg,ms){const t=$('toast');t.textContent=msg;t.classList.add('show');setTimeout(()=>t.classList.remove('show'),ms||2500);}
function showProgress(show,pct,stage){const bar=$('taskBar');if(!show){bar.classList.remove('show');return;}bar.classList.add('show');if(pct!=null)$('taskFill').style.width=pct+'%';if(stage)$('taskStage').textContent=stage;}

// ===== WebSocket =====
function initWS(){
const proto=location.protocol==='https:'?'wss':'ws';
let ws;
try{ws=new WebSocket(proto+'://'+location.host+'/ws');}catch(e){return;}
ws.onmessage=e=>{try{const d=JSON.parse(e.data);const tn={collect:'采集',score:'评分',greet:'招呼语',monitor:'监听',resume:'简历'}[d.task]||d.task;if(d.type==='task_done'){toast(tn+(d.ok===false?'失败':'完成'));reload();showProgress(false);}else if(d.type==='progress'){showProgress(true,d.percent,d.stage);}}catch{}};
ws.onclose=()=>setTimeout(initWS,3000);
}

// ===== Theme =====
function initTheme(){const t=localStorage.getItem('jh_theme');if(t==='dark')document.body.setAttribute('data-theme','dark');}
function toggleDark(){const c=document.body.getAttribute('data-theme');const n=c==='dark'?'light':'dark';document.body.setAttribute('data-theme',n);localStorage.setItem('jh_theme',n);$('btnDark').textContent=n==='dark'?'☀️':'🌙';}

// ===== Navigation =====
function switchView(view){
currentView=view;
document.querySelectorAll('.nav a[data-view]').forEach(a=>a.classList.toggle('active',a.dataset.view===view));
['board','calendar','compare','resume','messages'].forEach(v=>{$((v)+'View').style.display=v===view?'':'none';});
if(view==='calendar')renderCalendar();
if(view==='resume')renderResume();
if(view==='messages')renderMessages();
if(view==='compare')renderCompareView();
}

// ===== Data Loading =====
async function reload(){
try{
$('board').innerHTML='<div style="padding:40px;text-align:center;color:#888;font-size:14px">正在加载数据...</div>';
const r=await fetch('/api/jobs');
if(!r.ok){throw new Error('API返回错误: '+r.status+' '+r.statusText);}
const text=await r.text();
JOBS=JSON.parse(text);
const mr=await fetch('/api/messages').catch(()=>({json:()=>[]}));
MESSAGES=await mr.json();
renderStats();
renderFilters();
render();
}catch(e){
$('board').innerHTML='<div style="padding:40px;text-align:center;color:#c43e2d;font-size:14px">数据加载失败: '+esc(e.message)+'<br><br>请检查服务是否正常运行，或刷新页面重试。<br><button onclick="location.reload()" style="margin-top:12px;padding:6px 16px;background:#5b8def;color:#fff;border:none;border-radius:4px;cursor:pointer">刷新页面</button></div>';
}
}

// ===== Stats + Charts =====
function renderStats(){
const total=JOBS.length;
const aCnt=JOBS.filter(j=>j.priority==='A').length;
const hiScore=JOBS.filter(j=>(j.match_score||0)>=80).length;
const interview=JOBS.filter(j=>['笔试测评','面试中','等待结果'].includes(j.status)).length;
const offer=JOBS.filter(j=>j.status==='Offer').length;
let html='<span class="stat-chip s-total">共 <b>'+total+'</b> 岗位</span>';
html+='<span class="stat-chip s-A">A优 <b>'+aCnt+'</b></span>';
html+='<span class="stat-chip s-score">高分 <b>'+hiScore+'</b></span>';
html+='<span class="stat-chip s-interview">面试中 <b>'+interview+'</b></span>';
if(offer)html+='<span class="stat-chip s-offer">Offer <b>'+offer+'</b></span>';
// Charts
const byStatus={};STATUSES.forEach(s=>byStatus[s]=0);JOBS.forEach(j=>{byStatus[j.status]=(byStatus[j.status]||0)+1;});
const nonZero=STATUSES.filter(s=>byStatus[s]>0).map(s=>({label:s,value:byStatus[s],color:STATUS_COLORS[s]}));
if(nonZero.length>0){
html+='<div class="stat-charts"><div class="stat-chart-box"><div class="label">状态分布</div>'+donutChart(nonZero,90)+'</div>';
// Score histogram
const bins=[0,0,0,0,0];JOBS.forEach(j=>{const s=j.match_score||0;const i=Math.min(4,Math.floor(s/20));bins[i]++;});
const scoreData=[{label:'0-20',value:bins[0],color:'#c43e2d'},{label:'20-40',value:bins[1],color:'#cb9d06'},{label:'40-60',value:bins[2],color:'#0075de'},{label:'60-80',value:bins[3],color:'#0f7b6c'},{label:'80+',value:bins[4],color:'#00b894'}];
html+='<div class="stat-chart-box"><div class="label">匹配度分布</div>'+barChart(scoreData,140,60)+'</div></div>';
}
$('statsbar').innerHTML=html;
}
function donutChart(data,size){
const total=data.reduce((s,d)=>s+d.value,0);if(!total)return'';
let angle=0;const cx=size/2,cy=size/2,r=size/2-6,sw=12;let paths='';
data.forEach(d=>{const pct=d.value/total;const sa=angle,ea=angle+pct*360;angle=ea;
const x1=cx+r*Math.cos(sa*Math.PI/180),y1=cy+r*Math.sin(sa*Math.PI/180);
const x2=cx+r*Math.cos(ea*Math.PI/180),y2=cy+r*Math.sin(ea*Math.PI/180);
const lg=ea-sa>180?1:0;
paths+='<path d="M'+cx+','+cy+' L'+x1+','+y1+' A'+r+','+r+' 0 '+lg+' 1 '+x2+','+y2+' Z" fill="'+d.color+'" opacity="0.85"/>';});
return '<svg width="'+size+'" height="'+size+'">'+paths+'<circle cx="'+cx+'" cy="'+cy+'" r="'+(r-sw)+'" fill="var(--card)"/><text x="'+cx+'" y="'+cy+'" text-anchor="middle" dy="0.35em" font-size="14" font-weight="700" fill="var(--ink)">'+total+'</text></svg>';
}
function barChart(data,w,h){
const max=Math.max(...data.map(d=>d.value),1);const bw=w/data.length-4;let bars='';
data.forEach((d,i)=>{const bh=d.value/max*(h-15);const x=i*(bw+4)+2;const y=h-bh-12;
bars+='<rect x="'+x+'" y="'+y+'" width="'+bw+'" height="'+bh+'" rx="2" fill="'+d.color+'" opacity="0.85"/>';
bars+='<text x="'+(x+bw/2)+'" y="'+(y-3)+'" text-anchor="middle" font-size="8" fill="var(--ink-2)">'+d.value+'</text>';
bars+='<text x="'+(x+bw/2)+'" y="'+(h-2)+'" text-anchor="middle" font-size="7" fill="var(--ink-3)">'+d.label+'</text>';});
return '<svg width="'+w+'" height="'+h+'">'+bars+'</svg>';
}

// ===== Filters =====
function renderFilters(){
const cities=[...new Set(JOBS.map(j=>j.city).filter(Boolean))].sort();
const sources=[...new Set(JOBS.map(j=>j.source).filter(Boolean))].sort();
const citySel=$('fCity'),srcSel=$('fSource');
const cv=citySel.value,sv=srcSel.value;
citySel.innerHTML='<option value="">全部城市</option>'+cities.map(c=>'<option'+(c===cv?' selected':'')+'>'+esc(c)+'</option>').join('');
srcSel.innerHTML='<option value="">全部来源</option>'+sources.map(s=>'<option'+(s===sv?' selected':'')+'>'+esc(s)+'</option>').join('');
// Restore saved filters
const saved=JSON.parse(localStorage.getItem('jh_filters')||'{}');
if(saved.search!=null&&$('fSearch').value==='')$('fSearch').value=saved.search;
if(saved.city&&cv==='')citySel.value=saved.city;
if(saved.source&&sv==='')srcSel.value=saved.source;
if(saved.priority)$('fPriority').value=saved.priority;
if(saved.score)$('fScore').value=saved.score;
}
function getFiltered(){
const q=$('fSearch').value.toLowerCase();
const city=$('fCity').value,src=$('fSource').value,pri=$('fPriority').value,sc=$('fScore').value;
let r=JOBS;
if(q)r=r.filter(j=>(j.title+' '+j.company+' '+j.city).toLowerCase().includes(q));
if(city)r=r.filter(j=>j.city===city);
if(src)r=r.filter(j=>j.source===src);
if(pri)r=r.filter(j=>j.priority===pri);
if(sc)r=r.filter(j=>(j.match_score||0)>=parseInt(sc));
return r;
}
function saveFilters(){localStorage.setItem('jh_filters',JSON.stringify({search:$('fSearch').value,city:$('fCity').value,source:$('fSource').value,priority:$('fPriority').value,score:$('fScore').value}));}

// ===== Board Rendering =====
function render(){
if(currentView==='board')renderBoard();
else if(currentView==='calendar')renderCalendar();
else if(currentView==='compare')renderCompareView();
else if(currentView==='resume')renderResume();
else if(currentView==='messages')renderMessages();
$('fCount').textContent='显示 '+getFiltered().length+' / '+JOBS.length;
}
function renderBoard(){
const filtered=getFiltered();
const board=$('board');
board.innerHTML=STATUSES.map(s=>{
const colJobs=filtered.filter(j=>j.status===s);
const dot=STATUS_COLORS[s];
let cards='';
if(colJobs.length===0){
cards='<div class="empty-state" style="padding:16px;font-size:11px">暂无岗位</div>';
}else{
cards=colJobs.map(j=>renderCard(j)).join('');
}
return '<div class="column"><div class="col-head" data-status="'+s+'"><span class="col-name"><span class="col-dot" style="background:'+dot+'"></span>'+s+'</span><span class="col-count">'+colJobs.length+'</span></div><div class="col-body" data-status="'+s+'">'+cards+'</div></div>';
}).join('');
setupDragDrop();
}
function renderCard(j){
const s=j.match_score||0;
const sc=scoreColor(s);
let detail='';
if(j.match_points)detail+='<div class="jc-mp">'+esc(j.match_points).slice(0,100)+'</div>';
if(j.skill_gaps)detail+='<div class="jc-gap">⚠ '+esc(j.skill_gaps).slice(0,60)+'</div>';
let extra='';
if(j.action_deadline)extra+='<div class="jc-deadline">⏰ '+esc(fmtDate(j.action_deadline))+'</div>';
if(j.greeting)extra+='<div class="jc-greeting">💬 已有招呼语</div>';
const checkbox=batchMode?'<input type="checkbox" class="jc-check" data-url="'+esc(j.url)+'" '+(selectedUrls.has(j.url)?'checked':'')+' onclick="event.stopPropagation()">':'';
return '<div class="job-card" draggable="'+(batchMode?'false':'true')+'" data-url="'+esc(j.url)+'" onclick="onCardClick(\''+esc(j.url)+'\')"><div class="jc-head"><span class="jc-pri pri-'+j.priority+'">'+j.priority+'</span><span class="jc-score" style="color:'+sc+'">'+s+'</span></div><div class="jc-title">'+esc(j.title)+'</div><div class="jc-meta"><span>'+esc(j.company)+'</span><span>'+esc(j.city||'')+'</span><span>'+esc(j.salary||'')+'</span></div><div class="jc-detail">'+detail+extra+'</div>'+checkbox+'</div>';
}

// ===== Calendar Rendering =====
function renderCalendar(){
const today=new Date();
const y=today.getFullYear(),m=today.getMonth();
const firstDay=new Date(y,m,1).getDay();
const daysInMonth=new Date(y,m+1,0).getDate();
const events={};
JOBS.forEach(j=>{
['action_deadline','follow_up_date'].forEach(fld=>{
const d=j[fld];if(!d)return;
const dt=new Date(d);if(dt.getMonth()!==m)return;
const day=dt.getDate();if(!events[day])events[day]=[];
events[day].push({type:fld==='action_deadline'?'deadline':'followup',job:j});
});
});
let html='<div class="cal-header">日 一 二 三 四 五 六'.split(' ').map(d=>'<span>'+d+'</span>').join('')+'</div><div class="cal-body">';
for(let i=0;i<firstDay;i++)html+='<div class="cal-day empty"></div>';
for(let d=1;d<=daysInMonth;d++){
const evts=events[d]||[];const isToday=d===today.getDate();
let dots=evts.map(e=>'<span class="cal-dot '+e.type+'"></span>').join('');
let labels=evts.slice(0,2).map(e=>'<div class="cal-events">'+esc(e.job.title.slice(0,12))+'</div>').join('');
html+='<div class="cal-day'+(isToday?' today':'')+'" onclick="calClickDay('+d+')">'+d+dots+labels+'</div>';
}
html+='</div>';
$('calendar').innerHTML=html;
}
function calClickDay(day){
const today=new Date();const date=new Date(today.getFullYear(),today.getMonth(),day);
const jobs=JOBS.filter(j=>{const d=new Date(j.action_deadline||j.follow_up_date||'2000-01-01');return d.getDate()===day&&d.getMonth()===today.getMonth();});
if(jobs.length){toast(jobs.length+' 个事项');}else{toast('该日无事项');}
}

// ===== Resume Library =====
async function renderResume(){
const r=await fetch('/api/resume/versions');
const versions=await r.json();
if(!versions.length){$('resumeContent').innerHTML='<div class="empty-state">暂无简历版本。请在配置面板上传简历。</div>';return;}
$('resumeContent').innerHTML=versions.map(v=>'<div class="resume-item"><div class="ri-info"><div class="ri-name">'+esc(v.name)+'</div><div class="ri-meta">'+esc(v.direction||'通用')+' · '+esc(v.size_fmt)+' · '+esc(v.mtime)+'</div></div><a class="btn" href="/api/resume/download?path='+encodeURIComponent(v.path)+'">下载</a></div>').join('');
}

// ===== Messages =====
function renderMessages(){
if(!MESSAGES.length){$('messagesContent').innerHTML='<div class="empty-state">暂无 HR 回复消息。点击顶部「监听」按钮抓取最新回复。</div>';return;}
$('messagesContent').innerHTML=MESSAGES.map(m=>'<div class="msg-item"><span class="mi-company">'+esc(m.company||'')+'</span><span class="mi-text">'+esc(m.last_msg||'')+'</span><span class="mi-time">'+esc(m.time||m.fetched_at||'')+'</span></div>').join('');
}

// ===== Comparison =====
function renderCompareView(){
if(selectedUrls.size<2){$('compareEmpty').style.display='';$('compareContent').innerHTML='';return;}
$('compareEmpty').style.display='none';
const jobs=JOBS.filter(j=>selectedUrls.has(j.url));
const fields=[{l:'岗位名称',k:'title'},{l:'公司',k:'company'},{l:'城市',k:'city'},{l:'薪资',k:'salary'},{l:'匹配度',k:'match_score'},{l:'优先级',k:'priority'},{l:'状态',k:'status'},{l:'核心职责',k:'responsibilities'},{l:'必备条件',k:'requirements'},{l:'加分项',k:'plus_points'},{l:'匹配点',k:'match_points'},{l:'能力缺口',k:'skill_gaps'},{l:'招呼语',k:'greeting'},{l:'下一步',k:'next_action'},{l:'截止日期',k:'action_deadline'}];
let html='<table class="compare-table"><tr><th class="ck">字段</th>'+jobs.map(j=>'<th>'+esc(j.title)+'</th>').join('')+'</tr>';
fields.forEach(f=>{html+='<tr><td class="ck">'+f.l+'</td>'+jobs.map(j=>{const v=j[f.k];if(f.k==='match_score'){const s=parseInt(v)||0;return '<td style="color:'+scoreColor(s)+';font-weight:700">'+s+'</td>';}return '<td>'+esc(v||'—')+'</td>';}).join('')+'</tr>';});
html+='</table>';
$('compareContent').innerHTML=html;
}
function openCompareFromBatch(){if(selectedUrls.size<2){toast('请至少选择 2 个岗位');return;}switchView('compare');}

// ===== Drag & Drop =====
let draggedUrl=null;
function setupDragDrop(){
document.querySelectorAll('.job-card').forEach(card=>{
card.addEventListener('dragstart',e=>{if(batchMode){e.preventDefault();return;}draggedUrl=card.dataset.url;card.classList.add('dragging');e.dataTransfer.effectAllowed='move';});
card.addEventListener('dragend',()=>{card.classList.remove('dragging');});
});
document.querySelectorAll('.col-body').forEach(col=>{
col.addEventListener('dragover',e=>{e.preventDefault();col.classList.add('drop-target');});
col.addEventListener('dragleave',()=>col.classList.remove('drop-target'));
col.addEventListener('drop',async e=>{e.preventDefault();col.classList.remove('drop-target');if(!draggedUrl)return;
const newStatus=col.dataset.status;const r=await api('/api/jobs/update',{url:draggedUrl,fields:{status:newStatus}});
toast(r.ok?'已流转为「'+newStatus+'」':'流转失败');if(r.ok)reload();draggedUrl=null;});
});
}

// ===== Card Click =====
function onCardClick(url){if(batchMode){toggleSelect(url);return;}openDetail(url);}
function toggleSelect(url){if(selectedUrls.has(url))selectedUrls.delete(url);else selectedUrls.add(url);$('btCount').textContent='已选 '+selectedUrls.size+' 项';render();}

// ===== Detail Modal =====
function openDetail(url){
const j=JOBS.find(x=>x.url===url);if(!j)return;
currentDetailUrl=url;detailModified={};
$('dmTitle').textContent=j.title+' @ '+j.company;
const s=j.match_score||0;const sc=scoreColor(s);
let html='<div class="m-sec"><h4><span class="ic">📋</span>基本信息</h4><div class="kv">';
html+='<span class="k">公司</span><span class="v">'+esc(j.company)+'</span>';
html+='<span class="k">城市</span><span class="v">'+esc(j.city)+'</span>';
html+='<span class="k">薪资</span><span class="v">'+esc(j.salary)+'</span>';
html+='<span class="k">来源</span><span class="v">'+esc(j.source)+'</span>';
html+='<span class="k">匹配度</span><span class="v"><span style="color:'+sc+';font-weight:700;font-size:16px">'+s+'</span> / 100</span>';
html+='<span class="k">优先级</span><span class="v"><select id="ePriority"><option value="A"'+(j.priority==='A'?' selected':'')+'>A</option><option value="B"'+(j.priority==='B'?' selected':'')+'>B</option><option value="C"'+(j.priority==='C'?' selected':'')+'>C</option></select></span>';
html+='<span class="k">状态</span><span class="v"><select id="eStatus">'+STATUSES.map(st=>'<option value="'+st+'"'+(j.status===st?' selected':'')+'>'+st+'</option>').join('')+'</select></span>';
html+='</div></div>';
html+='<div class="m-sec"><h4><span class="ic">💼</span>岗位详情</h4><div class="kv">';
const respTxt = esc((j.responsibilities || j.description || '—').replace(/<[^>]+>/g, ''));
html+='<span class="k">核心职责</span><span class="v">'+respTxt+'</span>';
html+='<span class="k">必备条件</span><span class="v">'+esc((j.requirements||'—').replace(/<[^>]+>/g,''))+'</span>';
html+='<span class="k">加分项</span><span class="v">'+esc((j.plus_points||'—').replace(/<[^>]+>/g,''))+'</span>';
html+='</div></div>';
if(j.match_points||j.skill_gaps){
html+='<div class="m-sec"><h4><span class="ic">🎯</span>AI 分析</h4><div class="kv">';
html+='<span class="k">匹配点</span><span class="v">'+esc(j.match_points||'—')+'</span>';
html+='<span class="k">能力缺口</span><span class="v">'+esc(j.skill_gaps||'—')+'</span>';
html+='</div></div>';
}
html+='<div class="m-sec"><h4><span class="ic">💬</span>招呼语</h4>';
html+='<textarea id="eGreeting" placeholder="点击「AI生成」创建招呼语，或手动编辑...">'+esc(j.greeting||'')+'</textarea>';
html+='<div class="edit-row"><button class="btn" onclick="genGreetingSingle()">✨ AI 生成</button><button class="btn primary" onclick="saveGreeting()">保存招呼语</button></div></div>';
html+='<div class="m-sec"><h4><span class="ic">📝</span>投递计划</h4><div class="kv">';
html+='<span class="k">下一步</span><span class="v"><input id="eNextAction" value="'+esc(j.next_action||'')+'" style="width:100%"></span>';
html+='<span class="k">截止日期</span><span class="v"><input id="eDeadline" type="date" value="'+esc(fmtDate(j.action_deadline))+'"></span>';
html+='<span class="k">跟进日期</span><span class="v"><input id="eFollowUp" type="date" value="'+esc(fmtDate(j.follow_up_date))+'"></span>';
html+='</div></div>';
if(j.followups&&j.followups.length){
html+='<div class="m-sec"><h4><span class="ic">📅</span>跟进记录</h4><div class="timeline">';
j.followups.forEach(f=>{html+='<div class="timeline-item"><span class="ti-date">'+esc(f.date)+'</span><span class="ti-type">'+esc(f.type)+'</span>'+esc(f.note)+'</div>';});
html+='</div></div>';
}
html+='<div class="m-sec"><h4><span class="ic">💬</span>面试复盘</h4>';
html+='<textarea id="eFeedback" placeholder="面试复盘记录...">'+esc(j.interview_feedback||'')+'</textarea></div>';
html+='<div class="m-sec"><h4><span class="ic">🔗</span>招聘链接</h4><div class="kv"><span class="k">链接</span><span class="v">'+(j.url?('<a href="'+esc(j.url)+'" target="_blank">'+esc(j.url)+'</a>'):'—')+'</span></div></div>';
$('dmBody').innerHTML=html;
$('dmGenResume').onclick=()=>genResume(url);
$('detailOverlay').classList.add('show');
}
function closeDetail(){$('detailOverlay').classList.remove('show');currentDetailUrl='';}
async function saveDetail(){
if(!currentDetailUrl)return;
const fields={priority:$('ePriority').value,status:$('eStatus').value,greeting:$('eGreeting').value,next_action:$('eNextAction').value,action_deadline:$('eDeadline').value,follow_up_date:$('eFollowUp').value,interview_feedback:$('eFeedback').value};
const r=await api('/api/jobs/update',{url:currentDetailUrl,fields});
toast(r.ok?'已保存':'保存失败');if(r.ok)reload();closeDetail();
}
async function saveGreeting(){
if(!currentDetailUrl)return;
const r=await api('/api/jobs/update',{url:currentDetailUrl,fields:{greeting:$('eGreeting').value}});
toast(r.ok?'招呼语已保存':'保存失败');if(r.ok)reload();
}
async function genGreetingSingle(){
if(!currentDetailUrl)return;
const job=JOBS.find(j=>j.url===currentDetailUrl);if(!job)return;
showProgress(true,20,'正在生成招呼语…');
try{
const r=await fetch('/api/greet/single',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url:currentDetailUrl})});
const data=await r.json();
showProgress(false);
if(data.ok){$('eGreeting').value=data.greeting;toast('招呼语已生成');}else{toast(data.message||'生成失败');}
}catch(e){showProgress(false);toast('生成失败');}
}
async function genResume(url){
const r=await fetch('/api/resume/generate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url:url})});
const data=await r.json();
toast(data.message||'已启动');
}

// ===== Add Modal =====
function closeAdd(){$('addOverlay').classList.remove('show');}
async function submitAdd(){
const data={title:$('amTitle').value,company:$('amCompany').value,city:$('amCity').value,salary:$('amSalary').value,url:$('amUrl').value,description:$('amDesc').value};
if(!data.title||!data.company){toast('岗位名称和公司名称为必填');return;}
const r=await api('/api/jobs/add',data);
toast(r.message);if(r.ok){closeAdd();reload();$('amTitle').value='';$('amCompany').value='';$('amCity').value='';$('amSalary').value='';$('amUrl').value='';$('amDesc').value='';}
}

// ===== Batch Operations =====
function toggleBatch(){
batchMode=!batchMode;
if(!batchMode)selectedUrls.clear();
document.body.classList.toggle('batch-mode',batchMode);
$('batchToolbar').classList.toggle('show',batchMode);
$('btCount').textContent='已选 0 项';
render();
}
function batchSetStatus(){
const jobs=JOBS.filter(j=>selectedUrls.has(j.url));if(!jobs.length)return;
const status=prompt('输入目标状态：'+STATUSES.join(' / '));
if(!status||!STATUSES.includes(status)){toast('无效状态');return;}
batchAction('status',{status});
}
function batchSetPriority(){
const pri=prompt('优先级 A/B/C：');
if(!['A','B','C'].includes(pri)){toast('无效优先级');return;}
batchAction('priority',{priority:pri});
}
async function batchDelete(){
if(!confirm('确认删除 '+selectedUrls.size+' 个岗位？'))return;
await batchAction('delete',{});
selectedUrls.clear();$('btCount').textContent='已选 0 项';
}
async function batchAction(action,extra){
const r=await api('/api/jobs/batch',{urls:[...selectedUrls],action,...extra});
toast(r.message);if(r.ok)reload();
}

// ===== Export =====
function exportData(){
const params=new URLSearchParams({search:$('fSearch').value,city:$('fCity').value,source:$('fSource').value,priority:$('fPriority').value,score:$('fScore').value,format:'xlsx'});
window.open('/api/export?'+params);
}

// ===== Generic API =====
async function api(url,body){
try{const r=await fetch(url,{method:'POST',cache:'no-store',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});return await r.json();}
catch(e){return{ok:false,message:'请求失败'};}
}

// ===== Task Button Setup =====
function setupTaskButton(btnId,startApi,statusApi){
const btn=$(btnId);if(!btn)return;
const originalText=btn.textContent;
btn.onclick=async()=>{
if(btn.disabled)return;
btn.disabled=true;btn.innerHTML='<span class="spin"></span> 进行中';
showProgress(true,2,'启动中…');
let data=null,lastErr=null;
const rightPlace=(location.port==='8686');
for(let i=0;i<12;i++){
try{
const r=await fetch(startApi,{method:'POST',cache:'no-store'});
const ct=(r.headers.get('Content-Type')||'').toLowerCase();
data=ct.indexOf('json')>=0?await r.json():{ok:false,message:'服务返回异常('+r.status+')，请确认看板服务已启动'};
break;
}catch(e){
lastErr=e;
if(i===0)checkService();
const wait=i===0?500:Math.min(1000+i*1500,6000);
const stageMsg=i<2?'正在连接服务，'+Math.round(wait/1000)+' 秒后重试（第 '+(i+1)+'/12 次）…':'服务连接中断，'+Math.round(wait/1000)+' 秒后自动重试（第 '+(i+1)+'/12 次）…';
showProgress(true,2,stageMsg);
await new Promise(res=>setTimeout(res,wait));
}
}
if(!data){
btn.disabled=false;btn.textContent=originalText;showProgress(false);
checkService();
let msg;
if(rightPlace){
msg='后台服务暂时无响应'+(lastErr?('（'+lastErr.message+'）'):'')+'。服务可能正在重启，几分钟内会自动恢复，请稍后再点一次「'+originalText.replace(/^\S+\s/,'')+'」';
}else{
msg='无法连接看板服务（当前页面地址：'+location.host+'）：'+(lastErr?lastErr.message:'网络错误')+'。看板服务在 http://127.0.0.1:8686/ ，请关闭当前标签页，用正确地址重新打开';
}
toast(msg,10000);
return;
}
if(!data.ok&&data.running===undefined){btn.disabled=false;btn.textContent=originalText;showProgress(false);toast(data.message||'启动失败');return;}
pollTask(statusApi,btn,originalText);
};
}
function pollTask(statusApi,btn,originalText,fails){
fails=fails||0;
let timer=setTimeout(async()=>{
try{
const r=await fetch(statusApi,{cache:'no-store'});const s=await r.json();
if(!s.running){
btn.disabled=false;btn.textContent=originalText;showProgress(false);
if(s.done){
if(s.ok){toast(s.message||'完成');reload();}
else if(statusApi==='/api/collect/status'){showCollectResult(s);}
else{toast(s.message||'失败');}
}
return;
}
showProgress(true,s.percent,s.stage);
pollTask(statusApi,btn,originalText,0);
}catch(e){
if(fails<10){setTimeout(()=>pollTask(statusApi,btn,originalText,fails+1),1000);}
else{btn.disabled=false;btn.textContent=originalText;showProgress(false);toast('与服务失去连接，任务状态未知。若服务刚重启过，请重新点击按钮',8000);}
}
},1000);
}

// ===== 采集结果弹窗（报错类型 + 报错内容） =====
function showCollectResult(s){
$('cmStatus').textContent=s.ok?'✅ 采集成功':('❌ 采集失败：'+(s.stage||''));
const et=(s.error_type||'').trim();
const em=(s.error_message||'').trim();
const errWrap=$('cmErrWrap');
if(et||em){
errWrap.style.display='block';
$('cmErrType').textContent=et||'未知错误';
$('cmErrMsg').textContent=em||s.message||'';
$('cmLoginBtn').style.display=(et==='登录态失效'||et==='未配置登录')?'inline-block':'none';
}else{
errWrap.style.display='none';
$('cmLoginBtn').style.display='none';
}
$('collectOverlay').classList.add('show');
}
function closeCollect(){ $('collectOverlay').classList.remove('show'); }

// ===== BOSS 直聘 扫码登录 =====
let _loginTimer=null,_loginLock=false;
function openLogin(){
closeCollect();
$('loginQrImg').style.display='none';$('loginQrImg').src='';
$('loginStatus').textContent='正在生成二维码…';
$('btnLoginRegen').style.display='none';
$('loginOverlay').classList.add('show');
startLogin();
}
function closeLogin(){
$('loginOverlay').classList.remove('show');
if(_loginTimer){clearTimeout(_loginTimer);_loginTimer=null;}
fetch('/api/login/cancel',{method:'POST',cache:'no-store'}).catch(()=>{});
}
async function startLogin(){
if(_loginLock)return;_loginLock=true;
$('btnLoginRegen').style.display='none';
$('loginStatus').textContent='正在生成二维码…';
$('loginQrImg').style.display='none';
try{
const r=await fetch('/api/login/qr',{method:'POST',cache:'no-store'});const d=await r.json();
if(!d.ok){$('loginStatus').textContent=d.message||'启动登录失败';_loginLock=false;return;}
}catch(e){$('loginStatus').textContent='启动登录失败：网络异常';_loginLock=false;return;}
pollLoginStatus();
}
function pollLoginStatus(){
if(!$('loginOverlay').classList.contains('show'))return;
if(_loginTimer){clearTimeout(_loginTimer);_loginTimer=null;}
_loginTimer=setTimeout(async()=>{
try{
const r=await fetch('/api/login/status',{cache:'no-store'});const s=await r.json();
const phase=s.phase||'';
if(phase==='ready'||phase==='scanned'||phase==='confirmed'){
const img=$('loginQrImg');
img.style.display='block';
if(s.qr_img){if(img.src!==s.qr_img)img.src=s.qr_img;}
else if(!img.src||img.src.indexOf('nocache')<0){img.src='/api/login/qr/img?nocache='+Date.now();}
$('loginStatus').textContent = phase==='ready'?'二维码已生成，请用 BOSS 直聘 App 扫码'
:(phase==='scanned'?'已扫码，请在手机上确认登录':'正在写入登录凭证…');
pollLoginStatus();
}else if(phase==='generating'){
$('loginStatus').textContent='正在生成二维码…';
pollLoginStatus();
}else if(phase==='success'){
$('loginStatus').textContent='登录成功！';
toast('BOSS 直聘 登录成功');
setTimeout(()=>{closeLogin();reload();},800);
}else if(phase==='expired'||phase==='error'||phase==='cancelled'){
$('loginStatus').textContent=(s.error_message||s.message||(phase==='expired'?'二维码已过期':'登录失败'));
$('btnLoginRegen').style.display='inline-block';
}else{
pollLoginStatus();
}
}catch(e){pollLoginStatus();}
},1200);
}

// ===== Service Health Check =====
let _svcTimer=null;
async function checkService(){
let ok=false;
try{
const r=await fetch('/api/collect/status',{cache:'no-store'});
ok=r.ok;
}catch(e){ok=false;}
const b=$('svcBanner');
if(!ok&&!b){
const rightPlace=(location.port==='8686');
const d=document.createElement('div');d.id='svcBanner';
d.style.cssText='position:fixed;top:0;left:0;right:0;z-index:99999;background:#c43e2d;color:#fff;padding:10px 16px;font-size:14px;text-align:center;line-height:1.7;';
let inner='⚠️ 无法连接后台服务（当前页面地址：<b>'+esc(location.host)+'</b>）<br>';
if(rightPlace){
inner+='服务暂时无响应，可能正在重启…稍候自动恢复（本提示会自动消失）';
}else{
inner+='本看板服务的正确地址是 <b>http://127.0.0.1:8686/</b> —— '
+'<a href="http://127.0.0.1:8686/jobs" style="color:#fff;text-decoration:underline;font-weight:700">点此跳转到正确地址</a>';
}
inner+=' &nbsp;|&nbsp; <a href="javascript:void(0)" onclick="document.body.style.paddingTop=\'\';document.getElementById(\'svcBanner\').remove()" style="color:#fff;text-decoration:underline">关闭</a>';
d.innerHTML=inner;
document.body.appendChild(d);
document.body.style.paddingTop='70px';
}else if(ok&&b){b.remove();document.body.style.paddingTop='';}
}
function startSvcCheck(){
checkService();
if(_svcTimer)clearInterval(_svcTimer);
_svcTimer=setInterval(checkService,8000);
}
async function resumeRunningTask(){
try{
const tasks=[['/api/collect/status','采集'],['/api/score/status','评分'],['/api/greet/status','招呼语'],['/api/monitor/status','监听']];
for(const [api,name] of tasks){
const r=await fetch(api,{cache:'no-store'});
if(!r.ok)continue;
const s=await r.json();
if(s&&s.running){showProgress(true,s.percent||2,s.stage||(name+'进行中…'));break;}
}
}catch(e){}
}

// ===== Init =====
function init(){
try{
initTheme();
initWS();
startSvcCheck();
resumeRunningTask();
// Theme toggle
$('btnDark').onclick=toggleDark;
if(localStorage.getItem('jh_theme')==='dark')$('btnDark').textContent='☀️';
// Navigation
document.querySelectorAll('.nav a[data-view]').forEach(a=>a.onclick=()=>switchView(a.dataset.view));
// Filters
['fSearch','fCity','fSource','fPriority','fScore'].forEach(id=>$(id).oninput=()=>{saveFilters();render();});
$('fSearch').oninput=()=>{saveFilters();render();};
// Buttons
$('btnAdd').onclick=()=>$('addOverlay').classList.add('show');
$('btnBatch').onclick=toggleBatch;
$('btnExport').onclick=exportData;
$('btnLogin').onclick=openLogin;
$('cmLoginBtn').onclick=openLogin;
$('btnLoginRegen').onclick=startLogin;
setupTaskButton('btnCollect','/api/collect','/api/collect/status');
setupTaskButton('btnScore','/api/score','/api/score/status');
setupTaskButton('btnGreet','/api/greet','/api/greet/status');
setupTaskButton('btnMonitor','/api/monitor','/api/monitor/status');
// Close overlays on outside click
$('detailOverlay').onclick=e=>{if(e.target.id==='detailOverlay')closeDetail();};
$('addOverlay').onclick=e=>{if(e.target.id==='addOverlay')closeAdd();};
$('collectOverlay').onclick=e=>{if(e.target.id==='collectOverlay')closeCollect();};
$('loginOverlay').onclick=e=>{if(e.target.id==='loginOverlay')closeLogin();};
// Keyboard shortcuts
document.addEventListener('keydown',e=>{
if(e.key==='Escape'){closeDetail();closeAdd();closeCollect();closeLogin();}
if(e.key==='/'&&!e.target.matches('input,textarea')){e.preventDefault();$('fSearch').focus();}
});
// Load data
reload();
}catch(e){
$('board').innerHTML='<div style="padding:40px;text-align:center;color:#c43e2d;font-size:14px">初始化失败: '+esc(e.message)+'</div>';
}
}
init();
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# 启动
# ---------------------------------------------------------------------------

def start_server(port: int = None) -> None:
    if port is None:
        port = int(os.environ.get("WEB_PORT", "8686"))
    config.ensure_dirs()
    threading.Timer(1.5, lambda: webbrowser.open(f"http://127.0.0.1:{port}/jobs")).start()
    print(f"JobHunter 七步法求职管理看板已启动：http://127.0.0.1:{port}/jobs")
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning",
                ws_per_message_deflate=False, timeout_keep_alive=75)


if __name__ == "__main__":
    start_server()

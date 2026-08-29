"""JobHunter —— 多招聘平台注册表。

统一维护每个求职平台的元数据（来源标签 / 配色 / 首页 / 采集配置键），
让前端按「平台」分类展示（Boss 进 Boss 栏目、应届生进应届生栏目，不混放），
也让采集框架可以按平台分发到不同的采集器。

约定：
- `PLATFORMS` 中每一项用稳定的 `key` 标识（boss / yingsheng / zhaopin / 51job / qiuzhifangzhou）；
- 每个平台的岗位来源标签（写入 jobs.json 的 `source` 字段）取 `source` 字段；
- 各平台在 config.yaml 的 `platforms:<key>` 下可独立配置（profile 目录 / 是否启用等）。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

# key → 平台元数据。order 决定前端栏目展示顺序。
PLATFORMS: Dict[str, Dict[str, Any]] = {
    "boss": {
        "key": "boss",
        "label": "Boss直聘",
        "source": "Boss直聘",
        "color": "#0075de",
        "home_url": "https://www.zhipin.com",
        "collector": "boss",          # 采集器 key（见 collectors.py）
        "needs_login_profile": False,  # Boss 走 recommend API + 最小 cookie，不依赖浏览器 profile
        "note": "已验证（recommend 接口）",
    },
    "yingsheng": {
        "key": "yingsheng",
        "label": "应届生求职",
        "source": "应届生求职",
        "color": "#00a3a3",
        "home_url": "https://www.yingjiesheng.com",
        "collector": "playwright",
        "needs_login_profile": True,
        "note": "框架已就绪，需在该平台登录 profile 后调通选择器",
    },
    "zhaopin": {
        "key": "zhaopin",
        "label": "智联招聘",
        "source": "智联招聘",
        "color": "#e8520c",
        "home_url": "https://www.zhaopin.com",
        "collector": "playwright",
        "needs_login_profile": True,
        "note": "框架已就绪，需在该平台登录 profile 后调通选择器",
    },
    "51job": {
        "key": "51job",
        "label": "前程无忧51Job",
        "source": "前程无忧51Job",
        "color": "#1f7aec",
        "home_url": "https://www.51job.com",
        "collector": "playwright",
        "needs_login_profile": True,
        "note": "框架已就绪，需在该平台登录 profile 后调通选择器",
    },
    "qiuzhifangzhou": {
        "key": "qiuzhifangzhou",
        "label": "求职方舟·AI找工作",
        "source": "求职方舟·AI找工作",
        "color": "#9c27b0",
        "home_url": "https://www.qiuzhifangzhou.com/job",
        "collector": "playwright",
        "needs_login_profile": True,
        "note": "已接入真实站点，可在登录后采集",
    },
}

# 「全部」栏虚拟条目（前端使用）
ALL_KEY = "all"


def all_platforms() -> List[Dict[str, Any]]:
    """按注册顺序返回平台列表。"""
    return list(PLATFORMS.values())


def by_key(key: str) -> Optional[Dict[str, Any]]:
    """按下发/配置的来源 key 找到平台元数据；找不到返回 None。"""
    return PLATFORMS.get(key)


def source_to_key(source: str) -> str:
    """岗位来源标签 → 平台 key；未知来源返回 None。"""
    for key, p in PLATFORMS.items():
        if p["source"] == source:
            return key
    return None


def resolve_source(value: str = "") -> dict:
    """把用户输入（key 或来源标签）归一为 (key, source)。"""
    value = (value or "").strip()
    if not value:
        return {"key": "", "source": ""}
    if value in PLATFORMS:
        p = PLATFORMS[value]
        return {"key": p["key"], "source": p["source"]}
    key = source_to_key(value)
    if key:
        p = PLATFORMS[key]
        return {"key": key, "source": p["source"]}
    # 自定义来源（如手动录入）
    return {"key": "", "source": value}


def ensure_source(job: Dict[str, Any], default: str = "") -> str:
    """确保岗位带来源标签。已有 source 保留；否则用 default（默认 Boss直聘，兼容既有数据）。"""
    src = (job.get("source") or "").strip()
    if src:
        return src
    job["source"] = default or PLATFORMS["boss"]["source"]
    return job["source"]


def display_list() -> List[Dict[str, Any]]:
    """供前端平台栏目使用的元数据（含「全部」入口）。"""
    return [{**p, "is_all": False} for p in all_platforms()]
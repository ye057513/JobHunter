"""JobHunter 智能岗位过滤模块。

按用户技术栈对岗位描述（JD）做匹配过滤：
- JD 中命中用户技术栈 ≥2 项核心技术即收录（不只看岗位名称）
- 排除含"X年以上"经验年限要求的岗位
- 学历：本科及以下（本科/大专/不限），排除硕士/博士要求
- 工作性质：实习/全职均可
- 经验：应届生 / 经验不限
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List

try:
    from . import config
except ImportError:
    # 支持直接运行 python src/smart_filter.py
    import os
    import sys

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from src import config

# 用户技术栈核心技术关键词（来自简历，小写匹配）
TECH_KEYWORDS: List[str] = [
    # 后端
    "java", "spring", "springboot", "spring boot", "springmvc", "spring mvc",
    "mybatis", "mybatis-plus", "mysql", "redis", "maven", "docker", "nginx",
    "linux", "restful", "jwt", "websocket", "easyexcel", "langchain",
    "spring security", "数据库", "sql",
    # Android
    "kotlin", "android", "jetpack", "compose", "mvvm", "hilt", "retrofit",
    "okhttp", "coroutines", "datastore", "android studio",
    # AI / Python
    "python", "rag", "embedding", "大模型", "ai", "llm", "flask", "django",
    # 前端
    "javascript", "html", "css", "前端", "vue", "react", "node", "fetch",
    "json", "web前端",
    # 测试
    "测试", "接口测试", "功能测试", "自动化测试", "软件测试", "测试用例",
    # 基础
    "数据结构", "计算机网络", "操作系统", "git",
    # 宽泛技术岗补充
    "后端", "软件", "研发",
    # 金融
    "金融", "银行", "证券", "基金", "保险", "财务", "风控",
    # 传统软件
    "传统软件", "企业软件", "erp", "oa系统", "mes", "wms",
    # AI公司/数字化
    "人工智能", "数字化", "创新", "数字化转型", "数字化运营",
]

# 技术岗标题特征词：岗位名命中任一即视为明确技术岗（前端/后端等），
# 只要学历/年限合规（接收计算机专业）即收录，即使未写明具体技术栈
TECH_TITLE_TERMS: List[str] = [
    "前端", "后端", "开发", "工程师", "程序员", "软件", "全栈", "测试",
    "运维", "算法", "安卓", "android", "ios", "java", "python", "web",
    "研发", "游戏开发", "小程序",
    # 新增岗位分类
    "金融", "银行", "传统软件", "人工智能", "数字化", "创新",
]

# 学历排除词（要求硕士/博士/研究生）
EDU_EXCLUDE = ["硕士", "博士", "研究生", "mba", "phd"]

# 年限要求正则：如 "3年以上"、"3-5年"、"3 年以上"、"三年以上"
YEAR_RE = re.compile(r"(\d+)\s*[-~至到]?\s*(\d+)?\s*年(以上|以上经验|以上开发经验|以上工作经验|以上相关经验|以上java|以上前端|以上测试|以上android|以上后端|以上开发|以上工作)?")


def _has_year_requirement(text: str) -> bool:
    """判断文本是否含年限经验要求（如 3年以上 / 3-5年）。"""
    if not text:
        return False
    # 匹配 "X年以上" 或 "X-Y年"
    if re.search(r"\d+\s*[-~至到]?\s*\d*\s*年(以上|以上经验|以上开发经验|以上工作经验|以上相关经验|以上工作|以上开发|以上java|以上前端|以上测试|以上android|以上后端)?", text):
        return True
    # 中文数字年限：三年以上 / 五年以上
    if re.search(r"[一二三四五六七八九十百]+\s*年(以上|以上经验|以上工作经验|以上开发经验)", text):
        return True
    return False


def _has_edu_exclude(text: str) -> bool:
    """判断是否要求硕士/博士学历。"""
    if not text:
        return False
    low = text.lower()
    return any(k in low for k in EDU_EXCLUDE)


def _count_tech_matches(text: str) -> int:
    """统计 JD 文本命中用户技术栈关键词的数量。"""
    if not text:
        return 0
    low = text.lower()
    matched = set()
    for kw in TECH_KEYWORDS:
        if kw in low:
            matched.add(kw)
    return len(matched)


def match_job(job: Dict[str, Any], min_tech: int = 2) -> Dict[str, Any]:
    """对单条岗位做智能匹配，返回 (是否收录, 匹配详情)。"""
    # 拼接可匹配文本：岗位名 + 公司 + 描述 + 标签 + 经验 + 学历
    text = " ".join([
        str(job.get("title", "")),
        str(job.get("company", "")),
        str(job.get("description", "")),
        str(job.get("info", "")),
        str(job.get("tags", "")),
        str(job.get("experience", "")),  # 经验年限：如 "3-5年" / "5年以上" / "1-3年"
        str(job.get("education", "")),   # 学历要求：如 "本科" / "硕士"
    ])

    # 1. 排除年限要求（始终检测，含无 JD 描述的历史数据）
    if _has_year_requirement(text):
        return {"pass": False, "reason": "含年限经验要求", "tech_matches": 0}

    # 2. 排除硕士/博士学历要求（始终检测）
    if _has_edu_exclude(text):
        return {"pass": False, "reason": "要求硕士/博士学历", "tech_matches": 0}

    # 2.5 技术岗标题放宽：岗位名命中技术岗特征词（前端/后端/开发/工程师/程序员等）
    #     即视为明确技术岗，学历/年限已合规（本科及以下、无年限要求，即接收计算机专业），
    #     直接收录，不强制具体技术栈命中≥2项（覆盖"未写明具体技术"的前端/后端岗位）
    title = str(job.get("title", "")).lower()
    if any(t in title for t in TECH_TITLE_TERMS):
        return {"pass": True, "reason": "技术岗标题匹配", "tech_matches": _count_tech_matches(text)}

    # 无 JD 描述的历史数据（无 description/info/tags），无法做技术匹配，视为已通过人工筛选保留
    has_jd = any(job.get(k) for k in ("description", "info", "tags"))
    if not has_jd:
        return {"pass": True, "reason": "历史数据（无JD描述）", "tech_matches": 0}

    # 3. 技术栈匹配（≥2 项核心技术）
    tech_count = _count_tech_matches(text)
    if tech_count < min_tech:
        return {"pass": False, "reason": f"技术匹配不足（命中{tech_count}项<{min_tech}项）", "tech_matches": tech_count}

    return {"pass": True, "reason": "匹配", "tech_matches": tech_count}


def filter_jobs(jobs: List[Dict[str, Any]], min_tech: int = 2) -> List[Dict[str, Any]]:
    """批量过滤岗位，返回通过匹配的岗位列表（含匹配信息）。"""
    passed = []
    for job in jobs:
        result = match_job(job, min_tech)
        if result["pass"]:
            job["tech_matches"] = result["tech_matches"]
            job.setdefault("status", "待处理")
            passed.append(job)
    return passed


def run() -> None:
    """主入口：读取 jobs.json 原始数据，按智能规则过滤后写回。"""
    config.ensure_dirs()
    jobs_path = config.DATA_DIR / "jobs.json"
    if not jobs_path.exists():
        print("jobs.json 不存在，请先采集。")
        return

    with open(jobs_path, "r", encoding="utf-8") as f:
        jobs = json.load(f)

    passed = filter_jobs(jobs)
    with open(jobs_path, "w", encoding="utf-8") as f:
        json.dump(passed, f, ensure_ascii=False, indent=2)

    print(f"智能过滤完成：{len(jobs)} 条 → 保留 {len(passed)} 条（技术匹配≥2项、无年限要求、本科及以下）")


if __name__ == "__main__":
    run()

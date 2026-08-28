"""JobHunter 简历定制模块。

基于简历文件或信息库，按目标岗位 JD 定制简历并导出。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

import config

RESUME_PROMPT = """你是一位资深简历优化专家。请根据候选人的简历素材，针对目标岗位 JD 定制一份简历。

【候选人简历素材】
{resume}

【目标岗位 JD】
职位：{title}
公司：{company}
岗位要求：
{description}

【定制要求】
1. 保留候选人真实经历，不得编造任何技能、项目或数据
2. 根据 JD 调整经历描述的侧重点，突出与岗位最匹配的技能和项目
3. 结构：基本信息 / 求职意向 / 专业技能 / 项目经历 / 教育背景
4. 用 Markdown 格式输出，直接输出简历正文
"""


def load_resume() -> str:
    """读取简历素材（支持 .md/.txt/.docx/.pdf 文件或信息库）。"""
    return config.load_resume_text()


def load_job(job_id: str = "") -> Dict[str, Any]:
    """读取指定岗位，未指定时取最高分岗位。"""
    jobs_path = config.DATA_DIR / "jobs.json"
    if not jobs_path.exists():
        return {}
    with open(jobs_path, "r", encoding="utf-8") as f:
        jobs = json.load(f)
    if job_id:
        for j in jobs:
            if j.get("link") == job_id or j.get("title") == job_id:
                return j
    scored = [j for j in jobs if j.get("match_score", j.get("score", 0)) > 0]
    if scored:
        scored.sort(key=lambda x: x.get("match_score", x.get("score", 0)), reverse=True)
        return scored[0]
    return jobs[0] if jobs else {}


def generate_resume(client, job: Dict[str, Any], resume: str) -> str:
    """生成定制简历。"""
    prompt = RESUME_PROMPT.format(
        resume=resume[:4000],
        title=job.get("title", ""),
        company=job.get("company", ""),
        description=job.get("description", "")[:2000],
    )
    for attempt in range(3):
        try:
            resp = client.chat.completions.create(
                model=config.load_config()["llm"]["model"],
                messages=[{"role": "user", "content": prompt}],
                temperature=0.5,
            )
            return resp.choices[0].message.content.strip()
        except Exception as e:
            if attempt == 2:
                return f"（简历生成失败：{e}）"
            time.sleep(2)
    return ""


def run(job_id: str = "") -> None:
    """主入口：生成定制简历。"""
    config.ensure_dirs()
    job = load_job(job_id)
    if not job:
        print("没有可用的岗位数据，请先执行采集和评分。")
        return

    resume = load_resume()
    client = config.get_llm_client()

    print(f"为岗位「{job.get('title')} @ {job.get('company')}」生成定制简历...")
    content = generate_resume(client, job, resume)

    # 安全文件名
    safe_title = "".join(c for c in job.get("title", "岗位") if c not in '\\/:*?"<>|').strip()
    out_path = config.EXPORT_DIR / f"定制简历_{safe_title}.md"
    out_path.write_text(content, encoding="utf-8")

    print(f"\n定制简历已生成：{out_path}")
    print("如需导出 PDF/Word，可另行转换。")


if __name__ == "__main__":
    job_id = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        run(job_id)
    except Exception as e:
        print(f"简历定制出错：{e}")
        sys.exit(1)

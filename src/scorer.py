"""JobHunter AI 评分模块。

读取岗位列表与简历素材，调用硅基流动 API 为每个岗位打分（0-100）并给出理由。
字段与 web.py 七步法对齐：match_score / match_points / skill_gaps。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import config

SCORE_PROMPT = """你是一位资深技术招聘顾问。请根据候选人的简历信息，评估候选人与以下岗位的匹配度。

【候选人简历】
{resume}

【目标岗位】
职位：{title}
公司：{company}
薪资：{salary}
城市：{city}
岗位要求：
{description}

【评分要求】
请从以下维度综合评估，输出 0-100 的整数分数：
1. 技能匹配度（技术栈、工具是否对口）
2. 经验匹配度（工作年限、项目经验）
3. 行业匹配度（行业背景、公司规模）
4. 薪资匹配度（期望薪资与岗位薪资是否接近）

同时请指出：
- match_points：候选人匹配该岗位的优势点（1-3 条）
- skill_gaps：候选人的能力缺口（如有，没有则填"无明显缺口"）

【输出格式】
严格按以下 JSON 格式输出，不要输出其他内容：
{{"score": 85, "reason": "一句话说明评分理由", "match_points": "1. xxx\\n2. xxx", "skill_gaps": "1. xxx", "tags": ["技能匹配", "经验匹配"]}}
"""


def load_jobs() -> List[Dict[str, Any]]:
    """读取 data/jobs.json。"""
    jobs_path = config.DATA_DIR / "jobs.json"
    if not jobs_path.exists():
        return []
    with open(jobs_path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_resume() -> str:
    """读取简历素材（支持 .md/.txt/.docx/.pdf 文件或信息库）。"""
    return config.load_resume_text()


def score_job(client, job: Dict[str, Any], resume: str) -> Dict[str, Any]:
    """对单个岗位评分，返回带评分的岗位数据（字段对齐 web.py 七步法）。"""
    description = job.get("description", "")[:2000]
    prompt = SCORE_PROMPT.format(
        resume=resume[:3000],
        title=job.get("title", ""),
        company=job.get("company", ""),
        salary=job.get("salary", ""),
        city=job.get("city", ""),
        description=description,
    )
    llm_cfg = config.load_config()["llm"]
    try:
        resp = client.chat.completions.create(
            model=llm_cfg["model"],
            messages=[{"role": "user", "content": prompt}],
            temperature=llm_cfg["temperature"],
            response_format={"type": "json_object"},
        )
        content = resp.choices[0].message.content
        parsed = json.loads(content)
        job["match_score"] = max(0, min(100, int(parsed.get("score", 0))))
        job["match_points"] = parsed.get("match_points") or parsed.get("reason", "")
        job["skill_gaps"] = parsed.get("skill_gaps", "")
        job["score_reason"] = parsed.get("reason", "")
        job["score_tags"] = parsed.get("tags", [])
    except Exception as e:
        for attempt in range(2):
            time.sleep(2)
            try:
                resp = client.chat.completions.create(
                    model=llm_cfg["model"],
                    messages=[{"role": "user", "content": prompt}],
                    temperature=llm_cfg["temperature"],
                    response_format={"type": "json_object"},
                )
                content = resp.choices[0].message.content
                parsed = json.loads(content)
                job["match_score"] = max(0, min(100, int(parsed.get("score", 0))))
                job["match_points"] = parsed.get("match_points") or parsed.get("reason", "")
                job["skill_gaps"] = parsed.get("skill_gaps", "")
                job["score_reason"] = parsed.get("reason", "")
                job["score_tags"] = parsed.get("tags", [])
                break
            except Exception:
                if attempt == 1:
                    job["match_score"] = 0
                    job["match_points"] = ""
                    job["skill_gaps"] = f"评分失败：{e}"
                    job["score_reason"] = f"评分失败：{e}"
                    job["score_tags"] = []
    job["status"] = "待评估"
    job["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    return job


def score_jobs_web(
    on_progress: Optional[Callable[[int, int, str], None]] = None
) -> Dict[str, Any]:
    """Web 可调用入口：对 jobs.json 中所有岗位评分（包括已投递、面试中等）。

    使用硅基流动 API，不接触 Boss直聘，无封号风险。
    返回 {total, scored, failed, top_jobs} 汇总。
    """
    config.ensure_dirs()
    jobs = load_jobs()
    if not jobs:
        return {"total": 0, "scored": 0, "failed": 0, "message": "data/jobs.json 为空，请先采集"}

    resume = load_resume()
    client = config.get_llm_client()

    # 所有岗位均参与评分（含已投递、面试中等），跳过已停招
    pending = [j for j in jobs if j.get("status") not in (None, "已停招", "暂不考虑已结束")]
    if not pending:
        return {"total": len(jobs), "scored": 0, "failed": 0, "message": "没有待评分的岗位"}

    total = len(pending)
    scored = 0
    failed = 0
    for i, job in enumerate(pending, 1):
        if on_progress:
            on_progress(i, total, f"[{i}/{total}] 评分中：{job.get('title', '')} @ {job.get('company', '')}")
        score_job(client, job, resume)
        if job.get("match_score", 0) > 0:
            scored += 1
        else:
            failed += 1

    with open(config.DATA_DIR / "jobs.json", "w", encoding="utf-8") as f:
        json.dump(jobs, f, ensure_ascii=False, indent=2)

    scored_jobs = sorted(
        [j for j in jobs if j.get("match_score", 0) > 0],
        key=lambda x: x.get("match_score", 0),
        reverse=True,
    )
    top = [
        {"title": j.get("title"), "company": j.get("company"), "score": j.get("match_score")}
        for j in scored_jobs[:5]
    ]
    return {"total": total, "scored": scored, "failed": failed, "top_jobs": top}


def run() -> None:
    """CLI 主入口：对 jobs.json 中未评分的岗位进行评分。"""
    result = score_jobs_web(on_progress=lambda i, t, msg: print(msg))
    print(f"\n评分完成：共 {result['scored']} 个成功，{result['failed']} 个失败。")
    if result.get("top_jobs"):
        print("高分岗位 Top 5：")
        for j in result["top_jobs"]:
            print(f"  [{j['score']}] {j['title']} @ {j['company']}")


if __name__ == "__main__":
    try:
        run()
    except Exception as e:
        print(f"评分流程出错：{e}")
        sys.exit(1)

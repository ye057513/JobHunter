"""JobHunter 招呼语生成模块。

为高分岗位生成个性化招呼语（结合岗位 JD 与候选人经历）。
字段对齐 web.py 七步法：使用 match_score 筛选、greeting 存储。
"""
from __future__ import annotations

import json
import sys
import time
from typing import Any, Callable, Dict, List, Optional

import config

GREET_PROMPT = """你是一位求职沟通专家。请为候选人撰写一条发给招聘方的个性化招呼语（打招呼消息）。

【候选人简历】
{resume}

【目标岗位】
职位：{title}
公司：{company}
薪资：{salary}
岗位要求：
{description}

【招呼语要求】
1. 长度 80-150 字，语气自然真诚，不要模板化
2. 开头点出对岗位的兴趣，中间突出与岗位最匹配的 1-2 个经历/技能，结尾表达沟通意愿
3. 不要提薪资、不要过度自夸、不要用感叹号堆砌
4. 直接输出招呼语正文，不要任何前缀或解释
"""


def load_scored_jobs(min_score: int = 60) -> List[Dict[str, Any]]:
    """读取已评分岗位，返回 match_score >= min_score 的岗位。"""
    jobs_path = config.DATA_DIR / "jobs.json"
    if not jobs_path.exists():
        return []
    with open(jobs_path, "r", encoding="utf-8") as f:
        jobs = json.load(f)
    return [j for j in jobs if j.get("match_score", j.get("score", 0)) >= min_score]


def load_resume() -> str:
    """读取简历素材。"""
    return config.load_resume_text()


def generate_greeting(client, job: Dict[str, Any], resume: str) -> str:
    """为单个岗位生成招呼语。"""
    prompt = GREET_PROMPT.format(
        resume=resume[:3000],
        title=job.get("title", ""),
        company=job.get("company", ""),
        salary=job.get("salary", ""),
        description=job.get("description", "")[:1500],
    )
    llm_cfg = config.load_config()["llm"]
    for attempt in range(3):
        try:
            resp = client.chat.completions.create(
                model=llm_cfg["model"],
                messages=[{"role": "user", "content": prompt}],
                temperature=0.7,
            )
            return resp.choices[0].message.content.strip()
        except Exception as e:
            if attempt == 2:
                return f"（招呼语生成失败：{e}）"
            time.sleep(2)
    return ""


def generate_greetings_web(
    min_score: int = 60,
    on_progress: Optional[Callable[[int, int, str], None]] = None
) -> Dict[str, Any]:
    """Web 可调用入口：为高分岗位生成招呼语。

    使用硅基流动 API，不接触 Boss直聘，无封号风险。
    返回 {total, generated, failed, jobs} 汇总。
    """
    config.ensure_dirs()
    jobs = load_scored_jobs(min_score)
    if not jobs:
        return {"total": 0, "generated": 0, "failed": 0, "message": f"没有分数 >= {min_score} 的岗位"}

    resume = load_resume()
    client = config.get_llm_client()

    total = len(jobs)
    generated = 0
    failed = 0
    for i, job in enumerate(jobs, 1):
        if on_progress:
            on_progress(i, total, f"[{i}/{total}] 生成招呼语：{job.get('title', '')} @ {job.get('company', '')}")
        greeting = generate_greeting(client, job, resume)
        job["greeting"] = greeting
        if greeting and not greeting.startswith("（"):
            job["status"] = "待投递"
            generated += 1
        else:
            failed += 1
        job["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")

    # 写回 jobs.json（合并到全量数据）
    all_jobs_path = config.DATA_DIR / "jobs.json"
    if all_jobs_path.exists():
        with open(all_jobs_path, "r", encoding="utf-8") as f:
            all_jobs = json.load(f)
    else:
        all_jobs = []
    job_urls = {j.get("url") for j in jobs}
    for j in all_jobs:
        if j.get("url") in job_urls:
            for updated in jobs:
                if j.get("url") == updated.get("url"):
                    j["greeting"] = updated.get("greeting", "")
                    j["status"] = updated.get("status", j.get("status"))
                    j["updated_at"] = updated.get("updated_at", "")
    with open(all_jobs_path, "w", encoding="utf-8") as f:
        json.dump(all_jobs, f, ensure_ascii=False, indent=2)

    return {
        "total": total,
        "generated": generated,
        "failed": failed,
        "jobs": [{"title": j.get("title"), "company": j.get("company"),
                  "score": j.get("match_score", 0), "greeting": (j.get("greeting") or "")[:80]}
                 for j in jobs],
    }


def run(min_score: int = 60) -> None:
    """CLI 主入口。"""
    result = generate_greetings_web(min_score, on_progress=lambda i, t, msg: print(msg))
    print(f"\n招呼语生成完成：共 {result['generated']} 条成功，{result['failed']} 条失败。")
    for j in result.get("jobs", []):
        print(f"  [{j['score']}] {j['title']} @ {j['company']}")
        print(f"    {j['greeting']}...")


if __name__ == "__main__":
    try:
        run()
    except Exception as e:
        print(f"招呼语生成出错：{e}")
        sys.exit(1)

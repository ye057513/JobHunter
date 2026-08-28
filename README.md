# JobHunter — Boss直聘智能求职 Agent

基于 FastAPI + Playwright 的 Boss直聘求职自动化系统，支持岗位采集、AI 评分筛选、招呼语生成、简历投递全流程管理。

## 功能特性

- **智能采集**：Playwright 自动化抓取 Boss直聘岗位，支持多关键词、多城市、多页采集
- **停招检测**：自动识别已停招岗位（空薪资、乱码薪资、标题关键词等）
- **AI 评分**：调用 LLM API 对岗位进行匹配度评分
- **七步法看板**：拖拽式状态管理（新发现→待评估→简历待优化→待投递→已投递→笔试→面试→等待→Offer）
- **招呼语生成**：基于岗位 JD 和简历生成个性化打招呼文案
- **简历定制**：根据目标岗位自动生成定制简历
- **HR 监听**：自动监听 Boss直聘消息，汇总 HR 回复
- **暗色模式**：全界面支持亮色/暗色主题切换

## 技术栈

- Python 3.10+
- FastAPI + Uvicorn（Web 服务器）
- Playwright（浏览器自动化）
- OpenAI 兼容 API（Ollama / 硅基流动）
- 纯前端 SPA（无外部依赖）

## 安装

```bash
pip install -r requirements.txt
playwright install chromium
```

## 使用

启动服务：
```bash
python -m guard.launch_web
```

浏览器访问 `http://127.0.0.1:8686`，按面板引导完成配置。

## 配置

复制 `config.example.yaml` 并填写：

```bash
cp config.example.yaml config.yaml
```

主要配置项：
- `search.keywords` — 搜索关键词列表
- `search.city` — 目标城市
- `resume.files` — 简历文件路径（支持多份）
- `llm.api_key` — LLM API Key（Ollama 可留空）

## 项目结构

```
JobHunter/
├── src/                  # 核心源码
│   ├── web.py            # FastAPI 主服务 + 前端
│   ├── fetch_jobs.py     # 岗位采集与合并
│   ├── scorer.py         # AI 评分
│   ├── smart_filter.py   # 智能过滤
│   ├── monitor.py        # HR 消息监听
│   ├── resume.py         # 简历定制
│   ├── greeter.py        # 招呼语生成
│   ├── boss_api.py       # Boss直聘 API 交互
│   ├── collector.py      # Playwright 采集器
│   ├── sender.py         # 投递发送
│   └── dashboard.py      # 数据看板
├── guard/                # 启动脚本
│   └── launch_web.py
├── config.yaml           # 配置文件（gitignore，需自行创建）
├── config.example.yaml   # 配置模板
├── SKILL.md              # 技能描述（AI Agent 配置）
└── requirements.txt
```

## 安全说明

- 所有投递需人工确认后发送，无自动投递能力
- API Key 仅存本地，不上传任何服务器
- 自动化操作存在账号封禁风险，请合理控制频率

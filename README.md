# eco-acquire

> 经济学期刊文献智能获取与分析工具 v2.0

自动化检索、下载、分析中国国内经济学核心期刊文献。支持 AI Agent 调用， CLI 和 Python API。

[![Python 3.8+](https://img.shields.io/badge/python-3.8+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Selenium 4](https://img.shields.io/badge/Selenium-4.15+-green.svg)](https://www.selenium.dev/)

## 特性

- **多级容错搜索** — CNKI 不可达时自动切换 Google Scholar
- **多级 PDF 下载** — CNKI → Sci-Hub → Unpaywall → Scholar PDF 逐级降级
- **跨浏览器** — 自动检测 Chrome / Edge / Firefox，零配置驱动管理
- **高级筛选** — 支持按关键词、作者、期刊、年份、精确标题组合检索
- **结论提取** — 从 PDF 自动定位结论段落，生成 100 字精炼摘要
- **路径独立** — 用户数据写到 `~/eco-acquire/`，不依赖安装目录权限
- **AI 友好** — 标准化 JSON 输出，SKILL.md 描述文件，适合 Agent 调用

## 支持期刊

在中国知网开放获取的所有期刊文献

## 快速开始

### 安装

```bash
# 克隆
git clone https://github.com/ww11-max/AI-FOR-ECONOMIST.git
cd eco-acquire

# 安装依赖
pip install -r requirements.txt
```

> 需要 Python 3.8+ 和 Selenium 4.6+（自动管理浏览器驱动，无需手动安装）。
> 系统需安装 Chrome、Edge 或 Firefox 任一浏览器。

### 使用

```bash
# 基本搜索
python run.py --keywords "FDI" --max-results 10

# 组合筛选：某作者在某期刊近三年的文献
python run.py --keywords "数字经济" --author "张三" --journal "世界经济" --year-start 2023

# 精确定位单篇文献
python run.py --exact-title "数字经济对FDI的影响" --author "李四" --journal "金融研究" --year-start 2024

# 仅搜索不下载
python run.py --keywords "绿色金融" --no-download

# 列出支持的期刊
python run.py --list-journals
```

### Python API

```python
from src.workflow import EcoAcquireWorkflow, setup_logging

setup_logging()
wf = EcoAcquireWorkflow()

report = wf.run(
    keywords="FDI",
    journal="世界经济",
    year_start=2023,
    max_results=10,
)
# report["status"]: completed / no_results / error
# report["search_source"]: cnki / google_scholar
# report["articles"]: [{title, authors, journal, year, conclusion, ...}]
```

## 搜索模式

| 参数组合 | 模式 | 说明 |
|---------|------|------|
| `--exact-title` | 精确定位 | 标题 + 作者/期刊/年份 匹配 |
| `--journal`（无关键词） | 期刊导航 | 浏览整本期刊目录 |
| `--author` / `--year-*` / `--journal` + 关键词 | 高级检索 | CNKI 高级检索语法 |
| 仅 `--keywords` | 普通搜索 | 全站关键词搜索 |

## 容错架构

```
搜索阶段：
  CNKI 搜索 ──失败──→ Google Scholar 搜索
      │                      │
      └──成功──→ 继续        └──成功──→ 继续

下载阶段：
  CNKI 下载 ──失败──→ Sci-Hub ──失败──→ Unpaywall ──失败──→ Scholar PDF

每一步失败都记录到 task_report.json，不会中断流程。
```

## 输出

```
~/eco-acquire/                              ← 用户数据目录（可通过 ECO_ACQUIRE_HOME 自定义）
├── config/                                 ← 用户配置
├── logs/                                   ← 运行日志
├── downloads/                              ← PDF 临时缓存
└── outputs/
    └── 04-13-FDI文献/
        ├── task_report.json                ← 完整 JSON 报告
        ├── pdfs/                           ← PDF 文献
        │   ├── 001_FDI对中国制造业影响.pdf
        │   └── 002_数字经济与国际贸易.pdf
        └── report/                         ← 分析报告
            ├── 04-13-FDI文献_results.md
            └── 04-13-FDI文献_results.csv
```

## 命令行参数

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `--keywords` | 搜索关键词（可多个） | - |
| `--journal` | 限定期刊名称 | - |
| `--author` | 按作者筛选 | - |
| `--exact-title` | 精确文章标题 | - |
| `--year-start` | 起始年份（含） | - |
| `--year-end` | 结束年份（含） | - |
| `--max-results` | 最大结果数 | 10 |
| `--browser` | 浏览器：auto/chrome/edge/firefox | auto |
| `--task-name` | 自定义任务名 | 自动生成 |
| `--no-download` | 仅搜索不下载 | false |
| `--no-conclusion` | 不提取结论 | false |
| `--headless` | 无头浏览器模式 | false |

> `--keywords`、`--journal`、`--author`、`--exact-title` 至少提供一个。

## 环境变量

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `ECO_ACQUIRE_HOME` | 用户数据目录 | `~/eco-acquire/` |
| `BROWSER` | 浏览器选择 | `auto` |
| `USE_HEADLESS` | 无头模式 | `false` |
| `ENABLE_FALLBACK` | 启用备用下载 | `true` |
| `ENABLE_SEARCH_FALLBACK` | 启用备用搜索 | `true` |
| `UNPAYWALL_EMAIL` | Unpaywall API 邮箱 | 空 |
| `MAX_RETRIES` | 最大重试次数 | `3` |

## 反检测机制

- 浏览器指纹伪装（禁用 AutomationControlled）
- JS 属性覆写（navigator.webdriver / chrome / plugins）
- UA 轮换（随机版本号）
- 人类行为模拟（随机滚动、点击）
- 验证码感知（检测"拼图校验"并自动重试）

## 项目结构

```
eco-acquire/
├── SKILL.md              # AI Agent Skill 描述
├── README.md             # 本文件
├── LICENSE               # MIT
├── pyproject.toml        # Python 项目配置
├── run.py                # CLI 入口
├── requirements.txt      # 依赖
├── .env.example          # 环境变量模板
├── .gitignore
├── config/
│   ├── __init__.py
│   └── settings.py       # 全局配置（路径、期刊、容错策略）
├── src/
│   ├── __init__.py
│   ├── driver_manager.py # 跨浏览器驱动 + 反检测
│   ├── crawler.py        # CNKI + Google Scholar 爬虫
│   ├── text_extractor.py # PDF 文本提取
│   ├── summary_generator.py  # 结论摘要
│   ├── fallback_downloader.py # Sci-Hub/Unpaywall/Scholar
│   └── workflow.py       # 工作流引擎（容错编排）
├── scripts/              # 辅助脚本
├── outputs/              # 输出目录（仅在开发时使用）
├── downloads/            # 下载缓存（仅在开发时使用）
└── logs/                 # 日志（仅在开发时使用）
```

## 开发

```bash
# 安装开发依赖
pip install -e ".[dev]"

# 代码格式化
black .
ruff check .

# 运行
python run.py --keywords "测试" --max-results 2 --log-level DEBUG
```

## 免责声明

- 仅供个人学术研究使用
- 请遵守相关法律法规和网站使用条款
- 备用下载渠道（Sci-Hub 等）的使用请自行判断法律合规性

## License

[MIT](LICENSE)

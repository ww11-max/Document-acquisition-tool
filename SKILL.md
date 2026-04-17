---
description: "经济学文献题录检索、CNKI知网文献搜索、期刊文献元数据获取、文献摘要提取、经济学核心期刊检索、知网文献查找、学术文献搜索、文献题录导出、文献综述辅助工具、帮我找文献、搜索论文、查找期刊文章、文献检索工具、写文献综述"
---

# Skill: eco-acquire

## 概述

eco-acquire 是一个**经济学文献题录检索工具**，从 CNKI（知网）搜索文献，提取标题、作者、期刊、年份、摘要、关键词、DOI 等元数据，生成结构化报告。

**不下载全文 PDF**，专注于快速、稳定的题录信息获取。

**核心能力**：用户自然语言 → AI 分析生成检索计划 → 自动检索 → 题录报告

---

## AI Agent 工作流（重要！）

### 第一阶段：智能分析用户命令

当用户发出文献检索请求时，AI 必须先分析用户意图，生成结构化检索计划 JSON，然后再执行。

**分析步骤**：

1. **识别检索要素**：从用户自然语言中提取
   - 关键词 / 主题
   - 期刊范围（如有）
   - 年份范围（如有）
   - 作者（如有）
   - 具体文献标题（如有）

2. **确定检索策略**：
   | 用户意图 | strategy | 说明 |
   |---------|----------|------|
   | 主题/关键词广泛搜索 | `keyword` | 找多篇文章 |
   | 搜特定一篇文献 | `title_author` | 标题+作者精确匹配 |
   | 按期刊浏览 | `journal_browse` | 整本期刊检索 |
   | 有 DOI 号 | `doi` | 记录 DOI 信息 |

3. **构造 search_text（关键词策略的核心）**：
   - 从用户描述中提取 2-4 个核心词
   - 去掉虚词（"对""的""与""研究"等）
   - 空格分隔，控制在 10 字以内
   - **不要把期刊名放进 search_text**（journal 字段单独指定）

4. **生成检索计划 JSON**，保存到工作目录

### 第二阶段：执行检索

```bash
python run.py --batch /path/to/search_plan.json --connect 9222
```

可追加参数：
- `--journal "期刊名"` — 全局期刊过滤
- `--year-start YYYY --year-end YYYY` — 全局年份过滤
- `--no-abstract` — 跳过摘要提取（更快）

### 第三阶段：读取报告

执行完成后读取 `task_report.json`，向用户报告结果。

---

## 检索计划 JSON 格式

```json
{
  "task_name": "任务描述",
  "papers": [
    {
      "title": "文献标题（精确搜索时填写）",
      "authors": ["作者1", "作者2"],
      "journal": "期刊名",
      "year": 2024,
      "doi": "",
      "strategy": "keyword",
      "search_text": "核心关键词1 关键词2",
      "notes": "备注"
    }
  ]
}
```

### 字段说明

| 字段 | 必填 | 说明 |
|------|------|------|
| `title` | 精确搜索时必填 | 文章完整标题 |
| `authors` | 否 | 作者列表 |
| `journal` | 否 | 期刊名称（从支持列表中选择） |
| `year` | 否 | 年份（整数） |
| `doi` | 否 | DOI 号 |
| `strategy` | **必填** | 检索策略（见上表） |
| `search_text` | keyword 策略必填 | 核心检索词 |
| `notes` | 否 | 备注信息 |

---

## 前置条件

用户需先启动带调试端口的浏览器：

```
msedge --remote-debugging-port=9222
```

工具会复用用户的浏览器登录态，不会触发验证码。

---

## 输出

执行后在 `~/eco-acquire/outputs/` 下生成：

```
outputs/
└── MM-DD-任务名/
    ├── task_report.json      # 完整任务报告
    └── report/
        ├── *_results.md      # 题录表格 + 摘要详览
        ├── *_results.csv     # CSV 格式数据
        └── *_articles.json   # 原始 JSON 数据
```

---

## 支持的期刊

在CNKI中国知网开放获取的期刊文献

---

## 示例

### 示例1：主题搜索

用户："找2022-2025年世界经济期刊上关于自贸试验区与企业创新的文献"

AI 生成：
```json
{
  "task_name": "世界经济-自贸试验区与企业创新",
  "papers": [
    {
      "title": "自贸试验区与企业创新",
      "strategy": "keyword",
      "search_text": "自贸试验区 企业创新",
      "journal": "世界经济",
      "notes": "2022-2025年"
    }
  ]
}
```

执行：`python run.py --batch plan.json --connect 9222 --year-start 2022 --year-end 2025`

### 示例2：精确搜索特定文献

用户："帮我找赵涛2020年发在《中国工业经济》上关于数字经济的那篇"

AI 生成：
```json
{
  "task_name": "赵涛-数字经济-中国工业经济",
  "papers": [
    {
      "title": "数字经济对全要素生产率的影响",
      "authors": ["赵涛"],
      "journal": "中国工业经济",
      "year": 2020,
      "strategy": "title_author",
      "search_text": "数字经济 全要素生产率 赵涛"
    }
  ]
}
```

执行：`python run.py --batch plan.json --connect 9222`

### 示例3：直接命令行搜索

```bash
python run.py --keywords "绿色金融" --journal "金融研究" --year-start 2023 --connect 9222
```

---

## 版本

v3.0.0 — 纯题录检索模式，移除 PDF 下载

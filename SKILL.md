---
name: eco-acquire
description: "经济学文献题录检索、CNKI知网文献搜索、期刊文献元数据获取、文献摘要提取、经济学核心期刊检索、知网文献查找、学术文献搜索、文献题录导出、文献综述辅助工具、帮我找文献、搜索论文、查找期刊文章、文献检索工具、写文献综述"
---

# Skill: eco-acquire（零配置版）

## 概述

eco-acquire 是一个**经济学文献题录检索工具**，从 CNKI（知网）搜索文献，提取标题、作者、期刊、年份、摘要、关键词、DOI 等元数据，生成结构化报告。

**不下载全文 PDF**，专注于快速、稳定的题录信息获取。

**零配置设计**：首次运行 `--setup` 完成登录后，浏览器 cookie 自动保存。后续使用无需任何手动操作，无需启动调试端口，无需输入密码。

**核心能力**：用户自然语言 → AI 分析生成检索计划 → 自动检索 → 题录报告。**不限制期刊范围**，CNKI 所有可检索期刊均可搜索。

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

**零配置模式（默认，推荐）**：
```bash
python run.py --batch /path/to/search_plan.json
```

**兼容模式（连接已有浏览器）**：
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

## 前置条件（仅需一次）

**首次使用**：运行一次登录设置，后续永久免配：

```bash
python run.py --setup
```

这会：
1. 自动检测系统浏览器（Edge / Chrome / Firefox）
2. 打开 CNKI 知网搜索页面
3. 提示你在此浏览器中完成登录（机构登录/IP登录）
4. 登录状态自动保存到 `~/eco-acquire/browser_profile/`
5. 后续使用自动复用 cookie，无需任何操作

**如果登录超时**，可延长超时时间：
```bash
python run.py --setup --setup-timeout 600
```

**如果想不登录直接搜索**（CNKI 可能触发验证码），使用 `--no-profile`：
```bash
python run.py --keywords "数字经济" --no-profile
```

---

## 输出

执行后在 `~/eco-acquire/outputs/` 下生成：

```
outputs/
└── MM-DD-任务名/
    ├── task_report.json          # 完整任务报告
    └── report/
        ├── *_results.xlsx        # Excel 结构化表格（带样式、冻结窗格、自动筛选）
        ├── *_results.csv         # CSV 逗号分隔格式
        ├── *_results.md          # Markdown 完整题录表格 + 摘要详览
        └── *_articles.json       # 原始 JSON 数据
```

**Excel 表格特性**：
- 蓝色表头 + 白色字体，隔行交替底色
- 冻结首行，自动筛选已开启
- 各列预置合理宽度（标题 50、摘要 60、关键词 30 等）
- 单元格自动换行，适合在 Excel 中直接浏览、筛选、排序

**Markdown 表格特性**：
- 完整字段不截断，含 DOI 可点击链接
- 摘要详览区包含作者、期刊、DOI、关键词、原文链接、完整摘要

---

## 支持的期刊

CNKI 中国知网开放获取的所有期刊文献均可检索，无期刊限制。内置的常用经济学期刊列表（见 `config/settings.py` 中的 `TARGET_JOURNALS`）仅作为期刊名和 ISSN 的参考映射，**不是检索限制**。用户指定任意期刊名均可直接检索。

---

## 示例

### 示例 1：首次设置

```bash
python run.py --setup
```

### 示例 2：主题搜索

用户："找2022-2025年关于自贸试验区与企业创新的文献"

AI 生成：
```json
{
  "task_name": "自贸试验区与企业创新",
  "papers": [
    {
      "strategy": "keyword",
      "search_text": "自贸试验区 企业创新",
      "notes": "2022-2025年"
    }
  ]
}
```

执行（不指定期刊即搜索全部期刊）：
```bash
python run.py --batch plan.json --year-start 2022 --year-end 2025
```

也可指定期刊缩小范围：
```bash
python run.py --batch plan.json --journal "经济研究" --year-start 2022 --year-end 2025
```

### 示例 3：精确搜索特定文献

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

执行：
```bash
python run.py --batch plan.json
```

### 示例 4：直接命令行搜索

```bash
# 零配置模式（默认）
python run.py --keywords "绿色金融" --journal "金融研究" --year-start 2023

# 兼容模式（连接已有浏览器）
python run.py --keywords "绿色金融" --journal "金融研究" --year-start 2023 --connect 9222
```

---

## 命令行参数速查

| 参数 | 说明 |
|------|------|
| `--setup` | 首次使用：交互式登录，登录态永久保存 |
| `--keywords "词1" "词2"` | 搜索关键词 |
| `--journal "期刊名"` | 限定期刊 |
| `--author "姓名"` | 按作者筛选 |
| `--exact-title "标题"` | 精确标题搜索 |
| `--year-start YYYY` | 起始年份 |
| `--year-end YYYY` | 结束年份 |
| `--max-results N` | 最大结果数（默认20） |
| `--batch FILE.json` | AI Planning 模式 |
| `--no-abstract` | 跳过摘要提取（更快） |
| `--headless` | 无头浏览器模式 |
| `--no-profile` | 不保存登录态 |
| `--connect PORT` | 兼容模式：连接已有浏览器 |
| `--list-journals` | 列出支持的期刊 |
| `--browser edge/chrome` | 指定浏览器 |

---

## 版本

v3.1.0 — 零配置版：Profile 持久化登录态，移除手动启动调试端口依赖
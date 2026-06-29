"""
eco-acquire 工作流引擎
编排文献检索、元数据提取、结构化报告输出的完整流程

容错策略：
  1. CNKI搜索 → 失败 → Google Scholar搜索（自动切换）
  2. 每一步失败都记录到 report，不会中断整体流程
"""

import json
import logging
import sys
from pathlib import Path
from datetime import datetime
from typing import List, Dict, Optional, Union

import pandas as pd

from .crawler import CNKICrawler
from .driver_manager import wait_random_time
from config import settings

logger = logging.getLogger(__name__)

__version__ = "3.0.0"


class EcoAcquireWorkflow:
    """经济学文献题录检索工作流"""

    def __init__(self, headless: bool = None, browser: str = None,
                 connect_port: int = None, use_profile: bool = True):
        self.headless = headless if headless is not None else settings.USE_HEADLESS
        self.browser = browser
        self.connect_port = connect_port
        self.use_profile = use_profile and not bool(connect_port)

    # ============================================================
    # AI Planning 模式：批量执行结构化检索计划
    # ============================================================
    def run_batch(self, batch_file: str,
                  extract_abstract: bool = True,
                  connect_port: int = None,
                  global_journal: str = None,
                  global_year_start: int = None,
                  global_year_end: int = None) -> Dict:
        """
        执行 AI Planning 模式：读取 AI 生成的检索计划 JSON，逐条检索并提取元数据。

        AI Agent 调用流程：
        1. AI 分析用户需求，生成检索计划 JSON
        2. 调用此方法执行计划
        3. 读取返回的 report 展示结果

        Args:
            batch_file: 检索计划 JSON 文件路径
            extract_abstract: 是否提取摘要
            connect_port: 浏览器连接端口
            global_journal: 全局期刊过滤
            global_year_start: 全局起始年份
            global_year_end: 全局结束年份

        Returns:
            完整任务报告 dict
        """
        batch_path = Path(batch_file)
        if not batch_path.exists():
            return {"status": "error", "error": f"检索计划文件不存在: {batch_file}"}

        try:
            with open(batch_path, "r", encoding="utf-8") as f:
                batch = json.load(f)
        except json.JSONDecodeError as e:
            return {"status": "error", "error": f"JSON 格式错误: {e}"}

        papers = batch.get("papers", [])
        if not papers:
            return {"status": "error", "error": "检索计划为空（papers 数组无内容）"}

        task_name = batch.get("task_name", "文献检索")

        # 创建任务文件夹
        task_dir, report_dir, task_label = self._create_task_folders(task_name)

        report = {
            "task_name": task_label,
            "task_dir": str(task_dir),
            "report_dir": str(report_dir),
            "start_time": datetime.now().isoformat(),
            "version": __version__,
            "mode": "batch",
            "total_papers": len(papers),
            "params": {"batch_file": str(batch_path)},
            "papers": [],
            "articles": [],
            "success_count": 0,
            "fail_count": 0,
            "search_source": "cnki",
        }

        try:
            with CNKICrawler(
                headless=self.headless,
                download_dir=str(settings.OUTPUTS_DIR),
                browser=self.browser,
                connect_port=connect_port or self.connect_port,
                use_profile=self.use_profile,
            ) as crawler:

                for i, paper in enumerate(papers):
                    logger.info(f"处理第 {i+1}/{len(papers)} 项: "
                                f"{paper.get('title', paper.get('search_text', '未知'))[:40]}...")

                    paper_result = self._execute_single_search(
                        crawler, paper,
                        extract_abstract=extract_abstract,
                        global_journal=global_journal,
                        global_year_start=global_year_start,
                        global_year_end=global_year_end,
                    )

                    report["papers"].append(paper_result)

                    if paper_result["status"] == "found":
                        report["success_count"] += 1
                        # 收集所有文章（keyword 策略可能有多篇）
                        if paper_result.get("all_results"):
                            report["articles"].extend(paper_result["all_results"])
                        elif paper_result.get("article"):
                            report["articles"].append(paper_result["article"])
                    else:
                        report["fail_count"] += 1

                    if i < len(papers) - 1:
                        wait_random_time()

            # 生成输出
            all_articles = report["articles"]
            report["search_count"] = len(all_articles)
            self._generate_outputs(all_articles, report_dir, task_label)

        except Exception as e:
            logger.error(f"批量执行异常: {e}", exc_info=True)
            report["error"] = str(e)
            report["status"] = "error"

        report["end_time"] = datetime.now().isoformat()
        if "status" not in report:
            report["status"] = "completed" if report["success_count"] > 0 else "no_results"
        self._save_report(report, task_dir)
        return report

    def _execute_single_search(self, crawler, paper: Dict,
                                extract_abstract: bool,
                                global_journal: str = None,
                                global_year_start: int = None,
                                global_year_end: int = None) -> Dict:
        """
        执行单条检索任务。

        策略映射：
          - keyword         → search_by_keywords + 返回全部结果
          - title           → search_by_keywords + 相似度匹配最佳
          - title_author    → search_by_keywords + 作者匹配
          - title_journal   → search_by_keywords + 期刊匹配
          - journal_browse  → search_by_journal
          - doi             → 记录 DOI 信息
        """
        title = paper.get("title", "")
        authors = paper.get("authors", [])
        journal = paper.get("journal", "")
        year = paper.get("year")
        doi = paper.get("doi", "")
        strategy = paper.get("strategy", "title")
        search_text = paper.get("search_text", title)
        notes = paper.get("notes", "")

        result = {
            "input_title": title,
            "input_authors": authors,
            "input_journal": journal,
            "input_year": year,
            "strategy": strategy,
            "notes": notes,
            "status": "not_found",
            "article": None,
            "all_results": None,
            "total_results": 0,
        }

        try:
            articles = []

            if strategy == "journal_browse" and journal:
                # journal_browse 策略：尝试从 TARGET_JOURNALS 获取 ISSN
                # 如果指定期刊不在参考列表中，直接用期刊名搜索（无 ISSN）
                journal_info = settings.TARGET_JOURNALS.get(journal, {})
                issn = journal_info.get("issn", "")
                yr = year or datetime.now().year
                articles = crawler.search_by_journal(journal, issn, yr)

            elif strategy == "doi" and doi:
                result["status"] = "skip_search"
                result["message"] = "DOI 模式：已记录待后续查询"
                result["article"] = {
                    "title": title,
                    "authors": ", ".join(authors) if isinstance(authors, list) else authors,
                    "journal": journal,
                    "year": str(year) if year else "",
                    "doi": doi,
                    "link": f"https://doi.org/{doi}",
                }
                return result

            elif strategy == "keyword":
                kw = [search_text] if isinstance(search_text, str) else search_text
                kw_journal = journal or global_journal or ""

                # 安全处理年份
                try:
                    kw_year_s = int(year) if year and int(year) > 0 else global_year_start
                except (ValueError, TypeError):
                    kw_year_s = global_year_start
                try:
                    kw_year_e = int(year) if year and int(year) > 0 else global_year_end
                except (ValueError, TypeError):
                    kw_year_e = global_year_end

                # 从搜索词中移除期刊名
                if kw_journal:
                    kw = [k.replace(kw_journal, "").strip() for k in kw]
                    kw = [k for k in kw if k]
                if not kw:
                    result["status"] = "error"
                    result["message"] = "搜索词为空"
                    return result

                articles = crawler.search_by_keywords(
                    kw, max_results=20,
                    journal_filter=kw_journal,
                    year_start=kw_year_s, year_end=kw_year_e,
                )

            else:
                # title / title_author / title_journal
                kw = [search_text] if isinstance(search_text, str) else search_text
                max_r = paper.get("max_results", 20)
                articles = crawler.search_by_keywords(kw, max_results=max_r, sort_by="relevance")

            if not articles:
                result["status"] = "not_found"
                result["message"] = f"搜索返回空结果 (策略: {strategy})"
                return result

            if strategy == "keyword":
                # keyword 策略：批量提取元数据
                articles = crawler.batch_extract_metadata(
                    articles, extract_abstract=extract_abstract
                )
                result["status"] = "found"
                result["article"] = articles[0]
                result["all_results"] = articles
                result["total_results"] = len(articles)
                return result

            # 非keyword策略：相似度匹配
            best_match = self._find_best_match(
                articles, title=title,
                authors=authors, journal=journal, year=year,
            )

            if best_match:
                result["status"] = "found"
                result["article"] = best_match
                result["match_score"] = best_match.get("_match_score", 0.0)

                # 提取元数据
                if best_match.get("link"):
                    try:
                        meta = crawler._extract_article_meta(best_match["link"])
                        if meta:
                            for k in ("authors", "journal", "year", "keywords", "doi"):
                                if not best_match.get(k) and meta.get(k):
                                    best_match[k] = meta[k]
                        if extract_abstract:
                            abstract = crawler.extract_abstract(best_match["link"])
                            if abstract:
                                best_match["abstract"] = abstract
                    except Exception as e:
                        logger.debug(f"元数据提取失败: {e}")

                best_match.pop("_match_score", None)
            else:
                result["status"] = "not_found"
                result["message"] = f"搜索到 {len(articles)} 条，但无足够匹配"

        except Exception as e:
            result["status"] = "error"
            result["message"] = str(e)
            logger.warning(f"检索失败: {e}")

        return result

    def _find_best_match(self, articles: List[Dict],
                         title: str = "", authors: list = None,
                         journal: str = "", year=None) -> Optional[Dict]:
        """从搜索结果中找到最匹配的文献"""
        if not articles or not title:
            if articles:
                articles[0]["_match_score"] = 1.0
                return articles[0]
            return None

        scored = []
        clean_target = title.replace(" ", "").replace("—", "-").replace("（", "(").replace("）", ")")

        for art in articles:
            score = 0.0
            art_title = art.get("title", "")
            clean_art = art_title.replace(" ", "").replace("—", "-").replace("（", "(").replace("）", ")")

            if art_title and title:
                if clean_target in clean_art or clean_art in clean_target:
                    extra_chars = abs(len(clean_art) - len(clean_target))
                    if extra_chars == 0:
                        title_sim = 1.0
                    elif extra_chars <= 2:
                        ratio = min(len(clean_target), len(clean_art)) / max(len(clean_target), len(clean_art), 1)
                        title_sim = 0.8 + 0.15 * ratio
                    else:
                        longer = max(len(clean_target), len(clean_art))
                        penalty = max(0.2, 1.0 - (extra_chars / (longer * 0.5)))
                        ratio = min(len(clean_target), len(clean_art)) / max(len(clean_target), len(clean_art), 1)
                        title_sim = 0.4 * penalty + 0.2 * ratio
                else:
                    len_ratio = min(len(clean_target), len(clean_art)) / max(len(clean_target), len(clean_art), 1)
                    common = sum(1 for c in clean_target if c in clean_art)
                    char_sim = common / max(len(clean_target), 1)
                    title_sim = 0.4 * len_ratio + 0.6 * char_sim
                score += 0.6 * title_sim

            if authors and art.get("authors"):
                art_authors = art["authors"]
                if isinstance(art_authors, str):
                    art_authors = [a.strip() for a in art_authors.replace(",", "、").split("、") if a.strip()]
                if isinstance(authors, str):
                    authors = [a.strip() for a in authors.replace(",", "、").split("、") if a.strip()]
                if art_authors and authors:
                    overlap = sum(1 for a in authors if any(a in aa or aa in a for aa in art_authors))
                    author_sim = overlap / len(authors)
                    score += 0.2 * author_sim

            if journal and art.get("journal"):
                if journal in art["journal"] or art["journal"] in journal:
                    score += 0.1

            if year and art.get("year"):
                if str(year) in str(art["year"]) or str(art["year"]) == str(year):
                    score += 0.1

            art["_match_score"] = round(score, 3)
            scored.append((score, art))

        scored.sort(key=lambda x: x[0], reverse=True)
        best_score, best = scored[0] if scored else (0, None)

        if title and best_score < 0.4:
            return None
        return best

    # ============================================================
    # 主工作流入口（直接搜索模式）
    # ============================================================
    def run(self,
            keywords: Union[str, List[str]] = None,
            journal: str = None,
            author: str = None,
            exact_title: str = None,
            year_start: int = None,
            year_end: int = None,
            max_results: int = 20,
            extract_abstract: bool = True,
            task_name: str = None) -> Dict:
        """
        执行完整检索工作流。

        Args:
            keywords: 搜索关键词
            journal: 限定期刊
            author: 按作者筛选
            exact_title: 精确标题
            year_start: 起始年份
            year_end: 结束年份
            max_results: 最大结果数
            extract_abstract: 是否提取摘要
            task_name: 自定义任务名
        """
        task_dir, report_dir, task_label = self._create_task_folders(task_name)

        report = {
            "task_name": task_label,
            "task_dir": str(task_dir),
            "report_dir": str(report_dir),
            "start_time": datetime.now().isoformat(),
            "version": __version__,
            "params": {
                "keywords": keywords, "journal": journal, "author": author,
                "exact_title": exact_title, "year_start": year_start,
                "year_end": year_end, "max_results": max_results,
            },
            "articles": [],
            "search_source": "",
            "success_count": 0,
            "fail_count": 0,
        }

        try:
            # 搜索
            articles, search_source = self._search_with_fallback(
                keywords=keywords, journal=journal, author=author,
                exact_title=exact_title, year_start=year_start,
                year_end=year_end, max_results=max_results,
            )
            report["articles"] = articles
            report["search_count"] = len(articles)
            report["search_source"] = search_source

            if not articles:
                if search_source == "none":
                    report["status"] = "search_failed"
                    report["error"] = "所有搜索渠道均失败"
                else:
                    report["status"] = "no_results"
                    report["error"] = "未找到匹配文献"
                self._save_report(report, task_dir)
                return report

            logger.info(f"检索到 {len(articles)} 篇 (来源: {search_source})")

            # 提取元数据（CNKI 来源）
            if search_source == "cnki" and extract_abstract:
                try:
                    with CNKICrawler(
                        headless=self.headless,
                        download_dir=str(settings.OUTPUTS_DIR),
                        browser=self.browser,
                        connect_port=self.connect_port,
                        use_profile=self.use_profile,
                    ) as crawler:
                        articles = crawler.batch_extract_metadata(articles, extract_abstract=True)
                        report["articles"] = articles
                except Exception as e:
                    logger.warning(f"元数据提取阶段异常: {e}")

            # 生成输出
            self._generate_outputs(articles, report_dir, task_label)

        except Exception as e:
            logger.error(f"工作流出错: {e}", exc_info=True)
            report["error"] = str(e)
            report["status"] = "error"

        report["end_time"] = datetime.now().isoformat()
        if "status" not in report:
            report["status"] = "completed"
        report["success_count"] = len(articles)
        self._save_report(report, task_dir)
        return report

    # ============================================================
    # 搜索容错：CNKI → Google Scholar
    # ============================================================
    def _search_with_fallback(self, keywords, journal, author,
                               exact_title, year_start, year_end,
                               max_results) -> tuple:
        """多级搜索容错。Returns: (articles, source_name)"""
        try:
            with CNKICrawler(headless=self.headless,
                             download_dir=str(settings.OUTPUTS_DIR),
                             browser=self.browser,
                             connect_port=self.connect_port,
                             use_profile=self.use_profile) as crawler:
                try:
                    articles = self._cnki_search(
                        crawler, keywords, journal, author,
                        exact_title, year_start, year_end, max_results
                    )
                    if articles:
                        return articles, "cnki"
                except Exception as e:
                    logger.warning(f"CNKI搜索失败: {e}")

                if settings.ENABLE_SEARCH_FALLBACK:
                    logger.info("切换到 Google Scholar...")
                    gs_keywords = keywords or exact_title or journal
                    if gs_keywords:
                        try:
                            articles = crawler.search_google_scholar(
                                keywords=gs_keywords, max_results=max_results,
                                author=author or "",
                                year_start=year_start, year_end=year_end,
                            )
                            if articles:
                                return articles, "google_scholar"
                        except Exception as e:
                            logger.warning(f"Google Scholar搜索失败: {e}")

        except Exception as e:
            logger.error(f"搜索阶段异常: {e}")

        return [], "none"

    def _cnki_search(self, crawler, keywords, journal, author,
                      exact_title, year_start, year_end, max_results):
        """执行CNKI搜索"""
        if exact_title:
            if year_start and year_end and (year_end - year_start) > 1:
                return crawler.search_advanced(
                    keywords=exact_title, author=author,
                    journal=journal, year_start=year_start,
                    year_end=year_end, max_results=max_results,
                )
            return crawler.search_exact(
                title=exact_title, author=author,
                journal=journal, year=year_end or year_start,
            )

        elif journal and not keywords and not author:
            # 纯期刊浏览：尝试从参考列表获取 ISSN，没有则直接用期刊名
            journal_info = settings.TARGET_JOURNALS.get(journal, {})
            issn = journal_info.get("issn", "")
            if year_start or year_end:
                y_start = year_start or 2000
                y_end = year_end or datetime.now().year
                all_results = []
                for yr in range(y_start, y_end + 1):
                    all_results.extend(crawler.search_by_journal(journal, issn, yr))
                return all_results[:max_results]
            return crawler.search_by_journal(journal, issn, year_end or year_start or datetime.now().year)[:max_results]

        elif author or (year_start or year_end) or journal:
            return crawler.search_advanced(
                keywords=keywords, author=author, journal=journal,
                year_start=year_start, year_end=year_end, max_results=max_results,
            )

        elif keywords:
            if isinstance(keywords, str):
                keywords = [keywords]
            return crawler.search_by_keywords(
                keywords, max_results,
                journal_filter=journal or "", author_filter=author or "",
                year_start=year_start, year_end=year_end,
            )

        return []

    # ============================================================
    # 任务文件夹与输出
    # ============================================================
    def _create_task_folders(self, task_name: str = None):
        """创建任务文件夹（不再需要 pdfs/ 子目录）"""
        now = datetime.now()
        if task_name:
            label = f"{now.strftime('%m/%d')}-{task_name}"
        else:
            label = f"{now.strftime('%m/%d')}-文献检索"

        task_dir = settings.OUTPUTS_DIR / label.replace("/", "\\")
        report_dir = task_dir / "report"

        report_dir.mkdir(parents=True, exist_ok=True)
        logger.info(f"任务文件夹: {task_dir}")
        return task_dir, report_dir, label

    # ============================================================
    # 输出生成
    # ============================================================
    def _generate_outputs(self, articles: List[Dict], report_dir: Path,
                           task_label: str):
        """生成结构化报告：Markdown + CSV + Excel + JSON"""
        report_dir.mkdir(parents=True, exist_ok=True)
        safe_label = task_label.replace('/', '_')

        # Markdown
        md_path = report_dir / f"{safe_label}_results.md"
        md_content = self._build_markdown(articles, task_label)
        md_path.write_text(md_content, encoding="utf-8")
        logger.info(f"Markdown报告: {md_path}")

        # CSV
        csv_path = report_dir / f"{safe_label}_results.csv"
        self._build_csv(articles, csv_path)
        logger.info(f"CSV表格: {csv_path}")

        # Excel (.xlsx)
        xlsx_path = report_dir / f"{safe_label}_results.xlsx"
        self._build_excel(articles, xlsx_path, task_label)
        logger.info(f"Excel表格: {xlsx_path}")

        # JSON
        json_path = report_dir / f"{safe_label}_articles.json"
        json_path.write_text(
            json.dumps(articles, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8"
        )
        logger.info(f"JSON数据: {json_path}")

    def _build_markdown(self, articles: List[Dict], title: str) -> str:
        """构建完整题录表格 Markdown（不截断字段）"""
        lines = [f"# {title}\n",
                 f"> 生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"]

        total = len(articles)
        with_abstract = sum(1 for a in articles if a.get("abstract"))
        with_doi = sum(1 for a in articles if a.get("doi"))
        lines.append(f"**总计**: {total} 篇 | **含摘要**: {with_abstract} 篇 | **含DOI**: {with_doi} 篇\n\n")

        # 完整题录表格（不截断）
        lines.append("| # | 标题 | 作者 | 期刊 | 年份 | 关键词 | DOI |")
        lines.append("|---|------|------|------|------|--------|-----|")

        for i, a in enumerate(articles, 1):
            doi = a.get("doi", "")
            doi_cell = f"[{doi}](https://doi.org/{doi})" if doi else ""
            lines.append(
                f"| {i} | {a.get('title', '')} | {a.get('authors', '')} "
                f"| {a.get('journal', '')} | {a.get('year', '')} "
                f"| {a.get('keywords', '')} | {doi_cell} |"
            )

        # 摘要详览
        lines.append("\n## 摘要详览\n")
        for i, a in enumerate(articles, 1):
            lines.append(f"### {i}. {a.get('title', '未知')}\n")
            lines.append(f"- **作者**: {a.get('authors', '未知')}")
            lines.append(f"- **期刊**: {a.get('journal', '未知')} ({a.get('year', '未知')})")
            if a.get("doi"):
                lines.append(f"- **DOI**: [{a['doi']}](https://doi.org/{a['doi']})")
            if a.get("keywords"):
                lines.append(f"- **关键词**: {a['keywords']}")
            if a.get("link"):
                lines.append(f"- **链接**: {a['link']}")
            if a.get("abstract"):
                lines.append(f"- **摘要**: {a['abstract']}\n")
            else:
                lines.append("")

        return "\n".join(lines)

    def _build_csv(self, articles: List[Dict], csv_path: Path):
        if not articles:
            return
        rows = []
        for i, a in enumerate(articles, 1):
            rows.append({
                "序号": i,
                "标题": a.get("title", ""),
                "作者": a.get("authors", ""),
                "期刊": a.get("journal", ""),
                "年份": a.get("year", ""),
                "关键词": a.get("keywords", ""),
                "DOI": a.get("doi", ""),
                "链接": a.get("link", ""),
                "摘要": a.get("abstract", ""),
                "搜索来源": a.get("source", ""),
            })
        df = pd.DataFrame(rows)
        df.to_csv(csv_path, index=False, encoding="utf-8-sig")

    def _build_excel(self, articles: List[Dict], xlsx_path: Path, title: str):
        """生成结构化 Excel 表格（带样式、冻结窗格、自动筛选、合理列宽）"""
        if not articles:
            return
        try:
            from openpyxl import Workbook
            from openpyxl.styles import Font, Alignment, PatternFill, Border, Side, numbers
            from openpyxl.utils import get_column_letter
        except ImportError:
            logger.warning("openpyxl 未安装，跳过 Excel 输出。请运行: pip install openpyxl")
            return

        wb = Workbook()
        ws = wb.active
        ws.title = "题录检索结果"

        # 列定义
        columns = [
            ("序号", 6),
            ("标题", 50),
            ("作者", 20),
            ("期刊", 18),
            ("年份", 8),
            ("关键词", 30),
            ("DOI", 28),
            ("链接", 40),
            ("摘要", 60),
            ("搜索来源", 12),
        ]

        # 样式定义
        header_font = Font(name="微软雅黑", size=11, bold=True, color="FFFFFF")
        header_fill = PatternFill(start_color="2F5496", end_color="2F5496", fill_type="solid")
        header_alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell_font = Font(name="微软雅黑", size=10)
        cell_alignment = Alignment(vertical="top", wrap_text=True)
        thin_border = Border(
            left=Side(style="thin", color="B4C6E7"),
            right=Side(style="thin", color="B4C6E7"),
            top=Side(style="thin", color="B4C6E7"),
            bottom=Side(style="thin", color="B4C6E7"),
        )
        even_fill = PatternFill(start_color="D6E4F0", end_color="D6E4F0", fill_type="solid")

        # 写表头
        for col_idx, (col_name, col_width) in enumerate(columns, 1):
            cell = ws.cell(row=1, column=col_idx, value=col_name)
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = header_alignment
            cell.border = thin_border
            ws.column_dimensions[get_column_letter(col_idx)].width = col_width

        # 写数据行
        for row_idx, a in enumerate(articles):
            excel_row = row_idx + 2
            values = [
                row_idx + 1,
                a.get("title", ""),
                a.get("authors", ""),
                a.get("journal", ""),
                a.get("year", ""),
                a.get("keywords", ""),
                a.get("doi", ""),
                a.get("link", ""),
                a.get("abstract", ""),
                a.get("source", ""),
            ]
            for col_idx, val in enumerate(values, 1):
                cell = ws.cell(row=excel_row, column=col_idx, value=val)
                cell.font = cell_font
                cell.alignment = cell_alignment
                cell.border = thin_border
                if row_idx % 2 == 0:
                    cell.fill = even_fill

        # 冻结首行
        ws.freeze_panes = "A2"

        # 自动筛选
        ws.auto_filter.ref = f"A1:{get_column_letter(len(columns))}{len(articles) + 1}"

        # 行高
        ws.row_dimensions[1].height = 28
        for r in range(2, len(articles) + 2):
            ws.row_dimensions[r].height = 45

        wb.save(xlsx_path)

    def _save_report(self, report: Dict, task_dir: Path):
        report_path = task_dir / "task_report.json"
        with open(report_path, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2, default=str)
        logger.info(f"任务报告: {report_path}")


# ============================================================
# 日志配置
# ============================================================
def setup_logging(log_level: str = None):
    """配置日志系统"""
    level = log_level or settings.LOG_LEVEL
    settings.LOGS_DIR.mkdir(parents=True, exist_ok=True)
    log_file = settings.LOGS_DIR / f"eco_acquire_{datetime.now().strftime('%Y%m%d')}.log"

    logging.basicConfig(
        level=getattr(logging, level.upper()),
        format=settings.LOG_FORMAT,
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )

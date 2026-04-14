"""
eco-acquire 工作流引擎
编排完整的文献获取、下载、分析、输出流程

容错策略：
  1. CNKI搜索 → 失败 → Google Scholar搜索（自动切换）
  2. CNKI下载 → 失败 → Sci-Hub → Unpaywall → Google Scholar PDF（逐级降级）
  3. 每一步失败都记录到 report，不会中断整体流程
"""

import json
import logging
import sys
from pathlib import Path
from datetime import datetime
from typing import List, Dict, Optional, Union

import pandas as pd

from .crawler import CNKICrawler
from .text_extractor import PDFTextExtractor
from .summary_generator import SummaryGenerator
from .fallback_downloader import FallbackDownloader
from .driver_manager import BrowserManager, wait_random_time
from config import settings

logger = logging.getLogger(__name__)


class EcoAcquireWorkflow:
    """经济学期刊文献智能获取工作流"""

    def __init__(self, headless: bool = None, browser: str = None,
                 connect_port: int = None):
        self.headless = headless if headless is not None else settings.USE_HEADLESS
        self.browser = browser
        self.connect_port = connect_port
        self.extractor = PDFTextExtractor()
        self.summary_gen = SummaryGenerator()

    # ============================================================
    # AI Planning 模式：批量执行结构化文献清单
    # ============================================================
    def run_batch(self, batch_file: str,
                  download: bool = True,
                  extract_conclusion: bool = True,
                  connect_port: int = None,
                  global_journal: str = None,
                  global_year_start: int = None,
                  global_year_end: int = None) -> Dict:
        """
        执行 AI Planning 模式：读取结构化文献清单，逐条搜索、下载、提取结论。

        AI Agent 调用流程：
        1. AI 分析用户需求，生成文献清单 JSON 文件
        2. 调用此方法执行清单
        3. 读取返回的 report 展示结果

        Args:
            batch_file: 文献清单 JSON 文件路径
            download: 是否下载 PDF
            extract_conclusion: 是否提取结论
            connect_port: 浏览器连接端口（--connect 模式）
            global_journal: 全局期刊过滤（来自 CLI --journal）
            global_year_start: 全局起始年份（来自 CLI --year-start）
            global_year_end: 全局结束年份（来自 CLI --year-end）

        Returns:
            完整任务报告 dict
        """
        batch_path = Path(batch_file)
        if not batch_path.exists():
            return {
                "status": "error",
                "error": f"文献清单文件不存在: {batch_file}",
            }

        try:
            with open(batch_path, "r", encoding="utf-8") as f:
                batch = json.load(f)
        except json.JSONDecodeError as e:
            return {
                "status": "error",
                "error": f"文献清单 JSON 格式错误: {e}",
            }

        papers = batch.get("papers", [])
        if not papers:
            return {"status": "error", "error": "文献清单为空（papers 数组无内容）"}

        task_name = batch.get("task_name", "批量文献获取")

        # 创建任务文件夹
        task_dir, pdf_dir, report_dir, task_label = self._create_task_folders(task_name)

        report = {
            "task_name": task_label,
            "task_dir": str(task_dir),
            "pdf_dir": str(pdf_dir),
            "report_dir": str(report_dir),
            "start_time": datetime.now().isoformat(),
            "version": "2.2.0",
            "mode": "batch",
            "total_papers": len(papers),
            "params": {"batch_file": str(batch_path)},
            "papers": [],          # 每篇的执行结果
            "articles": [],        # 成功获取的文献详情
            "success_count": 0,
            "fail_count": 0,
            "search_source": "cnki",
        }

        try:
            # 统一创建一个 crawler 实例复用（减少浏览器创建开销）
            with CNKICrawler(
                headless=self.headless,
                download_dir=str(pdf_dir),
                browser=self.browser,
                connect_port=connect_port or self.connect_port,
            ) as crawler:

                for i, paper in enumerate(papers):
                    logger.info(f"处理第 {i+1}/{len(papers)} 篇: "
                                f"{paper.get('title', '未知')[:40]}...")

                    paper_result = self._execute_single_paper(
                        crawler, paper, pdf_dir,
                        download, extract_conclusion,
                        global_journal=global_journal,
                        global_year_start=global_year_start,
                        global_year_end=global_year_end,
                    )

                    report["papers"].append(paper_result)

                    if paper_result["status"] == "found":
                        report["success_count"] += 1
                        report["articles"].append(paper_result["article"])
                    else:
                        report["fail_count"] += 1

                    # 请求间隔，避免触发反爬
                    if i < len(papers) - 1:
                        wait_random_time()

            # 备用下载（CNKI 下载失败的）
            cnki_failed = [
                p["article"] for p in report["papers"]
                if p["status"] == "found" and not p.get("downloaded", False)
                and p.get("article")
            ]
            if cnki_failed and download and settings.ENABLE_FALLBACK:
                logger.info(f"启动备用下载渠道，尝试获取 {len(cnki_failed)} 篇...")
                try:
                    with FallbackDownloader(download_dir=str(pdf_dir)) as fb:
                        fb_results = fb.batch_download(cnki_failed)
                    report["fallback_success"] = fb_results["success"]
                    report["fallback_failed"] = fb_results["failed"]
                except Exception as e:
                    logger.error(f"备用下载异常: {e}")
                    report["fallback_error"] = str(e)

            # 结论提取（PDF 下载成功后）
            if extract_conclusion and download and report["articles"]:
                logger.info("开始批量提取结论摘要...")
                report["articles"] = self.summary_gen.batch_generate(
                    report["articles"], str(pdf_dir)
                )

            # 生成输出
            report["search_count"] = len(report["articles"])
            self._generate_outputs(report["articles"], report_dir, task_label)

        except Exception as e:
            logger.error(f"批量执行异常: {e}", exc_info=True)
            report["error"] = str(e)
            report["status"] = "error"

        report["end_time"] = datetime.now().isoformat()
        if "status" not in report:
            report["status"] = "completed" if report["success_count"] > 0 else "no_results"
        self._save_report(report, task_dir)
        return report

    def _execute_single_paper(self, crawler, paper: Dict, pdf_dir: Path,
                               download: bool, extract_conclusion: bool,
                               global_journal: str = None,
                               global_year_start: int = None,
                               global_year_end: int = None) -> Dict:
        """
        执行单篇文献的搜索+下载。

        AI 推荐的 strategy 映射到 crawler 方法：
          - title           → search_by_keywords([title])
          - title_author    → search_by_keywords([title]) + 客户端作者匹配
          - title_journal   → search_by_keywords([title]) + 客户端期刊匹配
          - journal_browse  → search_by_journal(journal, year)
          - keyword         → search_by_keywords([keyword])
          - doi             → 直接尝试下载（如果 DOI 可用）

        global_journal/year_start/year_end 来自 CLI 参数，优先级低于 paper 字段。
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
            "downloaded": False,
            "match_score": 0.0,
        }

        try:
            # ---- 根据策略执行搜索 ----
            articles = []

            if strategy == "journal_browse" and journal:
                journal_info = settings.TARGET_JOURNALS.get(journal, {})
                issn = journal_info.get("issn", "")
                yr = year or datetime.now().year
                articles = crawler.search_by_journal(journal, issn, yr)

            elif strategy == "doi" and doi:
                # DOI 模式：直接构建下载链接
                result["status"] = "skip_search"
                result["message"] = "DOI模式暂不支持直接下载，已记录待后续处理"
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
                # 使用 paper 字段优先，回退到 CLI 全局参数
                kw_journal = journal or global_journal or ""
                kw_year_s = year if year and year > 0 else global_year_start
                kw_year_e = year if year and year > 0 else global_year_end
                # 从搜索词中移除期刊名（避免重复过滤导致搜不到）
                if kw_journal:
                    kw = [k.replace(kw_journal, "").strip() for k in kw]
                    kw = [k for k in kw if k]  # 移除空字符串
                if not kw:
                    result["status"] = "error"
                    result["message"] = "搜索词为空（可能被期刊名清理后无剩余关键词）"
                    return result
                articles = crawler.search_by_keywords(
                    kw, max_results=20,  # keyword 策略给更多结果供 AI 筛选
                    journal_filter=kw_journal,
                    year_start=kw_year_s, year_end=kw_year_e,
                )

            else:
                # title / title_author / title_journal → 用标题搜索，客户端匹配
                kw = [search_text] if isinstance(search_text, str) else search_text
                max_r = paper.get("max_results", 20)

                # 对标题搜索策略，使用相关度排序（korder=SU）以提高目标论文命中率
                articles = crawler.search_by_keywords(
                    kw, max_results=max_r, sort_by="relevance"
                )

            if not articles:
                result["status"] = "not_found"
                result["message"] = f"搜索返回空结果 (策略: {strategy})"
                return result

            if strategy == "keyword":
                # keyword 策略：直接返回所有搜索结果，不做模糊匹配
                result["status"] = "found"
                result["article"] = articles[0]  # 存储第一条作为代表
                result["match_score"] = 1.0
                result["all_results"] = articles  # 存储全部结果
                result["total_results"] = len(articles)
                return result

            # ---- 非keyword策略：客户端相似度匹配 ----
            best_match = self._find_best_match(
                articles, title=title,
                authors=authors, journal=journal, year=year,
            )

            if best_match:
                result["status"] = "found"
                result["article"] = best_match
                result["match_score"] = best_match.get("_match_score", 0.0)

                # 提取元数据（CNKI 来源）
                if best_match.get("link"):
                    try:
                        meta = crawler._extract_article_meta(best_match["link"])
                        if meta:
                            for k in ("authors", "journal", "year"):
                                if not best_match.get(k) and meta.get(k):
                                    best_match[k] = meta[k]
                        if extract_conclusion:
                            abstract = crawler.extract_abstract(best_match["link"])
                            if abstract:
                                best_match["abstract"] = abstract
                    except Exception as e:
                        logger.debug(f"元数据提取失败: {e}")

                # 下载 PDF
                if download and best_match.get("link"):
                    try:
                        dl_ok = crawler._download_single_article(
                            best_match, "pdf", str(pdf_dir)
                        )
                        result["downloaded"] = dl_ok
                    except Exception as e:
                        logger.debug(f"下载失败: {e}")

                # 清理内部字段
                best_match.pop("_match_score", None)
            else:
                result["status"] = "not_found"
                result["message"] = (f"搜索到 {len(articles)} 条结果，"
                                     f"但无足够相似匹配 (最佳相似度 < 0.4)")

        except Exception as e:
            result["status"] = "error"
            result["message"] = str(e)
            logger.warning(f"处理文献 '{title[:30]}...' 失败: {e}")

        return result

    def _find_best_match(self, articles: List[Dict],
                         title: str = "", authors: list = None,
                         journal: str = "", year=None) -> Optional[Dict]:
        """
        从搜索结果中找到最匹配的文献（模糊匹配，非精确匹配）。

        匹配逻辑：
        1. 标题相似度（核心权重，0.6）
        2. 作者重叠度（加分项，0.2）
        3. 期刊匹配（加分项，0.1）
        4. 年份匹配（加分项，0.1）

        优先级：完全包含 > 高覆盖率 > 子串部分匹配 > 低覆盖率

        Returns:
            最佳匹配的 article dict（附加 _match_score 字段），或 None
        """
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

            # 1. 标题相似度（权重 0.6）
            if art_title and title:
                # 完全包含：目标标题是搜索结果标题的子串（或反过来）
                if clean_target in clean_art or clean_art in clean_target:
                    extra_chars = abs(len(clean_art) - len(clean_target))
                    if extra_chars == 0:
                        # 完全匹配
                        title_sim = 1.0
                    elif extra_chars <= 2:
                        # 极少量差异（如标点、空格）
                        ratio = min(len(clean_target), len(clean_art)) / max(len(clean_target), len(clean_art), 1)
                        title_sim = 0.8 + 0.15 * ratio
                    else:
                        # 包含关系但有多余字符——不同论文的可能性高
                        # 差异越大扣分越狠
                        longer = max(len(clean_target), len(clean_art))
                        penalty = max(0.2, 1.0 - (extra_chars / (longer * 0.5)))
                        ratio = min(len(clean_target), len(clean_art)) / max(len(clean_target), len(clean_art), 1)
                        title_sim = 0.4 * penalty + 0.2 * ratio
                else:
                    # 不包含，用覆盖率+公共字符比例
                    len_ratio = min(len(clean_target), len(clean_art)) / max(len(clean_target), len(clean_art), 1)
                    common = sum(1 for c in clean_target if c in clean_art)
                    char_sim = common / max(len(clean_target), 1)
                    title_sim = 0.4 * len_ratio + 0.6 * char_sim

                score += 0.6 * title_sim

            # 2. 作者重叠（权重 0.2）
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

            # 3. 期刊匹配（权重 0.1）
            if journal and art.get("journal"):
                if journal in art["journal"] or art["journal"] in journal:
                    score += 0.1

            # 4. 年份匹配（权重 0.1）
            if year and art.get("year"):
                art_year = str(art["year"])
                if str(year) in art_year or art_year == str(year):
                    score += 0.1

            art["_match_score"] = round(score, 3)
            scored.append((score, art))

        # 按分数降序排列
        scored.sort(key=lambda x: x[0], reverse=True)

        # Debug: 输出前5名的匹配分数
        if scored:
            logger.debug(f"匹配排名 (目标: {title[:30]}):")
            for rank, (s, a) in enumerate(scored[:5], 1):
                logger.debug(f"  #{rank} score={s:.3f} | {a.get('title', '')[:40]}")

        best_score, best = scored[0] if scored else (0, None)

        # 阈值：有标题搜索时，至少 0.4 才认为匹配
        if title and best_score < 0.4:
            return None

        return best

    # ============================================================
    # 主工作流入口
    # ============================================================
    def run(self,
            keywords: Union[str, List[str]] = None,
            journal: str = None,
            author: str = None,
            exact_title: str = None,
            year_start: int = None,
            year_end: int = None,
            max_results: int = 20,
            download: bool = True,
            extract_conclusion: bool = True,
            task_name: str = None) -> Dict:
        """
        执行完整工作流（含多级容错）。

        Args:
            keywords: 搜索关键词（字符串或列表）
            journal: 限定期刊名称（可选）
            author: 按作者筛选（可选）
            exact_title: 精确文章标题（用于定位单篇文献）
            year_start: 起始年份
            year_end: 结束年份
            max_results: 最大结果数
            download: 是否下载PDF
            extract_conclusion: 是否提取结论
            task_name: 自定义任务名（默认自动生成）

        Returns:
            完整任务报告 dict，包含搜索结果、下载状态、错误信息等。
            AI Agent 应检查 report["status"] 和 report["error"] 字段。
        """
        # 1. 创建任务文件夹
        task_dir, pdf_dir, report_dir, task_label = self._create_task_folders(task_name)

        report = self._init_report(task_dir, pdf_dir, report_dir, task_label,
                                    keywords, journal, author, exact_title,
                                    year_start, year_end, max_results)

        try:
            # 2. 搜索文献（含 CNKI 容错）
            articles, search_source = self._search_with_fallback(
                crawler_keywords=keywords,
                journal=journal,
                author=author,
                exact_title=exact_title,
                year_start=year_start,
                year_end=year_end,
                max_results=max_results,
            )
            report["articles"] = articles
            report["search_count"] = len(articles)
            report["search_source"] = search_source

            if not articles:
                if search_source == "none":
                    report["status"] = "search_failed"
                    report["error"] = ("所有搜索渠道均失败：CNKI不可达（可能需要验证码或网络受限），"
                                        "Google Scholar也不可达。建议用户检查网络环境，"
                                        "或通过校园网/VPN访问CNKI。")
                else:
                    report["status"] = "no_results"
                    report["error"] = (f"搜索渠道 {search_source} 可达，但未找到匹配的文献。"
                                        "请检查搜索条件是否正确，或尝试放宽筛选条件。")
                self._save_report(report, task_dir)
                return report

            logger.info(f"搜索到 {len(articles)} 篇文献 (来源: {search_source})")

            # 3. 提取页面信息（摘要+元数据）
            self._extract_metadata(articles, extract_conclusion, search_source)

            # 4. 下载PDF（含多级容错）
            if download and articles:
                self._download_with_fallback(articles, max_results, pdf_dir, report)

            # 5. 提取结论摘要
            if extract_conclusion and download:
                logger.info("开始提取结论摘要...")
                articles = self.summary_gen.batch_generate(articles, str(pdf_dir))

            # 6. 生成输出
            report["articles"] = articles
            self._generate_outputs(articles, report_dir, task_label)

        except Exception as e:
            logger.error(f"工作流执行出错: {e}", exc_info=True)
            report["error"] = str(e)
            report["status"] = "error"

        report["end_time"] = datetime.now().isoformat()
        if "status" not in report:
            report["status"] = "completed"
        self._save_report(report, task_dir)
        return report

    # ============================================================
    # 搜索容错：CNKI → Google Scholar
    # ============================================================
    def _search_with_fallback(self, crawler_keywords, journal, author,
                               exact_title, year_start, year_end,
                               max_results) -> tuple:
        """
        多级搜索容错（无前置检测，直接尝试搜索）。
        Returns: (articles, source_name)
        """
        try:
            with CNKICrawler(headless=self.headless,
                             download_dir=str(settings.DOWNLOADS_DIR),
                             browser=self.browser,
                             connect_port=self.connect_port) as crawler:

                # 直接尝试CNKI搜索，不做前置可达性检测
                # （前置检测误判率太高：CNKI页面含"错误"/"验证码"等词会误判为不可达）
                cnki_error = None
                try:
                    articles = self._cnki_search(
                        crawler, crawler_keywords, journal, author,
                        exact_title, year_start, year_end, max_results
                    )
                    if articles:
                        return articles, "cnki"
                    logger.warning("CNKI搜索返回空结果")
                except Exception as e:
                    cnki_error = str(e)
                    logger.warning(f"CNKI搜索失败: {e}")

                # CNKI失败 → Google Scholar
                if settings.ENABLE_SEARCH_FALLBACK:
                    logger.info("CNKI搜索无结果，切换到 Google Scholar...")
                    gs_keywords = crawler_keywords or exact_title or journal
                    if gs_keywords:
                        try:
                            articles = crawler.search_google_scholar(
                                keywords=gs_keywords,
                                max_results=max_results,
                                author=author or "",
                                year_start=year_start,
                                year_end=year_end,
                            )
                            if articles:
                                return articles, "google_scholar"
                            logger.warning("Google Scholar 也未找到结果")
                        except Exception as e:
                            logger.warning(f"Google Scholar搜索失败: {e}")

        except Exception as e:
            logger.error(f"搜索阶段发生异常: {e}")

        return [], "none"

    def _cnki_search(self, crawler, keywords, journal, author,
                      exact_title, year_start, year_end, max_results):
        """执行CNKI搜索（各种模式）"""
        if exact_title:
            logger.info("使用精确文献定位模式")
            # 优先传递完整年份范围给 search_exact
            if year_end and year_start:
                # 如果年份范围跨度大，用高级检索更合适
                if year_end - year_start > 1:
                    return crawler.search_advanced(
                        keywords=exact_title, author=author,
                        journal=journal, year_start=year_start,
                        year_end=year_end, max_results=max_results,
                    )
                return crawler.search_exact(
                    title=exact_title, author=author,
                    journal=journal, year=year_end,
                )
            return crawler.search_exact(
                title=exact_title, author=author,
                journal=journal, year=year_end or year_start,
            )

        elif journal and not keywords and not author:
            journal_info = settings.TARGET_JOURNALS.get(journal, {})
            issn = journal_info.get("issn", "")
            # 如果有年份范围，遍历每个年份搜索
            if year_start or year_end:
                y_start = year_start or 2000
                y_end = year_end or datetime.now().year
                all_journal_results = []
                for yr in range(y_start, y_end + 1):
                    yr_results = crawler.search_by_journal(journal, issn, yr)
                    all_journal_results.extend(yr_results)
                return all_journal_results[:max_results]
            year = year_end or year_start or datetime.now().year
            return crawler.search_by_journal(journal, issn, year)[:max_results]

        elif author or (year_start or year_end) or journal:
            logger.info("使用高级检索模式")
            return crawler.search_advanced(
                keywords=keywords, author=author, journal=journal,
                year_start=year_start, year_end=year_end,
                max_results=max_results,
            )

        elif keywords:
            if isinstance(keywords, str):
                keywords = [keywords]
            return crawler.search_by_keywords(
                keywords, max_results,
                journal_filter=journal or "",
                author_filter=author or "",
                year_start=year_start, year_end=year_end,
            )

        return []

    # ============================================================
    # 元数据提取
    # ============================================================
    def _extract_metadata(self, articles, extract_conclusion, search_source):
        """提取页面摘要和元数据（仅CNKI来源需要，Scholar已自带）"""
        if search_source != "cnki":
            return  # Scholar搜索已自带meta信息

        try:
            with CNKICrawler(headless=self.headless,
                             download_dir=str(settings.DOWNLOADS_DIR),
                             browser=self.browser,
                             connect_port=self.connect_port) as crawler:
                for i, article in enumerate(articles):
                    link = article.get("link", "")
                    if not link:
                        continue
                    try:
                        logger.info(f"提取信息 ({i+1}/{len(articles)}): {article['title'][:30]}...")

                        if not article.get("authors") or not article.get("journal"):
                            meta = crawler._extract_article_meta(link)
                            if meta:
                                article.setdefault("authors", meta.get("authors", ""))
                                article.setdefault("journal", meta.get("journal", ""))
                                article.setdefault("year", meta.get("year", ""))

                        if extract_conclusion:
                            abstract = crawler.extract_abstract(link)
                            if abstract:
                                article["abstract"] = abstract

                        wait_random_time()
                    except Exception as e:
                        logger.debug(f"提取单篇元数据失败: {e}")
        except Exception as e:
            logger.warning(f"元数据提取阶段异常（非致命）: {e}")

    # ============================================================
    # 下载容错：CNKI → Sci-Hub → Unpaywall → Scholar PDF
    # ============================================================
    def _download_with_fallback(self, articles, max_results, pdf_dir, report):
        """多级PDF下载"""
        target = articles[:max_results]

        # 第一级：CNKI下载
        cnki_failed = []
        try:
            with CNKICrawler(headless=self.headless,
                             download_dir=str(pdf_dir),
                             browser=self.browser,
                             connect_port=self.connect_port) as crawler:
                dl_results = crawler.download_articles(target, "pdf", max_workers=2)
                report["download_success"] = dl_results["success"]
                cnki_failed = [
                    a for a in target if a.get("title") in dl_results["failed"]
                ]
        except Exception as e:
            logger.error(f"CNKI下载阶段异常: {e}")
            cnki_failed = list(target)
            report["download_error"] = str(e)

        report["success_count"] = len(target) - len(cnki_failed)
        report["fail_count"] = len(cnki_failed)

        # 第二级：备用渠道
        if cnki_failed and settings.ENABLE_FALLBACK:
            logger.info(f"启动备用下载渠道，尝试获取 {len(cnki_failed)} 篇文献...")
            try:
                with FallbackDownloader(download_dir=str(pdf_dir)) as fb:
                    fb_results = fb.batch_download(cnki_failed)
                report["fallback_success"] = fb_results["success"]
                report["fallback_failed"] = fb_results["failed"]
                report["success_count"] += len(fb_results["success"])
                report["fail_count"] = len(fb_results["failed"])
                logger.info(f"备用下载: +{len(fb_results['success'])} 成功")
            except Exception as e:
                logger.error(f"备用下载渠道异常: {e}")
                report["fallback_error"] = str(e)

    # ============================================================
    # 任务文件夹
    # ============================================================
    def _create_task_folders(self, task_name: str = None):
        """创建任务文件夹（pdfs/ + report/ 子目录）"""
        now = datetime.now()
        if task_name:
            label = f"{now.strftime('%m/%d')}-{task_name}"
        else:
            label = f"{now.strftime('%m/%d')}-经济学文献"

        task_dir = settings.OUTPUTS_DIR / label.replace("/", "\\")
        pdf_dir = task_dir / "pdfs"
        report_dir = task_dir / "report"

        for d in [pdf_dir, report_dir]:
            d.mkdir(parents=True, exist_ok=True)

        logger.info(f"任务文件夹: {task_dir}")
        return task_dir, pdf_dir, report_dir, label

    def _init_report(self, task_dir, pdf_dir, report_dir, task_label,
                      keywords, journal, author, exact_title,
                      year_start, year_end, max_results):
        """初始化任务报告"""
        return {
            "task_name": task_label,
            "task_dir": str(task_dir),
            "pdf_dir": str(pdf_dir),
            "report_dir": str(report_dir),
            "start_time": datetime.now().isoformat(),
            "version": "2.2.0",
            "params": {
                "keywords": keywords,
                "journal": journal,
                "author": author,
                "exact_title": exact_title,
                "year_start": year_start,
                "year_end": year_end,
                "max_results": max_results,
            },
            "articles": [],
            "search_source": "",
            "success_count": 0,
            "fail_count": 0,
        }

    # ============================================================
    # 输出生成
    # ============================================================
    def _generate_outputs(self, articles: List[Dict], report_dir: Path,
                           task_label: str):
        """生成Markdown报告和CSV表格"""
        report_dir.mkdir(parents=True, exist_ok=True)

        md_path = report_dir / f"{task_label.replace('/', '_')}_results.md"
        md_content = self._build_markdown(articles, task_label)
        md_path.write_text(md_content, encoding="utf-8")
        logger.info(f"Markdown报告: {md_path}")

        csv_path = report_dir / f"{task_label.replace('/', '_')}_results.csv"
        self._build_csv(articles, csv_path)
        logger.info(f"CSV表格: {csv_path}")

    def _build_markdown(self, articles: List[Dict], title: str) -> str:
        lines = [f"# {title}\n",
                 f"> 生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"]

        total = len(articles)
        with_conclusion = sum(
            1 for a in articles
            if a.get("conclusion") and "无法" not in a.get("conclusion", "")
        )
        lines.append(f"**总计**: {total} 篇 | **已提取结论**: {with_conclusion} 篇\n")
        lines.append("")
        lines.append("| # | 标题 | 作者 | 期刊 | 年份 | 结论（<=100字） |")
        lines.append("|---|------|------|------|------|---------------|")

        for i, a in enumerate(articles, 1):
            lines.append(
                f"| {i} | {a.get('title', '')[:40]} | {a.get('authors', '')[:15]} "
                f"| {a.get('journal', '')[:10]} | {a.get('year', '')} "
                f"| {a.get('conclusion', '')[:100]} |"
            )

        lines.append("")
        lines.append("## 研究结论详览\n")
        for i, a in enumerate(articles, 1):
            if a.get("conclusion") and "无法" not in a.get("conclusion", ""):
                lines.append(f"### {i}. {a.get('title', '未知')}\n")
                lines.append(f"- **作者**: {a.get('authors', '未知')}")
                lines.append(f"- **期刊**: {a.get('journal', '未知')} ({a.get('year', '未知')})")
                lines.append(f"- **结论**: {a.get('conclusion', '')}\n")

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
                "链接": a.get("link", ""),
                "研究结论": a.get("conclusion", ""),
                "摘要来源": a.get("conclusion_source", ""),
                "搜索来源": a.get("source", ""),
            })
        df = pd.DataFrame(rows)
        df.to_csv(csv_path, index=False, encoding="utf-8-sig")

    def _save_report(self, report: Dict, task_dir: Path):
        report_path = task_dir / "task_report.json"
        with open(report_path, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2, default=str)
        logger.info(f"任务报告: {report_path}")


# ============================================================
# 日志配置
# ============================================================
def setup_logging(log_level: str = None):
    """配置日志系统（输出到用户目录）"""
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

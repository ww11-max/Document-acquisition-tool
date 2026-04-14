"""
CNKI文献爬虫核心模块
基于Selenium实现知网文献搜索、链接获取和PDF下载
"""

import re
import time
import random
import logging
from datetime import datetime
from typing import List, Dict, Optional, Tuple, Union
from urllib.parse import quote, quote_plus
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver import ActionChains

from .driver_manager import BrowserManager, simulate_human_behavior, wait_random_time
from config import settings

logger = logging.getLogger(__name__)


class CNKICrawler:
    """知网文献爬虫"""

    def __init__(self, headless: Optional[bool] = None, download_dir: Optional[str] = None,
                 browser: Optional[str] = None, connect_port: Optional[int] = None):
        self.headless = headless if headless is not None else settings.USE_HEADLESS
        self.download_dir = download_dir or str(settings.DOWNLOADS_DIR)
        self.browser = browser
        self.connect_port = connect_port
        self.driver_manager = None
        self.driver = None

    def __enter__(self):
        self.driver_manager = BrowserManager(self.headless, self.download_dir,
                                             browser=self.browser,
                                             connect_port=self.connect_port)
        self.driver = self.driver_manager.create_driver()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.driver_manager:
            self.driver_manager.close()

    # ============================================================
    # CNKI 可达性检测
    # ============================================================
    def _detect_captcha_page(self) -> bool:
        """检测当前页面是否为CNKI验证码页面"""
        captcha_indicators = [
            "验证码", "captcha", "CAPTCHA", "安全验证",
            "请完成验证", "请输入验证", "滑动验证",
            "点击验证", "图片验证", "yzm", "滑块验证",
        ]
        page_url = self.driver.current_url
        page_source = self.driver.page_source

        # URL包含验证码相关路径
        captcha_urls = ["captcha", "verify", "checkcode", "secverify"]
        for cu in captcha_urls:
            if cu in page_url.lower():
                logger.warning(f"CNKI验证码页面 (URL含: {cu})")
                return True

        # 页面内容包含验证码关键词
        for ci in captcha_indicators:
            if ci.lower() in page_source.lower():
                logger.warning(f"CNKI验证码页面 (内容含: {ci})")
                return True

        return False

    def check_cnki_accessible(self, timeout: int = 20) -> bool:
        """
        检测当前网络环境能否访问CNKI。
        1. 尝试加载CNKI搜索首页
        2. 排除错误页面（403/502/超时等）
        3. 排除验证码页面
        4. 确认搜索框元素存在（页面真正可用）
        """
        logger.info("正在检测CNKI可达性...")
        try:
            # 设置页面加载超时
            self.driver.set_page_load_timeout(timeout)
            self.driver.get(settings.CNKI_SEARCH_URL)

            # 等待页面基本加载，最多等 timeout 秒
            WebDriverWait(self.driver, timeout).until(
                lambda d: d.execute_script("return document.readyState") in ["complete", "interactive"]
            )
            time.sleep(1)

            # 1) 检查是否有浏览器级错误页面
            current_url = self.driver.current_url
            error_indicators = [
                "ERR_CONNECTION", "ERR_NAME_NOT_RESOLVED",
                "ERR_TIMED_OUT", "ERR_CONNECTION_TIMED_OUT",
                "ERR_INTERNET_DISCONNECTED", "ERR_PROXY_CONNECTION_FAILED",
            ]
            for err in error_indicators:
                if err in current_url:
                    logger.warning(f"CNKI不可达 (浏览器错误: {err})")
                    return False

            # 2) 检查HTTP错误页面
            page_source = self.driver.page_source
            http_errors = ["无法访问", "错误", "403", "404", "502", "503",
                           "Service Unavailable", "访问被拒绝", "prohibited",
                           "Bad Gateway", "Gateway Timeout"]
            for err in http_errors:
                if err.lower() in page_source.lower():
                    logger.warning(f"CNKI不可达 (检测到: {err})")
                    return False

            # 3) 检查验证码页面
            if self._detect_captcha_page():
                logger.warning("CNKI需要验证码，暂时不可用")
                return False

            # 4) 确认搜索页面真正加载（检查搜索输入框）
            try:
                WebDriverWait(self.driver, 5).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, "input.search-input, input#txt_SearchText, #graession"))
                )
                logger.info("CNKI可达")
                return True
            except Exception:
                # 搜索框找不到，再宽松检查一次
                normal_indicators = ["kns8s", "检索", "高级检索"]
                for indicator in normal_indicators:
                    if indicator in page_source:
                        logger.info(f"CNKI基本可达（通过关键词'{indicator}'判断）")
                        return True

                logger.warning("CNKI页面加载了但搜索框未找到，可能需要验证")
                return False

        except Exception as e:
            err_msg = str(e)
            if "timeout" in err_msg.lower() or "timed out" in err_msg.lower():
                logger.warning(f"CNKI连接超时 ({timeout}s)")
            else:
                logger.warning(f"CNKI可达性检测失败: {e}")
            return False

    # ============================================================
    # Google Scholar 备用搜索（CNKI不可达时使用）
    # ============================================================
    def search_google_scholar(self, keywords: Union[str, List[str]],
                               max_results: int = 20,
                               author: str = "",
                               year_start: int = None,
                               year_end: int = None) -> List[Dict]:
        """
        通过 Google Scholar 搜索文献（CNKI不可达时的备用方案）。

        Returns:
            标准化的文献列表 [{title, link, authors, journal, year, abstract, source}]
        """
        if isinstance(keywords, str):
            keywords = [keywords]

        all_results = []
        for kw in keywords:
            logger.info(f"[Google Scholar] 搜索: {kw}")
            try:
                results = self._gs_search_single(kw, max_results, author,
                                                  year_start, year_end)
                all_results.extend(results)
                logger.info(f"[Google Scholar] '{kw}' 找到 {len(results)} 条")
                wait_random_time()
            except Exception as e:
                logger.error(f"[Google Scholar] 搜索出错: {e}")

        # 去重（按标题相似度）
        return self._dedup_results(all_results)

    def _gs_search_single(self, keyword: str, max_results: int,
                            author: str = "", year_start: int = None,
                            year_end: int = None) -> List[Dict]:
        """Google Scholar 单次搜索"""
        query_parts = [keyword]
        if author:
            query_parts.append(f"author:{author}")
        if year_start or year_end:
            yr = year_start or ""
            yr_end = year_end or ""
            if yr and yr_end:
                query_parts.append(f"{yr}-{yr_end}")
            elif yr:
                query_parts.append(f"after:{yr}")
            elif yr_end:
                query_parts.append(f"before:{yr_end}")

        query = " ".join(query_parts)
        url = f"{settings.GOOGLE_SCHOLAR_URL}?q={quote_plus(query)}&hl=zh-CN"

        try:
            self.driver.get(url)
            time.sleep(3)

            results = []
            # Google Scholar 搜索结果结构
            items = self.driver.find_elements(By.CSS_SELECTOR,
                                              "div.gs_ri, .gs_r")
            for item in items[:max_results]:
                try:
                    # 标题
                    title_elem = item.find_element(By.CSS_SELECTOR,
                                                   "h3 a, .gs_rt a")
                    title = title_elem.text.strip()
                    link = title_elem.get_attribute("href") or ""
                    if not title:
                        continue

                    # 作者和期刊信息（绿色小字区域）
                    meta = ""
                    try:
                        meta = item.find_element(By.CSS_SELECTOR,
                                                 "div.gs_a").text.strip()
                    except Exception:
                        pass

                    # 简介文本
                    snippet = ""
                    try:
                        snippet = item.find_element(By.CSS_SELECTOR,
                                                    "div.gs_rs").text.strip()
                    except Exception:
                        pass

                    # 从 meta 文本中提取作者、年份、期刊
                    authors = ""
                    year = ""
                    journal = ""
                    if meta:
                        # Google Scholar 格式: "作者 - 期刊, 年份 - publisher"
                        meta_parts = meta.split(" - ")
                        if meta_parts:
                            authors = meta_parts[0].strip()
                        if len(meta_parts) > 1:
                            journal_year = meta_parts[1].strip()
                            yr_match = re.search(r"(\d{4})", journal_year)
                            if yr_match:
                                year = yr_match.group(1)
                                journal = journal_year.replace(year, "").strip(", ")

                    # 年份过滤
                    if year_start or year_end:
                        if year:
                            y = int(year)
                            if year_start and y < year_start:
                                continue
                            if year_end and y > year_end:
                                continue
                        else:
                            # 无法判断年份，保守保留
                            pass

                    # 作者过滤
                    if author and authors:
                        if author.lower() not in authors.lower():
                            continue

                    # PDF链接
                    pdf_link = ""
                    try:
                        pdf_btn = item.find_element(By.CSS_SELECTOR,
                                                     "div.gs_ggs a, a[href*='.pdf']")
                        pdf_link = pdf_btn.get_attribute("href") or ""
                    except Exception:
                        pass

                    results.append({
                        "title": title,
                        "link": link,
                        "pdf_link": pdf_link,
                        "authors": authors,
                        "journal": journal,
                        "year": year,
                        "abstract": snippet,
                        "source": "google_scholar",
                    })
                except Exception as e:
                    logger.debug(f"解析GS结果项出错: {e}")
                    continue

            return results[:max_results]

        except Exception as e:
            logger.error(f"Google Scholar搜索失败: {e}")
            return []

    def _dedup_results(self, results: List[Dict]) -> List[Dict]:
        """按标题去重（模糊匹配）"""
        seen = set()
        unique = []
        for r in results:
            title_key = r.get("title", "").lower().strip()
            # 去除空格和标点后的简化key
            title_key = re.sub(r'[\s\-_,.;:!?]+', '', title_key)
            if title_key and title_key not in seen:
                seen.add(title_key)
                unique.append(r)
        return unique

    # ============================================================
    # 关键词搜索
    # ============================================================
    def search_by_keywords(self, keywords: Union[str, List[str]],
                           max_results: int = 50,
                           journal_filter: str = "",
                           author_filter: str = "",
                           year_start: int = None,
                           year_end: int = None,
                           sort_by: str = "relevance") -> List[Dict]:
        """
        通过关键词搜索文献，支持按期刊、作者、年份过滤

        Args:
            keywords: 关键词或关键词列表
            max_results: 最大结果数量
            journal_filter: 期刊名称过滤
            author_filter: 作者姓名过滤
            year_start: 起始年份
            year_end: 结束年份
        """
        if isinstance(keywords, str):
            keywords = [keywords]

        all_results = []
        for keyword in keywords:
            logger.info(f"搜索关键词: {keyword}")
            try:
                results = self._search_single_keyword(
                    keyword, max_results, journal_filter,
                    author_filter, year_start, year_end,
                    sort_by=sort_by
                )
                all_results.extend(results)
                logger.info(f"关键词 '{keyword}' 找到 {len(results)} 条结果")
                wait_random_time()
            except Exception as e:
                logger.error(f"搜索关键词 '{keyword}' 时出错: {e}")
                continue
        return all_results

    def _build_search_url(self, keyword: str = None,
                          journal_filter: str = "",
                          author_filter: str = "",
                          year_start: int = None,
                          year_end: int = None,
                          sort_by: str = "relevance") -> str:
        """
        智能构建搜索URL（仅用于纯关键词搜索）。
        有筛选条件时走 _execute_expert_search() 在搜索框中输入。

        sort_by: "relevance"(相关度), "date"(日期), "citation"(被引)
        """
        if not keyword:
            return settings.CNKI_SEARCH_URL
        # CNKI 排序参数: SU=主题相关度, SC=被引, DT=发表时间
        korder_map = {"relevance": "SU", "citation": "SC", "date": "DT"}
        korder = korder_map.get(sort_by, "SU")
        return f"{settings.CNKI_SEARCH_URL}?kw={quote_plus(keyword)}&korder={korder}"

    def _execute_expert_search(self, conditions: List[str],
                                max_results: int,
                                journal_filter: str = "",
                                year_start: int = None,
                                year_end: int = None) -> List[Dict]:
        """
        通过CNKI专业检索入口执行多条件检索。

        使用 CNKI 的专业检索页面输入完整检索式（如 SU="关键词" AND LY="期刊"），
        比普通搜索框 + 页面筛选器更可靠。
        """
        if not conditions:
            return []

        # 构建完整检索式
        all_conditions = list(conditions)
        if journal_filter:
            all_conditions.append(f'LY="{journal_filter}"')
        if year_start and year_end:
            all_conditions.append(f'PY="{year_start}-{year_end}"')
        elif year_start:
            all_conditions.append(f'PY="{year_start}-{datetime.now().year}"')
        elif year_end:
            all_conditions.append(f'PY="1900-{year_end}"')

        full_query = " AND ".join(all_conditions)
        logger.info(f"专业检索式: {full_query}")

        try:
            # CNKI 专业检索入口
            expert_url = "https://kns.cnki.net/kns8s/AdvSearch?classid=YSTT4HG0"
            self.driver.set_page_load_timeout(20)
            self.driver.get(expert_url)

            try:
                WebDriverWait(self.driver, 15).until(
                    lambda d: d.execute_script("return document.readyState") == "complete"
                )
            except Exception:
                pass

            # 在专业检索输入框中输入检索式
            try:
                textarea = WebDriverWait(self.driver, 10).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR,
                        "#expert-input, .expert-search-input, textarea[name='expertvalue'], #gradetxt"))
                )
                textarea.clear()
                # 使用 JS 设置值（更可靠）
                self.driver.execute_script(
                    "arguments[0].value = arguments[1];", textarea, full_query
                )
                time.sleep(0.5)

                # 点击检索按钮
                search_btn = self.driver.find_element(
                    By.CSS_SELECTOR,
                    "#expert-search-btn, .btn-search, input.btn-search, button.search-btn"
                )
                search_btn.click()
            except Exception as e:
                logger.warning(f"专业检索输入失败，回退到普通搜索: {e}")
                # 回退：用第一个条件做普通搜索
                first_cond = all_conditions[0]
                m = re.search(r'"(.+?)"', first_cond)
                search_text = m.group(1) if m else first_cond
                search_text = search_text.replace('TI=', '').replace('SU=', '').replace('AU=', '').replace('LY=', '').replace('PY=', '')
                search_url = self._build_search_url(search_text)
                self.driver.get(search_url)
                try:
                    WebDriverWait(self.driver, 15).until(
                        lambda d: d.execute_script("return document.readyState") == "complete"
                    )
                except Exception:
                    pass

            # 等待搜索结果
            try:
                WebDriverWait(self.driver, 15).until(
                    lambda d: (
                        d.find_elements(By.CSS_SELECTOR,
                            ".s-single-text, .result-table-list, .gb_num")
                        or "没有找到" in (d.page_source or "")
                        or "未找到" in (d.page_source or "")
                    )
                )
            except Exception:
                logger.warning("搜索结果加载超时")
            time.sleep(1)

            # 提取结果
            results = []
            page = 1

            while len(results) < max_results:
                logger.info(f"搜索 - 第 {page} 页")
                page_results = self._extract_search_results()
                results.extend(page_results[:max_results - len(results)])

                if len(results) >= max_results or not self._has_next_page():
                    break

                try:
                    self._click_next_page()
                    page += 1
                    time.sleep(1.5)
                except Exception:
                    break

            return results[:max_results]

        except Exception as e:
            logger.error(f"搜索执行失败: {e}")
            return []

    def _search_single_keyword(self, keyword: str, max_results: int,
                                journal_filter: str = "",
                                author_filter: str = "",
                                year_start: int = None,
                                year_end: int = None,
                                sort_by: str = "relevance") -> List[Dict]:
        """搜索单个关键词，支持年份和作者筛选

        当有筛选条件时，使用 CNKI 高级检索 URL 组合多个条件，
        而非普通搜索框 + 客户端过滤（后者容易丢结果）。
        """
        has_filters = any([journal_filter, author_filter,
                           year_start is not None, year_end is not None])

        if has_filters:
            # ---- 构造检索条件，分离搜索词和筛选条件 ----
            conditions = []
            if keyword:
                conditions.append(f'SU="{keyword}"')
            if author_filter:
                conditions.append(f'AU="{author_filter}"')
            # journal 和 year 不再塞进 conditions，改用页面筛选器

            query_parts = conditions + []
            if journal_filter:
                query_parts.append(f'LY="{journal_filter}"')
            if year_start and year_end:
                query_parts.append(f'PY="{year_start}-{year_end}"')
            elif year_start:
                query_parts.append(f'PY="{year_start}-{datetime.now().year}"')
            elif year_end:
                query_parts.append(f'PY="1900-{year_end}"')

            logger.info(f"检索条件: {' AND '.join(query_parts)}")

            results = self._execute_expert_search(
                conditions, max_results,
                journal_filter=journal_filter,
                year_start=year_start, year_end=year_end,
            )

            # 客户端二次过滤兜底
            # keyword 搜索场景：只过滤年份和作者，不过滤期刊
            # （期刊筛选由 CNKI 页面/专业检索处理，客户端 journal 过滤容易误杀）
            results = self._client_side_filter(
                results, author=author_filter,
                journal="",  # 不做期刊客户端过滤
                year_start=year_start, year_end=year_end
            )
            return results[:max_results]

        # 纯关键词 → 普通搜索
        search_url = self._build_search_url(keyword, sort_by=sort_by)
        logger.info(f"搜索URL: {search_url[:120]}...")

        try:
            self.driver.set_page_load_timeout(30)
            self.driver.get(search_url)

            # 等待页面加载完成
            try:
                WebDriverWait(self.driver, 20).until(
                    lambda d: d.execute_script("return document.readyState") == "complete"
                )
            except Exception:
                pass

            # 等待搜索结果列表或提示出现（最多10秒）
            try:
                WebDriverWait(self.driver, 10).until(
                    lambda d: (
                        d.find_elements(By.CSS_SELECTOR, ".s-single-text, .result-table-list, .gb_num")
                        or "没有找到" in (d.page_source or "")
                        or "未找到" in (d.page_source or "")
                        or "0条" in (d.page_source or "")
                    )
                )
            except Exception:
                logger.warning("搜索结果列表加载超时，尝试继续解析...")

            results = []
            page = 1

            while len(results) < max_results:
                logger.info(f"正在处理第 {page} 页")
                page_results = self._extract_search_results()
                results.extend(page_results[:max_results - len(results)])

                if len(results) >= max_results or not self._has_next_page():
                    break

                self._click_next_page()
                page += 1
                time.sleep(1.5)

            # 客户端二次过滤兜底
            results = self._client_side_filter(
                results, author=author_filter, journal=journal_filter,
                year_start=year_start, year_end=year_end
            )

            return results[:max_results]
        except Exception as e:
            logger.error(f"搜索关键词 '{keyword}' 时出错: {e}")
            return []

    def _set_page_size(self, size: int):
        """设置每页显示数量"""
        try:
            per_page_div = WebDriverWait(self.driver, 30).until(
                EC.presence_of_element_located((By.ID, "perPageDiv"))
            )
            per_page_div.click()
            WebDriverWait(self.driver, 30).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "ul.sort-list"))
            )
            size_option = self.driver.find_element(
                By.CSS_SELECTOR, f'li[data-val="{size}"] a'
            )
            size_option.click()
            time.sleep(1)
        except Exception as e:
            logger.warning(f"设置每页显示数量失败: {e}")

    def _set_journal_filter(self, journal_name: str):
        """设置来源（期刊）过滤"""
        try:
            # 转义 XPath 特殊字符
            safe_name = journal_name.replace("'", "''")

            # 尝试点击"来源"展开区域
            source_btn = self.driver.find_element(
                By.CSS_SELECTOR, "#SourceType, .search-nav .source span, .facet-item[data-type='source'] span"
            )
            source_btn.click()
            time.sleep(0.5)

            # 输入期刊名
            source_input = self.driver.find_element(
                By.CSS_SELECTOR, "#SourceType input, .search-nav .source input, .facet-input"
            )
            source_input.clear()
            source_input.send_keys(journal_name)
            time.sleep(0.5)

            # 点击搜索/确认
            confirm_btn = self.driver.find_element(
                By.CSS_SELECTOR, "#SourceType .search-btn, .search-nav .source .search-btn, .facet-search-btn"
            )
            confirm_btn.click()
            time.sleep(1)

            # 点击过滤结果中的期刊名
            journal_option = self.driver.find_element(
                By.XPATH, f"//*[contains(text(), '{safe_name}')]"
            )
            journal_option.click()
            time.sleep(1)
        except Exception as e:
            logger.debug(f"期刊过滤操作未成功（可能页面结构不同）: {e}")

    def _extract_search_results(self) -> List[Dict]:
        """提取搜索结果列表"""
        results = []
        try:
            # 等待结果加载（多层兜底）
            for selector in ["#gridTable", "table.result-table-list", "table", ".search-result, .result-list"]:
                try:
                    WebDriverWait(self.driver, 15).until(
                        EC.presence_of_element_located((By.CSS_SELECTOR, selector))
                    )
                    logger.debug(f"搜索结果容器已定位: {selector}")
                    break
                except Exception:
                    continue

            tables = self.driver.find_elements(By.CSS_SELECTOR, "table.result-table-list")
            if not tables:
                tables = self.driver.find_elements(By.CSS_SELECTOR, "table")

            if not tables:
                containers = self.driver.find_elements(
                    By.CSS_SELECTOR, "#gridTable, .search-result, .result-list, .content-list"
                )
                if containers:
                    rows = containers[0].find_elements(By.CSS_SELECTOR, "tr, .list-item, .content-item")
                else:
                    rows = []
            else:
                rows = tables[0].find_elements(By.CSS_SELECTOR, "tbody tr")

            logger.info(f"找到 {len(rows)} 个结果行")

            for row in rows:
                try:
                    title, link = "", ""

                    # 多种选择器尝试提取标题和链接
                    for sel in ["td.name a.fz14", "td.name a", "a.fz14"]:
                        try:
                            title_elem = row.find_element(By.CSS_SELECTOR, sel)
                            title = title_elem.text.strip()
                            link = title_elem.get_attribute("href")
                            if title:
                                break
                        except Exception:
                            continue

                    if not title:
                        # 兜底：找任何含cnki链接的a标签
                        for a in row.find_elements(By.TAG_NAME, "a"):
                            href = a.get_attribute("href") or ""
                            if "cnki.net" in href and a.text.strip():
                                title = a.text.strip()
                                link = href
                                break

                    if not title:
                        continue

                    # 提取作者
                    authors = ""
                    for sel in ["td.author a", "td.author", ".author"]:
                        try:
                            authors = row.find_element(By.CSS_SELECTOR, sel).text.strip()
                            if authors:
                                break
                        except Exception:
                            continue

                    # 提取来源期刊
                    journal = ""
                    for sel in ["td.source", ".source", "td[data-field='source']"]:
                        try:
                            journal = row.find_element(By.CSS_SELECTOR, sel).text.strip()
                            if journal:
                                break
                        except Exception:
                            continue

                    # 提取日期/年份
                    date_text, year = "", ""
                    for sel in ["td.date", ".date"]:
                        try:
                            date_text = row.find_element(By.CSS_SELECTOR, sel).text.strip()
                            year_match = re.search(r"(\d{4})", date_text)
                            year = year_match.group(1) if year_match else ""
                            if year:
                                break
                        except Exception:
                            continue

                    if not year:
                        row_text = row.text
                        year_match = re.search(r"(\d{4})", row_text)
                        year = year_match.group(1) if year_match else ""

                    results.append({
                        "title": title,
                        "link": link,
                        "authors": authors,
                        "journal": journal,
                        "year": year,
                        "date": date_text,
                        "abstract": "",
                    })
                    logger.debug(f"提取: {title[:50]}...")
                except Exception as e:
                    logger.debug(f"提取单行时出错: {e}")
                    continue

            logger.info(f"本页成功提取 {len(results)} 个结果")
        except Exception as e:
            logger.error(f"提取搜索结果出错: {e}")

        return results

    def _has_next_page(self) -> bool:
        try:
            btn = self.driver.find_element(By.ID, "PageNext")
            return "disabled" not in (btn.get_attribute("class") or "")
        except Exception:
            return False

    def _click_next_page(self):
        try:
            btn = WebDriverWait(self.driver, 30).until(
                EC.element_to_be_clickable((By.ID, "PageNext"))
            )
            btn.click()
        except Exception as e:
            logger.error(f"翻页失败: {e}")
            raise

    # ============================================================
    # 高级检索（作者 + 年份 + 期刊 + 关键词 组合条件）
    # ============================================================
    def search_advanced(self,
                        keywords: Union[str, List[str]] = None,
                        author: str = None,
                        journal: str = None,
                        year_start: int = None,
                        year_end: int = None,
                        max_results: int = 50) -> List[Dict]:
        """
        CNKI专业检索：支持多条件组合。

        通过模拟人工操作：切换专业检索tab → 输入检索式 → 点搜索。
        SU="关键词" AND AU="作者" AND LY="期刊" AND PY="年份"
        """
        conditions = []

        if keywords:
            if isinstance(keywords, str):
                keywords = [keywords]
            for kw in keywords:
                conditions.append(f'SU="{kw}"')

        if author:
            conditions.append(f'AU="{author}"')

        if journal:
            conditions.append(f'LY="{journal}"')

        if not conditions:
            logger.warning("专业检索至少需要一个条件（keywords/author/journal）")
            return []

        # 年份
        if year_start and year_end:
            conditions.append(f'PY="{year_start}-{year_end}"')
        elif year_start:
            conditions.append(f'PY="{year_start}-{datetime.now().year}"')
        elif year_end:
            conditions.append(f'PY="1900-{year_end}"')

        # 使用搜索框输入的方式执行专业检索
        results = self._execute_expert_search(conditions, max_results)

        # 客户端二次过滤兜底
        results = self._client_side_filter(
            results, author=author, journal=journal,
            year_start=year_start, year_end=year_end
        )
        return results[:max_results]

    # ============================================================
    # 精确文献定位（标题 + 作者 + 期刊 + 年份 四要素）
    # ============================================================
    def search_exact(self,
                     title: str = None,
                     author: str = None,
                     journal: str = None,
                     year: int = None) -> List[Dict]:
        """
        精确定位一篇文献

        策略：
        1. 如果有标题+其他条件，直接用CNKI专业检索（TI字段精确匹配标题）
        2. 如果只有标题，用标题搜索 + 结果精筛
        3. 如果没有标题，降级到高级检索模式

        Args:
            title: 文章标题（最精确的定位方式）
            author: 作者
            journal: 期刊名称
            year: 年份
        """
        logger.info(f"精确搜索: title={'有' if title else '无'}, author={author}, "
                    f"journal={journal}, year={year}")

        if not title and not author:
            logger.warning("精确搜索至少需要 title 或 author")
            return []

        # 策略1：有标题时，优先用专业检索（TI字段精确匹配标题）
        if title:
            try:
                conditions = [f'TI="{title}"']
                if author:
                    conditions.append(f'AU="{author}"')
                if journal:
                    conditions.append(f'LY="{journal}"')
                if year:
                    conditions.append(f'PY="{year}"')

                all_results = self._execute_expert_search(conditions, 20)
                logger.info(f"精确检索返回 {len(all_results)} 条结果")

                matched = self._exact_match(all_results, title=title, author=author,
                                            journal=journal, year=year)
                if matched:
                    return matched

                logger.info("精确检索未匹配，尝试放宽标题条件重新搜索...")
            except Exception as e:
                logger.warning(f"精确检索失败: {e}")

        # 策略2：降级 — 用标题前半段做模糊搜索
        if title and len(title) > 6:
            try:
                short_title = title[:len(title) // 2] if len(title) > 15 else title
                conditions = [f'TI="{short_title}"']
                if author:
                    conditions.append(f'AU="{author}"')
                if journal:
                    conditions.append(f'LY="{journal}"')
                if year:
                    conditions.append(f'PY="{year}"')

                logger.info(f"降级检索（放宽标题）: {' AND '.join(conditions)}")

                all_results = self._execute_expert_search(conditions, 20)
                logger.info(f"降级检索返回 {len(all_results)} 条结果")

                matched = self._exact_match(all_results, title=title, author=author,
                                            journal=journal, year=year)
                if matched:
                    return matched
            except Exception as e:
                logger.warning(f"降级检索失败: {e}")

        # 策略3：最终降级 — 仅用作者+期刊+年份（不含标题）
        if any([author, journal, year]):
            logger.info("最终降级：仅用作者/期刊/年份条件检索")
            return self.search_advanced(
                keywords=None,
                author=author,
                journal=journal,
                year_start=year,
                year_end=year,
                max_results=20,
            )

        return []

    def _exact_match(self, results: List[Dict],
                     title: str = None, author: str = None,
                     journal: str = None, year: str = None) -> List[Dict]:
        """
        从结果列表中做精确匹配
        标题相似度 > 70% 且其他条件完全匹配
        """
        matched = []
        for r in results:
            score = 0
            total = 0

            # 标题匹配（最重要）
            if title:
                total += 3
                r_title = r.get("title", "")
                if title in r_title or r_title in title:
                    score += 3
                else:
                    # 计算字符重叠率
                    common = set(title) & set(r_title)
                    sim = len(common) / max(len(set(title)), 1)
                    if sim > 0.7:
                        score += 3
                    elif sim > 0.5:
                        score += 2
                    elif sim > 0.3:
                        score += 1

            # 作者匹配
            if author:
                total += 2
                r_authors = r.get("authors", "")
                if author in r_authors:
                    score += 2

            # 期刊匹配
            if journal:
                total += 2
                r_journal = r.get("journal", "")
                if journal in r_journal or r_journal in journal:
                    score += 2

            # 年份匹配
            if year:
                total += 1
                r_year = r.get("year", "")
                if str(year) == r_year:
                    score += 1

            # 判定：标题必须匹配，其他条件至少满足一半
            title_matched = (not title) or (score >= 3)
            other_ok = (score >= total * 0.5)

            if title_matched and other_ok and total > 0:
                r["match_score"] = f"{score}/{total}"
                matched.append(r)

        if matched:
            # 按匹配度排序，最高的放前面
            matched.sort(key=lambda x: int(x.get("match_score", "0/0").split("/")[0]), reverse=True)
            logger.info(f"精确匹配到 {len(matched)} 篇: {[m['title'][:30] for m in matched]}")

        return matched

    # ============================================================
    # 客户端二次过滤（兜底机制）
    # ============================================================
    def _client_side_filter(self, results: List[Dict],
                            author: str = None,
                            journal: str = None,
                            year_start: int = None,
                            year_end: int = None,
                            keywords: str = None) -> List[Dict]:
        """
        对搜索结果做客户端二次过滤
        用于兜底：CNKI页面筛选器可能未生效或结果不精确时
        """
        if not any([author, journal, year_start, year_end, keywords]):
            return results

        original_count = len(results)
        filtered = []

        for r in results:
            pass_filter = True

            # 作者过滤（模糊匹配：输入的作者名出现在作者列表中）
            if author:
                r_authors = r.get("authors", "")
                if author not in r_authors:
                    pass_filter = False

            # 期刊过滤（模糊匹配）
            if journal and pass_filter:
                r_journal = r.get("journal", "")
                if journal not in r_journal and r_journal not in journal:
                    pass_filter = False

            # 年份范围过滤
            if pass_filter and (year_start or year_end):
                r_year = r.get("year", "")
                try:
                    r_year_int = int(r_year) if r_year else 0
                    if year_start and r_year_int < year_start:
                        pass_filter = False
                    if year_end and r_year_int > year_end:
                        pass_filter = False
                except (ValueError, TypeError):
                    # 无法解析年份的条目，保留（宽松策略）
                    pass

            # 关键词过滤（标题或摘要中包含关键词）
            if keywords and pass_filter:
                r_title = r.get("title", "")
                r_abstract = r.get("abstract", "")
                combined = r_title + r_abstract
                if keywords not in combined:
                    pass_filter = False

            if pass_filter:
                filtered.append(r)

        filtered_count = len(filtered)
        if filtered_count != original_count:
            logger.info(f"客户端过滤: {original_count} → {filtered_count} 条 "
                        f"(移除 {original_count - filtered_count} 条不匹配结果)")

        return filtered

    # ============================================================
    # CNKI页面年份筛选器操作
    # ============================================================
    def _set_year_filter(self, year_start: int = None, year_end: int = None):
        """
        在CNKI搜索页面上操作年份筛选面板
        """
        try:
            # 展开"发表时间"筛选区
            for selector in [
                "#publishdate_Group .set-title",
                ".facet-item[data-type='publishdate'] .facet-title",
                "#publishdate input[type='text']",
            ]:
                try:
                    elem = self.driver.find_element(By.CSS_SELECTOR, selector)
                    elem.click()
                    time.sleep(0.5)
                    break
                except Exception:
                    continue

            # 尝试直接在输入框中填写年份
            year_input_start = None
            year_input_end = None

            for selector in [
                "#publishdate_Group input[type='text']",
                ".facet-input-group input:first-child",
                "input[placeholder*='起始']",
                "input[placeholder*='开始']",
            ]:
                try:
                    inputs = self.driver.find_elements(By.CSS_SELECTOR, selector)
                    if len(inputs) >= 2:
                        year_input_start = inputs[0]
                        year_input_end = inputs[1]
                        break
                    elif len(inputs) == 1:
                        year_input_start = inputs[0]
                except Exception:
                    continue

            if year_input_start and year_start:
                year_input_start.clear()
                year_input_start.send_keys(str(year_start))
                time.sleep(0.3)

            if year_input_end and year_end:
                year_input_end.clear()
                year_input_end.send_keys(str(year_end))
                time.sleep(0.3)

            # 点击确认按钮
            for selector in [
                "#publishdate_Group .search-btn",
                ".facet-search-btn",
                "button.facet-confirm",
            ]:
                try:
                    btn = self.driver.find_element(By.CSS_SELECTOR, selector)
                    btn.click()
                    time.sleep(1)
                    break
                except Exception:
                    continue

            logger.info(f"页面年份筛选已设置: {year_start}-{year_end}")

        except Exception as e:
            logger.debug(f"页面年份筛选操作未成功（将依赖客户端过滤兜底）: {e}")

    # ============================================================
    # CNKI页面作者筛选器操作
    # ============================================================
    def _set_author_filter(self, author_name: str):
        """
        在CNKI搜索页面上操作作者筛选面板
        """
        try:
            # 转义 XPath 特殊字符
            safe_name = author_name.replace("'", "''")

            # 展开"作者"筛选区
            for selector in [
                "#author_Group .set-title",
                ".facet-item[data-type='author'] .facet-title",
            ]:
                try:
                    elem = self.driver.find_element(By.CSS_SELECTOR, selector)
                    elem.click()
                    time.sleep(0.5)
                    break
                except Exception:
                    continue

            # 输入作者名
            for selector in [
                "#author_Group input[type='text']",
                ".facet-input",
                "input[placeholder*='作者']",
            ]:
                try:
                    inp = self.driver.find_element(By.CSS_SELECTOR, selector)
                    inp.clear()
                    inp.send_keys(author_name)
                    time.sleep(0.5)
                    break
                except Exception:
                    continue

            # 点击确认
            for selector in [
                "#author_Group .search-btn",
                ".facet-search-btn",
                "button.facet-confirm",
            ]:
                try:
                    btn = self.driver.find_element(By.CSS_SELECTOR, selector)
                    btn.click()
                    time.sleep(1)
                    break
                except Exception:
                    continue

            # 点击筛选结果中的作者名
            try:
                author_option = WebDriverWait(self.driver, 5).until(
                    EC.element_to_be_clickable(
                        (By.XPATH, f"//*[contains(text(), '{safe_name}')]")
                    )
                )
                author_option.click()
                time.sleep(1)
            except Exception:
                pass

            logger.info(f"页面作者筛选已设置: {author_name}")

        except Exception as e:
            logger.debug(f"页面作者筛选操作未成功（将依赖客户端过滤兜底）: {e}")

    # ============================================================
    # 期刊导航搜索（通过ISSN检索整本期刊）
    # ============================================================
    def search_by_journal(self, journal_name: str, issn: str = "",
                          year: int = None) -> List[Dict]:
        """通过期刊导航页检索文献"""
        logger.info(f"期刊导航检索: {journal_name}, ISSN: {issn}")

        try:
            self.driver.set_page_load_timeout(20)
            self.driver.get(settings.CNKI_NAVI_URL)

            # 等待页面加载
            try:
                WebDriverWait(self.driver, 15).until(
                    lambda d: d.execute_script("return document.readyState") == "complete"
                )
            except Exception:
                pass
            time.sleep(1)

            # 选择ISSN检索
            self._select_search_method("ISSN")
            # 输入ISSN或刊名
            self._input_search_value(issn if issn else journal_name)
            # 搜索
            self._click_search_button()

            # 等待搜索结果
            try:
                WebDriverWait(self.driver, 10).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, ".re_bookCover, .journal-cover, .result-table-list"))
                )
            except Exception:
                logger.warning("期刊搜索结果未加载")
                return []

            time.sleep(1)

            # 点击第一个期刊
            self._click_first_journal()

            # 获取文章
            if year:
                return self._get_journal_articles_by_year(year)
            else:
                # 获取最新一期的文章
                return self._get_latest_articles()
        except Exception as e:
            logger.error(f"期刊导航检索 '{journal_name}' 出错: {e}")
            return []

    def _select_search_method(self, method: str):
        try:
            sel = WebDriverWait(self.driver, 30).until(
                EC.element_to_be_clickable((By.ID, "txt_1_sel"))
            )
            for opt in sel.find_elements(By.TAG_NAME, "option"):
                if method in opt.text.strip():
                    opt.click()
                    break
        except Exception:
            pass

    def _input_search_value(self, value: str):
        try:
            inp = WebDriverWait(self.driver, 30).until(
                EC.element_to_be_clickable((By.ID, "txt_1_value1"))
            )
            inp.clear()
            inp.send_keys(value)
        except Exception:
            pass

    def _click_search_button(self):
        try:
            btn = WebDriverWait(self.driver, 30).until(
                EC.element_to_be_clickable((By.ID, "btnSearch"))
            )
            btn.click()
        except Exception:
            pass

    def _click_first_journal(self):
        try:
            WebDriverWait(self.driver, 30).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, ".re_bookCover, .journal-cover"))
            ).click()
            time.sleep(0.5)
            # 切换新窗口
            if len(self.driver.window_handles) > 1:
                self.driver.switch_to.window(self.driver.window_handles[-1])
        except Exception as e:
            logger.error(f"点击期刊封面失败: {e}")
            raise

    def _get_journal_articles_by_year(self, year: int) -> List[Dict]:
        """获取指定年份的文章列表"""
        results = []
        try:
            year_id = f"{year}_Year_Issue"
            year_element = WebDriverWait(self.driver, 30).until(
                EC.presence_of_element_located((By.ID, year_id))
            )
            dt = WebDriverWait(year_element, 10).until(
                EC.element_to_be_clickable((By.TAG_NAME, "dt"))
            )
            dt.click()
            time.sleep(0.5)

            issue_links = year_element.find_elements(By.CSS_SELECTOR, "dd a")
            for il in issue_links:
                try:
                    WebDriverWait(self.driver, 30).until(
                        lambda d: il.is_enabled() and il.is_displayed()
                    )
                    il.click()
                    time.sleep(0.5)

                    link_elements = WebDriverWait(self.driver, 30).until(
                        EC.presence_of_all_elements_located(
                            (By.CSS_SELECTOR, "#CataLogContent span.name a")
                        )
                    )
                    for le in link_elements:
                        href = le.get_attribute("href")
                        title = le.text.strip()
                        if href:
                            results.append({
                                "title": title,
                                "link": href,
                                "year": str(year),
                                "authors": "",
                                "journal": "",
                                "date": "",
                                "abstract": "",
                            })
                    time.sleep(0.5)
                except Exception as e:
                    logger.debug(f"处理期号文章时出错: {e}")
                    continue
        except Exception as e:
            logger.warning(f"获取 {year} 年文章时出错: {e}")
        return results

    def _get_latest_articles(self) -> List[Dict]:
        """获取最新一期的文章"""
        results = []
        try:
            link_elements = WebDriverWait(self.driver, 30).until(
                EC.presence_of_all_elements_located(
                    (By.CSS_SELECTOR, "#CataLogContent span.name a, .catalog-list a")
                )
            )
            for le in link_elements:
                href = le.get_attribute("href")
                title = le.text.strip()
                if href and title:
                    results.append({
                        "title": title,
                        "link": href,
                        "authors": "",
                        "journal": "",
                        "year": "",
                        "date": "",
                        "abstract": "",
                    })
        except Exception as e:
            logger.warning(f"获取最新期文章失败: {e}")
        return results

    # ============================================================
    # PDF下载
    # ============================================================
    def download_articles(self, articles: List[Dict],
                          file_type: str = "pdf",
                          max_workers: int = 2) -> Dict[str, List[str]]:
        """批量下载文献"""
        if file_type not in ["pdf", "caj"]:
            raise ValueError("文件类型必须是 'pdf' 或 'caj'")

        logger.info(f"开始下载 {len(articles)} 篇文献 ({file_type})")
        results = {"success": [], "failed": []}

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {}
            for i, article in enumerate(articles):
                future = executor.submit(
                    self._download_single_article, article, i, file_type
                )
                futures[future] = article.get("title", f"article_{i}")

            for future in as_completed(futures):
                title = futures[future]
                try:
                    success, msg = future.result()
                    if success:
                        results["success"].append(title)
                        logger.info(f"下载成功: {title}")
                    else:
                        results["failed"].append(title)
                        logger.warning(f"下载失败: {title} - {msg}")
                except Exception as e:
                    results["failed"].append(title)
                    logger.error(f"下载异常: {title} - {e}")

        logger.info(f"下载完成: {len(results['success'])} 成功, {len(results['failed'])} 失败")
        return results

    def _download_single_article(self, article: Dict, index: int,
                                  file_type: str) -> Tuple[bool, str]:
        """下载单篇文章"""
        link = article.get("link", "")
        title = article.get("title", f"article_{index}")

        if not link:
            return False, "无有效链接"

        for attempt in range(1, settings.MAX_RETRIES + 1):
            try:
                if attempt > 1:
                    simulate_human_behavior(self.driver)
                    wait_random_time()

                self.driver.get(link)
                time.sleep(1)

                # 执行重定向
                for _ in range(3):
                    try:
                        self.driver.execute_script("redirectNewLink()")
                    except Exception:
                        pass
                    time.sleep(0.5)

                # 刷新2次
                for _ in range(2):
                    self.driver.refresh()
                    time.sleep(1)
                    try:
                        self.driver.execute_script("redirectNewLink()")
                    except Exception:
                        pass

                time.sleep(0.5)

                # 查找下载按钮
                css = ".btn-dlpdf a" if file_type == "pdf" else ".btn-dlcaj a"
                link_elem = WebDriverWait(self.driver, 30).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, css))
                )
                download_link = link_elem.get_attribute("href")
                if not download_link:
                    return False, "未找到下载链接"

                # 模拟鼠标点击下载
                ActionChains(self.driver).move_to_element(link_elem).click(link_elem).perform()

                # 处理新窗口
                original_window = self.driver.current_window_handle
                if len(self.driver.window_handles) > 1:
                    self.driver.switch_to.window(self.driver.window_handles[-1])

                # 验证码检测
                if "拼图校验" in self.driver.page_source:
                    logger.warning(f"触发验证码: {title} (尝试 {attempt})")
                    if len(self.driver.window_handles) > 1:
                        self.driver.close()
                    self.driver.switch_to.window(original_window)

                    if attempt < settings.MAX_RETRIES:
                        wait_random_time()
                        for _ in range(4):
                            self.driver.refresh()
                            time.sleep(random.uniform(0.8, 1.0))
                        continue
                    else:
                        return False, "验证码重试次数用尽"

                time.sleep(3)

                # 验证文件是否实际落盘（检查下载目录最近3秒内的PDF）
                downloaded = False
                download_path = Path(self.download_dir)
                if download_path.exists():
                    recent_pdfs = sorted(
                        download_path.glob("*.pdf"),
                        key=lambda p: p.stat().st_mtime,
                        reverse=True
                    )
                    for pdf in recent_pdfs[:3]:
                        if (time.time() - pdf.stat().st_mtime) < 10 and pdf.stat().st_size > 10000:
                            downloaded = True
                            article["pdf_path"] = str(pdf)
                            logger.info(f"PDF已落盘: {pdf.name}")
                            break

                if not downloaded:
                    logger.warning(f"PDF文件未检测到落盘: {title}")

                # 切回主窗口
                if len(self.driver.window_handles) > 1:
                    self.driver.switch_to.window(original_window)

                if downloaded:
                    return True, "下载成功"
                else:
                    return False, "PDF文件未检测到落盘"

            except Exception as e:
                logger.warning(f"下载 '{title}' 尝试 {attempt} 失败: {e}")
                # 尝试恢复窗口状态
                try:
                    self.driver.switch_to.window(self.driver.window_handles[0])
                except Exception:
                    pass
                if attempt < settings.MAX_RETRIES:
                    wait_random_time()
                else:
                    return False, f"下载失败: {str(e)}"

        return False, "重试次数用尽"

    # ============================================================
    # 摘要提取（从文章详情页）
    # ============================================================
    def extract_abstract(self, article_link: str) -> str:
        """从文章详情页提取摘要"""
        try:
            self.driver.get(article_link)
            time.sleep(2)

            # 执行重定向
            try:
                self.driver.execute_script("redirectNewLink()")
                time.sleep(1)
                self.driver.refresh()
                time.sleep(1)
            except Exception:
                pass

            # 多种选择器尝试提取摘要
            for sel in [
                "#ChDivSummary",
                ".abstract-text",
                ".abstract",
                "#abstract",
                "div[class*='abstract']",
                "div[id*='summary']",
            ]:
                try:
                    elem = self.driver.find_element(By.CSS_SELECTOR, sel)
                    text = elem.text.strip()
                    if text and len(text) > 20:
                        return text
                except Exception:
                    continue

            # 兜底：查找包含"摘要"的段落
            try:
                elements = self.driver.find_elements(By.XPATH, "//*[contains(text(), '摘')]")
                for elem in elements:
                    parent = elem.find_element(By.XPATH, "./..")
                    text = parent.text.strip()
                    if len(text) > 30:
                        # 去掉"摘要"等标签
                        text = re.sub(r"^(摘要|Abstract|【摘要】|摘\s*要\s*[:：])\s*", "", text)
                        return text[:500]
            except Exception:
                pass

            return ""
        except Exception as e:
            logger.error(f"提取摘要失败: {e}")
            return ""

    def _extract_article_meta(self, article_link: str) -> Optional[Dict]:
        """从文章详情页提取元数据（作者、期刊、年份）"""
        try:
            self.driver.get(article_link)
            time.sleep(2)
            try:
                self.driver.execute_script("redirectNewLink()")
                time.sleep(0.5)
                self.driver.refresh()
                time.sleep(1)
            except Exception:
                pass

            meta = {}

            # 提取作者
            for sel in ["#authorPart", ".author", "span.name.author", "a.author"]:
                try:
                    elems = self.driver.find_elements(By.CSS_SELECTOR, sel)
                    if elems:
                        authors = "、".join([e.text.strip() for e in elems if e.text.strip()])
                        if authors:
                            meta["authors"] = authors
                            break
                except Exception:
                    continue

            # 提取期刊来源
            for sel in ["#catalog Div", ".source", "a[href*='navi.cnki.net']",
                        "span.source", "p.source"]:
                try:
                    elem = self.driver.find_element(By.CSS_SELECTOR, sel)
                    text = elem.text.strip()
                    if text:
                        meta["journal"] = text
                        break
                except Exception:
                    continue

            # 提取年份/日期
            for sel in ["#date", ".date", "span.date", "p.date",
                        ".pub-date", "span.pub-date"]:
                try:
                    elem = self.driver.find_element(By.CSS_SELECTOR, sel)
                    text = elem.text.strip()
                    if text:
                        year_match = re.search(r"(\d{4})", text)
                        if year_match:
                            meta["year"] = year_match.group(1)
                        break
                except Exception:
                    continue

            # 尝试从页面文本中提取
            if not meta.get("journal") or not meta.get("year"):
                page_text = self.driver.find_element(By.TAG_NAME, "body").text
                if not meta.get("year"):
                    ym = re.search(r"(\d{4})\s*年", page_text)
                    if ym:
                        meta["year"] = ym.group(1)

            return meta if meta else None

        except Exception as e:
            logger.debug(f"提取文章元数据失败: {e}")
            return None

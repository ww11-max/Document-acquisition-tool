"""
eco-acquire 配置模块
经济学期刊文献题录检索工具

路径策略：
  - 代码目录（SKILL_DIR）：只读，存放代码和配置
  - 数据目录（DATA_DIR）：可写，存放在用户目录下，存放输出、日志
  - 支持通过 ECO_ACQUIRE_HOME 环境变量自定义数据目录
"""

import os
from pathlib import Path
from dotenv import load_dotenv

# 加载环境变量（从多个可能位置）
_env_paths = [
    Path.cwd() / ".env",                          # 当前工作目录
    Path.home() / "eco-acquire" / ".env",        # 用户数据目录
]
if "__file__" in dir():
    _env_paths.insert(0, Path(__file__).parent.parent / ".env")  # skill目录

for _p in _env_paths:
    if _p.exists():
        load_dotenv(_p)
        break


# ============================================================
# 路径系统
# ============================================================

# 代码目录（只读）
SKILL_DIR = Path(__file__).resolve().parent.parent

# 用户数据目录（可写）——默认 ~/eco-acquire/
# 可通过环境变量 ECO_ACQUIRE_HOME 覆盖
DATA_DIR = Path(os.getenv("ECO_ACQUIRE_HOME", Path.home() / "eco-acquire")).resolve()

# 子目录
OUTPUTS_DIR = DATA_DIR / "outputs"
LOGS_DIR = DATA_DIR / "logs"
CONFIG_DIR = DATA_DIR / "config"
USER_DATA_DIR = DATA_DIR / "browser_profile"   # 浏览器用户数据目录（持久化登录态）

# ============================================================
# 浏览器配置
# ============================================================
BROWSER = os.getenv("BROWSER", "auto").lower()
USE_HEADLESS = os.getenv("USE_HEADLESS", "false").lower() == "true"
DEBUG = os.getenv("DEBUG", "false").lower() == "true"
UNSAFE_SSL = os.getenv("UNSAFE_SSL", "false").lower() == "true"

# ============================================================
# 常用经济学期刊参考列表（非检索限制！）
# 此列表仅用于：
#   1. journal_browse 策略：按 ISSN 精确检索整本期刊
#   2. --list-journals：展示常用期刊名和 ISSN
# 用户指定任意期刊名均可直接检索，不受此列表限制。
# keyword 策略不传 journal 参数即可搜索全部期刊。
# ============================================================
TARGET_JOURNALS = {
    "经济研究": {
        "code": "JJYJ",
        "full_name": "经济研究",
        "publisher": "中国社会科学院经济研究所",
        "issn": "0577-9154",
    },
    "经济学（季刊）": {
        "code": "JJXJK",
        "full_name": "经济学（季刊）",
        "publisher": "北京大学中国经济研究中心",
        "issn": "2095-1086",
    },
    "中国工业经济": {
        "code": "ZGGYJJ",
        "full_name": "中国工业经济",
        "publisher": "中国社会科学院工业经济研究所",
        "issn": "1006-480X",
    },
    "世界经济": {
        "code": "SJJJ",
        "full_name": "世界经济",
        "publisher": "中国世界经济学会",
        "issn": "1002-9621",
    },
    "金融研究": {
        "code": "JRYJ",
        "full_name": "金融研究",
        "publisher": "中国金融学会",
        "issn": "1002-7246",
    },
    "管理世界": {
        "code": "GLSJ",
        "full_name": "管理世界",
        "publisher": "国务院发展研究中心",
        "issn": "1002-5502",
    },
    "数量经济技术经济研究": {
        "code": "SLJSJJ",
        "full_name": "数量经济技术经济研究",
        "publisher": "中国社会科学院数量经济与技术经济研究所",
        "issn": "1000-3894",
    },
    "财贸经济": {
        "code": "CMJJ",
        "full_name": "财贸经济",
        "publisher": "中国社会科学院财经战略研究院",
        "issn": "1002-8102",
    },
    "中国农村经济": {
        "code": "ZGNCJJ",
        "full_name": "中国农村经济",
        "publisher": "中国社会科学院农村发展研究所",
        "issn": "1002-8870",
    },
    "国际金融研究": {
        "code": "GJJRYJ",
        "full_name": "国际金融研究",
        "publisher": "中国国际金融学会",
        "issn": "1006-1029",
    },
}

# ============================================================
# CNKI 相关配置
# ============================================================
CNKI_SEARCH_URL = "https://kns.cnki.net/kns8s/defaultresult/index"
CNKI_NAVI_URL = "https://navi.cnki.net/"
CNKI_ARTICLE_BASE = "https://kns.cnki.net/kcms2/article/abstract"

# ============================================================
# Google Scholar 配置（备用搜索渠道）
# ============================================================
GOOGLE_SCHOLAR_URL = "https://scholar.google.com/scholar"
BING_ACADEMIC_URL = "https://cn.bing.com/academic/search"

# ============================================================
# 搜索容错配置
# ============================================================
# CNKI搜索失败时是否自动切换备用搜索引擎
ENABLE_SEARCH_FALLBACK = os.getenv("ENABLE_SEARCH_FALLBACK", "true").lower() == "true"

# ============================================================
# 爬虫行为配置
# ============================================================
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "30"))
WAIT_TIME_MIN = float(os.getenv("WAIT_TIME_MIN", "2.0"))
WAIT_TIME_MAX = float(os.getenv("WAIT_TIME_MAX", "5.0"))

# ============================================================
# 日志配置
# ============================================================
LOG_LEVEL = "DEBUG" if DEBUG else "INFO"
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"


# ============================================================
# 初始化：确保目录存在 + 验证写入权限
# ============================================================
def ensure_dirs() -> list:
    """
    确保所有必需目录存在且可写。

    Returns:
        可用目录列表；如果写入失败返回信息。
    """
    dirs_info = []
    for d in [DATA_DIR, OUTPUTS_DIR, LOGS_DIR, CONFIG_DIR, USER_DATA_DIR]:
        try:
            d.mkdir(parents=True, exist_ok=True)
            # 验证写入权限
            test_file = d / ".write_test"
            test_file.write_text("ok")
            test_file.unlink()
            dirs_info.append(f"  {d} [OK]")
        except PermissionError:
            dirs_info.append(f"  {d} [NO WRITE PERMISSION]")
        except Exception as e:
            dirs_info.append(f"  {d} [ERROR: {e}]")
    return dirs_info


# 自动初始化（import 时执行）
_dir_status = ensure_dirs()

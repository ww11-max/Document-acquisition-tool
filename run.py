"""
eco-acquire CLI入口
"""

import argparse
import sys
import logging
from pathlib import Path

# 确保项目根目录在路径中
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.workflow import EcoAcquireWorkflow, setup_logging


def main():
    parser = argparse.ArgumentParser(
        description="eco-acquire: 经济学期刊文献智能获取工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
使用示例:
  # 关键词搜索并下载
  python run.py --keywords "FDI" "国际直接投资" --max-results 10

  # 指定期刊搜索
  python run.py --journal "经济研究" --keywords "数字经济" --max-results 5

  # 按作者筛选
  python run.py --keywords "企业创新" --author "张三" --max-results 10

  # 近三年文献
  python run.py --keywords "FDI" --year-start 2023 --year-end 2026 --max-results 20

  # 精确定位一篇文献
  python run.py --exact-title "数字经济对FDI的影响" --author "李四" --journal "世界经济" --year-start 2024

  # 仅搜索不下载
  python run.py --keywords "人民币国际化" --no-download

  # AI Planning 模式：执行 AI 生成的文献清单
  python run.py --batch literature_list.json

  # AI Planning + 连接用户浏览器
  python run.py --batch literature_list.json --connect 9222

  # 自定义任务名
  python run.py --keywords "企业创新" --task-name "创新文献"
        """
    )

    # 搜索参数
    parser.add_argument("--keywords", nargs="+", help="搜索关键词（可多个）")
    parser.add_argument("--batch", metavar="JSON_FILE",
                        help="AI Planning 模式：指定文献清单 JSON 文件路径")
    parser.add_argument("--journal", help="限定期刊名称")
    parser.add_argument("--author", help="按作者姓名筛选")
    parser.add_argument("--exact-title", help="精确文章标题（用于定位单篇文献）")
    parser.add_argument("--year-start", type=int, help="起始年份（含）")
    parser.add_argument("--year-end", type=int, help="结束年份（含）")
    parser.add_argument("--max-results", type=int, default=10, help="最大结果数（默认10）")

    # 行为参数
    parser.add_argument("--browser", choices=["auto", "chrome", "edge", "firefox"],
                        default="auto", help="浏览器选择（默认auto自动检测）")
    parser.add_argument("--connect", type=int, metavar="PORT",
                        help="连接已打开的浏览器（需先用 --remote-debugging-port=PORT 启动）")
    parser.add_argument("--task-name", help="自定义任务名称")
    parser.add_argument("--no-download", action="store_true", help="仅搜索，不下载PDF")
    parser.add_argument("--no-conclusion", action="store_true", help="不提取结论摘要")
    parser.add_argument("--headless", action="store_true", help="使用无头浏览器模式")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    parser.add_argument("--list-journals", action="store_true", help="列出支持的期刊")

    args = parser.parse_args()

    # 列出期刊
    if args.list_journals:
        from config.settings import TARGET_JOURNALS
        print("\n=== eco-acquire 支持的经济学期刊 ===\n")
        for i, (name, info) in enumerate(TARGET_JOURNALS.items(), 1):
            print(f"  {i:2d}. {name}")
            print(f"      ISSN: {info['issn']}  |  主办: {info['publisher']}")
        print(f"\n共 {len(TARGET_JOURNALS)} 本期刊\n")
        return

    # 校验参数
    if not args.batch and not args.keywords and not args.journal and not args.author and not args.exact_title:
        parser.error("请提供 --batch、--keywords、--journal、--author 或 --exact-title 参数")
        return

    # 设置日志
    setup_logging(args.log_level)
    logger = logging.getLogger(__name__)

    # 执行工作流
    browser_val = args.browser if args.browser != "auto" else None

    # ========================================
    # AI Planning 模式：批量执行文献清单
    # ========================================
    if args.batch:
        workflow = EcoAcquireWorkflow(headless=args.headless, browser=browser_val,
                                       connect_port=args.connect)

        try:
            print(f"\n{'='*60}")
            print(f"  eco-acquire AI Planning 模式")
            print(f"{'='*60}\n")
            print(f"  文献清单: {args.batch}\n")

            report = workflow.run_batch(
                batch_file=args.batch,
                download=not args.no_download,
                extract_conclusion=not args.no_conclusion,
                connect_port=args.connect,
                global_journal=args.journal,
                global_year_start=args.year_start,
                global_year_end=args.year_end,
            )

            print(f"\n{'='*60}")
            print(f"  任务完成: {report.get('status', 'unknown')}")
            print(f"  总计: {report.get('total_papers', 0)} 篇")
            print(f"  找到: {report.get('success_count', 0)} 篇")
            print(f"  未找到: {report.get('fail_count', 0)} 篇")
            print(f"  输出目录: {report.get('task_dir', 'N/A')}")
            if report.get("error"):
                print(f"  错误: {report['error']}")
            print(f"{'='*60}\n")

        except KeyboardInterrupt:
            logger.info("用户中断")
        except Exception as e:
            logger.error(f"执行出错: {e}")
            raise
        return

    # ========================================
    # 传统模式：单次搜索
    # ========================================
    workflow = EcoAcquireWorkflow(headless=args.headless, browser=browser_val,
                                   connect_port=args.connect)

    try:
        print(f"\n{'='*60}")
        print(f"  eco-acquire 经济学文献获取工具")
        print(f"{'='*60}\n")

        # 显示搜索条件
        print("  搜索条件:")
        if args.exact_title:
            print(f"    精确标题: {args.exact_title}")
        if args.keywords:
            print(f"    关键词: {', '.join(args.keywords)}")
        if args.author:
            print(f"    作者: {args.author}")
        if args.journal:
            print(f"    期刊: {args.journal}")
        if args.year_start or args.year_end:
            print(f"    年份: {args.year_start or '不限'} - {args.year_end or '不限'}")
        print()

        report = workflow.run(
            keywords=args.keywords,
            journal=args.journal,
            author=args.author,
            exact_title=args.exact_title,
            year_start=args.year_start,
            year_end=args.year_end,
            max_results=args.max_results,
            download=not args.no_download,
            extract_conclusion=not args.no_conclusion,
            task_name=args.task_name,
        )

        # 打印摘要
        print(f"\n{'='*60}")
        print(f"  任务完成: {report.get('status', 'unknown')}")
        print(f"  搜索到: {report.get('search_count', 0)} 篇")
        print(f"  下载成功: {report.get('success_count', 0)} 篇")
        print(f"  下载失败: {report.get('fail_count', 0)} 篇")
        if report.get('fallback_success'):
            print(f"  备用渠道下载: {len(report.get('fallback_success', []))} 篇")
        print(f"  输出目录: {report.get('task_dir', 'N/A')}")
        if report.get("error"):
            print(f"  错误: {report['error']}")
        print(f"{'='*60}\n")

    except KeyboardInterrupt:
        logger.info("用户中断")
    except Exception as e:
        logger.error(f"执行出错: {e}")
        raise


if __name__ == "__main__":
    main()

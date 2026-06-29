"""
eco-acquire CLI入口 — 零配置版
经济学文献题录检索工具 — 搜索、提取摘要与元数据、生成结构化报告

默认使用 Profile 模式：自动启动浏览器 + 持久化登录态。
首次使用运行 --setup 完成登录，后续无需任何配置。
"""

import argparse
import sys
import logging
from pathlib import Path

# 确保项目根目录在路径中
PROJECT_ROOT = Path(__file__).parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.workflow import EcoAcquireWorkflow, setup_logging
from src.driver_manager import BrowserManager
from config import settings


def main():
    parser = argparse.ArgumentParser(
        description="eco-acquire: 经济学文献题录检索工具（零配置版）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
使用示例:
  # 首次使用：完成登录（只需一次）
  python run.py --setup

  # 关键词搜索（最常用）
  python run.py --keywords "数字经济" --max-results 10

  # 指定期刊 + 年份范围
  python run.py --keywords "全球价值链" --journal "世界经济" --year-start 2023 --year-end 2026

  # 精确定位一篇文献
  python run.py --exact-title "数字经济对FDI的影响" --author "李四" --journal "世界经济"

  # AI Planning 模式：执行 AI 生成的检索计划
  python run.py --batch search_plan.json

  # 列出支持的期刊
  python run.py --list-journals

  # 兼容模式：连接已有浏览器
  python run.py --keywords "数字经济" --connect 9222
        """
    )

    # 搜索参数
    parser.add_argument("--keywords", nargs="+", help="搜索关键词（可多个）")
    parser.add_argument("--batch", metavar="JSON_FILE",
                        help="AI Planning 模式：指定检索计划 JSON 文件路径")
    parser.add_argument("--journal", help="限定期刊名称")
    parser.add_argument("--author", help="按作者姓名筛选")
    parser.add_argument("--exact-title", help="精确文章标题（用于定位单篇文献）")
    parser.add_argument("--year-start", type=int, help="起始年份（含）")
    parser.add_argument("--year-end", type=int, help="结束年份（含）")
    parser.add_argument("--max-results", type=int, default=20, help="最大结果数（默认20）")

    # 行为参数
    parser.add_argument("--browser", choices=["auto", "chrome", "edge", "firefox"],
                        default="auto", help="浏览器选择（默认auto自动检测）")
    parser.add_argument("--connect", type=int, metavar="PORT",
                        help="兼容模式：连接已打开的浏览器（需先用 --remote-debugging-port=PORT 启动）")
    parser.add_argument("--task-name", help="自定义任务名称")
    parser.add_argument("--no-abstract", action="store_true", help="不提取摘要")
    parser.add_argument("--headless", action="store_true", help="使用无头浏览器模式")
    parser.add_argument("--no-profile", action="store_true",
                        help="不使用 profile 持久化（每次都需要登录）")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    parser.add_argument("--list-journals", action="store_true", help="列出支持的期刊")

    # 零配置核心命令
    parser.add_argument("--setup", action="store_true",
                        help="首次使用：交互式登录，完成后登录态永久保存")
    parser.add_argument("--setup-timeout", type=int, default=300,
                        help="--setup 登录超时秒数（默认300秒=5分钟）")

    args = parser.parse_args()

    # 列出期刊
    if args.list_journals:
        from config.settings import TARGET_JOURNALS
        print("\n=== eco-acquire 常用经济学期刊参考（非检索限制） ===")
        print("  CNKI 所有期刊均可检索，以下仅为常用期刊的 ISSN 参考。")
        print("  不指定 --journal 参数即可搜索全部期刊。\n")
        for i, (name, info) in enumerate(TARGET_JOURNALS.items(), 1):
            print(f"  {i:2d}. {name}")
            print(f"      ISSN: {info['issn']}  |  主办: {info['publisher']}")
        print(f"\n共 {len(TARGET_JOURNALS)} 本常用期刊（参考列表，非检索限制）\n")
        return

    # ========================================
    # --setup 交互式登录
    # ========================================
    if args.setup:
        print("\n" + "=" * 60)
        print("  eco-acquire 首次设置向导")
        print("=" * 60)
        print(f"  浏览器 profile 目录: {settings.USER_DATA_DIR}")
        print(f"  超时时间: {args.setup_timeout} 秒")
        print()

        manager = BrowserManager(
            headless=False,  # 登录必须可视化
            download_dir=str(settings.OUTPUTS_DIR),
            browser=args.browser if args.browser != "auto" else None,
            use_profile=True,
        )
        try:
            success = manager.setup_login(timeout=args.setup_timeout)
            if success:
                print("=" * 60)
                print("  🎉 设置完成！")
                print("  登录态已保存到:", settings.USER_DATA_DIR)
                print()
                print("  现在可以直接搜索了，例如：")
                print('    python run.py --keywords "数字经济" --journal "经济研究" --year-start 2023')
                print("=" * 60)
            else:
                print("=" * 60)
                print("  ⚠️ 登录超时，请重试:")
                print("    python run.py --setup --setup-timeout 600")
                print("=" * 60)
        finally:
            manager.close()
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
    use_profile = not args.no_profile

    # ========================================
    # AI Planning 模式：批量执行检索计划
    # ========================================
    if args.batch:
        workflow = EcoAcquireWorkflow(headless=args.headless, browser=browser_val,
                                       connect_port=args.connect,
                                       use_profile=use_profile)

        try:
            print(f"\n{'='*60}")
            print(f"  eco-acquire AI Planning 模式")
            print(f"{'='*60}\n")
            print(f"  检索计划: {args.batch}\n")

            report = workflow.run_batch(
                batch_file=args.batch,
                extract_abstract=not args.no_abstract,
                connect_port=args.connect,
                global_journal=args.journal,
                global_year_start=args.year_start,
                global_year_end=args.year_end,
            )

            print(f"\n{'='*60}")
            print(f"  任务完成: {report.get('status', 'unknown')}")
            print(f"  总计: {report.get('total_papers', 0)} 项检索")
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
    # 直接搜索模式
    # ========================================
    workflow = EcoAcquireWorkflow(headless=args.headless, browser=browser_val,
                                   connect_port=args.connect,
                                   use_profile=use_profile)

    try:
        print(f"\n{'='*60}")
        print(f"  eco-acquire 经济学文献题录检索")
        print(f"{'='*60}\n")

        # 显示搜索条件
        print("  检索条件:")
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
            extract_abstract=not args.no_abstract,
            task_name=args.task_name,
        )

        # 打印摘要
        print(f"\n{'='*60}")
        print(f"  任务完成: {report.get('status', 'unknown')}")
        print(f"  检索到: {report.get('search_count', 0)} 篇")
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
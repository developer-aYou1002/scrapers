from __future__ import annotations

import argparse
import json
import logging
import math
import sqlite3
import sys
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import requests

from .crawler import DEFAULT_URL, ScrapeConfig, create_session, scrape
from .database import (
    CrawlCheckpoint,
    CrawlRunOutcome,
    DetailCandidate,
    DetailSelection,
    crawl_task_lock,
    database_connection,
    delete_checkpoint,
    finish_crawl_run,
    load_detail_candidates,
    load_rank_candidates,
    load_checkpoint,
    now_iso,
    refresh_crawl_lock,
    ranking_counts,
    rank_daily_visit_count,
    record_rank_visit,
    replace_rankings,
    save_detail_result,
    save_provider_results,
    save_checkpoint,
    start_crawl_run,
    upsert_games,
    upsert_list_games,
)
from .daily import (
    DailyListConfig,
    DailyListLock,
    DailyListProgress,
    crawl_daily_list,
)
from .detail_enricher import (
    DetailResolver,
    EnrichConfig,
    build_detail_update,
    wait_between_candidates,
)
from .errors import DailyQuotaExceeded, LoginStateExpired, TaskInterrupted
from .exporter import export_database
from .list_crawler import (
    DEFAULT_LIST_URL,
    FullListConfig,
    fetch_list_page,
    wait_for_next_page,
)
from .login import save_login_state
from .logging_setup import LoggingConfig, configure_logging
from .ranking import fetch_hot_ranking


DEFAULT_LOGIN_URL = "https://www.xdgame.com/user/"
PROJECT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT = PROJECT_DIR / "output/today_games.json"
DEFAULT_EXPORT_OUTPUT = PROJECT_DIR / "output/games.json"
DEFAULT_LOGIN_STATE = PROJECT_DIR / "login_state.json"
DEFAULT_DATABASE = PROJECT_DIR / "data/xdgame.db"
DEFAULT_LOG_FILE = PROJECT_DIR / "logs/xdgame.log"
DEFAULT_DELAY_MIN = 2.0
DEFAULT_DELAY_MAX = 4.0
DEFAULT_MAX_PAGES = 1000
DEFAULT_DAILY_MAX_PAGES = 10
DEFAULT_EXISTING_PAGE_LIMIT = 3
DEFAULT_BATCH_SIZE = 20
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_RECHECK_DAYS = 30
DETAIL_TASK_KEY = "detail-enrichment"
EXIT_FAILURE = 1
EXIT_LOGIN_EXPIRED = 3
EXIT_DAILY_QUOTA = 4
EXIT_INTERRUPTED = 130
LOGGER = logging.getLogger(__name__)


@dataclass
class FullListProgress:
    task_key: str
    lock_owner: str
    current_url: str | None
    pages: int = 0
    games: int = 0


@dataclass
class EnrichProgress:
    selected: int
    processed: int = 0
    completed: int = 0
    errors: int = 0
    has_more: bool = False


@dataclass(frozen=True)
class EnrichBatch:
    candidates: list[DetailCandidate]
    config: EnrichConfig
    lock_owner: str
    task_key: str
    ranked: bool = False


@dataclass
class DailyProgress:
    list_progress: DailyListProgress
    detail_progress: EnrichProgress


@dataclass(frozen=True)
class DailyRuntime:
    list_config: DailyListConfig
    enrich_config: EnrichConfig
    selection: DetailSelection
    list_lock: DailyListLock
    detail_lock: DailyListLock


class FullListLimitReached(RuntimeError):
    """达到安全页数上限，但网站仍存在下一页。"""


def save_json(games: list[dict[str, Any]], source_url: str, output: Path) -> None:
    result = {
        "source": source_url,
        "fetched_at": now_iso(),
        "count": len(games),
        "quark_count": sum(bool(game["quark_url"]) for game in games),
        "games": games,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="抓取并增量维护 XDGAME 游戏数据")
    parser.add_argument(
        "command",
        nargs="?",
        choices=(
            "today", "login", "full-list", "enrich", "daily", "export",
            "rank-scan", "rank-enrich",
        ),
        default="today",
    )
    add_list_arguments(parser)
    add_enrich_arguments(parser)
    add_common_arguments(parser)
    return parser.parse_args()


def add_list_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--url", default=DEFAULT_URL, help="目标首页 URL")
    parser.add_argument("--list-url", default=DEFAULT_LIST_URL, help="电脑游戏列表入口 URL")
    parser.add_argument(
        "--max-pages",
        type=int,
        default=None,
        help="列表扫描页数上限（full-list 默认 1000，daily 默认 10）",
    )
    parser.add_argument(
        "--existing-page-limit",
        type=int,
        default=DEFAULT_EXISTING_PAGE_LIMIT,
        help="日常扫描连续无新增页面停止阈值",
    )
    parser.add_argument(
        "--restart",
        action="store_true",
        help="忽略全量列表断点并从第一页重新扫描",
    )


def add_enrich_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help="详情补全单批处理数量",
    )
    parser.add_argument(
        "--batch-interval",
        type=float,
        default=None,
        help="enrich 专用：批次间隔秒数；指定后持续处理至无候选记录",
    )
    parser.add_argument(
        "--max-attempts",
        type=int,
        default=DEFAULT_MAX_ATTEMPTS,
        help="单条详情最大失败尝试次数",
    )
    parser.add_argument(
        "--recheck-days",
        type=int,
        default=DEFAULT_RECHECK_DAYS,
        help="已成功夸克地址的复查周期天数",
    )
    parser.add_argument(
        "--daily-game-limit",
        type=int,
        default=40,
        help="rank-enrich 每个北京时间自然日最多访问的不同游戏数，默认 40",
    )


def add_common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help="JSON 输出路径（today 和 export 使用各自默认文件）",
    )
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE, help="SQLite 数据库路径")
    parser.add_argument("--log-file", type=Path, default=DEFAULT_LOG_FILE, help="轮转日志文件路径")
    parser.add_argument("--timeout", type=float, default=20, help="请求超时秒数")
    parser.add_argument("--login-state", type=Path, default=DEFAULT_LOGIN_STATE)
    parser.add_argument("--login", action="store_true", help="打开浏览器并保存登录状态")
    parser.add_argument("--login-url", default=DEFAULT_LOGIN_URL, help="登录页 URL")
    parser.add_argument("--login-timeout", type=float, default=180, help="等待登录的秒数")
    parser.add_argument("--show-browser", action="store_true", help="抓取时显示浏览器")
    parser.add_argument("--delay-min", type=float, default=DEFAULT_DELAY_MIN, help="请求最小间隔秒数")
    parser.add_argument("--delay-max", type=float, default=DEFAULT_DELAY_MAX, help="请求最大间隔秒数")


def build_scrape_config(args: argparse.Namespace) -> ScrapeConfig:
    validate_delay_range(args)
    return ScrapeConfig(
        url=args.url,
        timeout=args.timeout,
        login_state=args.login_state,
        headless=not args.show_browser,
        delay_min=args.delay_min,
        delay_max=args.delay_max,
    )


def build_full_list_config(args: argparse.Namespace) -> FullListConfig:
    max_pages = resolve_max_pages(args.max_pages, DEFAULT_MAX_PAGES)
    validate_delay_range(args)
    return FullListConfig(
        url=args.list_url,
        timeout=args.timeout,
        max_pages=max_pages,
        delay_min=args.delay_min,
        delay_max=args.delay_max,
    )


def build_daily_list_config(args: argparse.Namespace) -> DailyListConfig:
    max_pages = resolve_max_pages(args.max_pages, DEFAULT_DAILY_MAX_PAGES)
    if args.existing_page_limit <= 0:
        raise ValueError("existing-page-limit 必须大于 0")
    validate_delay_range(args)
    return DailyListConfig(
        url=args.list_url,
        timeout=args.timeout,
        max_pages=max_pages,
        existing_page_limit=args.existing_page_limit,
        delay_min=args.delay_min,
        delay_max=args.delay_max,
    )


def resolve_max_pages(value: int | None, default: int) -> int:
    result = default if value is None else value
    if result <= 0:
        raise ValueError("max-pages 必须大于 0")
    return result


def build_enrich_config(args: argparse.Namespace) -> EnrichConfig:
    if args.timeout <= 0:
        raise ValueError("timeout 必须大于 0")
    validate_delay_range(args)
    return EnrichConfig(
        timeout=args.timeout,
        login_state=args.login_state,
        headless=not args.show_browser,
        delay_min=args.delay_min,
        delay_max=args.delay_max,
        max_attempts=args.max_attempts,
    )


def validate_delay_range(args: argparse.Namespace) -> None:
    if args.delay_min < 0 or args.delay_max < args.delay_min:
        raise ValueError("请求间隔必须满足 0 <= delay-min <= delay-max")


def build_detail_selection(
    args: argparse.Namespace,
    priority_urls: tuple[str, ...] = (),
) -> DetailSelection:
    if args.batch_size <= 0:
        raise ValueError("batch-size 必须大于 0")
    if args.max_attempts <= 0:
        raise ValueError("max-attempts 必须大于 0")
    if args.recheck_days < 0:
        raise ValueError("recheck-days 不能小于 0")
    return DetailSelection(
        args.batch_size,
        args.max_attempts,
        args.recheck_days,
        priority_urls,
    )


def record_run_outcome(
    connection: sqlite3.Connection,
    run_id: int,
    outcome: CrawlRunOutcome,
) -> None:
    connection.rollback()
    try:
        with connection:
            finish_crawl_run(connection, run_id, outcome)
    except sqlite3.Error:
        # 数据库自身故障时保留原始异常，避免记录失败状态再次失败后掩盖根因。
        pass


def run_scrape(args: argparse.Namespace) -> list[dict[str, Any]]:
    config = build_scrape_config(args)
    output = args.output or DEFAULT_OUTPUT
    with database_connection(args.database) as connection:
        # 先提交 running 记录，网络或解析失败时才能在独立事务中留下失败结果。
        with connection:
            run_id = start_crawl_run(connection, "today")
        try:
            games = scrape(config)
            # 游戏数据与成功状态原子提交，避免运行标记成功但游戏只写入一部分。
            with connection:
                upsert_games(connection, games, "今日更新")
                save_json(games, args.url, output)
                outcome = CrawlRunOutcome("success", len(games))
                finish_crawl_run(connection, run_id, outcome)
        except Exception as error:
            outcome = CrawlRunOutcome("failed", 0, str(error)[:1000])
            record_run_outcome(connection, run_id, outcome)
            raise
    return games


def crawl_full_list(
    connection: sqlite3.Connection,
    config: FullListConfig,
    progress: FullListProgress,
) -> None:
    visited: set[str] = set()
    with create_session() as session:
        while progress.current_url is not None:
            if progress.current_url in visited:
                raise RuntimeError(f"检测到重复分页链接：{progress.current_url}")
            visited.add(progress.current_url)
            page = fetch_list_page(session, progress.current_url, config.timeout)
            last_game_url = page.games[-1]["url"]
            # 页面数据、下一页断点和锁心跳必须原子提交，失败时三者一起回滚。
            with connection:
                upsert_list_games(connection, page.games)
                checkpoint = CrawlCheckpoint(page.next_url, last_game_url)
                save_checkpoint(connection, progress.task_key, checkpoint)
                refresh_crawl_lock(connection, progress.task_key, progress.lock_owner)
            progress.pages += 1
            progress.games += len(page.games)
            LOGGER.info(
                "已写入第 %d 页，本页 %d 条，累计 %d 条",
                progress.pages,
                len(page.games),
                progress.games,
            )
            progress.current_url = page.next_url
            if progress.current_url is not None and progress.pages >= config.max_pages:
                raise FullListLimitReached(
                    f"达到安全页数上限 {config.max_pages}，全量抓取尚未结束"
                )
            if progress.current_url is not None:
                wait_for_next_page(config)


def full_list_task_key(config: FullListConfig) -> str:
    return f"full-list:{config.url}"


def checkpoint_start_url(
    connection: sqlite3.Connection,
    config: FullListConfig,
    restart: bool,
) -> str | None:
    task_key = full_list_task_key(config)
    if restart:
        with connection:
            delete_checkpoint(connection, task_key)
        return config.url
    checkpoint = load_checkpoint(connection, task_key)
    if checkpoint is None:
        return config.url
    return checkpoint.cursor


def run_full_list(args: argparse.Namespace) -> FullListProgress:
    config = build_full_list_config(args)
    with database_connection(args.database) as connection:
        task_key = full_list_task_key(config)
        # 锁必须覆盖 restart 删除和 checkpoint 读取，避免并发任务在两步之间抢跑。
        with crawl_task_lock(connection, task_key) as lock_owner:
            start_url = checkpoint_start_url(connection, config, args.restart)
            progress = FullListProgress(task_key, lock_owner, start_url)
            with connection:
                run_id = start_crawl_run(connection, "full-list")
            try:
                if progress.current_url is None:
                    LOGGER.info("全量列表 checkpoint 已完成；如需重扫请使用 --restart")
                else:
                    crawl_full_list(connection, config, progress)
                with connection:
                    outcome = CrawlRunOutcome("success", progress.games)
                    finish_crawl_run(connection, run_id, outcome)
            except (FullListLimitReached, KeyboardInterrupt) as error:
                summary = str(error) or "用户中断全量列表抓取"
                outcome = CrawlRunOutcome("incomplete", progress.games, summary[:1000])
                record_run_outcome(connection, run_id, outcome)
                if isinstance(error, KeyboardInterrupt):
                    raise TaskInterrupted("用户中断；已保留最近成功页面的断点") from None
                raise
            except Exception as error:
                outcome = CrawlRunOutcome("failed", progress.games, str(error)[:1000])
                record_run_outcome(connection, run_id, outcome)
                raise
    return progress


def enrich_candidates(
    connection: sqlite3.Connection,
    batch: EnrichBatch,
    progress: EnrichProgress,
) -> EnrichProgress:
    with DetailResolver(batch.config) as resolver:
        for index, candidate in enumerate(batch.candidates, start=1):
            LOGGER.info(
                "[%d/%d] 补全详情：%s",
                index,
                len(batch.candidates),
                candidate.name,
            )
            if batch.ranked:
                with connection:
                    record_rank_visit(connection, candidate.id)
            resolution = resolver.resolve(candidate)
            update = build_detail_update(
                candidate,
                resolution,
                batch.config.max_attempts,
            )
            with connection:
                save_detail_result(connection, candidate.id, update)
                save_provider_results(connection, candidate.id, resolution.provider_results)
                refresh_crawl_lock(connection, batch.task_key, batch.lock_owner)
            progress.processed += 1
            if update.detail_status == "complete":
                progress.completed += 1
            else:
                progress.errors += 1
            LOGGER.info(
                "详情状态：%s；夸克状态：%s；其他网盘成功：%d/%d；累计尝试：%d",
                update.detail_status,
                update.quark_status,
                sum(item.url is not None for item in resolution.provider_results),
                len(resolution.provider_results),
                update.detail_attempts,
            )
            if update.detail_status == "login_expired":
                raise LoginStateExpired("登录状态已失效，请先执行 login 后重新运行当前任务")
            wait_between_candidates(batch.config, index, len(batch.candidates))
    return progress


def run_enrich(args: argparse.Namespace) -> EnrichProgress:
    return run_detail_batch(args, ranked=False)


def run_rank_enrich(args: argparse.Namespace) -> EnrichProgress:
    return run_detail_batch(args, ranked=True)


def run_detail_batch(args: argparse.Namespace, ranked: bool) -> EnrichProgress:
    config = build_enrich_config(args)
    selection = build_detail_selection(args)
    if ranked and args.daily_game_limit <= 0:
        raise ValueError("daily-game-limit 必须大于 0")
    with database_connection(args.database) as connection:
        # 排名补全与普通补全共用详情锁，避免并发任务绕过当天访问额度。
        with crawl_task_lock(connection, DETAIL_TASK_KEY) as lock_owner:
            if ranked:
                total, _ = ranking_counts(connection)
                if total == 0:
                    raise RuntimeError("尚无人气榜快照，请先执行 rank-scan")
                remaining = max(0, args.daily_game_limit - rank_daily_visit_count(connection))
                candidates = load_rank_candidates(
                    connection, min(selection.limit, remaining), selection.max_attempts
                )
                if remaining == 0:
                    LOGGER.info("人气榜今日保守访问上限已达 %d 个游戏", args.daily_game_limit)
            else:
                candidates = load_detail_candidates(connection, selection)
            progress = EnrichProgress(selected=len(candidates))
            with connection:
                run_id = start_crawl_run(connection, "rank-enrich" if ranked else "enrich")
            try:
                batch = EnrichBatch(candidates, config, lock_owner, DETAIL_TASK_KEY, ranked)
                progress = enrich_candidates(connection, batch, progress)
                if not ranked:
                    progress.has_more = bool(
                        load_detail_candidates(connection, replace(selection, limit=1))
                    )
                status = "success" if progress.errors == 0 else "completed_with_errors"
                with connection:
                    finish_crawl_run(
                        connection,
                        run_id,
                        CrawlRunOutcome(status, progress.processed),
                    )
            except KeyboardInterrupt:
                outcome = CrawlRunOutcome("incomplete", progress.processed, "用户中断详情补全")
                record_run_outcome(connection, run_id, outcome)
                raise TaskInterrupted("用户中断；已保留逐条提交的详情结果") from None
            except DailyQuotaExceeded as error:
                outcome = CrawlRunOutcome("quota_exceeded", progress.processed, str(error))
                record_run_outcome(connection, run_id, outcome)
                raise
            except Exception as error:
                outcome = CrawlRunOutcome("failed", progress.processed, str(error)[:1000])
                record_run_outcome(connection, run_id, outcome)
                raise
    return progress


def run_rank_scan(args: argparse.Namespace) -> tuple[int, int]:
    if args.timeout <= 0:
        raise ValueError("timeout 必须大于 0")
    with database_connection(args.database) as connection:
        with crawl_task_lock(connection, "ranking:hot") as owner:
            with connection:
                run_id = start_crawl_run(connection, "rank-scan")
            try:
                rows = fetch_hot_ranking(args.timeout)
                with connection:
                    replace_rankings(connection, rows)
                    refresh_crawl_lock(connection, "ranking:hot", owner)
                    finish_crawl_run(connection, run_id, CrawlRunOutcome("success", len(rows)))
                return ranking_counts(connection)
            except KeyboardInterrupt:
                record_run_outcome(
                    connection, run_id, CrawlRunOutcome("incomplete", 0, "用户中断人气榜扫描")
                )
                raise TaskInterrupted("用户中断；旧榜单快照未改变") from None
            except Exception as error:
                record_run_outcome(connection, run_id, CrawlRunOutcome("failed", 0, str(error)[:1000]))
                raise


def execute_daily(
    connection: sqlite3.Connection,
    runtime: DailyRuntime,
    progress: DailyProgress,
) -> None:
    LOGGER.info("阶段开始：日常列表扫描")
    progress.list_progress = crawl_daily_list(
        connection,
        runtime.list_config,
        runtime.list_lock,
    )
    priority_urls = tuple(progress.list_progress.new_urls)
    selection = replace(runtime.selection, priority_urls=priority_urls)
    candidates = load_detail_candidates(connection, selection)
    progress.detail_progress = EnrichProgress(selected=len(candidates))
    LOGGER.info("阶段开始：详情补全；本批选取 %d 条", len(candidates))
    batch = EnrichBatch(
        candidates,
        runtime.enrich_config,
        runtime.detail_lock.owner,
        runtime.detail_lock.task_key,
    )
    enrich_candidates(connection, batch, progress.detail_progress)


def daily_outcome(progress: DailyProgress) -> CrawlRunOutcome:
    processed = daily_processed_count(progress)
    if progress.list_progress.incomplete:
        return CrawlRunOutcome("incomplete", processed, progress.list_progress.stop_reason)
    if progress.detail_progress.errors:
        summary = f"详情补全存在 {progress.detail_progress.errors} 条非成功状态"
        return CrawlRunOutcome("completed_with_errors", processed, summary)
    return CrawlRunOutcome("success", processed)


def daily_processed_count(progress: DailyProgress) -> int:
    return progress.list_progress.games + progress.detail_progress.processed


def run_daily(args: argparse.Namespace) -> DailyProgress:
    list_config = build_daily_list_config(args)
    enrich_config = build_enrich_config(args)
    selection = build_detail_selection(args)
    progress = DailyProgress(DailyListProgress(), EnrichProgress(0))
    list_task_key = f"full-list:{list_config.url}"
    with database_connection(args.database) as connection:
        # 固定按“列表锁、详情锁”顺序持有，既避免与独立任务重叠，也不会形成交叉等待。
        with crawl_task_lock(connection, list_task_key) as list_owner:
            with crawl_task_lock(connection, DETAIL_TASK_KEY) as detail_owner:
                runtime = DailyRuntime(
                    list_config,
                    enrich_config,
                    selection,
                    DailyListLock(list_task_key, list_owner),
                    DailyListLock(DETAIL_TASK_KEY, detail_owner),
                )
                run_daily_locked(connection, runtime, progress)
    return progress


def run_daily_locked(
    connection: sqlite3.Connection,
    runtime: DailyRuntime,
    progress: DailyProgress,
) -> None:
    with connection:
        run_id = start_crawl_run(connection, "daily")
    try:
        execute_daily(connection, runtime, progress)
        with connection:
            finish_crawl_run(connection, run_id, daily_outcome(progress))
    except KeyboardInterrupt:
        outcome = CrawlRunOutcome(
            "incomplete",
            daily_processed_count(progress),
            "用户中断日常更新",
        )
        record_run_outcome(connection, run_id, outcome)
        raise TaskInterrupted("用户中断；已保留逐页和逐条提交的结果") from None
    except DailyQuotaExceeded as error:
        outcome = CrawlRunOutcome("quota_exceeded", daily_processed_count(progress), str(error))
        record_run_outcome(connection, run_id, outcome)
        raise
    except Exception as error:
        outcome = CrawlRunOutcome(
            "failed",
            daily_processed_count(progress),
            str(error)[:1000],
        )
        record_run_outcome(connection, run_id, outcome)
        raise


def main() -> int:
    args = parse_args()
    if not initialize_logging(args.log_file):
        return EXIT_FAILURE
    started_at = time.monotonic()
    command = "login" if args.login else args.command
    LOGGER.info("任务开始：命令=%s", command)
    exit_code = execute_safely(args)
    elapsed = time.monotonic() - started_at
    log_run_finished(command, exit_code, elapsed)
    return exit_code


def initialize_logging(path: Path) -> bool:
    try:
        configure_logging(LoggingConfig(path))
    except OSError as error:
        sys.stderr.write(f"无法初始化日志文件：{error}\n")
        return False
    return True


def execute_safely(args: argparse.Namespace) -> int:
    try:
        return execute_command(args)
    except LoginStateExpired as error:
        LOGGER.error("登录状态已失效：%s；请执行 python scraper.py login 后重试", error)
        return EXIT_LOGIN_EXPIRED
    except DailyQuotaExceeded as error:
        LOGGER.error("下载额度已用完：%s；请等待网站额度恢复后继续", error)
        return EXIT_DAILY_QUOTA
    except (TaskInterrupted, KeyboardInterrupt) as error:
        LOGGER.error("用户中断任务：%s", str(error) or "已停止")
        return EXIT_INTERRUPTED
    except (requests.RequestException, sqlite3.Error, RuntimeError, ValueError, OSError) as error:
        LOGGER.error("执行失败：%s", error)
        return EXIT_FAILURE
    except Exception as error:
        # 未预期异常也必须让计划任务得到失败码；不输出堆栈可避免第三方异常携带敏感页面数据。
        LOGGER.error("未预期错误：%s", error)
        return EXIT_FAILURE


def execute_command(args: argparse.Namespace) -> int:
    if args.command == "login" or args.login:
        save_login_state(args.login_url, args.login_state, args.login_timeout)
        return 0
    if args.command == "full-list":
        return execute_full_list_command(args)
    if args.command == "enrich":
        return execute_enrich_command(args)
    if args.command == "rank-scan":
        total, matched = run_rank_scan(args)
        LOGGER.info("人气榜快照完成：共 %d 条，其中电脑游戏库匹配 %d 条", total, matched)
        return 0
    if args.command == "rank-enrich":
        if args.batch_interval is not None:
            raise ValueError("rank-enrich 只执行单批，不能使用 batch-interval")
        progress = run_rank_enrich(args)
        LOGGER.info(
            "人气榜详情补全完成：选取 %d 条，处理 %d 条，成功 %d 条，其他状态 %d 条",
            progress.selected, progress.processed, progress.completed, progress.errors,
        )
        return 0
    if args.command == "daily":
        return execute_daily_command(args)
    if args.command == "export":
        return execute_export_command(args)
    return execute_today_command(args)


def execute_full_list_command(args: argparse.Namespace) -> int:
    progress = run_full_list(args)
    LOGGER.info(
        "全量列表抓取完成：%d 页，共处理 %d 条记录；数据库=%s",
        progress.pages,
        progress.games,
        args.database,
    )
    return 0


def execute_enrich_command(args: argparse.Namespace) -> int:
    interval = args.batch_interval
    if interval is not None and (not math.isfinite(interval) or interval <= 0):
        raise ValueError("batch-interval 必须是大于 0 的有限秒数")
    while True:
        progress = run_enrich(args)
        LOGGER.info(
            "详情补全完成：选取 %d 条，处理 %d 条，成功 %d 条，其他状态 %d 条；数据库=%s",
            progress.selected,
            progress.processed,
            progress.completed,
            progress.errors,
            args.database,
        )
        if interval is None or not progress.has_more:
            return 0
        LOGGER.info("等待 %.1f 秒后处理下一批详情", interval)
        time.sleep(interval)


def execute_daily_command(args: argparse.Namespace) -> int:
    progress = run_daily(args)
    log_daily_summary(progress, args.database)
    return EXIT_FAILURE if progress.list_progress.incomplete else 0


def execute_export_command(args: argparse.Namespace) -> int:
    output = args.output or DEFAULT_EXPORT_OUTPUT
    result = export_database(args.database, output)
    LOGGER.info(
        "数据库导出完成：共 %d 条，详情完成 %d 条，含夸克地址 %d 条、其他网盘地址 %d 条；输出=%s",
        result.count,
        result.complete_count,
        result.quark_count,
        result.other_disk_link_count,
        result.output,
    )
    return 0


def execute_today_command(args: argparse.Namespace) -> int:
    games = run_scrape(args)
    quark_count = sum(bool(game["quark_url"]) for game in games)
    output = args.output or DEFAULT_OUTPUT
    LOGGER.info(
        "今日更新完成：抓取 %d 个游戏，%d 个获得夸克地址；输出=%s；数据库=%s",
        len(games),
        quark_count,
        output,
        args.database,
    )
    return 0


def log_run_finished(command: str, exit_code: int, elapsed: float) -> None:
    log = LOGGER.info if exit_code == 0 else LOGGER.error
    log("任务结束：命令=%s；退出码=%d；耗时=%.1f 秒", command, exit_code, elapsed)


def log_daily_summary(progress: DailyProgress, database: Path) -> None:
    listing = progress.list_progress
    details = progress.detail_progress
    LOGGER.info(
        "日常更新完成：扫描 %d 页、%d 条，新增 %d 条；"
        "详情处理 %d 条，成功 %d 条，其他状态 %d 条",
        listing.pages,
        listing.games,
        listing.new_games,
        details.processed,
        details.completed,
        details.errors,
    )
    LOGGER.info("停止原因：%s", listing.stop_reason)
    if listing.incomplete:
        LOGGER.error("日常扫描尚未安全收敛，请扩大 --max-pages 后重新运行")
    LOGGER.info("数据库已更新：%s", database)

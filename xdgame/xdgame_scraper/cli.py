from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

from .crawler import DEFAULT_URL, ScrapeConfig, create_session, scrape
from .database import (
    CrawlCheckpoint,
    CrawlRunOutcome,
    crawl_task_lock,
    database_connection,
    delete_checkpoint,
    finish_crawl_run,
    load_checkpoint,
    now_iso,
    refresh_crawl_lock,
    save_checkpoint,
    start_crawl_run,
    upsert_games,
    upsert_list_games,
)
from .list_crawler import (
    DEFAULT_LIST_URL,
    FullListConfig,
    fetch_list_page,
    wait_for_next_page,
)
from .login import save_login_state


DEFAULT_LOGIN_URL = "https://www.xdgame.com/user/"
PROJECT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT = PROJECT_DIR / "output/today_games.json"
DEFAULT_LOGIN_STATE = PROJECT_DIR / "login_state.json"
DEFAULT_DATABASE = PROJECT_DIR / "data/xdgame.db"
DEFAULT_DELAY_MIN = 2.0
DEFAULT_DELAY_MAX = 4.0
DEFAULT_MAX_PAGES = 1000


@dataclass
class FullListProgress:
    task_key: str
    lock_owner: str
    current_url: str | None
    pages: int = 0
    games: int = 0


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
    parser = argparse.ArgumentParser(description="抓取 XDGAME 今日更新或电脑游戏全量列表")
    parser.add_argument(
        "command",
        nargs="?",
        choices=("today", "login", "full-list"),
        default="today",
    )
    parser.add_argument("--url", default=DEFAULT_URL, help="目标首页 URL")
    parser.add_argument("--list-url", default=DEFAULT_LIST_URL, help="电脑游戏列表入口 URL")
    parser.add_argument(
        "--max-pages",
        type=int,
        default=DEFAULT_MAX_PAGES,
        help="全量列表安全页数上限",
    )
    parser.add_argument(
        "--restart",
        action="store_true",
        help="忽略全量列表断点并从第一页重新扫描",
    )
    parser.add_argument("-o", "--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE, help="SQLite 数据库路径")
    parser.add_argument("--timeout", type=float, default=20, help="请求超时秒数")
    parser.add_argument("--login-state", type=Path, default=DEFAULT_LOGIN_STATE)
    parser.add_argument("--login", action="store_true", help="打开浏览器并保存登录状态")
    parser.add_argument("--login-url", default=DEFAULT_LOGIN_URL, help="登录页 URL")
    parser.add_argument("--login-timeout", type=float, default=180, help="等待登录的秒数")
    parser.add_argument("--show-browser", action="store_true", help="抓取时显示浏览器")
    parser.add_argument("--delay-min", type=float, default=DEFAULT_DELAY_MIN, help="请求最小间隔秒数")
    parser.add_argument("--delay-max", type=float, default=DEFAULT_DELAY_MAX, help="请求最大间隔秒数")
    return parser.parse_args()


def build_scrape_config(args: argparse.Namespace) -> ScrapeConfig:
    if args.delay_min < 0 or args.delay_max < args.delay_min:
        raise ValueError("请求间隔必须满足 0 <= delay-min <= delay-max")
    return ScrapeConfig(
        url=args.url,
        timeout=args.timeout,
        login_state=args.login_state,
        headless=not args.show_browser,
        delay_min=args.delay_min,
        delay_max=args.delay_max,
    )


def build_full_list_config(args: argparse.Namespace) -> FullListConfig:
    if args.max_pages <= 0:
        raise ValueError("max-pages 必须大于 0")
    if args.delay_min < 0 or args.delay_max < args.delay_min:
        raise ValueError("请求间隔必须满足 0 <= delay-min <= delay-max")
    return FullListConfig(
        url=args.list_url,
        timeout=args.timeout,
        max_pages=args.max_pages,
        delay_min=args.delay_min,
        delay_max=args.delay_max,
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
    with database_connection(args.database) as connection:
        # 先提交 running 记录，网络或解析失败时才能在独立事务中留下失败结果。
        with connection:
            run_id = start_crawl_run(connection, "today")
        try:
            games = scrape(config)
            # 游戏数据与成功状态原子提交，避免运行标记成功但游戏只写入一部分。
            with connection:
                upsert_games(connection, games, "今日更新")
                save_json(games, args.url, args.output)
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
            print(
                f"已写入第 {progress.pages} 页，"
                f"本页 {len(page.games)} 条，累计 {progress.games} 条。"
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
                    print("全量列表 checkpoint 已完成；如需重扫请使用 --restart。")
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
                    raise RuntimeError("用户中断；已保留最近成功页面的断点") from None
                raise
            except Exception as error:
                outcome = CrawlRunOutcome("failed", progress.games, str(error)[:1000])
                record_run_outcome(connection, run_id, outcome)
                raise
    return progress


def main() -> int:
    args = parse_args()
    try:
        if args.command == "login" or args.login:
            save_login_state(args.login_url, args.login_state, args.login_timeout)
            return 0
        if args.command == "full-list":
            progress = run_full_list(args)
            print(
                f"全量列表抓取完成：{progress.pages} 页，"
                f"共处理 {progress.games} 条记录。"
            )
            print(f"数据库已更新：{args.database}")
            return 0
        games = run_scrape(args)
    except (requests.RequestException, sqlite3.Error, RuntimeError, ValueError, OSError) as error:
        print(f"执行失败：{error}", file=sys.stderr)
        return 1

    quark_count = sum(bool(game["quark_url"]) for game in games)
    print(f"已抓取 {len(games)} 个游戏，其中 {quark_count} 个获得夸克地址")
    print(f"结果已保存至：{args.output}")
    print(f"数据库已更新：{args.database}")
    return 0

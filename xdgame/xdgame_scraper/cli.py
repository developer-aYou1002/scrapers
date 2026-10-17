from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

import requests

from .crawler import DEFAULT_URL, ScrapeConfig, scrape
from .database import (
    CrawlRunOutcome,
    database_connection,
    finish_crawl_run,
    now_iso,
    start_crawl_run,
    upsert_games,
)
from .login import save_login_state


DEFAULT_LOGIN_URL = "https://www.xdgame.com/user/"
PROJECT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT = PROJECT_DIR / "output/today_games.json"
DEFAULT_LOGIN_STATE = PROJECT_DIR / "login_state.json"
DEFAULT_DATABASE = PROJECT_DIR / "data/xdgame.db"
DEFAULT_DELAY_MIN = 2.0
DEFAULT_DELAY_MAX = 4.0


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
    parser = argparse.ArgumentParser(description="抓取 XDGAME 今日更新及夸克网盘地址")
    parser.add_argument("--url", default=DEFAULT_URL, help="目标首页 URL")
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


def record_failed_run(connection: sqlite3.Connection, run_id: int, error: Exception) -> None:
    connection.rollback()
    summary = str(error)[:1000]
    try:
        with connection:
            finish_crawl_run(connection, run_id, CrawlRunOutcome("failed", 0, summary))
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
            record_failed_run(connection, run_id, error)
            raise
    return games


def main() -> int:
    args = parse_args()
    try:
        if args.login:
            save_login_state(args.login_url, args.login_state, args.login_timeout)
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

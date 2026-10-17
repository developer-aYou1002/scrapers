from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from .crawler import create_session
from .database import existing_game_urls, refresh_crawl_lock, upsert_list_games
from .list_crawler import ListPage, fetch_list_page, wait_for_next_page


@dataclass(frozen=True)
class DailyListConfig:
    url: str
    timeout: float
    max_pages: int
    existing_page_limit: int
    delay_min: float
    delay_max: float


@dataclass(frozen=True)
class DailyListLock:
    task_key: str
    owner: str


@dataclass
class DailyListProgress:
    pages: int = 0
    games: int = 0
    new_games: int = 0
    consecutive_existing_pages: int = 0
    new_urls: list[str] = field(default_factory=list)
    incomplete: bool = False
    stop_reason: str = ""


def store_daily_page(
    connection: sqlite3.Connection,
    page: ListPage,
    task_lock: DailyListLock,
) -> list[str]:
    detail_urls = [str(game["url"]) for game in page.games]
    # 已存在判断和 UPSERT 必须在同一事务中完成，避免“新增数”与实际写入结果不一致。
    with connection:
        known_urls = existing_game_urls(connection, detail_urls)
        upsert_list_games(connection, page.games)
        refresh_crawl_lock(connection, task_lock.task_key, task_lock.owner)
    return [url for url in detail_urls if url not in known_urls]


def update_daily_progress(
    progress: DailyListProgress,
    page: ListPage,
    new_urls: list[str],
) -> None:
    progress.pages += 1
    progress.games += len(page.games)
    progress.new_games += len(new_urls)
    progress.new_urls.extend(new_urls)
    if new_urls:
        progress.consecutive_existing_pages = 0
    else:
        progress.consecutive_existing_pages += 1


def should_stop_daily(
    progress: DailyListProgress,
    config: DailyListConfig,
    has_next_page: bool,
) -> bool:
    if not has_next_page:
        progress.stop_reason = "已到达列表末页"
        return True
    if progress.consecutive_existing_pages >= config.existing_page_limit:
        progress.stop_reason = f"连续 {config.existing_page_limit} 页没有发现新游戏"
        return True
    if progress.pages >= config.max_pages:
        # 只有到达上限时仍在发现新增才要求扩页；最后一页无新增表示新增区间已结束。
        progress.incomplete = progress.consecutive_existing_pages == 0
        if progress.incomplete:
            progress.stop_reason = (
                f"达到日常扫描页数上限 {config.max_pages}，最后一页仍发现新游戏"
            )
        else:
            progress.stop_reason = (
                f"达到日常扫描页数上限 {config.max_pages}，最后一页没有新游戏"
            )
        return True
    return False


def crawl_daily_list(
    connection: sqlite3.Connection,
    config: DailyListConfig,
    task_lock: DailyListLock,
) -> DailyListProgress:
    progress = DailyListProgress()
    current_url: str | None = config.url
    visited: set[str] = set()
    with create_session() as session:
        while current_url is not None:
            if current_url in visited:
                raise RuntimeError(f"检测到重复分页链接：{current_url}")
            visited.add(current_url)
            page = fetch_list_page(session, current_url, config.timeout)
            new_urls = store_daily_page(connection, page, task_lock)
            update_daily_progress(progress, page, new_urls)
            print(
                f"日常扫描第 {progress.pages} 页：本页 {len(page.games)} 条，"
                f"新增 {len(new_urls)} 条，累计新增 {progress.new_games} 条。"
            )
            if should_stop_daily(progress, config, page.next_url is not None):
                break
            current_url = page.next_url
            wait_for_next_page(config)
    return progress

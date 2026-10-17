from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


DATABASE_VERSION = 1
DATABASE_MIGRATIONS = {
    1: (
        """
        CREATE TABLE games (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            detail_url TEXT NOT NULL UNIQUE,
            category TEXT,
            quark_url TEXT,
            quark_status TEXT NOT NULL DEFAULT '待解析',
            detail_status TEXT NOT NULL DEFAULT 'pending',
            detail_attempts INTEGER NOT NULL DEFAULT 0 CHECK (detail_attempts >= 0),
            last_error TEXT,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            detail_updated_at TEXT,
            updated_at TEXT NOT NULL
        )
        """,
        "CREATE INDEX idx_games_detail_status ON games(detail_status, detail_attempts)",
        """
        CREATE TABLE crawl_checkpoints (
            task_key TEXT PRIMARY KEY,
            cursor TEXT,
            last_game_url TEXT,
            updated_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE crawl_runs (
            id INTEGER PRIMARY KEY,
            run_type TEXT NOT NULL,
            status TEXT NOT NULL,
            processed_count INTEGER NOT NULL DEFAULT 0 CHECK (processed_count >= 0),
            error_summary TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT
        )
        """,
        "CREATE INDEX idx_crawl_runs_started_at ON crawl_runs(started_at)",
    ),
}


@dataclass(frozen=True)
class CrawlCheckpoint:
    cursor: str | None
    last_game_url: str | None


@dataclass(frozen=True)
class CrawlRunOutcome:
    status: str
    processed_count: int
    error_summary: str | None = None


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


@contextmanager
def database_connection(path: Path) -> Iterator[sqlite3.Connection]:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        initialize_database(connection)
        yield connection
    finally:
        connection.close()


def initialize_database(connection: sqlite3.Connection) -> None:
    current_version = connection.execute("PRAGMA user_version").fetchone()[0]
    if current_version > DATABASE_VERSION:
        raise RuntimeError(
            f"数据库版本 {current_version} 高于程序支持的版本 {DATABASE_VERSION}"
        )
    if current_version == DATABASE_VERSION:
        return

    # 每个结构版本独立提交，失败时只回滚当前版本，已完成的旧版本仍可识别。
    for version in range(current_version + 1, DATABASE_VERSION + 1):
        statements = DATABASE_MIGRATIONS.get(version)
        if statements is None:
            raise RuntimeError(f"缺少数据库版本 {version} 的迁移定义")
        with connection:
            for statement in statements:
                connection.execute(statement)
            connection.execute(f"PRAGMA user_version = {version}")


def game_record(game: dict[str, Any], category: str | None, timestamp: str) -> tuple[Any, ...]:
    quark_url = game.get("quark_url")
    quark_status = str(game.get("quark_status") or "待解析")
    detail_status = "complete" if quark_url else "pending"
    detail_attempts = 0 if quark_url else 1
    last_error = quark_status if "失败" in quark_status else None
    return (
        game["name"],
        game["url"],
        category,
        quark_url,
        quark_status,
        detail_status,
        detail_attempts,
        last_error,
        timestamp,
        timestamp,
        timestamp,
        timestamp,
    )


def upsert_games(
    connection: sqlite3.Connection,
    games: list[dict[str, Any]],
    category: str | None,
) -> None:
    timestamp = now_iso()
    records = [game_record(game, category, timestamp) for game in games]
    # 新请求没有拿到地址时保留库中已有有效地址；尝试次数只累计连续未完成次数。
    connection.executemany(
        """
        INSERT INTO games (
            name, detail_url, category, quark_url, quark_status, detail_status,
            detail_attempts, last_error, first_seen_at, last_seen_at,
            detail_updated_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(detail_url) DO UPDATE SET
            name = excluded.name,
            category = COALESCE(excluded.category, games.category),
            quark_url = COALESCE(excluded.quark_url, games.quark_url),
            quark_status = CASE
                WHEN excluded.quark_url IS NOT NULL OR games.quark_url IS NULL
                THEN excluded.quark_status ELSE games.quark_status END,
            detail_status = CASE
                WHEN excluded.quark_url IS NOT NULL THEN 'complete'
                WHEN games.quark_url IS NOT NULL THEN games.detail_status
                ELSE excluded.detail_status END,
            detail_attempts = CASE
                WHEN excluded.quark_url IS NOT NULL OR games.quark_url IS NOT NULL THEN 0
                ELSE games.detail_attempts + 1 END,
            last_error = CASE
                WHEN excluded.quark_url IS NULL AND games.quark_url IS NULL
                THEN excluded.last_error ELSE NULL END,
            last_seen_at = excluded.last_seen_at,
            detail_updated_at = excluded.detail_updated_at,
            updated_at = excluded.updated_at
        """,
        records,
    )


def save_checkpoint(
    connection: sqlite3.Connection,
    task_key: str,
    checkpoint: CrawlCheckpoint,
) -> None:
    connection.execute(
        """
        INSERT INTO crawl_checkpoints (task_key, cursor, last_game_url, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(task_key) DO UPDATE SET
            cursor = excluded.cursor,
            last_game_url = excluded.last_game_url,
            updated_at = excluded.updated_at
        """,
        (task_key, checkpoint.cursor, checkpoint.last_game_url, now_iso()),
    )


def load_checkpoint(connection: sqlite3.Connection, task_key: str) -> CrawlCheckpoint | None:
    row = connection.execute(
        "SELECT cursor, last_game_url FROM crawl_checkpoints WHERE task_key = ?",
        (task_key,),
    ).fetchone()
    if row is None:
        return None
    return CrawlCheckpoint(cursor=row["cursor"], last_game_url=row["last_game_url"])


def start_crawl_run(connection: sqlite3.Connection, run_type: str) -> int:
    cursor = connection.execute(
        """
        INSERT INTO crawl_runs (run_type, status, started_at)
        VALUES (?, 'running', ?)
        """,
        (run_type, now_iso()),
    )
    return int(cursor.lastrowid)


def finish_crawl_run(
    connection: sqlite3.Connection,
    run_id: int,
    outcome: CrawlRunOutcome,
) -> None:
    connection.execute(
        """
        UPDATE crawl_runs
        SET status = ?, processed_count = ?, error_summary = ?, finished_at = ?
        WHERE id = ?
        """,
        (
            outcome.status,
            outcome.processed_count,
            outcome.error_summary,
            now_iso(),
            run_id,
        ),
    )

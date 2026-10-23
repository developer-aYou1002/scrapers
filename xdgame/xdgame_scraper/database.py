from __future__ import annotations

import sqlite3
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


DATABASE_VERSION = 5
LOCK_STALE_SECONDS = 30 * 60
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
    2: (
        "ALTER TABLE games ADD COLUMN score TEXT",
        "ALTER TABLE games ADD COLUMN size_text TEXT",
        "ALTER TABLE games ADD COLUMN source_updated_date TEXT",
        "ALTER TABLE games ADD COLUMN cover_url TEXT",
    ),
    3: (
        """
        CREATE TABLE crawl_locks (
            task_key TEXT PRIMARY KEY,
            owner TEXT NOT NULL,
            acquired_at REAL NOT NULL,
            heartbeat_at REAL NOT NULL
        )
        """,
    ),
    4: (
        "ALTER TABLE games ADD COLUMN quark_checked_at TEXT",
        "CREATE INDEX idx_games_quark_checked_at ON games(quark_checked_at)",
    ),
    5: (
        """
        CREATE TABLE game_disk_links (
            game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
            provider TEXT NOT NULL,
            url TEXT,
            status TEXT NOT NULL,
            last_error TEXT,
            checked_at TEXT NOT NULL,
            PRIMARY KEY (game_id, provider)
        )
        """,
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


@dataclass(frozen=True)
class DetailSelection:
    limit: int
    max_attempts: int
    recheck_days: int
    priority_urls: tuple[str, ...] = ()


@dataclass(frozen=True)
class DetailCandidate:
    id: int
    name: str
    detail_url: str
    quark_url: str | None
    detail_status: str
    detail_attempts: int


@dataclass(frozen=True)
class DetailUpdate:
    quark_url: str | None
    quark_status: str
    detail_status: str
    detail_attempts: int
    last_error: str | None
    checked_at: str | None


@dataclass(frozen=True)
class ProviderResult:
    provider: str
    url: str | None
    status: str
    error: str | None = None


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


@contextmanager
def readonly_database_connection(path: Path) -> Iterator[sqlite3.Connection]:
    if not path.is_file():
        raise FileNotFoundError(f"数据库不存在：{path}")
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version != DATABASE_VERSION:
            raise RuntimeError(
                f"数据库版本为 {version}，请先使用当前程序完成迁移到 {DATABASE_VERSION}"
            )
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
        timestamp if quark_url else None,
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
            detail_updated_at, updated_at, quark_checked_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            updated_at = excluded.updated_at,
            quark_checked_at = CASE WHEN excluded.quark_url IS NOT NULL
                THEN excluded.quark_checked_at ELSE games.quark_checked_at END
        """,
        records,
    )


def list_game_record(game: dict[str, Any], timestamp: str) -> tuple[Any, ...]:
    return (
        game["name"],
        game["url"],
        game.get("category"),
        game.get("score"),
        game.get("size"),
        game.get("source_updated_date"),
        game.get("cover_url"),
        timestamp,
        timestamp,
        timestamp,
    )


def upsert_list_games(
    connection: sqlite3.Connection,
    games: list[dict[str, Any]],
) -> None:
    timestamp = now_iso()
    records = [list_game_record(game, timestamp) for game in games]
    # 列表扫描只更新发现信息，不计入详情尝试次数，也不覆盖已取得的夸克地址。
    connection.executemany(
        """
        INSERT INTO games (
            name, detail_url, category, score, size_text, source_updated_date,
            cover_url, quark_url, quark_status, detail_status, detail_attempts,
            last_error, first_seen_at, last_seen_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, '待解析', 'pending', 0, NULL, ?, ?, ?)
        ON CONFLICT(detail_url) DO UPDATE SET
            name = excluded.name,
            category = COALESCE(excluded.category, games.category),
            score = COALESCE(excluded.score, games.score),
            size_text = COALESCE(excluded.size_text, games.size_text),
            source_updated_date = COALESCE(
                excluded.source_updated_date, games.source_updated_date
            ),
            cover_url = COALESCE(excluded.cover_url, games.cover_url),
            last_seen_at = excluded.last_seen_at,
            updated_at = excluded.updated_at
        """,
        records,
    )


def existing_game_urls(
    connection: sqlite3.Connection,
    detail_urls: list[str],
) -> set[str]:
    if not detail_urls:
        return set()
    placeholders = ", ".join("?" for _ in detail_urls)
    rows = connection.execute(
        f"SELECT detail_url FROM games WHERE detail_url IN ({placeholders})",
        detail_urls,
    ).fetchall()
    return {str(row["detail_url"]) for row in rows}


def load_games_for_export(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT id, name, detail_url, category, score, size_text,
               source_updated_date, cover_url, quark_url, quark_status,
               detail_status, detail_attempts, last_error, quark_checked_at,
               first_seen_at, last_seen_at, detail_updated_at, updated_at
        FROM games
        ORDER BY id
        """
    ).fetchall()
    games = [dict(row) for row in rows]
    by_id = {game["id"]: game for game in games}
    for game in games:
        game["other_disk_links"] = {}
    for row in connection.execute(
        """SELECT game_id, provider, url, status, last_error, checked_at
           FROM game_disk_links ORDER BY game_id, provider"""
    ):
        by_id[row["game_id"]]["other_disk_links"][row["provider"]] = {
            "url": row["url"],
            "status": row["status"],
            "last_error": row["last_error"],
            "checked_at": row["checked_at"],
        }
    return games


def load_detail_candidates(
    connection: sqlite3.Connection,
    selection: DetailSelection,
) -> list[DetailCandidate]:
    query, parameters = detail_selection_query(selection)
    rows = connection.execute(query, parameters).fetchall()
    return [detail_candidate_from_row(row) for row in rows]


def detail_selection_query(selection: DetailSelection) -> tuple[str, tuple[Any, ...]]:
    recheck_before = (
        datetime.now().astimezone() - timedelta(days=selection.recheck_days)
    ).isoformat(timespec="seconds")
    priority_urls = selection.priority_urls[: selection.limit]
    priority_order = ""
    if priority_urls:
        # 日常新增地址只需覆盖本批上限，避免大量 URL 占满 SQLite 绑定参数。
        placeholders = ", ".join("?" for _ in priority_urls)
        priority_order = f"CASE WHEN detail_url IN ({placeholders}) THEN 0 ELSE 1 END,"
    query = f"""
        SELECT id, name, detail_url, quark_url, detail_status, detail_attempts
        FROM games
        WHERE (
            detail_status IN ('pending', 'retry', 'recheck_retry', 'login_expired')
            AND detail_attempts < ?
        ) OR (
            detail_status = 'complete'
            AND COALESCE(quark_checked_at, detail_updated_at, first_seen_at) <= ?
        ) OR (
            detail_status = 'recheck_failed'
            AND detail_updated_at <= ?
        )
        ORDER BY
            {priority_order}
            CASE WHEN detail_status IN ('retry', 'login_expired', 'recheck_retry') THEN 0
                 WHEN detail_status = 'pending' THEN 1
                 ELSE 2 END,
            -- 全量列表从首页依次入库，普通 pending 按入库顺序补全；daily 新增由上方优先级单独保证。
            CASE WHEN detail_status = 'pending' THEN id END ASC,
            COALESCE(detail_updated_at, first_seen_at),
            id
        LIMIT ?
    """
    parameters: tuple[Any, ...] = (
        selection.max_attempts,
        recheck_before,
        recheck_before,
        *priority_urls,
        selection.limit,
    )
    return query, parameters


def detail_candidate_from_row(row: sqlite3.Row) -> DetailCandidate:
    return DetailCandidate(
        id=int(row["id"]),
        name=str(row["name"]),
        detail_url=str(row["detail_url"]),
        quark_url=row["quark_url"],
        detail_status=str(row["detail_status"]),
        detail_attempts=int(row["detail_attempts"]),
    )


def save_detail_result(
    connection: sqlite3.Connection,
    game_id: int,
    update: DetailUpdate,
) -> None:
    timestamp = now_iso()
    connection.execute(
        """
        UPDATE games
        SET quark_url = ?,
            quark_status = ?,
            detail_status = ?,
            detail_attempts = ?,
            last_error = ?,
            quark_checked_at = COALESCE(?, quark_checked_at),
            detail_updated_at = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (
            update.quark_url,
            update.quark_status,
            update.detail_status,
            update.detail_attempts,
            update.last_error,
            update.checked_at,
            timestamp,
            timestamp,
            game_id,
        ),
    )


def save_provider_results(
    connection: sqlite3.Connection,
    game_id: int,
    results: tuple[ProviderResult, ...],
) -> None:
    timestamp = now_iso()
    connection.executemany(
        """
        INSERT INTO game_disk_links (game_id, provider, url, status, last_error, checked_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(game_id, provider) DO UPDATE SET
            url = COALESCE(excluded.url, game_disk_links.url),
            status = CASE
                WHEN excluded.url IS NOT NULL THEN 'complete'
                WHEN game_disk_links.url IS NOT NULL THEN 'stale'
                ELSE excluded.status END,
            last_error = excluded.last_error,
            checked_at = excluded.checked_at
        """,
        (
            (game_id, item.provider, item.url, item.status, item.error, timestamp)
            for item in results
        ),
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


def delete_checkpoint(connection: sqlite3.Connection, task_key: str) -> None:
    connection.execute(
        "DELETE FROM crawl_checkpoints WHERE task_key = ?",
        (task_key,),
    )


@contextmanager
def crawl_task_lock(
    connection: sqlite3.Connection,
    task_key: str,
) -> Iterator[str]:
    owner = uuid.uuid4().hex
    acquired_at = time.time()
    stale_before = acquired_at - LOCK_STALE_SECONDS
    # 仅允许接管长时间未刷新心跳的遗留锁，活跃任务的锁不会被覆盖。
    with connection:
        cursor = connection.execute(
            """
            INSERT INTO crawl_locks (task_key, owner, acquired_at, heartbeat_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(task_key) DO UPDATE SET
                owner = excluded.owner,
                acquired_at = excluded.acquired_at,
                heartbeat_at = excluded.heartbeat_at
            WHERE crawl_locks.heartbeat_at < ?
            """,
            (task_key, owner, acquired_at, acquired_at, stale_before),
        )
    if cursor.rowcount != 1:
        raise RuntimeError("相同任务正在运行，请勿重复启动")
    try:
        yield owner
    finally:
        with connection:
            connection.execute(
                "DELETE FROM crawl_locks WHERE task_key = ? AND owner = ?",
                (task_key, owner),
            )


def refresh_crawl_lock(
    connection: sqlite3.Connection,
    task_key: str,
    owner: str,
) -> None:
    cursor = connection.execute(
        """
        UPDATE crawl_locks
        SET heartbeat_at = ?
        WHERE task_key = ? AND owner = ?
        """,
        (time.time(), task_key, owner),
    )
    if cursor.rowcount != 1:
        raise RuntimeError("任务锁已失效，已停止写入")


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

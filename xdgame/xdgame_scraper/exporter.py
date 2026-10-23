from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .database import load_games_for_export, now_iso, readonly_database_connection


@dataclass(frozen=True)
class ExportResult:
    output: Path
    count: int
    complete_count: int
    quark_count: int
    other_disk_link_count: int


def export_database(database: Path, output: Path) -> ExportResult:
    if database.resolve() == output.resolve():
        raise ValueError("导出路径不能覆盖 SQLite 数据库")
    with readonly_database_connection(database) as connection:
        games = load_games_for_export(connection)
    payload = build_export_payload(database, games)
    write_export(output, payload)
    return ExportResult(
        output,
        len(games),
        int(payload["complete_count"]),
        int(payload["quark_count"]),
        int(payload["other_disk_link_count"]),
    )


def build_export_payload(database: Path, games: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "source": "sqlite",
        "source_database": database.name,
        "exported_at": now_iso(),
        "count": len(games),
        "complete_count": sum(game["detail_status"] == "complete" for game in games),
        "quark_count": sum(bool(game["quark_url"]) for game in games),
        "other_disk_link_count": sum(
            bool(link["url"])
            for game in games
            for link in game["other_disk_links"].values()
        ),
        "games": games,
    }


def write_export(output: Path, payload: dict[str, Any]) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f"{output.name}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        # 先完整写入同目录临时文件，避免中断时破坏上一份可用导出。
        temporary.replace(output)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise

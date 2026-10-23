from __future__ import annotations

import sqlite3
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import Mock, patch

from xdgame_scraper.cli import run_rank_enrich
from xdgame_scraper.database import (
    DATABASE_MIGRATIONS,
    DetailCandidate,
    database_connection,
    initialize_database,
    load_rank_candidates,
    rank_daily_visit_count,
    ranking_counts,
    record_rank_visit,
    replace_rankings,
    upsert_list_games,
)
from xdgame_scraper.detail_enricher import DetailResolver, EnrichConfig
from xdgame_scraper.disk_links import page_has_daily_quota
from xdgame_scraper.errors import DailyQuotaExceeded
from xdgame_scraper.ranking import parse_hot_ranking


def ranking_rows() -> list[tuple[int, str, int]]:
    return [(rank, f"https://www.xdgame.com/game/{rank}.html", 1000 - rank)
            for rank in range(1, 101)]


class RankingTests(unittest.TestCase):
    def test_existing_database_migrates_to_rankings(self) -> None:
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        for version in range(1, 6):
            with connection:
                for statement in DATABASE_MIGRATIONS[version]:
                    connection.execute(statement)
                connection.execute(f"PRAGMA user_version = {version}")
        initialize_database(connection)
        self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 6)
        self.assertEqual(ranking_counts(connection), (0, 0))
        self.assertEqual(rank_daily_visit_count(connection), 0)
        connection.close()

    def test_quota_title_stops_resolver(self) -> None:
        page = Mock()
        page.title.return_value = "今日游戏下载额度已用完 - XDGAME"
        self.assertTrue(page_has_daily_quota(page))
        candidate = DetailCandidate(
            1, "测试游戏", "https://www.xdgame.com/game/1.html", None, "pending", 0
        )
        config = EnrichConfig(20, Path("unused.json"), True, 0, 0, 3)
        with DetailResolver(config) as resolver:
            with patch.object(resolver, "_resolve_http", return_value=(None, None)):
                with patch.object(resolver, "_resolve_browser", side_effect=DailyQuotaExceeded("额度用完")):
                    with patch("xdgame_scraper.detail_enricher.wait_for_request"):
                        with self.assertRaises(DailyQuotaExceeded):
                            resolver.resolve(candidate)

    def test_parse_requires_complete_ranking(self) -> None:
        items = "".join(
            f'<li><em class="rk">{rank}</em>'
            f'<a class="tit" href="/game/{rank}.html">游戏{rank}</a>'
            f'<time class="num">{1000 - rank} 热度</time></li>'
            for rank in range(1, 101)
        )
        html = f'<ul class="tab-panel ranklist" data-panel="hot">{items}</ul>'.encode()
        self.assertEqual(parse_hot_ranking(html), ranking_rows())
        with self.assertRaises(ValueError):
            parse_hot_ranking(html.replace(b'<li>', b'', 1))

    def test_only_existing_pending_games_follow_rank_order(self) -> None:
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        initialize_database(connection)
        with connection:
            upsert_list_games(connection, [
                {"name": "第三名", "url": "https://www.xdgame.com/game/3.html"},
                {"name": "第一名", "url": "https://www.xdgame.com/game/1.html"},
            ])
            replace_rankings(connection, ranking_rows())
        self.assertEqual(ranking_counts(connection), (100, 2))
        candidates = load_rank_candidates(connection, 10, 3)
        self.assertEqual([item.name for item in candidates], ["第一名", "第三名"])
        with connection:
            record_rank_visit(connection, candidates[0].id)
            record_rank_visit(connection, candidates[0].id)
        self.assertEqual(rank_daily_visit_count(connection), 1)
        connection.close()

    def test_quota_stops_without_incrementing_attempts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rank.db"
            with database_connection(path) as connection:
                with connection:
                    upsert_list_games(connection, [
                        {"name": "第一名", "url": "https://www.xdgame.com/game/1.html"}
                    ])
                    replace_rankings(connection, ranking_rows())
            args = Namespace(
                database=path, timeout=20, login_state=Path(directory) / "state.json",
                show_browser=False, delay_min=0, delay_max=0, max_attempts=3,
                batch_size=1, recheck_days=30, daily_game_limit=40,
            )
            with patch.object(DetailResolver, "resolve", side_effect=DailyQuotaExceeded("额度用完")):
                with self.assertRaises(DailyQuotaExceeded):
                    run_rank_enrich(args)
            with database_connection(path) as connection:
                game = connection.execute(
                    "SELECT detail_status, detail_attempts FROM games WHERE name = '第一名'"
                ).fetchone()
                run = connection.execute(
                    "SELECT status, processed_count FROM crawl_runs ORDER BY id DESC LIMIT 1"
                ).fetchone()
                self.assertEqual(tuple(game), ("pending", 0))
                self.assertEqual(tuple(run), ("quota_exceeded", 0))
                self.assertEqual(rank_daily_visit_count(connection), 1)


if __name__ == "__main__":
    unittest.main()

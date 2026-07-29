from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


DEFAULT_URL = "https://www.xdgame.com/"
DEFAULT_OUTPUT = "today_games.json"


def create_session() -> requests.Session:
    session = requests.Session()
    retries = Retry(
        total=3,
        backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
    )
    session.mount("https://", HTTPAdapter(max_retries=retries))
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/138.0 Safari/537.36"
            ),
            "Accept-Language": "zh-CN,zh;q=0.9",
        }
    )
    return session


def parse_today_games(html: bytes, base_url: str) -> list[dict[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    game_list = soup.select_one(".index-new-list .index-news-list .new-list")
    if game_list is None:
        raise ValueError("未找到首页的游戏更新列表，网站结构可能已经改变")

    games: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in game_list.select("li"):
        update_label = item.select_one("font.news")
        game_link = item.select_one("a.tit")
        if (
            update_label is None
            or update_label.get_text(strip=True) != "今日"
            or game_link is None
        ):
            continue

        name = game_link.get_text(" ", strip=True)
        if not name or name in seen:
            continue

        seen.add(name)
        games.append(
            {
                "name": name,
                "url": urljoin(base_url, game_link.get("href", "")),
            }
        )

    return games


def scrape(url: str, timeout: float) -> list[dict[str, str]]:
    with create_session() as session:
        response = session.get(url, timeout=timeout)
        response.raise_for_status()
        return parse_today_games(response.content, response.url)


def save_json(games: list[dict[str, str]], source_url: str, output: Path) -> None:
    result = {
        "source": source_url,
        "fetched_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "count": len(games),
        "games": games,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="抓取 XDGAME 首页今日更新的游戏名")
    parser.add_argument("--url", default=DEFAULT_URL, help="目标首页 URL")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=Path(DEFAULT_OUTPUT),
        help=f"JSON 输出路径，默认：{DEFAULT_OUTPUT}",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=15,
        help="请求超时秒数，默认：15",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    print(f"args: {args}")
    try:
        games = scrape(args.url, args.timeout)
        save_json(games, args.url, args.output)
    except (requests.RequestException, ValueError, OSError) as error:
        print(f"抓取失败：{error}", file=sys.stderr)
        return 1

    print(f"已抓取 {len(games)} 个今日更新游戏，保存至 {args.output}")
    for game in games:
        print(f"- {game['name']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

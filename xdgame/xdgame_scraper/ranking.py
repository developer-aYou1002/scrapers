from __future__ import annotations

import re
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from .crawler import create_session


HOT_RANKING_URL = "https://www.xdgame.com/update/"
HOT_RANKING_SIZE = 100


def fetch_hot_ranking(timeout: float) -> list[tuple[int, str, int]]:
    with create_session() as session:
        response = session.get(HOT_RANKING_URL, timeout=timeout)
        response.raise_for_status()
        actual = urlsplit(response.url)
        expected = urlsplit(HOT_RANKING_URL)
        if (actual.hostname, actual.path) != (expected.hostname, expected.path):
            raise ValueError("人气榜跳转到非目标页面，未更新榜单快照")
        return parse_hot_ranking(response.content)


def parse_hot_ranking(content: bytes) -> list[tuple[int, str, int]]:
    soup = BeautifulSoup(content, "html.parser")
    items = soup.select('ul.tab-panel.ranklist[data-panel="hot"] > li')
    if len(items) != HOT_RANKING_SIZE:
        raise ValueError("人气榜记录数不符合预期，未更新榜单快照")
    rows: list[tuple[int, str, int]] = []
    for expected_position, item in enumerate(items, start=1):
        position = item.select_one("em.rk")
        title = item.select_one("a.tit[href]")
        heat = item.select_one("time.num")
        if position is None or title is None or heat is None:
            raise ValueError("人气榜条目结构变化，未更新榜单快照")
        url = urljoin(HOT_RANKING_URL, str(title.get("href")))
        parsed = urlsplit(url)
        heat_value = re.fullmatch(r"([\d,]+)\s*热度", heat.get_text(" ", strip=True))
        if (
            position.get_text(strip=True) != str(expected_position)
            or parsed.scheme != "https"
            or parsed.hostname != "www.xdgame.com"
            or not re.fullmatch(r"/game/\d+\.html", parsed.path)
            or parsed.query
            or heat_value is None
        ):
            raise ValueError("人气榜名次、地址或热度异常，未更新榜单快照")
        rows.append((expected_position, url, int(heat_value.group(1).replace(",", ""))))
    if len({url for _, url, _ in rows}) != len(rows):
        raise ValueError("人气榜出现重复游戏地址，未更新榜单快照")
    return rows

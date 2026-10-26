from __future__ import annotations

import re
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from .crawler import create_session


HOT_RANKING_URL = "https://www.xdgame.com/update/"
HOT_RANKING_SIZE = 100
RANKING_ORDER = ("hot", "month", "quarter", "year", "good", "rank", "size")
RANKING_LABELS = {
    "hot": "人气",
    "month": "月度",
    "quarter": "季度",
    "year": "年度",
    "good": "点赞",
    "rank": "评分",
    "size": "容量",
}
RANKING_UNITS = {
    "hot": ("热度",),
    "month": ("热度",),
    "quarter": ("热度",),
    "year": ("热度",),
    "good": ("赞",),
    "rank": ("分",),
    "size": ("MB", "GB", "TB"),
}


def fetch_hot_ranking(timeout: float) -> list[tuple[int, str, int]]:
    return parse_hot_ranking(fetch_ranking_page(timeout))


def fetch_all_rankings(timeout: float) -> dict[str, list[tuple[int, str, int, str]]]:
    return parse_all_rankings(fetch_ranking_page(timeout))


def fetch_ranking_page(timeout: float) -> bytes:
    with create_session() as session:
        response = session.get(HOT_RANKING_URL, timeout=timeout)
        response.raise_for_status()
        actual = urlsplit(response.url)
        expected = urlsplit(HOT_RANKING_URL)
        if (actual.hostname, actual.path) != (expected.hostname, expected.path):
            raise ValueError("榜单跳转到非目标页面，未更新榜单快照")
        return response.content


def parse_hot_ranking(content: bytes) -> list[tuple[int, str, int]]:
    rows = parse_ranking_panel(BeautifulSoup(content, "html.parser"), "hot")
    return [(position, url, heat) for position, url, heat, _ in rows]


def parse_all_rankings(content: bytes) -> dict[str, list[tuple[int, str, int, str]]]:
    soup = BeautifulSoup(content, "html.parser")
    # 全部面板先完成校验，调用方才会在同一事务里替换快照。
    return {ranking_type: parse_ranking_panel(soup, ranking_type) for ranking_type in RANKING_ORDER}


def parse_ranking_panel(
    soup: BeautifulSoup,
    ranking_type: str,
) -> list[tuple[int, str, int, str]]:
    items = soup.select(f'ul.tab-panel.ranklist[data-panel="{ranking_type}"] > li')
    if len(items) != HOT_RANKING_SIZE:
        raise ValueError(f"{RANKING_LABELS[ranking_type]}榜记录数不符合预期，未更新榜单快照")
    rows: list[tuple[int, str, int, str]] = []
    for expected_position, item in enumerate(items, start=1):
        position = item.select_one("em.rk")
        title = item.select_one("a.tit[href]")
        heat = item.select_one("time.num")
        if position is None or title is None or heat is None:
            raise ValueError(f"{RANKING_LABELS[ranking_type]}榜条目结构变化，未更新榜单快照")
        url = urljoin(HOT_RANKING_URL, str(title.get("href")))
        parsed = urlsplit(url)
        metric_text = heat.get_text(" ", strip=True)
        metric = re.fullmatch(r"([\d,]+(?:\.\d+)?)\s*([\w\u4e00-\u9fff]+)", metric_text)
        if (
            position.get_text(strip=True) != str(expected_position)
            or parsed.scheme != "https"
            or parsed.hostname != "www.xdgame.com"
            or not re.fullmatch(r"/game/\d+\.html", parsed.path)
            or parsed.query
            or parsed.fragment
            or metric is None
            or metric.group(2) not in RANKING_UNITS[ranking_type]
        ):
            raise ValueError(f"{RANKING_LABELS[ranking_type]}榜名次、地址或指标异常，未更新榜单快照")
        numeric = int(metric.group(1).replace(",", "")) if metric.group(2) == "热度" else 0
        rows.append((expected_position, url, numeric, metric_text))
    if len({url for _, url, _, _ in rows}) != len(rows):
        raise ValueError(f"{RANKING_LABELS[ranking_type]}榜出现重复游戏地址，未更新榜单快照")
    return rows

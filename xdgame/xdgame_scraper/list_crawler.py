from __future__ import annotations

import random
import re
import time
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup


DEFAULT_LIST_URL = "https://www.xdgame.com/list/1/"


@dataclass(frozen=True)
class FullListConfig:
    url: str
    timeout: float
    max_pages: int
    delay_min: float
    delay_max: float


@dataclass(frozen=True)
class ListPage:
    games: list[dict[str, Any]]
    next_url: str | None


class DelayConfig(Protocol):
    delay_min: float
    delay_max: float


def element_text(item: Any, selector: str) -> str | None:
    element = item.select_one(selector)
    if element is None:
        return None
    value = element.get_text(" ", strip=True)
    return value or None


def cover_url(item: Any, base_url: str) -> str | None:
    image = item.select_one("img.img")
    if image is None:
        image = item.select_one("a.grid-cover img")
    if image is None:
        return None
    for attribute in ("data-list-src", "data-original", "data-litpic", "src"):
        value = image.get(attribute)
        if value:
            return urljoin(base_url, str(value))
    return None


def parse_list_game(item: Any, base_url: str) -> dict[str, Any] | None:
    title = item.select_one("a.tit[href]")
    if title is None:
        return None
    name = title.get_text(" ", strip=True)
    detail_url = urljoin(base_url, str(title.get("href") or ""))
    base = urlparse(base_url)
    detail = urlparse(detail_url)
    is_same_site = (detail.scheme, detail.netloc) == (base.scheme, base.netloc)
    is_game_path = re.fullmatch(r"/game/\d+\.html", detail.path) is not None
    if not name or not is_same_site or not is_game_path:
        return None
    score = element_text(item, ".data .rank b")
    return {
        "name": name,
        "url": detail_url,
        "category": element_text(item, ".type"),
        "score": score or element_text(item, ".data .rank"),
        "size": element_text(item, ".data .size"),
        "source_updated_date": element_text(item, ".data time.news"),
        "cover_url": cover_url(item, base_url),
    }


def find_next_url(soup: BeautifulSoup, page_url: str) -> str | None:
    for link in soup.select(".pages a[href]"):
        if link.get_text(" ", strip=True) != "下一页":
            continue
        candidate = urljoin(page_url, str(link.get("href")))
        current = urlparse(page_url)
        target = urlparse(candidate)
        if (target.scheme, target.netloc) != (current.scheme, current.netloc):
            raise ValueError("列表页的下一页链接指向其他站点，已停止抓取")
        return candidate
    return None


def parse_list_page(html: bytes, page_url: str) -> ListPage:
    soup = BeautifulSoup(html, "html.parser")
    game_list = soup.select_one(".soft-list-page ul.game-list")
    if game_list is None:
        raise ValueError("未找到电脑游戏列表，网站结构可能已经改变")
    games = [
        game
        for item in game_list.find_all("li", recursive=False)
        if (game := parse_list_game(item, page_url)) is not None
    ]
    if not games:
        raise ValueError("电脑游戏列表没有可识别的记录，网站结构可能已经改变")
    return ListPage(games=games, next_url=find_next_url(soup, page_url))


def fetch_list_page(
    session: requests.Session,
    page_url: str,
    timeout: float,
) -> ListPage:
    response = session.get(page_url, timeout=timeout)
    response.raise_for_status()
    return parse_list_page(response.content, response.url)


def wait_for_next_page(config: DelayConfig) -> None:
    delay = random.uniform(config.delay_min, config.delay_max)
    print(f"等待 {delay:.1f} 秒后抓取下一页。")
    time.sleep(delay)

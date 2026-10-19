from __future__ import annotations

import logging
import random
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .errors import LoginStateExpired
from .login import page_requires_login


DEFAULT_URL = "https://www.xdgame.com/"
QUARK_PATTERN = re.compile(r"https?://pan\.quark\.cn/s/[A-Za-z0-9_-]+")
LINK_ATTRIBUTES = ("href", "data-url", "data-href", "data-link", "onclick")
LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class ScrapeConfig:
    url: str
    timeout: float
    login_state: Path
    headless: bool
    delay_min: float
    delay_max: float


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


def parse_today_games(html: bytes, base_url: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    game_list = soup.select_one(".index-new-list .index-news-list .new-list")
    if game_list is None:
        raise ValueError("未找到首页的游戏更新列表，网站结构可能已经改变")

    games: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in game_list.select("li"):
        game = parse_game_item(item, base_url)
        if game is None or game["name"] in seen:
            continue
        seen.add(game["name"])
        games.append(game)
    return games


def parse_game_item(item: Any, base_url: str) -> dict[str, Any] | None:
    update_label = item.select_one("font.news")
    game_link = item.select_one("a.tit")
    if update_label is None or game_link is None:
        return None
    if update_label.get_text(strip=True) != "今日":
        return None

    name = game_link.get_text(" ", strip=True)
    if not name:
        return None
    return {
        "name": name,
        "url": urljoin(base_url, game_link.get("href", "")),
        "quark_url": None,
        "quark_status": "待解析",
    }


def extract_quark_url(content: str) -> str | None:
    direct_match = QUARK_PATTERN.search(content)
    if direct_match:
        return direct_match.group(0)

    # 站点不同模板会把下载地址放在不同属性中，统一遍历可避免为每种模板复制解析逻辑。
    soup = BeautifulSoup(content, "html.parser")
    for element in soup.find_all(True):
        for attribute in LINK_ATTRIBUTES:
            value = element.get(attribute)
            match = QUARK_PATTERN.search(str(value or ""))
            if match:
                return match.group(0)
    return None


def enrich_from_html(
    session: requests.Session,
    games: list[dict[str, Any]],
    config: ScrapeConfig,
) -> list[dict[str, Any]]:
    unresolved: list[dict[str, Any]] = []
    for index, game in enumerate(games, start=1):
        LOGGER.info("[%d/%d] 读取详情页：%s", index, len(games), game["name"])
        try:
            response = session.get(game["url"], timeout=config.timeout)
            response.raise_for_status()
            game["quark_url"] = extract_quark_url(response.text)
            game["quark_status"] = "已从详情页获取" if game["quark_url"] else "需要浏览器解析"
        except requests.RequestException as error:
            game["quark_status"] = f"详情页请求失败：{error}"
        if not game["quark_url"]:
            unresolved.append(game)
        wait_before_next(index, len(games), config)
    return unresolved


def wait_before_next(index: int, total: int, config: ScrapeConfig) -> None:
    if index >= total:
        return
    delay = random.uniform(config.delay_min, config.delay_max)
    LOGGER.info("等待 %.1f 秒后继续，降低请求频率", delay)
    time.sleep(delay)


def collect_response_url(response: Any, candidates: list[str]) -> None:
    match = QUARK_PATTERN.search(response.url)
    if match:
        candidates.append(match.group(0))
        return
    if "json" not in response.headers.get("content-type", ""):
        return
    try:
        match = QUARK_PATTERN.search(response.text())
    except Exception:
        return
    if match:
        candidates.append(match.group(0))


def resolve_with_browser(page: Any, game: dict[str, Any], timeout_ms: int) -> None:
    candidates: list[str] = []
    page.on("response", lambda response: collect_response_url(response, candidates))
    page.goto(game["url"], wait_until="domcontentloaded", timeout=timeout_ms)
    if page_requires_login(page):
        raise LoginStateExpired("详情页要求重新登录")

    quark_url = extract_quark_url(page.content())
    if quark_url:
        set_browser_result(game, quark_url)
        return

    button = page.get_by_text("夸克网盘", exact=True).first
    if button.count() == 0:
        game["quark_status"] = "未找到夸克网盘按钮"
        return

    for attribute in LINK_ATTRIBUTES:
        value = button.get_attribute(attribute)
        match = QUARK_PATTERN.search(value or "")
        if match:
            set_browser_result(game, match.group(0))
            return

    pages_before = set(page.context.pages)
    button.click(timeout=timeout_ms)
    page.wait_for_timeout(2000)
    new_pages = [candidate for candidate in page.context.pages if candidate not in pages_before]
    urls = candidates + [page.url] + [candidate.url for candidate in new_pages]
    quark_url = next((url for url in urls if QUARK_PATTERN.search(url)), None)
    if quark_url:
        match = QUARK_PATTERN.search(quark_url)
        set_browser_result(game, match.group(0))
    else:
        game["quark_status"] = "点击后未发现公开夸克地址，可能需要登录或会员权限"


def set_browser_result(game: dict[str, Any], quark_url: str) -> None:
    game["quark_url"] = quark_url
    game["quark_status"] = "已通过浏览器获取"


def enrich_with_browser(games: list[dict[str, Any]], config: ScrapeConfig) -> None:
    if not games:
        return
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as error:
        raise RuntimeError("缺少 Playwright，请先安装 requirements.txt 中的依赖") from error

    state = str(config.login_state) if config.login_state.exists() else None
    timeout_ms = int(config.timeout * 1000)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=config.headless)
        context = None
        try:
            # 所有页面复用同一上下文，确保登录 Cookie 一致，同时每个游戏使用独立页面避免跳转互相干扰。
            try:
                context = browser.new_context(storage_state=state)
            except Exception as error:
                if state is not None:
                    raise LoginStateExpired("登录状态文件无法加载，请重新登录") from error
                raise
            for index, game in enumerate(games, start=1):
                LOGGER.info("[%d/%d] 浏览器解析：%s", index, len(games), game["name"])
                page = context.new_page()
                try:
                    resolve_with_browser(page, game, timeout_ms)
                except LoginStateExpired:
                    raise
                except Exception as error:
                    game["quark_status"] = f"浏览器解析失败：{error}"
                finally:
                    page.close()
                wait_before_next(index, len(games), config)
        finally:
            # 即使关闭上下文失败也继续关闭浏览器，避免异常路径遗留 Chromium 进程。
            try:
                if context is not None:
                    context.close()
            finally:
                browser.close()


def scrape(config: ScrapeConfig) -> list[dict[str, Any]]:
    with create_session() as session:
        response = session.get(config.url, timeout=config.timeout)
        response.raise_for_status()
        games = parse_today_games(response.content, response.url)
        unresolved = enrich_from_html(session, games, config)
    enrich_with_browser(unresolved, config)
    return games

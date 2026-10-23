from __future__ import annotations

import html
import logging
import random
import re
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote, urljoin, urlsplit

from bs4 import BeautifulSoup

from .database import ProviderResult
from .errors import DailyQuotaExceeded
from .login import page_requires_login


PROVIDER_HOSTS = {
    "百度网盘": ("pan.baidu.com",),
    "天翼网盘": ("cloud.189.cn",),
    "迅雷网盘": ("pan.xunlei.com",),
    "移动网盘": ("caiyun.139.com", "yun.139.com"),
    "夸克网盘": ("pan.quark.cn",),
}
OTHER_PROVIDERS = tuple(provider for provider in PROVIDER_HOSTS if provider != "夸克网盘")
FORBIDDEN_MARKERS = ("无权限", "权限不足", "仅限会员", "会员可见", "开通会员")
URL_PATTERN = re.compile(r"https?://[^\s\"'<>\\]+", re.IGNORECASE)
LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProviderBrowser:
    page: Any
    request: Any
    candidates: list[str]


def resolve_http_providers(session: Any, response: Any, config: Any) -> tuple[ProviderResult, ...]:
    # 只信任目标详情页已渲染的下载区，并仅请求同站固定下载接口，避免跟随未知跳转。
    soup = BeautifulSoup(response.text, "html.parser")
    region = soup.select_one(".article-down")
    if soup.select_one("h1") is None or region is None:
        return ()
    if not region.select("a.downbtn[data-server]"):
        return ()
    results: list[ProviderResult] = []
    for provider in PROVIDER_HOSTS:
        button = region.select_one(f'a.downbtn[data-server="{provider}"]')
        if button is None:
            results.append(ProviderResult(provider, None, "not_found", "下载区域无对应网盘入口"))
            continue
        direct = extract_provider_url(str(button), provider)
        if direct:
            results.append(ProviderResult(provider, direct, "complete"))
            continue
        endpoint = urljoin(response.url, str(button.get("data-url") or ""))
        parsed = urlsplit(endpoint)
        if (
            parsed.scheme != "https"
            or parsed.hostname != urlsplit(response.url).hostname
            or parsed.path != "/plus/download.php"
        ):
            results.append(ProviderResult(provider, None, "retry", "下载接口地址不符合预期"))
            continue
        try:
            # 每个入口都对应一次站点请求，沿用用户配置的节流间隔。
            time.sleep(random.uniform(config.delay_min, config.delay_max))
            download = session.get(endpoint, allow_redirects=False, timeout=config.timeout)
            target = download.headers.get("Location", "")
            url = extract_provider_url(target, provider)
            if not url and download.status_code == 200:
                url = extract_provider_url(download.text, provider)
            status = "complete" if url else "retry"
            error = None if url else f"下载接口未返回对应地址（HTTP {download.status_code}）"
            results.append(ProviderResult(provider, url, status, error))
        except Exception as error:
            LOGGER.warning("%s 下载接口请求失败：%s", provider, type(error).__name__)
            results.append(ProviderResult(provider, None, "retry", "下载接口请求失败"))
    return tuple(results)


def extract_provider_url(content: str, provider: str) -> str | None:
    normalized = html.unescape(content).replace("\\/", "/")
    if re.search(r"https?%3a%2f%2f", normalized, re.IGNORECASE):
        normalized += " " + unquote(normalized)
    for match in URL_PATTERN.finditer(normalized):
        url = match.group(0).rstrip(".,;)]}")
        try:
            parsed = urlsplit(url)
            hostname = parsed.hostname or ""
        except ValueError:
            continue
        if parsed.path in ("", "/"):
            continue
        hosts = PROVIDER_HOSTS[provider]
        if any(hostname == host or hostname.endswith(f".{host}") for host in hosts):
            return url
    return None


def collect_provider_response(response: Any, candidates: list[str]) -> None:
    for provider in OTHER_PROVIDERS:
        url = extract_provider_url(response.url, provider)
        if url:
            candidates.append(url)
            return
    try:
        if "json" in response.headers.get("content-type", ""):
            body = response.text()
            for provider in OTHER_PROVIDERS:
                url = extract_provider_url(body, provider)
                if url:
                    candidates.append(url)
    except Exception:
        return


def resolve_other_providers(request: Any) -> tuple[ProviderResult, ...]:
    page = request.page.context.new_page()
    candidates: list[str] = []
    page.on("response", lambda response: collect_provider_response(response, candidates))
    timeout_ms = int(request.config.timeout * 1000)
    try:
        response = page.goto(
            request.candidate.detail_url,
            wait_until="domcontentloaded",
            timeout=timeout_ms,
        )
        if response is None or not 200 <= response.status < 300 or page_requires_login(page):
            return ()
        region = page.locator(".article-down").first
        region.wait_for(state="visible", timeout=timeout_ms)
        if region.locator("a.downbtn[data-server]").count() == 0:
            return ()
        available = frozenset(
            provider
            for provider in OTHER_PROVIDERS
            if region.locator(f'a.downbtn[data-server="{provider}"]').count()
        )
        return collect_provider_results(ProviderBrowser(page, request, candidates), available)
    finally:
        if not page.is_closed():
            page.close()


def collect_provider_results(
    browser: ProviderBrowser,
    available: frozenset[str],
) -> tuple[ProviderResult, ...]:
    results: list[ProviderResult] = []
    for provider in OTHER_PROVIDERS:
        try:
            result = resolve_provider(browser, provider, available)
        except DailyQuotaExceeded:
            raise
        except Exception as error:
            LOGGER.warning("%s 入口解析失败：%s", provider, type(error).__name__)
            result = ProviderResult(provider, None, "retry", "入口解析发生临时错误")
        results.append(result)
    return tuple(results)


def resolve_provider(
    browser: ProviderBrowser,
    provider: str,
    available: frozenset[str],
) -> ProviderResult:
    page = browser.page
    detail_url = browser.request.candidate.detail_url
    timeout_ms = int(browser.request.config.timeout * 1000)
    if provider not in available:
        return ProviderResult(provider, None, "not_found", "下载区域无对应网盘入口")
    if page.url != detail_url:
        response = page.goto(detail_url, wait_until="domcontentloaded", timeout=timeout_ms)
        if response is None or not 200 <= response.status < 300:
            return ProviderResult(provider, None, "retry", "详情页重新加载失败")
    button = page.locator(f'.article-down a.downbtn[data-server="{provider}"]').first
    if button.count() == 0:
        return ProviderResult(provider, None, "retry", "入口在页面交互后消失")
    for attribute in ("href", "data-url", "data-href", "data-link", "onclick"):
        url = extract_provider_url(button.get_attribute(attribute) or "", provider)
        if url:
            return ProviderResult(provider, url, "complete")

    return click_provider(browser, provider, button)


def click_provider(browser: ProviderBrowser, provider: str, button: Any) -> ProviderResult:
    page = browser.page
    timeout_ms = int(browser.request.config.timeout * 1000)
    pages_before = set(page.context.pages)
    browser.candidates.clear()
    try:
        button.click(timeout=timeout_ms)
        page.wait_for_timeout(2000)
        new_pages = [item for item in page.context.pages if item not in pages_before]
        # 额度耗尽优先于地址提取，避免将站点限制误记为单个网盘的失败。
        if page_has_daily_quota(page) or any(page_has_daily_quota(item) for item in new_pages):
            raise DailyQuotaExceeded("今日游戏下载额度已用完；未增加当前游戏的失败次数")
        urls = browser.candidates + [page.url]
        urls.extend(item.url for item in new_pages)
        for item in [page, *new_pages]:
            try:
                urls.append(item.content())
            except Exception:
                pass
        url = next((found for item in urls if (found := extract_provider_url(item, provider))), None)
        if url:
            return ProviderResult(provider, url, "complete")
        if page_requires_login(page) or any(page_requires_login(item) for item in new_pages):
            return ProviderResult(provider, None, "login_expired", "入口要求重新登录")
        if page_has_forbidden_marker(page) or any(
            page_has_forbidden_marker(item) for item in new_pages
        ):
            return ProviderResult(provider, None, "forbidden", "入口提示需要会员或访问权限")
        return ProviderResult(provider, None, "retry", "点击入口后未发现对应网盘地址")
    finally:
        for item in page.context.pages:
            if item not in pages_before:
                try:
                    item.close()
                except Exception:
                    pass


def page_has_forbidden_marker(page: Any) -> bool:
    body = page.locator("body")
    text = body.inner_text() if body.count() else ""
    return any(marker in text for marker in FORBIDDEN_MARKERS)


def page_has_daily_quota(page: Any) -> bool:
    return "今日游戏下载额度已用完" in page.title()

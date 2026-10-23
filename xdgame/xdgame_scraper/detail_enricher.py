from __future__ import annotations

import logging
import random
import re
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import requests

from .crawler import LINK_ATTRIBUTES, QUARK_PATTERN, create_session, extract_quark_url
from .database import DetailCandidate, DetailUpdate, ProviderResult, now_iso
from .disk_links import (
    OTHER_PROVIDERS,
    page_has_daily_quota,
    page_has_forbidden_marker,
    resolve_http_providers,
    resolve_other_providers,
)
from .errors import DailyQuotaExceeded, LoginStateExpired
from .login import page_requires_login


DOWNLOAD_REGION = ".article-down"
QUARK_BUTTON = 'a.downbtn[data-server="夸克网盘"]'
DOWNLOAD_BUTTON = "a.downbtn[data-server]"
LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class EnrichConfig:
    timeout: float
    login_state: Path
    headless: bool
    delay_min: float
    delay_max: float
    max_attempts: int


@dataclass(frozen=True)
class DetailResolution:
    quark_url: str | None
    quark_status: str
    detail_status: str
    error: str | None = None
    provider_results: tuple[ProviderResult, ...] = ()


@dataclass(frozen=True)
class BrowserRequest:
    page: Any
    candidate: DetailCandidate
    candidates: list[str]
    config: EnrichConfig


class BrowserUnavailable(RuntimeError):
    """浏览器依赖或登录状态文件无法用于详情补全。"""


class DetailResolver:
    def __init__(self, config: EnrichConfig) -> None:
        self.config = config
        self.session = create_session()
        self.playwright: Any = None
        self.browser: Any = None
        self.context: Any = None

    def __enter__(self) -> DetailResolver:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        try:
            if self.context is not None:
                self.context.close()
        finally:
            try:
                if self.browser is not None:
                    self.browser.close()
            finally:
                try:
                    if self.playwright is not None:
                        self.playwright.stop()
                finally:
                    self.session.close()

    def resolve(self, candidate: DetailCandidate) -> DetailResolution:
        http_result, http_error = self._resolve_http(candidate)
        # 公开下载接口已给出全部结果时无需启动浏览器；否则只用浏览器补齐不确定项。
        if (
            http_result is not None
            and http_result.quark_url
            and len(http_result.provider_results) == len(OTHER_PROVIDERS)
            and all(item.status in ("complete", "not_found") for item in http_result.provider_results)
        ):
            return http_result
        reason = "其他网盘解析" if http_result is not None else "浏览器回退"
        wait_for_request(self.config, reason)
        try:
            browser_result = self._resolve_browser(candidate)
        except LoginStateExpired as error:
            if http_result is not None and http_result.quark_url:
                return http_result
            results = http_result.provider_results if http_result is not None else ()
            return DetailResolution(None, "登录状态已失效", "login_expired", str(error), results)
        except BrowserUnavailable:
            if http_result is not None and http_result.quark_url:
                return http_result
            raise
        except DailyQuotaExceeded:
            raise
        except Exception as error:
            if http_result is not None and http_result.quark_url:
                return http_result
            details = combine_errors(http_error, type(error).__name__)
            results = http_result.provider_results if http_result is not None else ()
            return DetailResolution(None, "详情补全临时失败", "retry", details, results)
        providers = merge_provider_results(http_result, browser_result)
        if http_result is not None and http_result.quark_url and browser_result.quark_url is None:
            return replace(http_result, provider_results=providers)
        return replace(browser_result, provider_results=providers)

    def _resolve_http(
        self,
        candidate: DetailCandidate,
    ) -> tuple[DetailResolution | None, str | None]:
        try:
            response = self.session.get(candidate.detail_url, timeout=self.config.timeout)
            response.raise_for_status()
        except requests.RequestException as error:
            return None, f"HTTP 请求失败：{type(error).__name__}"
        expected = urlsplit(candidate.detail_url)
        actual = urlsplit(response.url)
        if (actual.hostname, actual.path) != (expected.hostname, expected.path):
            return None, "HTTP 详情页跳转到非目标页面"
        try:
            provider_results = resolve_http_providers(self.session, response, self.config)
        except Exception as error:
            LOGGER.warning("HTTP 网盘入口解析失败：%s", type(error).__name__)
            provider_results = ()
        quark_result = next(
            (item for item in provider_results if item.provider == "夸克网盘"), None
        )
        quark_url = extract_quark_url(response.text)
        if quark_url is None and quark_result is not None:
            quark_url = quark_result.url
        other_results = tuple(
            item for item in provider_results if item.provider in OTHER_PROVIDERS
        )
        status = "complete" if quark_url else "retry"
        result = DetailResolution(
            quark_url,
            "已从详情页获取" if quark_url else "需要浏览器解析",
            status,
            provider_results=other_results,
        )
        return result, None

    def _resolve_browser(self, candidate: DetailCandidate) -> DetailResolution:
        context = self._ensure_browser_context()
        pages_before = set(context.pages)
        page = context.new_page()
        candidates: list[str] = []
        page.on("response", lambda response: collect_response_url(response, candidates))
        try:
            request = BrowserRequest(page, candidate, candidates, self.config)
            result = resolve_browser_page(request)
            if result.detail_status == "login_expired":
                return result
            try:
                return replace(result, provider_results=resolve_other_providers(request))
            except DailyQuotaExceeded:
                raise
            except Exception as error:
                LOGGER.warning("其他网盘入口解析失败：%s", type(error).__name__)
                return result
        finally:
            close_new_pages(context, pages_before)

    def _ensure_browser_context(self) -> Any:
        if self.context is not None:
            return self.context
        try:
            from playwright.sync_api import sync_playwright

            self.playwright = sync_playwright().start()
            self.browser = self.playwright.chromium.launch(headless=self.config.headless)
        except Exception as error:
            raise BrowserUnavailable(f"浏览器初始化失败：{error}") from error
        state = str(self.config.login_state) if self.config.login_state.exists() else None
        try:
            self.context = self.browser.new_context(storage_state=state)
        except Exception as error:
            if state is not None:
                raise LoginStateExpired("登录状态文件无法加载，请重新登录") from error
            raise BrowserUnavailable(f"浏览器上下文初始化失败：{error}") from error
        return self.context


def resolve_browser_page(request: BrowserRequest) -> DetailResolution:
    page = request.page
    timeout_ms = int(request.config.timeout * 1000)
    response = page.goto(
        request.candidate.detail_url,
        wait_until="domcontentloaded",
        timeout=timeout_ms,
    )
    if page_requires_login(page):
        return DetailResolution(None, "登录状态已失效", "login_expired", "详情页要求重新登录")
    navigation_error = check_detail_navigation(page, response, request.candidate.detail_url)
    if navigation_error:
        return retry_page(page, response, navigation_error)

    quark_url = extract_quark_url(page.content())
    if quark_url:
        return DetailResolution(quark_url, "已通过浏览器获取", "complete")
    if page_has_forbidden_marker(page):
        return DetailResolution(None, "无权限访问夸克地址", "forbidden", "页面提示需要会员或访问权限")

    region = page.locator(DOWNLOAD_REGION).first
    try:
        # DOM 就绪不代表下载区域已出现，缺失时不能将整页正文误当作“无夸克入口”。
        region.wait_for(state="visible", timeout=timeout_ms)
    except Exception:
        return retry_page(page, response, "等待下载区域超时")

    button = find_quark_button(region)
    if button.count() == 0:
        # 若入口由页面脚本补入，给已出现的下载区一次短暂稳定窗口。
        page.wait_for_timeout(min(1000, timeout_ms))
        button = find_quark_button(region)
        if button.count() == 0:
            return classify_missing_button(page, response)
    attribute_url = find_attribute_url(button)
    if attribute_url:
        return DetailResolution(attribute_url, "已通过浏览器获取", "complete")

    pages_before = set(page.context.pages)
    button.click(timeout=timeout_ms)
    # 下载入口可能通过异步请求或新标签页返回地址，短暂等待其完成跳转。
    page.wait_for_timeout(2000)
    new_pages = [item for item in page.context.pages if item not in pages_before]
    # 额度页不是链接缺失或临时网络错误；必须在状态分类前让整批停止。
    if page_has_daily_quota(page) or any(page_has_daily_quota(item) for item in new_pages):
        raise DailyQuotaExceeded("今日游戏下载额度已用完；未增加当前游戏的失败次数")
    if page_requires_login(page) or new_page_requires_login(page, pages_before):
        return DetailResolution(None, "登录状态已失效", "login_expired", "点击入口后要求重新登录")
    quark_url = find_redirect_url(page, pages_before, request.candidates)
    if quark_url:
        return DetailResolution(quark_url, "已通过浏览器获取", "complete")
    if page_has_forbidden_marker(page):
        return DetailResolution(None, "无权限访问夸克地址", "forbidden", "页面提示需要会员或访问权限")
    return retry_page(page, response, "点击夸克入口后未发现地址")


def check_detail_navigation(page: Any, response: Any, expected_url: str) -> str | None:
    if response is None:
        return "详情页导航没有响应"
    if not 200 <= response.status < 300:
        return f"详情页 HTTP 状态异常：{response.status}"
    expected = urlsplit(expected_url)
    actual = urlsplit(page.url)
    if (actual.hostname, actual.path) != (expected.hostname, expected.path):
        return "详情页跳转到非目标页面"
    if page.locator("h1").count() == 0:
        return "详情页标题结构缺失"
    return None


def page_diagnostic(page: Any, response: Any, reason: str) -> str:
    # 仅公开站内游戏详情路径；跳转路径可能包含分享令牌，其他情况只记录主机名。
    parsed = urlsplit(page.url)
    safe_path = parsed.path if re.fullmatch(r"/game/\d+\.html", parsed.path) else ""
    safe_url = f"{parsed.scheme}://{parsed.hostname or ''}{safe_path}"[:180]
    title = (
        " ".join(page.title().split())[:80]
        if parsed.hostname == "www.xdgame.com"
        else "站外页面"
    )
    status = response.status if response is not None else "无响应"
    return f"{reason}；HTTP={status}；URL={safe_url}；标题={title}"[:1000]


def retry_page(page: Any, response: Any, reason: str) -> DetailResolution:
    return DetailResolution(
        None, "详情页待重试", "retry", page_diagnostic(page, response, reason)
    )


def find_quark_button(region: Any) -> Any:
    button = region.locator(QUARK_BUTTON).first
    if button.count() == 0:
        button = region.locator("a").filter(has_text="夸克网盘").first
    return button


def new_page_requires_login(page: Any, pages_before: set[Any]) -> bool:
    new_pages = [item for item in page.context.pages if item not in pages_before]
    return any(page_requires_login(item) for item in new_pages)


def close_new_pages(context: Any, pages_before: set[Any]) -> None:
    for page in [item for item in context.pages if item not in pages_before]:
        try:
            page.close()
        except Exception:
            # 页面可能已经被站点脚本关闭；不应因此覆盖本次详情解析结果。
            pass


def classify_missing_button(page: Any, response: Any) -> DetailResolution:
    region = page.locator(DOWNLOAD_REGION).first
    if "夸克网盘" in region.inner_text():
        return retry_page(page, response, "下载区域存在夸克文字，但入口结构无法识别")
    if region.locator(DOWNLOAD_BUTTON).count() == 0:
        return retry_page(page, response, "下载区域未出现可识别的网盘入口")
    return DetailResolution(
        None, "未找到夸克网盘入口", "not_found", "已确认下载区域无夸克网盘入口"
    )


def find_attribute_url(button: Any) -> str | None:
    for attribute in LINK_ATTRIBUTES:
        match = QUARK_PATTERN.search(button.get_attribute(attribute) or "")
        if match:
            return match.group(0)
    return None


def collect_response_url(response: Any, candidates: list[str]) -> None:
    match = QUARK_PATTERN.search(response.url)
    if match:
        candidates.append(match.group(0))
        return
    try:
        if "json" in response.headers.get("content-type", ""):
            match = QUARK_PATTERN.search(response.text())
            if match:
                candidates.append(match.group(0))
    except Exception:
        return


def find_redirect_url(page: Any, pages_before: set[Any], candidates: list[str]) -> str | None:
    new_pages = [item for item in page.context.pages if item not in pages_before]
    urls = candidates + [page.url] + [item.url for item in new_pages]
    for url in urls:
        match = QUARK_PATTERN.search(url)
        if match:
            return match.group(0)
    return None


def combine_errors(first: str | None, second: str) -> str:
    return "；".join(part for part in (first, f"浏览器解析失败：{second}") if part)


def merge_provider_results(
    http_result: DetailResolution | None,
    browser_result: DetailResolution,
) -> tuple[ProviderResult, ...]:
    merged = {
        item.provider: item
        for item in (http_result.provider_results if http_result is not None else ())
    }
    for item in browser_result.provider_results:
        previous = merged.get(item.provider)
        if previous is None or item.url or previous.status not in ("complete", "not_found"):
            merged[item.provider] = item
    return tuple(merged.values())


def build_detail_update(
    candidate: DetailCandidate,
    resolution: DetailResolution,
    max_attempts: int,
) -> DetailUpdate:
    if resolution.detail_status == "complete":
        return DetailUpdate(
            resolution.quark_url,
            resolution.quark_status,
            "complete",
            0,
            None,
            now_iso(),
        )
    if resolution.detail_status == "login_expired":
        return DetailUpdate(
            candidate.quark_url,
            resolution.quark_status,
            "login_expired",
            candidate.detail_attempts,
            resolution.error,
            None,
        )

    # 复查失败记录等待一个完整周期后会开启新一轮尝试，不能沿用上一轮已耗尽的计数。
    previous_attempts = 0 if candidate.detail_status == "recheck_failed" else candidate.detail_attempts
    attempts = previous_attempts + 1
    if candidate.quark_url:
        retryable = resolution.detail_status == "retry" and attempts < max_attempts
        detail_status = "recheck_retry" if retryable else "recheck_failed"
    elif resolution.detail_status == "retry":
        detail_status = "retry" if attempts < max_attempts else "failed"
    else:
        detail_status = resolution.detail_status
    return DetailUpdate(
        candidate.quark_url,
        resolution.quark_status,
        detail_status,
        attempts,
        resolution.error[:1000] if resolution.error else None,
        None,
    )


def wait_for_request(config: EnrichConfig, reason: str) -> None:
    delay = random.uniform(config.delay_min, config.delay_max)
    LOGGER.info("等待 %.1f 秒后进行%s，降低请求频率", delay, reason)
    time.sleep(delay)


def wait_between_candidates(config: EnrichConfig, index: int, total: int) -> None:
    if index < total:
        wait_for_request(config, "下一条详情请求")

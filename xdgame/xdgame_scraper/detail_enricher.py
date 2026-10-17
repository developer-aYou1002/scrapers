from __future__ import annotations

import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

from .crawler import LINK_ATTRIBUTES, QUARK_PATTERN, create_session, extract_quark_url
from .database import DetailCandidate, DetailUpdate, now_iso


FORBIDDEN_MARKERS = ("无权限", "权限不足", "仅限会员", "会员可见", "开通会员")


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


@dataclass(frozen=True)
class BrowserRequest:
    page: Any
    candidate: DetailCandidate
    candidates: list[str]
    config: EnrichConfig


class LoginStateExpired(RuntimeError):
    """详情页需要重新登录，当前批次必须停止。"""


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
        if http_result is not None:
            return http_result
        wait_for_request(self.config, "浏览器回退")
        try:
            return self._resolve_browser(candidate)
        except BrowserUnavailable:
            raise
        except Exception as error:
            details = combine_errors(http_error, str(error))
            return DetailResolution(None, "详情补全临时失败", "retry", details)

    def _resolve_http(
        self,
        candidate: DetailCandidate,
    ) -> tuple[DetailResolution | None, str | None]:
        try:
            response = self.session.get(candidate.detail_url, timeout=self.config.timeout)
            response.raise_for_status()
        except requests.RequestException as error:
            return None, f"HTTP 请求失败：{error}"
        quark_url = extract_quark_url(response.text)
        if quark_url is None:
            return None, None
        result = DetailResolution(quark_url, "已从详情页获取", "complete")
        return result, None

    def _resolve_browser(self, candidate: DetailCandidate) -> DetailResolution:
        context = self._ensure_browser_context()
        pages_before = set(context.pages)
        page = context.new_page()
        candidates: list[str] = []
        page.on("response", lambda response: collect_response_url(response, candidates))
        try:
            request = BrowserRequest(page, candidate, candidates, self.config)
            return resolve_browser_page(request)
        finally:
            close_new_pages(context, pages_before)

    def _ensure_browser_context(self) -> Any:
        if self.context is not None:
            return self.context
        try:
            from playwright.sync_api import sync_playwright

            self.playwright = sync_playwright().start()
            self.browser = self.playwright.chromium.launch(headless=self.config.headless)
            state = str(self.config.login_state) if self.config.login_state.exists() else None
            self.context = self.browser.new_context(storage_state=state)
        except Exception as error:
            raise BrowserUnavailable(f"浏览器初始化失败：{error}") from error
        return self.context


def resolve_browser_page(request: BrowserRequest) -> DetailResolution:
    page = request.page
    timeout_ms = int(request.config.timeout * 1000)
    page.goto(request.candidate.detail_url, wait_until="domcontentloaded", timeout=timeout_ms)
    if page_requires_login(page):
        return DetailResolution(None, "登录状态已失效", "login_expired", "详情页要求重新登录")

    quark_url = extract_quark_url(page.content())
    if quark_url:
        return DetailResolution(quark_url, "已通过浏览器获取", "complete")

    button = page.get_by_text("夸克网盘", exact=True).first
    if button.count() == 0:
        return classify_missing_button(page)
    attribute_url = find_attribute_url(button)
    if attribute_url:
        return DetailResolution(attribute_url, "已通过浏览器获取", "complete")

    pages_before = set(page.context.pages)
    button.click(timeout=timeout_ms)
    # 下载入口可能通过异步请求或新标签页返回地址，短暂等待其完成跳转。
    page.wait_for_timeout(2000)
    if page_requires_login(page) or new_page_requires_login(page, pages_before):
        return DetailResolution(None, "登录状态已失效", "login_expired", "点击入口后要求重新登录")
    quark_url = find_redirect_url(page, pages_before, request.candidates)
    if quark_url:
        return DetailResolution(quark_url, "已通过浏览器获取", "complete")
    if page_has_forbidden_marker(page):
        return DetailResolution(None, "无权限访问夸克地址", "forbidden", "页面提示需要会员或访问权限")
    return DetailResolution(None, "夸克入口页面结构变化", "structure_changed", "点击入口后未发现夸克地址")


def page_requires_login(page: Any) -> bool:
    return page.locator("#useridl").count() > 0


def page_has_forbidden_marker(page: Any) -> bool:
    return any(marker in page_body_text(page) for marker in FORBIDDEN_MARKERS)


def page_body_text(page: Any) -> str:
    return page.locator("body").inner_text() if page.locator("body").count() else ""


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


def classify_missing_button(page: Any) -> DetailResolution:
    if page_has_forbidden_marker(page):
        return DetailResolution(None, "无权限访问夸克地址", "forbidden", "页面提示需要会员或访问权限")
    if not page_body_text(page).strip():
        return DetailResolution(None, "详情页结构异常", "structure_changed", "详情页正文为空")
    return DetailResolution(None, "未找到夸克网盘入口", "not_found", "详情页没有夸克网盘按钮")


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
    print(f"等待 {delay:.1f} 秒后进行{reason}，降低请求频率。")
    time.sleep(delay)


def wait_between_candidates(config: EnrichConfig, index: int, total: int) -> None:
    if index < total:
        wait_for_request(config, "下一条详情请求")

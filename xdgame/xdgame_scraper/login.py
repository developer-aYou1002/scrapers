from __future__ import annotations

import os
from pathlib import Path
from typing import Any


def fill_login_form(page: Any) -> bool:
    username = os.environ.get("XDGAME_USERNAME")
    password = os.environ.get("XDGAME_PASSWORD")
    if not username or not password:
        return False
    page.locator("#useridl").fill(username)
    page.locator("#pwdl").fill(password)
    return True


def check_standard_checkbox(page: Any) -> bool:
    checkbox = page.locator("input[type='checkbox']").first
    if checkbox.count() == 0:
        return False
    if not checkbox.is_checked():
        checkbox.check()
    return True


def wait_and_submit_login(page: Any, timeout_ms: int) -> None:
    if not check_standard_checkbox(page):
        input("请在浏览器中完成人机验证或填写图片验证码，然后回到终端按回车：")
    if page.locator("#useridl").count() == 0:
        return
    login_button = page.locator("button[onclick='login()']")
    if login_button.count() == 0:
        raise RuntimeError("未找到登录按钮，登录状态未保存")
    login_button.click()
    page.wait_for_function(
        "!document.querySelector('#useridl')",
        timeout=timeout_ms,
    )


def wait_for_manual_login(page: Any, reason: str) -> None:
    input(f"{reason}，请在浏览器中完成登录后回到终端按回车：")
    if page.locator("#useridl").count() > 0:
        raise RuntimeError("未检测到登录成功，登录状态未保存")


def save_login_state(url: str, output: Path, timeout: float) -> None:
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ImportError as error:
        raise RuntimeError("缺少 Playwright，请先安装 requirements.txt 中的依赖") from error

    with sync_playwright() as playwright:
        browser = None
        context = None
        try:
            browser = playwright.chromium.launch(headless=False)
            context = browser.new_context()
            page = context.new_page()
            timeout_ms = int(timeout * 1000)
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            if fill_login_form(page):
                print("账号密码已自动填写。")
                wait_and_submit_login(page, timeout_ms)
            else:
                wait_for_manual_login(page, "未检测到完整的账号环境变量")
            output.parent.mkdir(parents=True, exist_ok=True)
            context.storage_state(path=str(output))
        except PlaywrightTimeoutError as error:
            raise RuntimeError(f"等待登录超时（{timeout:g} 秒），登录状态未保存") from error
        finally:
            # 登录失败时同样必须释放上下文和浏览器，且上下文关闭失败不能阻止浏览器关闭。
            try:
                if context is not None:
                    context.close()
            finally:
                if browser is not None:
                    browser.close()
    print(f"登录状态已保存至：{output}")

# XDGAME 今日更新爬虫

抓取 XDGAME 首页“今日更新”中的游戏，进入详情页解析夸克网盘地址，并将结果保存为 JSON。

## 能力与限制

- 使用 `requests + BeautifulSoup` 抓取今日更新列表和详情页。
- 详情页没有公开夸克地址时，使用 Playwright 执行 JavaScript 并点击“夸克网盘”。
- 支持保存本人账号的合法登录状态，不会把账号密码写入代码或结果文件。
- 不会绕过验证码、会员权限或其他访问控制。

## 安装

请使用 Python 3.10 或更高版本，在本目录执行：

```powershell
python -m pip install -r requirements.txt
python -m playwright install chromium
```

## 首次登录

如下载入口需要登录，执行：

```powershell
python scraper.py --login
```

程序会打开浏览器。请在浏览器中手动登录，确认登录成功后回到终端按回车。登录状态默认保存在 `login_state.json`。

`login_state.json` 可能包含有效 Cookie，属于敏感文件，请勿发送给他人或提交到 Git 仓库。登录失效后重新执行上述命令即可。

## 抓取

```powershell
python scraper.py
```

调试时显示浏览器：

```powershell
python scraper.py --show-browser
```

指定输出文件：

```powershell
python scraper.py --output data\today.json
```

输出会包含游戏名、详情页、夸克网盘地址以及解析状态。

请控制抓取频率，遵守目标网站规则，并仅访问本人有权获取的内容。

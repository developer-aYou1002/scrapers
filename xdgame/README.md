# XDGAME 今日更新爬虫

抓取 XDGAME 首页“今日更新”中的游戏，进入详情页解析夸克网盘地址，并将结果持久化到 SQLite，同时保留 JSON 输出。

## 能力与限制

- 使用 `requests + BeautifulSoup` 抓取今日更新列表和详情页。
- 详情页没有公开夸克地址时，使用 Playwright 执行 JavaScript 并点击“夸克网盘”。
- 使用 Python 内置 SQLite 保存游戏、运行记录和后续断点续跑所需的 checkpoint。
- 支持保存本人账号的合法登录状态，不会把账号密码写入代码或结果文件。
- 不会绕过验证码、会员权限或其他访问控制。

## 代码结构

- `scraper.py`：兼容原有调用方式的轻量启动器。
- `xdgame_scraper/cli.py`：命令行参数、流程编排、JSON 输出和退出码。
- `xdgame_scraper/crawler.py`：HTTP/Playwright 抓取、页面解析和请求节流。
- `xdgame_scraper/database.py`：SQLite 初始化、迁移、事务及数据访问。
- `xdgame_scraper/login.py`：登录表单填写、人工验证回退和登录状态保存。
- `xdgame_scraper/__main__.py`：支持模块方式启动。

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

使用环境变量提供账号密码后，程序会自动填写登录表单：

```powershell
$env:XDGAME_USERNAME="你的账号"
$env:XDGAME_PASSWORD="你的密码"
python scraper.py --login
```

程序会打开浏览器并自动填写账号密码。如果页面提供原生普通勾选框，程序会自动勾选并提交；如果网站返回智能验证或图片验证码，需要在浏览器中完成验证后回到终端按回车，程序随后提交登录。该流程不依赖特定验证字段，避免图片验证码模式一直等待空的 `cap_token`。

未设置完整环境变量时，`--login` 保持原有手动登录方式。登录超时或人工确认时仍存在登录表单，程序会返回简短错误且不保存无效状态。登录状态默认保存在 `login_state.json`，浏览器会在成功和失败路径中关闭。

`login_state.json` 可能包含有效 Cookie，属于敏感文件，请勿发送给他人或提交到 Git 仓库。登录失效后重新执行上述命令即可。

## 抓取

```powershell
python scraper.py
```

也可以在本目录使用模块方式启动：

```powershell
python -m xdgame_scraper
```

详情页请求默认使用 2～4 秒的随机间隔，降低触发限流或账号风控的概率。可按需覆盖间隔：

```powershell
python scraper.py --delay-min 3 --delay-max 6
```

调试时显示浏览器：

```powershell
python scraper.py --show-browser
```

指定输出文件：

```powershell
python scraper.py --output output\custom.json
```

默认结果保存在 `output/today_games.json`，包含游戏名、详情页、夸克网盘地址以及解析状态。`output/` 是运行产物目录，不提交到 Git。

## SQLite 数据库

抓取时会自动初始化并更新 `data/xdgame.db`，不需要手动建库。数据库结构使用 `PRAGMA user_version` 管理版本，重复运行不会重复建表或清空已有数据。

当前包含三类数据：

- `games`：游戏、详情地址、夸克地址、解析状态、尝试次数及发现时间。
- `crawl_runs`：每次任务的开始、结束、状态、处理数量和错误摘要。
- `crawl_checkpoints`：为后续列表抓取和详情补全保存断点位置。

同一详情页地址会执行 UPSERT，不会重复插入。可以通过参数指定其他数据库路径：

```powershell
python scraper.py --database data\custom.db
```

默认数据库、登录状态和 JSON 输出均相对于脚本所在目录定位，Windows 定时任务从其他工作目录启动时也不会写错位置。`data/` 已被 Git 忽略；备份前应先停止抓取进程，再复制数据库文件。

请控制抓取频率，遵守目标网站规则，并仅访问本人有权获取的内容。

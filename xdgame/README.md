# XDGAME 游戏抓取器

抓取 XDGAME 首页“今日更新”或“电脑游戏”分类全量列表，将结果持久化到 SQLite；今日更新模式还会解析夸克网盘地址并保留 JSON 输出。

## 能力与限制

- 使用 `requests + BeautifulSoup` 抓取今日更新列表和详情页。
- 支持逐页抓取电脑游戏分类全量列表，每页成功后立即写入 SQLite。
- 详情页没有公开夸克地址时，使用 Playwright 执行 JavaScript 并点击“夸克网盘”。
- 使用 Python 内置 SQLite 保存游戏、运行记录和后续断点续跑所需的 checkpoint。
- 支持保存本人账号的合法登录状态，不会把账号密码写入代码或结果文件。
- 不会绕过验证码、会员权限或其他访问控制。

## 代码结构

- `scraper.py`：兼容原有调用方式的轻量启动器。
- `xdgame_scraper/cli.py`：命令行参数、流程编排、JSON 输出和退出码。
- `xdgame_scraper/crawler.py`：HTTP/Playwright 抓取、页面解析和请求节流。
- `xdgame_scraper/list_crawler.py`：电脑游戏列表页解析、分页识别和列表请求节流。
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
python scraper.py login
```

原有的 `python scraper.py --login` 仍然兼容。

使用环境变量提供账号密码后，程序会自动填写登录表单：

```powershell
$env:XDGAME_USERNAME="你的账号"
$env:XDGAME_PASSWORD="你的密码"
python scraper.py login
```

程序会打开浏览器并自动填写账号密码。如果页面提供原生普通勾选框，程序会自动勾选并提交；如果网站返回智能验证或图片验证码，需要在浏览器中完成验证后回到终端按回车，程序随后提交登录。该流程不依赖特定验证字段，避免图片验证码模式一直等待空的 `cap_token`。

未设置完整环境变量时，`--login` 保持原有手动登录方式。登录超时或人工确认时仍存在登录表单，程序会返回简短错误且不保存无效状态。登录状态默认保存在 `login_state.json`，浏览器会在成功和失败路径中关闭。

`login_state.json` 可能包含有效 Cookie，属于敏感文件，请勿发送给他人或提交到 Git 仓库。登录失效后重新执行上述命令即可。

## 抓取

### 今日更新

```powershell
python scraper.py
```

也可以显式指定今日更新子命令：

```powershell
python scraper.py today
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

### 电脑游戏全量列表

从电脑游戏分类第一页开始，按网站提供的“下一页”链接逐页抓取：

```powershell
python scraper.py full-list
```

默认入口为 `https://www.xdgame.com/list/1/`，默认最多抓取 1000 页，并在相邻页面请求之间随机等待 2～4 秒。当前站点页数较多，完整运行可能需要较长时间。

可在人工验证时限制页数，例如只抓取前两页：

```powershell
python scraper.py full-list --max-pages 2
```

如果达到上限时网站仍有下一页，程序会以非零状态退出并明确提示“全量抓取尚未结束”；已经成功写入的页面不会回滚。也可以覆盖列表入口或请求间隔：

```powershell
python scraper.py full-list --list-url https://www.xdgame.com/list/1/ --delay-min 3 --delay-max 6
```

每条列表记录会先保存名称、详情页、分类、评分、容量、站点日期和封面地址，并显式设置以下详情初始状态：

- `quark_url = NULL`
- `quark_status = 待解析`
- `detail_status = pending`
- `detail_attempts = 0`
- `last_error = NULL`

如果详情地址已经存在，列表 UPSERT 会保留已有详情和夸克解析结果，不会把已成功记录重置为待处理。当前第三阶段的 `full-list` 子命令只负责稳定完成全量列表入库，不会增加详情补全尝试次数。

后续“详情和夸克地址分批补充”阶段会对全量列表中的所有待处理记录进入详情页，并补充夸克网盘地址、夸克解析状态、详情处理状态、详情尝试次数和最后错误。详情补全会采用每批 20 条、单条最多尝试 3 次的策略，并在断点续跑阶段完成后接入，避免数百页列表抓取与上万条详情访问绑定成一个无法恢复的长任务。

## SQLite 数据库

抓取时会自动初始化并更新 `data/xdgame.db`，不需要手动建库。数据库结构使用 `PRAGMA user_version` 管理版本，重复运行不会重复建表或清空已有数据。

当前数据库结构版本为 2，包含三类数据：

- `games`：游戏、详情地址、分类、列表元数据、夸克地址、解析状态、尝试次数及发现时间。
- `crawl_runs`：每次任务的开始、结束、状态、处理数量和错误摘要。
- `crawl_checkpoints`：为后续列表抓取和详情补全保存断点位置。

同一详情页地址会执行 UPSERT，不会重复插入。可以通过参数指定其他数据库路径：

```powershell
python scraper.py --database data\custom.db
```

默认数据库、登录状态和 JSON 输出均相对于脚本所在目录定位，Windows 定时任务从其他工作目录启动时也不会写错位置。`data/` 已被 Git 忽略；备份前应先停止抓取进程，再复制数据库文件。

## 执行与人工验证命令

以下命令均在 PowerShell 中执行。先进入项目目录：

```powershell
cd E:\Code\MyProjects\scrapers\xdgame
```

### 检查环境和命令行参数

```powershell
python --version
python -m pip show requests beautifulsoup4 playwright
python scraper.py --help
```

### 只初始化验证数据库

使用独立的 `data/verify.db`，避免验证数据与正式数据库混在一起：

```powershell
@'
from pathlib import Path
from xdgame_scraper.database import database_connection

path = Path("data/verify.db")
with database_connection(path) as db:
    version = db.execute("PRAGMA user_version").fetchone()[0]
    print("数据库路径：", path.resolve())
    print("数据库版本：", version)
'@ | python -
```

预期数据库版本为 `2`，并且下面的命令返回 `True`：

```powershell
Test-Path .\data\verify.db
```

### 快速验证前两页列表

```powershell
python scraper.py full-list --database data\verify.db --max-pages 2
$LASTEXITCODE
```

因为网站在第二页之后仍有下一页，程序应在成功写入两页后提示达到安全页数上限，退出码应为 `1`，本次运行状态应记录为 `incomplete`。这是预期的安全上限行为，不代表前两页写入失败。

### 检查数据库结构、数据和运行记录

为避免 PowerShell 与 Python 单行命令之间的引号转义问题，统一使用以下多行形式：

```powershell
@'
import sqlite3

db = sqlite3.connect("data/verify.db")

version = db.execute("PRAGMA user_version").fetchone()[0]
tables = db.execute(
    "SELECT name FROM sqlite_master WHERE type = ? ORDER BY name",
    ("table",),
).fetchall()
total, unique_urls = db.execute(
    "SELECT COUNT(*), COUNT(DISTINCT detail_url) FROM games"
).fetchone()

print("数据库版本：", version)
print("数据表：", [row[0] for row in tables])
print("游戏总数：", total)
print("唯一详情地址数：", unique_urls)
print("是否没有重复详情地址：", total == unique_urls)

pending = db.execute("""
    SELECT COUNT(*)
    FROM games
    WHERE quark_url IS NULL
      AND quark_status = '待解析'
      AND detail_status = 'pending'
      AND detail_attempts = 0
      AND last_error IS NULL
""").fetchone()[0]
print("符合详情初始状态的记录数：", pending)

print("最近写入的游戏：")
for row in db.execute("""
    SELECT name, category, score, size_text, source_updated_date
    FROM games
    ORDER BY id DESC
    LIMIT 5
"""):
    print(row)

print("最近运行记录：")
for row in db.execute("""
    SELECT run_type, status, processed_count, error_summary
    FROM crawl_runs
    ORDER BY id DESC
    LIMIT 3
"""):
    print(row)

db.close()
'@ | python -
```

预期结果：

- 数据库版本为 `2`。
- 数据表包含 `games`、`crawl_checkpoints`、`crawl_runs`。
- 游戏总数大于 `0`，并与唯一详情地址数相同。
- 游戏记录包含分类、评分、容量和站点日期等列表元数据。
- 新入库且尚未处理的游戏符合五项详情初始状态。
- 两页验证对应的最新运行记录为 `full-list`、`incomplete`。

### 验证重复抓取不会产生重复游戏

再次执行两页验证：

```powershell
python scraper.py full-list --database data\verify.db --max-pages 2
```

然后重新运行上一节的数据库检查命令。游戏总数可以因为网站新增内容而变化，但“游戏总数”和“唯一详情地址数”必须保持相同。

### 正式执行

保存登录状态：

```powershell
python scraper.py login
```

抓取今日更新、详情和夸克地址：

```powershell
python scraper.py today
```

抓取电脑游戏全量列表：

```powershell
python scraper.py full-list
```

查看正式数据库的最近运行记录时，把上述验证命令中的 `data/verify.db` 替换为 `data/xdgame.db`。

当前全量列表有数百页，完整运行可能需要几十分钟。本阶段尚未实现断点续跑，正式运行时请避免主动中断；断点续跑将在下一阶段实现。

请控制抓取频率，遵守目标网站规则，并仅访问本人有权获取的内容。

# XDGAME 爬虫命令速查（macOS）

以下命令均在本机终端执行。先进入项目目录；本机没有 `python` 命令时，使用虚拟环境中的 `.venv/bin/python`。

```bash
cd /Users/niko/Code/projects/git-projects/scrapers/xdgame
```

## 安装与检查

| 命令 | 作用 |
| --- | --- |
| `python3 --version` | 检查 Python 版本；项目要求 3.10 或更高。 |
| `python3 -m venv .venv` | 首次创建项目虚拟环境；已有 `.venv` 时不必重复创建。 |
| `.venv/bin/python -m pip install -r requirements.txt` | 安装 Python 依赖。 |
| `.venv/bin/python -m playwright install chromium` | 安装浏览器回退所需的 Chromium。 |
| `.venv/bin/python scraper.py --help` | 查看当前程序接受的子命令和参数。 |

## 抓取与导出

| 命令 | 作用 |
| --- | --- |
| `.venv/bin/python scraper.py login` | 打开浏览器登录自己的站点账号，将登录状态保存到本地 `login_state.json`；状态失效时重新执行。 |
| `.venv/bin/python scraper.py full-list` | 从电脑游戏类目抓取完整列表。已有断点时从下一页续跑；列表入库不等于补全网盘地址。 |
| `.venv/bin/python scraper.py full-list --max-pages 2` | 限制本次最多扫描 2 页，用于小范围检查；若列表尚未结束，命令可能返回非零退出码。 |
| `.venv/bin/python scraper.py full-list --restart` | 忽略已完成的列表断点，从第一页重新检查。仅在确实需要重扫时使用。 |
| `.venv/bin/python scraper.py rank-scan` | 扫描并保存“人气排名”前 100 名的名次、热度和详情 URL；不点击下载入口。 |
| `.venv/bin/python scraper.py rank-enrich --batch-size 20` | 从最近的人气榜快照中按名次补全一批尚未成功的电脑游戏；与 `full-list` 相互独立。 |
| `.venv/bin/python scraper.py rank-enrich --batch-size 10 --daily-game-limit 40` | 缩小单批数量，并设置人气榜补全每个北京时间自然日访问不同游戏的上限。 |
| `.venv/bin/python scraper.py enrich` | 补全一批详情，默认最多 20 条，同时尝试夸克、百度、天翼、迅雷和移动网盘。 |
| `.venv/bin/python scraper.py enrich --batch-size 10 --delay-min 3 --delay-max 6` | 单次小批补全，并将请求间隔设为随机 3～6 秒。适合人工观察结果；不保证避开网站每日额度。 |
| `.venv/bin/python scraper.py enrich --batch-size 1 --show-browser` | 只处理一条候选记录并显示浏览器，便于人工观察入口行为；仍会消耗网站访问或下载额度。 |
| `.venv/bin/python scraper.py enrich --recheck-days 0 --batch-size 1` | 将已有夸克地址的成功记录提前纳入复查候选；并非只复查一条指定游戏，可能增加下载额度消耗，谨慎使用。 |
| `.venv/bin/python scraper.py daily` | 扫描近期电脑游戏列表并补全一批详情；当天新入库的游戏优先。 |
| `.venv/bin/python scraper.py daily --max-pages 20 --existing-page-limit 5 --batch-size 10` | 调整日常扫描页数、连续无新增页数阈值和本次详情补全上限。 |
| `.venv/bin/python scraper.py today` | 单独抓取首页“今日更新”，输出 `output/today_games.json`；此旧流程只处理夸克。 |
| `.venv/bin/python scraper.py export` | 将数据库导出到 `output/games.json`；不请求网站，也不修改数据库中的游戏记录。 |
| `.venv/bin/python scraper.py export --output output/custom-games.json` | 指定 JSON 导出文件路径。 |

`enrich` 先处理可重试记录，再按数据库 `games.id` 从小到大处理普通 `pending`。该 ID 是入库顺序，并非网站游戏 ID 或固定页码。旧版产生的 `not_found`、`structure_changed`，以及已耗尽尝试次数的 `failed`，不会因再次运行 `enrich` 而自动重试；当前没有为它们提供安全的重入队命令。

抓取命令默认使用 `data/xdgame.db`，日志写入 `logs/xdgame.log`，单次请求超时 20 秒。需要使用其他数据库时，在命令后加 `--database 路径`；需要改变请求超时时加 `--timeout 秒数`。不要把已有数据库误写到其他路径。

### 连续补全命令：需留意每日下载额度

```bash
.venv/bin/python scraper.py enrich --batch-size 50 --batch-interval 120
```

这条命令每批最多 50 条，批次之间暂停 120 秒，直到无候选记录。但暂停时间**不会重置网站的每日下载额度**。2026-10-23 实际检查发现，当前登录账号的下载入口提示“每日额度 50 个游戏”；同一游戏切换网盘不重复计数。程序现会在识别到“今日游戏下载额度已用完”页面时立即停止，退出码为 `4`，且不增加当前游戏的失败次数。识别依赖站点页面文案，若网站改版仍需人工观察；`--daily-game-limit` 仅限制 `rank-enrich`，不限制此连续命令。

## 查看进度（只读）

以下命令同样在 `xdgame` 目录执行，不显示完整网盘分享地址：

| 命令 | 作用 |
| --- | --- |
| `pgrep -fl 'scraper.py (enrich|daily|full-list|rank-scan|rank-enrich)'` | 查看相关爬虫进程是否仍在运行。 |
| `tail -n 50 logs/xdgame.log` | 查看最近 50 行日志，判断是否持续成功、失败或等待下一批。 |
| `sqlite3 -header -column 'file:data/xdgame.db?mode=ro' "SELECT detail_status, count(*) AS n FROM games GROUP BY detail_status ORDER BY n DESC;"` | 按详情状态统计游戏数量。 |
| `sqlite3 -header -column 'file:data/xdgame.db?mode=ro' "SELECT id, status, processed_count, started_at, finished_at FROM crawl_runs ORDER BY id DESC LIMIT 10;"` | 查看最近 10 次任务记录。 |
| `sqlite3 -header -column 'file:data/xdgame.db?mode=ro' "SELECT count(*) AS quark_links FROM games WHERE coalesce(quark_url, '') <> '';"` | 统计已保存的夸克地址数。 |
| `sqlite3 -header -column 'file:data/xdgame.db?mode=ro' "SELECT provider, count(*) AS links FROM game_disk_links WHERE coalesce(url, '') <> '' GROUP BY provider;"` | 按网盘类型统计已保存的其他网盘地址数。 |

只查看有地址的游戏名称和网盘类型：

```bash
sqlite3 -header -column 'file:data/xdgame.db?mode=ro' \
"SELECT g.id, g.name, CASE WHEN coalesce(g.quark_url, '') <> '' THEN '有' ELSE '无' END AS quark,
 (SELECT group_concat(d.provider, '、') FROM game_disk_links d
  WHERE d.game_id = g.id AND coalesce(d.url, '') <> '') AS other_disks
 FROM games g
 WHERE coalesce(g.quark_url, '') <> ''
    OR EXISTS (SELECT 1 FROM game_disk_links d WHERE d.game_id = g.id AND coalesce(d.url, '') <> '')
 ORDER BY g.id;"
```

## 使用注意

- `login_state.json`、`data/xdgame.db` 和导出的 JSON 可能含 Cookie 或有效分享地址，不要提交到 Git 或上传在线数据库查看器。
- 同一数据库同一时间不要启动两个详情补全任务。运行中的任务可在其终端按 `Ctrl+C` 中断；已逐条写入的结果会保留。
- 图形化查看数据库可使用 DB Browser for SQLite 的只读打开方式；查看与抓取同时进行时不要编辑数据库。
- `--batch-interval` 仅适用于 `enrich`，不是“每日最多抓取 50 个游戏”的限制参数；`rank-enrich` 是独立的单批命令。

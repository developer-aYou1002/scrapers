# XDGAME 游戏抓取器

抓取 XDGAME 首页“今日更新”或“电脑游戏”分类全量列表，将结果持久化到 SQLite，并支持分批补全及定期复查夸克和其他网盘地址；今日更新模式还会保留 JSON 输出。

macOS 常用命令及作用见 [COMMANDS.md](COMMANDS.md)。

## 能力与限制

- 使用 `requests + BeautifulSoup` 抓取今日更新列表和详情页。
- 支持逐页抓取电脑游戏分类全量列表，每页成功后原子写入 SQLite 并保存断点。
- 支持按批次补全详情，每条结果独立提交，任务中断后可从剩余记录继续。
- 支持日常增量扫描，更新已有列表元数据，并优先补全当天新增记录。
- 详情页没有公开夸克地址时，使用 Playwright 执行 JavaScript 并点击“夸克网盘”。
- 使用 Python 内置 SQLite 保存游戏、运行记录和后续断点续跑所需的 checkpoint。
- 使用带运行标识的控制台与轮转文件日志，并对登录敏感信息统一脱敏。
- 支持保存本人账号的合法登录状态，不会把账号密码写入代码或结果文件。
- 不会绕过验证码、会员权限或其他访问控制。

## 代码结构

- `scraper.py`：兼容原有调用方式的轻量启动器。
- `xdgame_scraper/cli.py`：命令行参数、流程编排、JSON 输出和退出码。
- `xdgame_scraper/crawler.py`：HTTP/Playwright 抓取、页面解析和请求节流。
- `xdgame_scraper/list_crawler.py`：电脑游戏列表页解析、分页识别和列表请求节流。
- `xdgame_scraper/detail_enricher.py`：详情分批补全、浏览器回退、状态分类与请求节流。
- `xdgame_scraper/game_worker.py`：单游戏独立工作进程、总时限与浏览器子进程回收。
- `xdgame_scraper/stage_trace.py`：详情阶段的脱敏开始、结束及耗时事件。
- `xdgame_scraper/daily.py`：日常列表扫描、新增识别和安全停止判断。
- `xdgame_scraper/ranking.py`：人气榜前 100 名解析与完整性校验。
- `xdgame_scraper/database.py`：SQLite 初始化、迁移、事务及数据访问。
- `xdgame_scraper/exporter.py`：从 SQLite 只读加载并原子导出完整 JSON。
- `xdgame_scraper/login.py`：登录表单填写、人工验证回退和登录状态保存。
- `xdgame_scraper/logging_setup.py`：日志初始化、文件轮转、运行标识和敏感信息脱敏。
- `xdgame_scraper/errors.py`：跨抓取流程共享的登录失效与用户中断错误类型。
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

每页的游戏数据、下一页地址和最后一条游戏地址在同一事务中提交。任务中断后再次执行相同命令，会自动从最近成功页面的下一页继续：

```powershell
python scraper.py full-list
```

如果 checkpoint 已经标记为完成，命令不会重复扫描。需要从第一页重新检查时使用：

```powershell
python scraper.py full-list --restart
```

可在人工验证时限制页数，例如只抓取前两页：

```powershell
python scraper.py full-list --max-pages 2
```

如果达到上限时网站仍有下一页，程序会以非零状态退出并明确提示“全量抓取尚未结束”；已经成功写入的页面和下一页断点不会回滚。再次执行会从断点继续。也可以覆盖列表入口或请求间隔：

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

全量列表扫描和详情补全相互独立，避免数百页列表抓取与上万条详情访问绑定成一个无法恢复的长任务。列表入库完成或取得部分数据后，可按后续章节分批补全详情。

同一数据库和列表入口同一时间只允许一个全量任务运行。任务锁每页刷新心跳，正常退出时自动释放；进程异常消失且超过30分钟没有心跳时，后续任务可以自动接管旧锁。

### 人气榜优先补全（不影响全量列表）

先扫描站点“天梯排名”的人气榜前 100 名，保存名次和热度快照；榜单包含非电脑游戏条目，只有能按详情 URL 匹配到 `full-list` 数据库的记录才进入补全队列：

```bash
.venv/bin/python scraper.py rank-scan
.venv/bin/python scraper.py rank-enrich --batch-size 20
```

`rank-enrich` 每次只处理一批，严格按榜单名次选取尚未成功且未耗尽尝试次数的记录；已完成的游戏、旧版 `not_found` / `structure_changed` 和 `failed` 不会自动重试。再次运行 `rank-scan` 可刷新榜单快照；`full-list` 的断点、普通 `enrich` 的顺序及 `daily` 的新增优先级不变。榜单页目前只直接提供前 100 名，不能据此对全库游戏排序。

为留出余量，`rank-enrich` 默认每个北京时间自然日最多访问 40 个不同游戏，可用 `--daily-game-limit` 调整；这个上限只统计 `rank-enrich` 自己访问的游戏，不知道账号在网页或其他命令中的用量。站点提示“今日游戏下载额度已用完”时，补全任务立即停止并返回退出码 `4`，当前游戏不增加失败次数；同样的止损也适用于普通 `enrich` 和 `daily`。站点额度何时恢复以页面提示为准。历史上已因额度耗尽标为 `failed` 的记录不会自动重置，需要先单独核实再处理。

### 详情和夸克地址分批补全

默认从数据库稳定选取最多 20 条待处理或可重试记录：

```powershell
python scraper.py enrich
```

每条记录先通过 HTTP 检查详情页和各网盘入口的公开跳转地址；无法确认的入口再由带登录状态的 Playwright 解析。当前额外支持百度、天翼、迅雷、移动网盘；“正版购买”不作为网盘链接。入口请求和相邻游戏之间沿用随机 2～4 秒间隔，每条结果立即写入数据库，因此中断后重新执行相同命令即可继续处理剩余记录。解析多个入口会比只处理夸克耗时更久，并增加站点请求量。

每条游戏在独立工作进程内处理，默认总时限为 180 秒，可用 `--game-timeout 180` 调整；这与单次请求的 `--timeout 20` 不同。超时后会回收工作进程及其浏览器子进程，将当前游戏按可重试失败计次，然后继续下一条；达到 `--max-attempts` 后仍会转为失败状态。两条游戏之间的随机等待不计入单游戏时限。工作进程隔离会增加浏览器启动开销，首次使用建议小批量人工观察。

详情日志按游戏 ID 记录 HTTP 详情、下载接口、浏览器导航、夸克入口、其他网盘页面及各网盘入口的阶段开始、结束、耗时和结果；超时会记录最后一个未结束阶段。日志不应包含分享地址、Cookie 或页面正文。浏览器响应事件不再同步读取 JSON 正文；如果某个链接只存在于该正文中，可能暂时无法取得，需要在人工验证后增加受总时限保护的专门解析步骤。

其他网盘结果按游戏和网盘类型分别写入 `game_disk_links` 表，保存地址、状态、错误原因和检查时间；复查失败时保留上次有效地址，状态记为 `stale`。原有 `games.quark_url`、`quark_status` 和 `detail_status` 仍只表示夸克补全结果。其他网盘可取得地址并不等于夸克补全成功；单个其他网盘失败也不会清除已有夸克地址。首次使用新版本执行写数据库的命令时会自动迁移旧数据库结构，建议先备份数据库。

多网盘解析适用于 `enrich` 和 `daily` 共用的详情补全阶段；独立的旧版 `today` 输出仍只包含夸克地址。

独立 `enrich` 会先处理可重试记录，再按数据库 ID 从小到大处理普通 `pending`；数据库 ID 是入库顺序，不等于网站游戏 ID 或固定页码。`daily` 扫描到的当日新增 URL 仍优先于历史积压。

可以覆盖批量大小、最大尝试次数和请求间隔：

```powershell
python scraper.py enrich --batch-size 20 --max-attempts 3 --delay-min 2 --delay-max 4
```

默认只处理一批。需要连续补全时，可指定批次间隔（单位：秒）；每批结束后若仍有可处理记录，等待指定时间再处理下一批，直到没有候选记录或任务出错：

```bash
.venv/bin/python scraper.py enrich --batch-size 50 --batch-interval 120
```

上述命令每批最多处理 50 条，批次之间等待 2 分钟；最后一批结束后不会额外等待。每批都会独立记录运行结果，已提交的数据在中断后仍会保留。`--batch-interval` 仅对 `enrich` 生效，不影响 `daily` 的单批补全。

详情状态包括：

- `complete`：成功取得夸克地址。
- `retry`：临时失败、页面未完整加载或入口点击后未能确认地址，后续批次继续尝试；累计 3 次后转为 `failed`。有限的响应状态、最终页面主机和标题等诊断信息保存在 `last_error`，不含 URL 查询参数。
- `login_expired`：登录状态失效，当前批次立即停止并返回退出码 `3`；重新执行 `login` 后可继续。
- 今日下载额度耗尽：当前批次立即停止并返回退出码 `4`；当前游戏不写入失败结果，已完成的逐条结果保留。
- `forbidden`：页面明确提示无权限或需要会员。
- `not_found`：确认正常详情页已加载下载区域及其他网盘入口，但未发现夸克网盘入口。
- `structure_changed`：旧版本产生的结构异常状态；新抓取遇到结构不确定时改为可重试，不再新增此状态。
- `recheck_retry` / `recheck_failed`：已有地址复查失败；原地址会保留，不会因临时故障被清空。

成功地址默认每 30 天进入复查队列。可在临时人工检查时覆盖周期：

```powershell
python scraper.py enrich --recheck-days 30
```

同一数据库同一时间只允许一个 `enrich` 任务运行。浏览器回退需要有效的 `login_state.json`；登录失效后执行 `python scraper.py login` 更新状态。

旧版本留下的 `not_found` 和 `structure_changed` 不会自动进入补全队列。待人工抽样确认后，应先停止补全任务并备份数据库，再分批重新入队；直接运行 `enrich` 不会重试这些历史状态。

已完成的旧记录也不会仅因新增其他网盘支持而立即重新入队；它们会按现有复查周期进入流程。必要时可用 `--recheck-days 0` 提前复查已完成记录，但这会增加请求量；历史 `not_found` 和 `structure_changed` 仍需单独处理。

### 日常增量更新

日常任务先扫描最近的电脑游戏列表，再对当天新增、可重试、待处理或到期复查的记录执行一批详情补全：

```powershell
python scraper.py daily
```

默认最多扫描 10 页。连续 3 个完整页面没有发现新游戏时提前停止；这些页面中的已有记录仍会更新名称、评分、容量、封面、站点日期和 `last_seen_at`。当天新增记录优先进入默认 20 条详情补全批次，其余待处理记录会在后续运行中继续处理。

可以覆盖列表停止条件和详情批量大小：

```powershell
python scraper.py daily --max-pages 20 --existing-page-limit 5 --batch-size 30
```

如果到达页数上限时最后一页仍发现新游戏，已扫描页面和已处理详情会保留，`crawl_runs` 状态记录为 `incomplete`，命令返回退出码 `1`。扩大 `--max-pages` 后重新运行即可。连续无新增提前停止、到达列表末页，或到达上限时最后一页已经无新增，退出码为 `0`；登录失效统一返回 `3`，其他网络或数据库错误返回 `1`。

`daily` 与独立 `enrich` 共用详情任务锁，并与相同列表入口的 `full-list` 共用列表任务锁，避免两个进程重复补全或同时扫描同一列表。

### 导出 SQLite 数据

将当前数据库中的全部游戏导出为 JSON，不访问网站，也不修改游戏、断点或运行状态：

```powershell
python scraper.py export
```

默认输出为 `output/games.json`。可以覆盖输出路径：

```powershell
python scraper.py export --output output\custom-games.json
```

导出文件包含导出时间、记录总数、详情完成数、夸克地址数、其他网盘地址数，以及 `games` 表中的全部业务字段；每条游戏的 `other_disk_links` 对象按网盘类型列出地址和状态。程序先完整写入同目录临时文件再替换目标文件，中断时不会破坏上一份成功导出；导出路径不能与 SQLite 数据库路径相同。

数据库不存在或结构版本与当前程序不一致时，命令会返回退出码 `1`，不会创建空数据库或空 JSON。导出文件可能包含有效网盘分享地址，`output/` 已被 Git 忽略，请勿将文件提交到仓库或通过不安全渠道发送。

## Windows 定时任务

当前电脑已创建计划任务 `XDGAME 每日增量更新`，每天 `23:00` 执行：

```text
E:\Python\Python314\python.exe "E:\Code\MyProjects\scrapers\xdgame\scraper.py" daily
```

任务工作目录为 `E:\Code\MyProjects\scrapers\xdgame`，使用当前 Windows 用户的交互式登录令牌，不保存账号密码。任务设置包括：

- 仅在用户保持登录时运行；锁屏和关闭显示器不影响执行，注销或关机后不能运行。
- 允许唤醒电脑运行，并在错过计划时间后尽快补跑；实际唤醒能力取决于系统电源计划和硬件设置。
- 失败后每 15 分钟重试一次，最多重试 3 次。
- 单次最长运行 4 小时；已有实例运行时不启动重复实例。
- 允许使用电池启动，切换到电池供电后不强制停止。

查看任务及下次运行时间：

```powershell
Get-ScheduledTask -TaskName "XDGAME 每日增量更新"
Get-ScheduledTaskInfo -TaskName "XDGAME 每日增量更新"
```

手动触发、禁用和重新启用：

```powershell
Start-ScheduledTask -TaskName "XDGAME 每日增量更新"
Disable-ScheduledTask -TaskName "XDGAME 每日增量更新"
Enable-ScheduledTask -TaskName "XDGAME 每日增量更新"
```

不再需要时删除任务，执行前 PowerShell 会要求确认：

```powershell
Unregister-ScheduledTask -TaskName "XDGAME 每日增量更新" -Confirm
```

计划任务参数不包含站点账号、密码、Cookie 或登录状态内容。当前阶段未手动触发任务；首次实际运行结果可通过 `Get-ScheduledTaskInfo`、数据库 `crawl_runs` 和 `logs\xdgame.log` 检查。

### 迁移到另一台电脑

Windows 计划任务、SQLite 数据库和登录状态不会随 Git 同步。新电脑需要分别部署代码、初始化本地运行数据并重新创建计划任务。

获取代码并进入项目目录：

```powershell
git clone https://github.com/developer-aYou1002/scrapers.git
cd .\scrapers\xdgame
```

建议为新电脑创建独立虚拟环境，再按前文“安装”章节安装依赖：

```powershell
py -3.14 -m venv .venv
.\.venv\Scripts\Activate.ps1
```

按照“首次登录”章节在新电脑重新生成 `login_state.json`，不要通过 Git 或不安全渠道传输旧电脑的 Cookie。数据库可以选择重新执行 `full-list` 创建，也可以在停止旧电脑任务和抓取进程后，将 `data\xdgame.db` 一次性安全复制到新电脑；禁止两台电脑同时写同一个网络共享数据库。

完成前文人工验证后，在新电脑的项目目录运行以下 PowerShell。命令会根据当前位置、虚拟环境和当前 Windows 账户生成绝对路径，并创建与本机相同策略的每日任务：

```powershell
$taskName = "XDGAME 每日增量更新"
$projectDir = (Resolve-Path .).Path
$pythonPath = (Resolve-Path ".\.venv\Scripts\python.exe").Path
$scriptPath = (Resolve-Path ".\scraper.py").Path
$userId = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name

if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
    throw "计划任务已存在，未覆盖：$taskName"
}

$action = New-ScheduledTaskAction `
    -Execute $pythonPath `
    -Argument ('"' + $scriptPath + '" daily') `
    -WorkingDirectory $projectDir
$trigger = New-ScheduledTaskTrigger -Daily -At "23:00"
$principal = New-ScheduledTaskPrincipal `
    -UserId $userId `
    -LogonType Interactive `
    -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -WakeToRun `
    -ExecutionTimeLimit (New-TimeSpan -Hours 4) `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 15) `
    -MultipleInstances IgnoreNew `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries
$task = New-ScheduledTask `
    -Action $action `
    -Trigger $trigger `
    -Principal $principal `
    -Settings $settings `
    -Description "每日增量抓取 XDGAME 数据；仅在用户登录会话中运行。"

Register-ScheduledTask -TaskName $taskName -InputObject $task
```

创建后使用本节前面的查看命令核对任务。如果这是永久迁移，确认新电脑首次运行正常后，再使用前面的禁用命令停用旧电脑任务，避免两台机器重复抓取。

## 运行日志与退出码

所有命令都会同时输出控制台日志和轮转文件日志。默认日志文件为 `logs\xdgame.log`，单个文件达到 5 MiB 后自动轮转，最多保留 5 份历史文件，例如 `xdgame.log.1`。`logs/` 已被 Git 忽略，日志不会提交到仓库。

每次启动会生成 12 位运行标识，日志中的同一次执行都带有相同的 `[run=...]`。日志会记录命令、处理阶段、页数、记录数、重试状态、最终退出码和耗时。账号、密码、Cookie、授权令牌及当前环境变量中的登录值会在写入控制台和文件前统一脱敏。

查看最近日志：

```powershell
Get-Content .\logs\xdgame.log -Tail 100
```

持续观察计划任务日志：

```powershell
Get-Content .\logs\xdgame.log -Wait -Tail 50
```

需要将日志写到其他位置时使用绝对路径：

```powershell
python scraper.py daily --log-file D:\XDGAME-Logs\xdgame.log
```

退出码含义：

- `0`：任务正常完成。
- `1`：普通运行失败，或日常列表达到页数上限后仍未安全收敛。
- `2`：命令行参数无效，由 `argparse` 返回。
- `3`：登录状态失效；执行 `python scraper.py login` 重新登录后，再运行原命令。
- `4`：网站今日游戏下载额度已用完；当前游戏不增加失败次数，等待额度恢复后再运行。
- `130`：用户通过键盘中断任务。

登录失效仅通过日志和退出码反馈，不发送 Windows 通知或 Webhook。Windows 计划任务会把任何非零退出码视为失败并按已配置策略重试；排查时应先按日志中的运行标识查找同一次执行的错误摘要。

## SQLite 数据库

抓取时会自动初始化并更新 `data/xdgame.db`，不需要手动建库。数据库结构使用 `PRAGMA user_version` 管理版本，重复运行不会重复建表或清空已有数据。

当前数据库结构版本为 6，包含以下数据：

- `games`：游戏、详情地址、分类、列表元数据、夸克地址、解析状态、尝试次数、最后成功复查时间及发现时间。
- `crawl_runs`：每次任务的开始、结束、状态、处理数量和错误摘要。
- `crawl_checkpoints`：保存全量列表的分页断点位置。
- `crawl_locks`：防止相同全量列表或详情补全任务并发写入，并支持超时接管异常遗留锁。
- `game_disk_links`：其他网盘类型的解析结果。
- `game_rankings`：最近一次完整解析的人气榜快照，不改变游戏原有入库顺序。
- `ranking_daily_visits`：人气榜补全在北京时间当日访问过的游戏，用于保守额度限制。

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

预期数据库版本为 `6`，并且下面的命令返回 `True`：

```powershell
Test-Path .\data\verify.db
```

### 快速验证前两页列表

```powershell
python scraper.py full-list --database data\verify.db --max-pages 2
$LASTEXITCODE
```

因为网站在第二页之后仍有下一页，程序应在成功写入两页后提示达到安全页数上限，退出码应为 `1`，本次运行状态应记录为 `incomplete`，checkpoint 应保存第三页地址。这是预期的安全上限行为，不代表前两页写入失败。

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

print("全量列表断点：")
for row in db.execute("""
    SELECT task_key, cursor, last_game_url, updated_at
    FROM crawl_checkpoints
    ORDER BY updated_at DESC
"""):
    print(row)

print("当前任务锁：", db.execute(
    "SELECT COUNT(*) FROM crawl_locks"
).fetchone()[0])

print("最近写入的游戏：")
for row in db.execute("""
    SELECT name, category, score, size_text, source_updated_date,
           detail_status, detail_attempts, quark_checked_at
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

- 数据库版本为 `6`。
- 数据表包含 `games`、`crawl_checkpoints`、`crawl_runs`、`crawl_locks`、`game_disk_links`、`game_rankings`、`ranking_daily_visits`。
- 游戏总数大于 `0`，并与唯一详情地址数相同。
- 游戏记录包含分类、评分、容量和站点日期等列表元数据。
- 新入库且尚未处理的游戏符合五项详情初始状态。
- 两页验证对应的最新运行记录为 `full-list`、`incomplete`。
- checkpoint 的 `cursor` 指向下一页，命令退出后任务锁数量为 `0`。

### 验证断点续跑

再次执行相同命令：

```powershell
python scraper.py full-list --database data\verify.db --max-pages 2
```

本次应从原 checkpoint 指向的第三页继续，再处理两页并把 checkpoint 向后推进。重新运行上一节的数据库检查命令，“游戏总数”和“唯一详情地址数”必须保持相同。

### 验证从头重新扫描

```powershell
python scraper.py full-list --database data\verify.db --max-pages 2 --restart
```

该命令会先删除当前列表入口的 checkpoint，再从第一页抓取。游戏仍通过 UPSERT 保存，不应产生重复详情地址。

### 验证详情分批补全

先保存有效登录状态，再使用较小批次验证；显示浏览器便于人工观察登录与入口状态：

```powershell
python scraper.py login
python scraper.py enrich --database data\verify.db --batch-size 2 --show-browser
$LASTEXITCODE
```

检查最近两条详情状态：

```powershell
@'
import sqlite3

db = sqlite3.connect("data/verify.db")
for row in db.execute("""
    SELECT name, quark_url, quark_status, detail_status,
           detail_attempts, last_error, quark_checked_at
    FROM games
    WHERE detail_updated_at IS NOT NULL
    ORDER BY detail_updated_at DESC
    LIMIT 2
"""):
    print(row)
db.close()
'@ | python -
```

预期单次最多处理 2 条，成功记录为 `complete`、尝试次数归零且写入 `quark_checked_at`。临时失败会累计尝试次数；重新执行命令只选择剩余待处理或允许重试的记录。登录失效时退出码为 `3`，已处理记录仍保留。

### 验证日常增量更新

使用已完成列表入库的验证数据库，以较小参数观察提前停止：

```powershell
python scraper.py daily --database data\verify.db --max-pages 5 --existing-page-limit 2 --batch-size 2 --show-browser
$LASTEXITCODE
```

如果连续 2 页没有新增，应提前停止且退出码为 `0`。随后检查最近运行记录：

```powershell
@'
import sqlite3

db = sqlite3.connect("data/verify.db")
print(db.execute("""
    SELECT run_type, status, processed_count, error_summary
    FROM crawl_runs
    WHERE run_type = 'daily'
    ORDER BY id DESC
    LIMIT 1
""").fetchone())
db.close()
'@ | python -
```

在仍持续出现新增数据时将 `--max-pages` 设为 `1`，预期退出码为 `1`，最新运行状态为 `incomplete`，已经扫描的列表记录不会回滚。

### 验证数据库导出

从验证数据库导出到独立文件：

```powershell
python scraper.py export --database data\verify.db --output output\verify-games.json
$LASTEXITCODE
```

检查导出摘要、字段和记录数：

```powershell
$export = Get-Content .\output\verify-games.json -Raw | ConvertFrom-Json
$export | Select-Object source, source_database, exported_at, count, complete_count, quark_count
$export.games | Select-Object -First 1
```

预期退出码为 `0`，`count` 与验证数据库的 `games` 表数量一致，单条记录包含列表元数据、详情状态、夸克地址、错误和时间字段。`git status --short` 不应出现导出文件。

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

分批补全详情和夸克地址：

```powershell
python scraper.py enrich
```

执行日常增量更新：

```powershell
python scraper.py daily
```

导出当前数据库：

```powershell
python scraper.py export
```

查看正式数据库的最近运行记录时，把上述验证命令中的 `data/verify.db` 替换为 `data/xdgame.db`。

当前全量列表有数百页，完整运行可能需要几十分钟。任务中断后重新执行相同命令即可从最近成功页面继续；如需明确从头扫描，再增加 `--restart`。

请控制抓取频率，遵守目标网站规则，并仅访问本人有权获取的内容。

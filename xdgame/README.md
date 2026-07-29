# XDGAME 今日更新爬虫

抓取 `https://www.xdgame.com/` 首页“今日更新”分类中的游戏名和详情页链接，并保存为 JSON。

## 安装

请先安装 Python 3.10 或更高版本，然后在当前目录执行：

```powershell
python -m pip install -r requirements.txt
```

## 运行

```powershell
python scraper.py
```

结果默认保存到 `today_games.json`，同时会在终端列出游戏名。

指定输出文件：

```powershell
python scraper.py --output data\today.json
```

输出示例：

```json
{
  "source": "https://www.xdgame.com/",
  "fetched_at": "2026-07-28T17:40:00+08:00",
  "count": 1,
  "games": [
    {
      "name": "示例游戏/Example Game",
      "url": "https://www.xdgame.com/game/1.html"
    }
  ]
}
```

请控制运行频率，并遵守目标网站的使用规则。

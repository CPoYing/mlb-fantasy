# MLB Fantasy Dashboard

本地 web app，串接 Yahoo Fantasy Sports API + MLB Stats API，輔助 MLB Fantasy Baseball 5×5 H2H 決策。

## 啟動

```bash
cd /path/to/mlb-fantasy
lsof -ti:5001 | xargs kill -9 2>/dev/null
venv/bin/python app.py &
```

打開 `https://localhost:5001`（自簽 SSL，瀏覽器點「進階 → 仍要前往」）。

## 聯盟

- 名稱：菜政宜的秘密花園
- league_key：`469.l.203968`
- 賽季：2026（開季：2026-04-13）
- 隊數：14 · H2H 5×5

## Yahoo OAuth

- Redirect URI：`https://localhost:5001/callback`
- 金鑰存在 `.env`（勿上傳）

## 必要環境變數（`.env`）

```
SECRET_KEY=<隨便一串夠長的隨機字>       # 必填，沒填會 raise
YAHOO_CLIENT_ID=...
YAHOO_CLIENT_SECRET=...
YAHOO_REDIRECT_URI=https://localhost:5001/callback
EMAIL_SENDER=你的gmail                  # 選填（不填則不發週報）
EMAIL_PASSWORD=gmail應用程式密碼
EMAIL_RECEIVER=收件人
```

Gmail 要開「兩步驟驗證」後產「應用程式密碼」（不是登入密碼）。

## 模組

| 檔案 | 功能 |
|---|---|
| `app.py` | Flask 路由 + APScheduler 週報排程 |
| `yahoo_api.py` | Yahoo Fantasy API 封裝（leagues / teams / roster / matchup / FA） |
| `mlb_stats.py` | MLB Stats API：2026 整季成績、近 7 日 hot、位置 mapping（已移除 2025 歷史數據） |
| `player_values.py` | 5×5 z-score 計算（dashboard / rankings / waiver 共用） |
| `mlb_schedule.py` | 各隊週出賽場次 heatmap 資料 |
| `email_report.py` | 每週一 08:00 自動寄 Email 週報 |

## 頁面

| 路由 | 功能 |
|---|---|
| `/dashboard` | 我的隊伍 5×5 z-score、近 7 日 hot 球員、聯盟入口 |
| `/matchup/<league_key>` | 本週計分板 + H2H 對手陣容深度比較 + 5×5 各項 z 加總 |
| `/rankings/<league_key>` | 全聯盟球員 5×5 z 排名（JS 表格，可排序、依位置篩選） |
| `/waiver/<league_key>` | 自由球員推薦，依「upgrade z 分（替換我隊最弱者後的提升）」排序 |
| `/schedule/<league_key>` | 各 MLB 隊週出賽場次 heatmap，搭配 Waiver 找場次多的球員 |

## 設計

- **主題**：復古棒球卡（Topps）風 — cream + navy + Topps 紅 + 復古金
- **字型**：Special Elite（display）+ JetBrains Mono（數據）+ system sans（本文）
- **深淺切換**：右上角 toggle，存到 `localStorage.mlb-theme`
- **RWD**：手機優先，斷點 640px / 800px
- **CSS**：`static/css/main.css` 用 CSS 變數，dark mode 透過 `[data-theme="dark"]` 覆寫

## 重要技術細節

- Yahoo API `players` endpoint 不支援 `type=lastseason`、`status=T`，整季成績改用 MLB Stats API 補（目前僅使用當季 2026 數據）
- Yahoo player info 是 list of single-key dicts，用 `parse_player_info()` 合併
- R 在 Yahoo API 回傳 `"3/5"`，要取 `/` 前數字
- MLB Stats API 用球員全名匹配（含去除音調 normalize）
- SSL：自簽憑證 `cert.pem` / `key.pem`
- Port 5000 被 macOS AirPlay 佔用，改用 5001

## 已移除（vs 舊版）

- `/draft`、`/api/draft`、`draft_engine.py` 整個選秀模組（只保留 z-score 計算到 `player_values.py`）
- `/h2h`（合併進 `/matchup`）
- `/league-overview`（內容沒被新版採用，rankings 已涵蓋）
- `/team`、`/players`、`/trades`（功能簡化）
- `/debug/*`（公開 repo 不適合留 raw API dump）

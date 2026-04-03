# MLB Fantasy Dashboard

本地 web app，串接 Yahoo Fantasy Sports API + MLB Stats API，用來輔助 MLB Fantasy Baseball 決策。

## 專案位置

```
/Users/user/mlb-fantasy/
```

## 啟動方式

```bash
cd /Users/user/mlb-fantasy
lsof -ti:5001 | xargs kill -9 2>/dev/null   # 清除舊 process
venv/bin/python app.py &                     # 啟動
```

開啟瀏覽器：`https://localhost:5001`
瀏覽器會警告 SSL 不安全（自簽憑證），點「進階 → 仍要前往」即可。

## 聯盟資訊

- 聯盟名稱：菜政宜的秘密花園
- league_key：`469.l.203968`
- 賽季：2026（開季日：2026-04-13）
- 隊數：14 隊，H2H 計分制
- 狀態：預選秀中（predraft）

## Yahoo OAuth

- App ID：`Oh3bd5d6`
- Redirect URI：`https://localhost:5001/callback`
- 金鑰存在 `.env`（勿上傳）

## 技術架構

| 檔案 | 功能 |
|---|---|
| `app.py` | Flask 主程式，所有路由，APScheduler 週報排程 |
| `yahoo_api.py` | Yahoo Fantasy API 呼叫、球員解析、強弱分析 |
| `mlb_stats.py` | MLB Stats API，抓 2025 整季成績，用球員姓名匹配，有 cache |
| `mlb_schedule.py` | MLB 週賽程，從 statsapi.mlb.com 抓，按週分組 |
| `email_report.py` | 每週一 08:00 自動寄 Email 週報（需設定 .env） |

## 目前功能頁面

| 路由 | 功能 |
|---|---|
| `/dashboard` | 列出所有 MLB Fantasy 聯盟 |
| `/team/<league_key>` | 我的隊伍名單 + 本週成績 |
| `/matchup/<league_key>` | 本週 H2H 對戰分數 |
| `/players/<league_key>` | 全聯盟球員總覽，2025 整季成績，可依位置/成績排序，含優缺點分析 |
| `/schedule/<league_key>` | MLB 各隊週出賽場數 heatmap，H2H 必備 |
| `/h2h/<league_key>` | 選擇對手，左右對比雙方名單優缺點，補強建議 |
| `/free-agents/<league_key>` | 自由球員推薦，可依位置篩選 |
| `/trades/<league_key>` | 交易建議（根據各隊陣容分析） |
| `/debug/players/<league_key>` | 除錯用，看 Yahoo API 原始 JSON |

## 重要技術細節

- Yahoo API `players` endpoint 不支援 `type=lastseason` 或 `status=T`，成績改用 MLB Stats API 補
- Yahoo player info 是 list of single-key dicts，用 `parse_player_info()` 合併，不要用 index 存取
- R（得分）在 Yahoo API 回傳格式為 `"3/5"`（需取 `/` 前的數字）
- MLB Stats API 用球員全名匹配（含去除音調的 normalize）
- SSL：自簽憑證 `cert.pem` / `key.pem`，Flask 直接載入
- Port 5000 被 macOS AirPlay 佔用，改用 5001

## Email 週報設定（尚未完成）

在 `.env` 填入：
```
EMAIL_SENDER=你的gmail
EMAIL_PASSWORD=gmail應用程式密碼（非登入密碼）
EMAIL_RECEIVER=收件人
```

Gmail 需開啟「兩步驟驗證」後產生「應用程式密碼」。

## 待辦 / 可擴充方向

- [ ] 選秀完成後測試 H2H 分析、交易建議實際效果
- [ ] 加入球員傷兵狀態顯示
- [ ] Email 週報完整測試
- [ ] 考慮上傳 GitHub 備份（`.env` 加入 `.gitignore`）

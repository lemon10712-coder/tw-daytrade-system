# 台股當沖選股系統（自動化版）

這是「股票系統」Claude 專案裡設計、實作、測試過的分析引擎，接上真實資料、透過 GitHub Actions
每個交易日自動執行。跟另一套 `daily-trading-report` repo 是分開維護的兩套系統。

## 運作方式

1. `.github/workflows/daily-report.yml` 在台北時間每個交易日 08:15 自動觸發（也可以在 GitHub 網頁的
   Actions 分頁手動點「Run workflow」立即測試）。
2. 跑單元測試（`pytest tests/`）——**測試沒過就不會產生今天的報告**，避免帶著已知的錯誤產生報告。
3. `scripts/fetch_and_report.py` 打證交所 (TWSE) / 櫃買中心 (TPEx) OpenAPI 抓當天全市場資料，存進
   `data/daily/YYYY-MM-DD.json`（逐日累積，用來組出技術指標需要的歷史序列，見
   `src/stockSystem/real_providers.py` 檔案開頭的完整說明）。
4. 跑族群強度分析、個股篩選、資金配置、進出場價位計算，產生 `reports/YYYY-MM-DD.md`。
5. 用 GitHub Actions 內建的 `GITHUB_TOKEN` 把 `data/`、`reports/` 的變更 commit + push 回這個 repo
   （**不需要另外設定 Personal Access Token**，比手動配置 PAT 更安全，權限也只侷限在這個 repo）。

## 目錄結構

- `src/stockSystem/`：核心分析邏輯（族群強度、個股篩選、資金配置、進出場價位、回測驗證閘門），
  全部是純計算、不碰網路，每個模組都有對應的單元測試。
- `src/stockSystem/real_providers.py`：真實資料抓取（TWSE/TPEx OpenAPI + Yahoo Finance 國際指數），
  **這是唯一需要對外連線的部分**，也是最需要在第一次真正執行時人工核對欄位名稱是否正確的地方——
  檔案開頭有完整說明。
- `scripts/fetch_and_report.py`：GitHub Actions 執行的進入點。
- `scripts/run_daily_report.py`：本地開發用，只吃合成測試資料（`FixtureProvider`），不會對外連線，
  報告標題會強制顯示「測試模式」，不會被誤當成正式報告。
- `tests/`：單元測試，`pytest tests/` 執行。

## 第一次執行前必讀

`real_providers.py` 裡打 TWSE/TPEx API 的欄位名稱，是根據官方 OpenAPI 文件寫的，**沒有機會在
Claude Cowork 的雲端沙盒裡對著真實回應驗證過**（那個環境的網路被組織政策擋死）。所以：

1. 第一次執行請用 GitHub 網頁 Actions 分頁的「Run workflow」手動觸發，不要只等排程，這樣出問題
   當天就能發現、不用等到隔天。
2. 如果失敗，去看那次執行的 log——程式設計成「欄位對不上就直接清楚報錯，並把目前實際拿到的欄位
   全部列出來」，不會是一段看不懂的通用錯誤，照著錯誤訊息把 `real_providers.py` 裡對應的欄位名稱
   改掉即可，不需要用猜的。
3. 已知還沒串接的部分（會誠實顯示在報告的「資料品質檢查」區塊，不會被隱藏）：
   - 全額交割股清單
   - 先賣後買（放空）資格的官方清單（目前用保守近似）
   - 注意股/處置股清單的端點路徑還沒驗證過
   下單前這幾項請自行到證交所網站或券商系統核對。

## 資料只會愈用愈準

因為技術指標需要多天歷史，而 TWSE/TPEx 沒有「一次拿全部股票多天歷史」的端點，這裡採用「每天存一份
快照、逐日累積」的做法——剛上線的頭幾週歷史還不夠長，均線/ATR 這些數字會比較不精確，這是預期中的
暫時現象，不是bug，累積到 30 個交易日之後就會穩定。

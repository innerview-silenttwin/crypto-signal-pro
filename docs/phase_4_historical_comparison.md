# Phase 4 歷史對照與回測系統架構 (Historical Comparison System)

## 選項 A 與 B 雙軌並行 (Dual-Track Approach)
為了豐富四面向的縱深視角，我們將同時導入「財報技術純對比法」與「歷史分數高低水位」系統。

### Track 1: 面向過去的歷史財政/技術快照 (Option A)
只針對完全可追溯的 `Fundamental` 與 `Technical` 兩種指標進行「去年今日」或「歷年區間」的回朔。
- **作法 (基本面)**：
  - 放棄依賴目前僅有 YTD 或 TTM 的 TWSE API。
  - 新增串接 `yfinance.Ticker().financials` 獲取個股長達 3~5 年的「年度/季度 EPS」、「淨利潤率」變化。
  - 在卡片 UI 上新增『折線圖的微縮縮圖 (Sparkline)』，一眼看穿近三年的獲利軌跡是上升還是衰退。
- **作法 (技術面)**：
  - 由於 K 線已有 1~5 年長度，加入一支函數抓取 `df[-252]` (約去年這一天) 的均線多空狀態。
  - 顯示：*「當前技術動能 85 分 vs 去年同期 45 分」*。

### Track 2: 面向未來的自家分數庫累積 (Option B)
由於無法追溯過去的法人籌碼與財經新聞，我們建立自己的本機資料庫。
- **資料庫架構 (SQLite)**：
  - 表格：`daily_scores`
  - 欄位：`date | symbol | fundamental | chipflow | technical | sentiment | composite`
- **運行方式**：
  - 每天自動執行（可以搭配 cron job 或一啟動就掃描一次全市場），將所有計算完的 4 面向分數寫入資料庫。
  - 在單檔分析或選股 UI 顯示「近一年最高/最低標尺」。
  - 例：`[=====|===*=======]` 星號代表目前的籌碼面分數正處於今年最火熱的區間。

## 開發順序建議 (Roadmap)
1. 先建置 **SQLite Daily Scores DB (Track 2)**：馬上打下基礎，因為歷史是「越晚開門，失去越多」。這部分幾乎不佔系統資源，每天只塞輕量的數字。
2. 擴充 **Fundamental Layer (Track 1)**：在前端的「基本面」模塊內加入「歷年 EPS 柱狀微縮圖」，讓財報品質一覽無遺。

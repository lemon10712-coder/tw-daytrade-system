"""微台指模組的資料層：抓取並逐日累積台股加權指數(TAIEX)的日線 OHLC。

**2026-09-24 第一次在 GitHub Actions 真實執行後已修正（原本的端點猜測是錯的）**：
最初猜測的 `www.twse.com.tw/rwd/zh/afterTrading/MI_5MINS_HIST?date=YYYYMMDD` 整個路徑是錯的
（回應 404 頁面，導致 JSONDecodeError），透過瀏覽器打開 TWSE 官網「發行量加權股價指數歷史資料」
頁面（https://www.twse.com.tw/zh/indices/taiex/mi-5min-hist.html）並攔截它實際呼叫的 API，
找到真正的端點：`www.twse.com.tw/rwd/zh/TAIEX/MI_5MINS_HIST?date=YYYYMMDD&response=json`
（差別：路徑是 `TAIEX/`，不是 `afterTrading/`）。已對照真實回應驗證過，行為如下：
- `date` 參數用西元年 `YYYYMMDD`（月份/日期哪一天不重要，只要落在目標月份內即可），
  **回傳的是「整個月」的資料**，不是單日一筆——例如 `date=20260801` 會回傳 115年08月
  全部交易日的資料，`title` 欄位會是「115年08月 發行量加權股價指數歷史資料」。
- 回應格式：`{"stat":"OK","title":"...","date":"...","fields":["日期","開盤指數","最高指數",
  "最低指數","收盤指數"],"data":[["115/08/03","42,780.42","43,784.19","42,780.42","43,386.41"],
  ...],"total":...}`，`日期` 欄位是**民國年**格式（`115/08/03`），數字欄位帶千分位逗號
  （例如 `"42,780.42"`），這裡的 `_to_float` 已經會處理逗號。
- 呼叫端要自己從整個月的 `data` 陣列裡，用民國年格式比對出目標日期那一列，不能只取最後一列
  （最後一列是「這個月目前為止最新一天」，不是「呼叫時要的那一天」，兩者只有在查當月最新
  交易日時才會一樣）。

跟 real_providers.py 開頭聲明同樣的可信度原則：欄位比對用關鍵字、找不到就丟出清楚的
DataValidationError、把實際欄位列出來，避免之後這個端點格式又變動時悄悄用錯的欄位算出
看起來正常但其實是錯的結果。

設計上刻意跟股票系統的 `DailySnapshotStore` 分開存放（`data/index_daily/` 而不是塞進
`data/daily/`），因為兩者的資料形狀完全不同（一個是全市場千檔股票，一個是單一指數的
一天一筆 OHLC），混在一起沒有好處，分開也方便之後如果指數資料源要換掉不會動到股票那邊。
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from pathlib import Path

from stockSystem.data_sources import DataValidationError

logger = logging.getLogger("stockSystem.futures_data")

TWSE_LEGACY_BASE = "https://www.twse.com.tw/rwd/zh"

_BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def _find_key(row: dict, *must_contain: str) -> str:
    for k in row.keys():
        if all(token in k for token in must_contain):
            return k
    raise DataValidationError(
        f"找不到符合條件 {must_contain} 的欄位。目前這筆資料實際的欄位有：{sorted(row.keys())}。"
    )


def _to_float(value) -> float:
    if value is None:
        return float("nan")
    try:
        s = str(value).strip().replace(",", "")
        if s in ("", "--", "X", "N/A"):
            return float("nan")
        return float(s)
    except (TypeError, ValueError):
        return float("nan")


def _roc_date_str(as_of: dt.date) -> str:
    """西元年日期轉成 TWSE 這個端點用的民國年字串，例如 2026-08-03 -> "115/08/03"。"""
    return f"{as_of.year - 1911}/{as_of.month:02d}/{as_of.day:02d}"


def _fetch_twse_index_ohlc(session, as_of: dt.date) -> dict | None:
    """抓「指定某一天」台股加權指數(TAIEX)的日線 OHLC。

    用的是 TWSE 官網「發行量加權股價指數歷史資料」頁面實際呼叫的端點：
    `www.twse.com.tw/rwd/zh/TAIEX/MI_5MINS_HIST?date=YYYYMMDD&response=json`
    （2026-09-24 已對照真實回應驗證過，見檔案開頭聲明）。這個端點一次回傳「整個月」的
    資料，不是單日一筆，所以這裡要從回傳的月資料裡，用民國年格式比對出目標日期那一列。

    非交易日/假日、或該月資料裡找不到目標日期那一列，回傳 None（呼叫端當成「這天沒有
    資料」處理，不是錯誤——例如週末、國定假日本來就不會有交易資料）。
    """
    date_str = as_of.strftime("%Y%m%d")
    resp = session.get(
        f"{TWSE_LEGACY_BASE}/TAIEX/MI_5MINS_HIST?date={date_str}&response=json",
        timeout=30,
        headers={"Accept": "application/json", "User-Agent": _BROWSER_USER_AGENT},
    )
    resp.raise_for_status()
    raw = resp.json()

    stat = raw.get("stat") if isinstance(raw, dict) else None
    if stat is not None and stat != "OK":
        logger.info("台股加權指數日線(TAIEX/MI_5MINS_HIST, %s) 這個月沒有資料（可能是非交易日/假日）: stat=%r", date_str, stat)
        return None

    fields = raw.get("fields") or []
    data_rows = raw.get("data") or []
    if not fields or not data_rows:
        raise DataValidationError(
            f"台股加權指數日線(TAIEX/MI_5MINS_HIST, {date_str}) 回應裡沒有 fields/data，"
            f"實際回應的 top-level key 有：{sorted(raw.keys()) if isinstance(raw, dict) else type(raw)}。"
            "格式可能又變了，需要對照這次錯誤訊息更新 _fetch_twse_index_ohlc。"
        )

    target = _roc_date_str(as_of)
    date_field_idx = None
    for i, f in enumerate(fields):
        if "日期" in f:
            date_field_idx = i
            break
    if date_field_idx is None:
        raise DataValidationError(
            f"台股加權指數日線(TAIEX/MI_5MINS_HIST, {date_str}) 回應的 fields 裡找不到「日期」欄位，"
            f"實際 fields 有：{fields}。"
        )

    matched_row = next((r for r in data_rows if len(r) > date_field_idx and r[date_field_idx] == target), None)
    if matched_row is None:
        logger.info("台股加權指數日線(TAIEX/MI_5MINS_HIST)：%s（民國 %s）不在這個月的資料裡（可能是非交易日）", as_of, target)
        return None

    row = dict(zip(fields, matched_row))
    open_key = _find_key(row, "開盤")
    high_key = _find_key(row, "最高")
    low_key = _find_key(row, "最低")
    close_key = _find_key(row, "收盤")
    return {
        "as_of": as_of.isoformat(),
        "open": _to_float(row[open_key]),
        "high": _to_float(row[high_key]),
        "low": _to_float(row[low_key]),
        "close": _to_float(row[close_key]),
    }


class IndexDailyStore:
    """把台股加權指數每日 OHLC 存成 `<data_dir>/index_daily/YYYY-MM-DD.json`，
    介面故意跟 real_providers.DailySnapshotStore 對稱（save / load / load_recent），
    熟悉那個檔案的人不用重新學一套用法。
    """

    def __init__(self, data_dir: str | Path):
        self.data_dir = Path(data_dir)
        self.index_dir = self.data_dir / "index_daily"
        self.index_dir.mkdir(parents=True, exist_ok=True)

    def save(self, as_of: dt.date, bar: dict) -> Path:
        path = self.index_dir / f"{as_of.isoformat()}.json"
        path.write_text(json.dumps(bar, ensure_ascii=False), encoding="utf-8")
        return path

    def load(self, as_of: dt.date) -> dict | None:
        path = self.index_dir / f"{as_of.isoformat()}.json"
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def load_recent(self, as_of: dt.date, lookback_days: int = 60) -> list[dict]:
        """跟 DailySnapshotStore.load_recent 同樣的邏輯：往前找「有存到檔案」的日期，
        不是往前數日曆天，自然跳過假日/系統沒跑的日子，避免歷史序列裡出現缺洞。
        """
        candidates = sorted(self.index_dir.glob("*.json"))
        cutoff_files = [p for p in candidates if p.stem <= as_of.isoformat()]
        selected = cutoff_files[-lookback_days:]
        return [json.loads(p.read_text(encoding="utf-8")) for p in selected]

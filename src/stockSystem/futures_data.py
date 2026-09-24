"""微台指模組的資料層：抓取並逐日累積台股加權指數(TAIEX)的日線 OHLC。

**跟 real_providers.py 開頭聲明同樣的可信度警語**：這個雲端沙盒本身連不到 TWSE 的端點
（見 KNOWN_ISSUES.md），下面 `_fetch_twse_index_ohlc` 用的端點路徑與欄位名稱是根據 TWSE
公開資料慣例寫的，**沒有機會在這個環境裡對著真實回應驗證過**。用跟 real_providers.py 一致
的防禦性寫法：欄位比對用關鍵字、找不到就丟出清楚的 DataValidationError、把實際欄位列出來，
刻意設計成第一次在 GitHub Actions 真正執行時，如果格式不對會清楚失敗並告訴你差在哪裡，
而不是悄悄用錯的欄位算出一個看起來正常但其實是錯的結果——這個模組是全新的，比 real_providers.py
其他端點多一層不確定性，第一次真實執行後務必核對 Actions 執行紀錄。

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


def _fetch_twse_index_ohlc(session, as_of: dt.date) -> dict | None:
    """抓「指定某一天」台股加權指數(TAIEX)的日線 OHLC。

    用的是 TWSE 舊版「盤後資訊」系統的加權指數歷史行情端點（MI_5MINS_HIST，跟
    real_providers.py 的 MI_INDEX 屬於同一套舊系統，日期查詢方式相同）。預期格式是
    `{"stat": "OK", "fields": [...], "data": [[日期, 開盤指數, 最高指數, 最低指數, 收盤指數], ...]}`，
    一天一筆。如果實際格式不同，會在 GitHub Actions 第一次真實執行時由下面的防禦性解析
    清楚報錯（見檔案開頭聲明）。

    非交易日/假日會回傳 stat 不是 "OK"，這種情況回傳 None（呼叫端當成「今天沒有資料」處理，
    不是錯誤）。
    """
    date_str = as_of.strftime("%Y%m%d")
    resp = session.get(
        f"{TWSE_LEGACY_BASE}/afterTrading/MI_5MINS_HIST?date={date_str}&response=json",
        timeout=30,
        headers={"Accept": "application/json", "User-Agent": _BROWSER_USER_AGENT},
    )
    resp.raise_for_status()
    raw = resp.json()

    stat = raw.get("stat") if isinstance(raw, dict) else None
    if stat is not None and stat != "OK":
        logger.info("台股加權指數日線(MI_5MINS_HIST, %s) 今天沒有資料（可能是非交易日）: stat=%r", date_str, stat)
        return None

    fields = raw.get("fields") or []
    data_rows = raw.get("data") or []
    if not fields or not data_rows:
        raise DataValidationError(
            f"台股加權指數日線(MI_5MINS_HIST, {date_str}) 回應裡沒有 fields/data，"
            f"實際回應的 top-level key 有：{sorted(raw.keys()) if isinstance(raw, dict) else type(raw)}。"
            "格式可能跟這裡假設的不一樣，需要對照這次錯誤訊息更新 _fetch_twse_index_ohlc。"
        )
    # 通常只有當天這一筆，取最後一筆保險（避免端點意外回傳多天）
    row = dict(zip(fields, data_rows[-1]))
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

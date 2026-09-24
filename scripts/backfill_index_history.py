#!/usr/bin/env python3
"""一次性回補台股加權指數(TAIEX)歷史日線 OHLC，給微台指(MXF)當沖模組的均線/ATR 計算用。

2026-09-24 新增。跟 `scripts/backfill_history.py`（股票歷史回補）同一種設計：不重新猜
日期範圍，直接沿用 `data/daily/` 底下已經確認是真實交易日的檔名列表回補（這些日期已經
被股票模組的每日執行證實過是交易日，不需要重新判斷假日/非交易日），對每一天呼叫
`futures_data._fetch_twse_index_ohlc`，已經有的日期自動跳過（冪等設計）。

用法（在 repo 根目錄執行，需要能連外部網路的環境，例如 GitHub Actions）：
    python scripts/backfill_index_history.py
"""

from __future__ import annotations

import datetime as dt
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from stockSystem.futures_data import IndexDailyStore, _fetch_twse_index_ohlc  # noqa: E402
from stockSystem.logging_setup import get_logger, new_run_id  # noqa: E402

DATA_DIR = REPO_ROOT / "data"


def main() -> int:
    run_id = new_run_id()
    log = get_logger("backfill_index_history", run_id)

    daily_dir = DATA_DIR / "daily"
    if not daily_dir.exists():
        log.error("找不到 data/daily/ 目錄，請先確認股票模組已經至少執行過一次（回補依賴這裡已知的交易日清單）")
        return 1

    known_trading_days = sorted(p.stem for p in daily_dir.glob("*.json"))
    if not known_trading_days:
        log.error("data/daily/ 底下沒有任何已知交易日檔案，無法回補")
        return 1

    log.info("準備回補 %d 個已知交易日的加權指數OHLC", len(known_trading_days))

    import requests

    session = requests.Session()
    store = IndexDailyStore(DATA_DIR)

    fetched, skipped, failed = 0, 0, 0
    for date_str in known_trading_days:
        as_of = dt.date.fromisoformat(date_str)
        if store.load(as_of) is not None:
            skipped += 1
            continue
        try:
            bar = _fetch_twse_index_ohlc(session, as_of)
        except Exception as exc:  # noqa: BLE001
            log.warning("回補 %s 失敗: %r", date_str, exc)
            failed += 1
            continue
        if bar is None:
            log.info("%s 沒有加權指數資料（可能實際上不是交易日），略過", date_str)
            continue
        store.save(as_of, bar)
        fetched += 1
        time.sleep(0.3)  # 避免短時間內連續打太多請求

    log.info("回補完成：新抓取 %d 天、已存在跳過 %d 天、失敗 %d 天", fetched, skipped, failed)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

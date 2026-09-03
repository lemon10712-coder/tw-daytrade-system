#!/usr/bin/env python3
"""一次性回補歷史每日快照，讓系統不用「每天累積一天」慢慢等三四週才有足夠資料算指標。

## 為什麼可以回補（跟原本設計文件的假設不一樣）

`real_providers.py` 檔案開頭原本的設計說明寫「TWSE/TPEx 沒有『一次拿全部股票近30日歷史』的
端點，要拿歷史只能一檔一檔查，對上千檔股票不可行」——這句話對「一次拿全部歷史」是對的，
但漏了一種端點：TWSE/TPEx 的舊版系統其實有「指定某一天，拿當天全市場資料」的端點
（`_fetch_twse_day_all` / `_fetch_tpex_day_all`，跟三大法人 T86 用的是同一套舊系統）。
用這個端點**逐日**往前查（30 天大概就是 30 次 HTTP 請求，不是一檔一檔查的量級），
一樣可以把 `data/daily/YYYY-MM-DD.json` 補齊，之後 `technicals.py` 的均線(MA20)、
ATR(14)、量能擴張比(基準期20天) 等需要多天序列的指標就有資料可以算，不用真的等好幾週。

## 用法

在 repo 根目錄執行（GitHub Actions 或任何有對外網路的環境；這個 Claude Cowork 沙盒連不到
外部網路，見 KNOWN_ISSUES.md，沒辦法在這裡直接跑）：

    python scripts/backfill_history.py --target-days 30

只會補「`data/daily/` 底下目前還沒有檔案」的日期，已經存在的日期會直接跳過——冪等設計，
可以放心重複執行（例如第一次因為某些天 TPEx 抓失敗而中斷，修好後重跑不會重複浪費額度重抓
已經成功的日期）。
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from stockSystem.logging_setup import get_logger, new_run_id  # noqa: E402
from stockSystem.real_providers import (  # noqa: E402
    TWSE_BASE,
    DailySnapshotStore,
    _fetch_tpex_day_all,
    _fetch_twse_day_all,
    _get_json,
)

DATA_DIR = REPO_ROOT / "data"
TAIPEI = ZoneInfo("Asia/Taipei")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-days", type=int, default=30, help="要補到有幾個交易日的資料（預設 30）")
    parser.add_argument("--max-calendar-days", type=int, default=90, help="最多往前找幾個日曆天，避免遇到長假無限往前找（預設 90）")
    parser.add_argument("--end-date", type=str, default=None, help="從哪一天開始往前補，預設用今天（台北時間）")
    args = parser.parse_args()

    run_id = new_run_id()
    log = get_logger("backfill_history", run_id)

    import requests

    session = requests.Session()
    store = DailySnapshotStore(DATA_DIR)

    end_date = dt.date.fromisoformat(args.end_date) if args.end_date else dt.datetime.now(TAIPEI).date()

    # 產業別對照只抓現在的一份，套用到所有回補的日期——產業分類變動很少，
    # 用現在的對照表回推過去，比完全沒有產業分類好，這個近似值得記錄下來讓使用者知道。
    try:
        twse_industry = _get_json(session, f"{TWSE_BASE}/opendata/t187ap03_L")
    except Exception as exc:  # noqa: BLE001
        log.warning("產業別資料抓取失敗，回補的快照將不含產業分類: %r", exc)
        twse_industry = []

    fetched = 0
    tried = 0
    skipped_existing = 0
    twse_failed_dates: list[str] = []
    tpex_failed_dates: list[str] = []
    d = end_date

    while fetched < args.target_days and tried < args.max_calendar_days:
        if d.weekday() >= 5:  # 週六日不算一次嘗試，直接跳過
            d -= dt.timedelta(days=1)
            continue
        tried += 1

        if store.load(d) is not None:
            log.info("%s 已經有快照檔案，跳過（冪等，不重複抓）", d)
            skipped_existing += 1
            fetched += 1
            d -= dt.timedelta(days=1)
            continue

        try:
            twse_daily = _fetch_twse_day_all(session, d)
        except Exception as exc:  # noqa: BLE001
            log.info("%s 沒有已公布的 TWSE 資料（可能是非交易日/假日），跳過: %r", d, exc)
            twse_failed_dates.append(d.isoformat())
            d -= dt.timedelta(days=1)
            continue

        try:
            tpex_daily = _fetch_tpex_day_all(session, d)
        except Exception as exc:  # noqa: BLE001
            log.warning("%s TPEx 資料抓取失敗，這天快照只會有上市(TWSE)資料: %r", d, exc)
            tpex_failed_dates.append(d.isoformat())
            tpex_daily = []

        snap = {
            "as_of": d.isoformat(),
            "fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "twse_daily": twse_daily,
            "tpex_daily": tpex_daily,
            "twse_industry": twse_industry,
            "twse_institutional": None,
            "twse_margin": None,
            "twse_watch": None,
            "twse_disposition": None,
            "backfilled": True,  # 標記這天是回補的，不是當天排程正常執行產生的
        }
        store.save(d, snap)
        fetched += 1
        log.info("回補完成: %s（已補 %d/%d 個交易日）", d, fetched, args.target_days)
        d -= dt.timedelta(days=1)

    log.info(
        "回補結束，共補了 %d 個交易日（含 %d 個本來就有的），往前找了 %d 個日曆天",
        fetched, skipped_existing, tried,
    )
    if tpex_failed_dates:
        log.warning("以下 %d 天 TPEx 資料抓取失敗（快照只有上市資料，不影響 TWSE 上市股票）: %s",
                    len(tpex_failed_dates), ", ".join(tpex_failed_dates))
    if fetched < args.target_days:
        log.warning("沒有補到目標 %d 個交易日（只補到 %d 天），可能是 --max-calendar-days 不夠、"
                    "或往前遇到長假／端點格式有變，請檢查上面的 log", args.target_days, fetched)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

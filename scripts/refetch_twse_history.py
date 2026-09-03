#!/usr/bin/env python3
"""一次性把已經回補過、但當時 TWSE 抓失敗（twse_daily 是空的）的快照檔案，重新抓一次補進去。

## 背景

`scripts/backfill_history.py` 第一次真正執行時（2026-09-03），MI_INDEX（帶 `type=ALL`）
端點的實際回傳格式跟 `real_providers.py` 原本猜測的不一樣：原本以為是扁平的
`{"stat","fields","data"}`，實際上是 `{"stat","tables":[{...}, ...]}`，資料被拆成好幾個
表格（指數、報酬指數、每日收盤行情…）。因為 `stat` 仍然是 `"OK"`，原本的程式碼沒有拋錯，
只是安靜地回傳空清單——所以那次回補的快照，`twse_daily` 全部是空的，跟 TPEx 那次遇到的
狀況（見 `refetch_tpex_history.py`）是同一種「沒報錯但資料是空的」問題，只是晚一輪才發現
（因為要等 TPEx 修好、重新產生報告，才發現族群強度全部是 nan%，追下去才挖到這個）。

現在 `_fetch_twse_day_all` 已經對照真實回應修好、驗證過了，這支程式只需要把「已經存在、
但 twse_daily 是空的」那些快照，重新抓一次 TWSE 補進去、存回去——**不重新抓 TPEx**
（已經成功的部分，不要浪費額度重抓），也不會動到還沒回補過的日期。

## 用法

    python scripts/refetch_twse_history.py

冪等設計：只處理 `data/daily/*.json` 裡 `twse_daily` 是空陣列的檔案，已經有資料的會跳過，
可以放心重複執行。
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from stockSystem.logging_setup import get_logger, new_run_id  # noqa: E402
from stockSystem.real_providers import DailySnapshotStore, _fetch_twse_day_all  # noqa: E402

DATA_DIR = REPO_ROOT / "data"


def main() -> int:
    run_id = new_run_id()
    log = get_logger("refetch_twse_history", run_id)

    import requests

    session = requests.Session()
    store = DailySnapshotStore(DATA_DIR)

    daily_dir = DATA_DIR / "daily"
    if not daily_dir.exists():
        log.warning("找不到 %s，沒有任何快照可以補", daily_dir)
        return 0

    fixed = 0
    already_ok = 0
    still_failed: list[str] = []

    for path in sorted(daily_dir.glob("*.json")):
        try:
            as_of = dt.date.fromisoformat(path.stem)
        except ValueError:
            continue  # 檔名不是日期格式，跳過（不是這支程式管的檔案）

        snap = store.load(as_of)
        if snap is None:
            continue
        if snap.get("twse_daily"):
            already_ok += 1
            continue  # 已經有 TWSE 資料了，不重抓，冪等

        try:
            twse_daily = _fetch_twse_day_all(session, as_of)
        except Exception as exc:  # noqa: BLE001
            log.warning("%s TWSE 重抓還是失敗: %r", as_of, exc)
            still_failed.append(as_of.isoformat())
            continue

        snap["twse_daily"] = twse_daily
        store.save(as_of, snap)
        fixed += 1
        log.info("補上 %s 的 TWSE 資料（%d 檔股票）", as_of, len(twse_daily))

    log.info(
        "補完，共補上 %d 天的 TWSE 資料（%d 天本來就有，不需要重抓），%d 天還是失敗",
        fixed, already_ok, len(still_failed),
    )
    if still_failed:
        log.warning("這些天 TWSE 還是抓不到，請檢查上面的錯誤訊息: %s", ", ".join(still_failed))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

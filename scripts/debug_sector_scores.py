#!/usr/bin/env python3
"""一次性診斷用腳本：把當天完整的族群強度排名（不是報告裡只顯示的前5強/前5弱）全部印出來，
連同每個族群裡「量能擴張比異常大」的個股明細，方便追查「使用者覺得應該很強的族群為什麼沒被選到」
這種問題——不是報告本身要用的東西，用完可以留著、之後還要查別的日期再改 REPORT_DATE 重跑即可。

用法（在 repo 根目錄，或透過 GitHub Actions）：
    REPORT_DATE=2026-09-07 python scripts/debug_sector_scores.py
"""

from __future__ import annotations

import datetime as dt
import os
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from stockSystem.real_providers import RealTwseProvider  # noqa: E402
from stockSystem.sector_strength import compute_sector_scores, rank_sectors  # noqa: E402
from stockSystem.technicals import volume_expansion_ratio  # noqa: E402

DATA_DIR = REPO_ROOT / "data"
TAIPEI = ZoneInfo("Asia/Taipei")


def main() -> int:
    as_of_str = os.environ.get("REPORT_DATE")
    as_of = dt.date.fromisoformat(as_of_str) if as_of_str else dt.datetime.now(TAIPEI).date()
    print(f"=== 族群強度完整排名診斷，資料日期={as_of} ===\n")

    provider = RealTwseProvider(data_dir=DATA_DIR, lookback_days=60)
    snapshot = provider.get_snapshot(as_of)

    market_return_by_window = {3: 0.0, 5: 0.0, 10: 0.0}
    scores = compute_sector_scores(snapshot.ohlcv, market_return_by_window)
    ranked, _ = rank_sectors(scores)

    print(f"{'排名':<4}{'族群':<12}{'股數':<6}{'相對強度':<12}{'廣度':<8}{'量能擴張':<10}{'綜合分數':<10}")
    for i, s in enumerate(ranked, 1):
        n_stocks = int((snapshot.ohlcv["sector"] == s.sector).sum())
        rs_str = f"{s.relative_strength_pct:+.2%}" if s.relative_strength_pct == s.relative_strength_pct else "nan"
        breadth_str = f"{s.breadth:.0%}" if s.breadth == s.breadth else "nan"
        vol_str = f"{s.volume_expansion:.2f}倍" if s.volume_expansion == s.volume_expansion else "nan"
        print(f"{i:<4}{s.sector:<12}{n_stocks:<6}{rs_str:<12}{breadth_str:<8}{vol_str:<10}{s.composite_score:<10.2f}")

    print("\n=== 特別關注：半導體業／通信網路業／光電業／電子零組件業（使用者問的「記憶體」「光通訊」大概落在這幾個族群） ===\n")
    watch_sectors = ["半導體業", "通信網路業", "光電業", "電子零組件業"]
    for sector in watch_sectors:
        matches = [s for s in ranked if s.sector == sector]
        if not matches:
            print(f"{sector}：這次資料裡沒有這個族群名稱（可能是產業分類沒對到，或今天剛好沒有這個族群的股票）")
            continue
        s = matches[0]
        rank_pos = ranked.index(s) + 1
        n_stocks = int((snapshot.ohlcv["sector"] == s.sector).sum())
        rs_str = f"{s.relative_strength_pct:+.2%}" if s.relative_strength_pct == s.relative_strength_pct else "nan"
        breadth_str = f"{s.breadth:.0%}" if s.breadth == s.breadth else "nan"
        vol_str = f"{s.volume_expansion:.2f}倍" if s.volume_expansion == s.volume_expansion else "nan"
        print(f"{sector}：排名第 {rank_pos}/{len(ranked)}，{n_stocks} 檔股票，相對強度 {rs_str}，廣度 {breadth_str}，"
              f"量能擴張 {vol_str}，綜合分數 {s.composite_score:.2f}")

    print("\n=== 逐股檢查：找出量能擴張比異常大(>=5倍)的個股，通常是最強前幾名族群名次異常的根因 ===\n")
    anomalies = []
    for stock_id, row in snapshot.ohlcv.iterrows():
        vol_hist = row["volume_hist"]
        ratio = volume_expansion_ratio(vol_hist)
        if ratio == ratio and ratio >= 5.0:  # 排除 nan
            recent_avg = float(vol_hist[-5:].mean()) if len(vol_hist) >= 5 else float("nan")
            base_avg = float(vol_hist[-20:-5].mean()) if len(vol_hist) >= 20 else float("nan")
            anomalies.append((ratio, stock_id, row.get("name", stock_id), row["sector"], len(vol_hist), recent_avg, base_avg))
    anomalies.sort(reverse=True)
    if not anomalies:
        print("沒有發現量能擴張比 >= 5倍的個股。")
    for ratio, stock_id, name, sector, hist_len, recent_avg, base_avg in anomalies[:20]:
        print(f"{stock_id}\t{name}\t{sector}\t量能擴張{ratio:.2f}倍\t歷史天數={hist_len}\t"
              f"近5日均量={recent_avg:.1f}張\t基準期(前15日)均量={base_avg:.2f}張")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

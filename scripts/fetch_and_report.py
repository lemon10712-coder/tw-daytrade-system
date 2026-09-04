#!/usr/bin/env python3
"""GitHub Actions 用的每日報告產生器。跟 scripts/run_daily_report.py 的差別：

- `run_daily_report.py` 是給這個 Claude Cowork 雲端沙盒用的，只能用 `--source fixture`
  （沙盒連不到外部網路，見 KNOWN_ISSUES.md）。
- 這個檔案是給 **GitHub Actions**（或任何有正常對外網路的環境）用的，會真的打
  TWSE／TPEx OpenAPI 抓當天的真實資料、存進 data/daily/ 累積歷史、再產生報告。

用法（在 repo 根目錄執行）：
    python scripts/fetch_and_report.py

環境變數：
    REPORT_DATE：可覆寫日期（YYYY-MM-DD），預設用執行當下的日期（UTC+8 台北時間）。
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from stockSystem.backtest import GateResult  # noqa: E402
from stockSystem.data_sources import DataSourceUnavailableError, DataValidationError  # noqa: E402
from stockSystem.entry_exit import compute_entry_exit  # noqa: E402
from stockSystem.logging_setup import get_logger, new_run_id  # noqa: E402
from stockSystem.position_sizing import build_all_combos  # noqa: E402
from stockSystem.real_providers import RealTwseProvider, RealYFinanceIntlProvider  # noqa: E402
from stockSystem.report import render_daily_report, self_check  # noqa: E402
from stockSystem.sector_strength import apply_macro_overlay, compute_sector_scores, rank_sectors  # noqa: E402
from stockSystem.stock_screener import screen_sector  # noqa: E402

DATA_DIR = REPO_ROOT / "data"
REPORTS_DIR = REPO_ROOT / "reports"
TAIPEI = ZoneInfo("Asia/Taipei")


def _entry_exit_for(snapshot, candidates):
    result = {}
    for c in candidates:
        row = snapshot.ohlcv.loc[c.stock_id]
        result[c.stock_id] = compute_entry_exit(
            stock_id=c.stock_id,
            direction=c.direction,
            prev_close=float(row["prev_close"]),
            open_price=float(row["open"]),
            high=float(row["high"]),
            low=float(row["low"]),
            close=float(row["close"]),
            close_hist=row["close_hist"],
        )
    return result


def main() -> int:
    run_id = new_run_id()
    log = get_logger("fetch_and_report", run_id)

    as_of_str = os.environ.get("REPORT_DATE")
    as_of = dt.date.fromisoformat(as_of_str) if as_of_str else dt.datetime.now(TAIPEI).date()
    log.info("開始執行真實資料每日報告，資料日期=%s", as_of)

    provider = RealTwseProvider(data_dir=DATA_DIR, lookback_days=60)
    try:
        snapshot = provider.get_snapshot(as_of)
    except DataSourceUnavailableError as exc:
        log.error("核心資料源無法取得，中止本次執行（不產生報告，避免用不完整資料誤導）：%s", exc)
        return 1
    except DataValidationError as exc:
        log.error("資料驗證失敗，中止本次執行：%s", exc)
        return 1

    # --- 把「哪些非核心端點今天沒抓到」記下來，之後併進報告的品質提示，誠實告知使用者 ---
    today_raw = provider.store.load(as_of) or {}
    endpoint_issues = []
    if today_raw.get("twse_institutional") is None:
        endpoint_issues.append("三大法人買賣超資料今日未能取得，本次分析未納入法人籌碼面")
    if today_raw.get("twse_margin") is None:
        endpoint_issues.append("融資融券資料今日未能取得，本次分析未納入資券面")
    if today_raw.get("twse_watch") is None:
        endpoint_issues.append("注意股票清單今日未能取得，本次篩選可能未排除實際上的注意股，下單前請自行到證交所核對")
    if today_raw.get("twse_disposition") is None:
        endpoint_issues.append("處置股票清單今日未能取得，本次篩選可能未排除實際上的處置股，下單前請自行到證交所核對")
    endpoint_issues.append("全額交割股清單目前尚未串接資料源，本次篩選未排除全額交割股，下單前請自行核對")
    endpoint_issues.append("先賣後買(放空)資格目前用「排除處置股/全額交割股後的剩餘股票」保守近似，未串接官方當沖資格清單，下單前請自行到券商系統核對")

    try:
        intl_snapshot = RealYFinanceIntlProvider().get_intl_snapshot(as_of)
    except Exception as exc:  # noqa: BLE001
        log.warning("國際情勢資料抓取失敗，本次分析不含國際情勢修正: %r", exc)
        intl_snapshot = {}
    snapshot.intl_snapshot = intl_snapshot
    if not intl_snapshot:
        endpoint_issues.append("國際情勢資料（那斯達克/標普/費半等）今日未能取得，本次分析未套用國際情勢修正")

    market_return_by_window = {3: 0.0, 5: 0.0, 10: 0.0}  # 大盤加權指數的多天期報酬率，之後可另外接入取代0
    scores = compute_sector_scores(snapshot.ohlcv, market_return_by_window)
    # 2026-09-03：sector 現在存的是 real_providers.SECTOR_CODE_NAME 解析出來的官方全名
    # （例如「半導體業」），不再是族群代碼，這裡要對齊，不然這個修正條件永遠不會命中。
    scores = apply_macro_overlay(
        scores, snapshot.intl_snapshot, semiconductor_sectors={"半導體業", "通信網路業", "電子零組件業"}
    )
    strongest, weakest = rank_sectors(scores)
    log.info("族群強度排名完成，最強=%s，最弱=%s", strongest[0].sector, weakest[0].sector)

    long_candidates = []
    # 2026-09-04 使用者明確要求：只篩選最強族群的多方候選，最弱族群的放空(空方)篩選先不用——
    # 不是「篩不出來」，是刻意不篩，所以這裡固定給空清單，不呼叫 screen_sector(..., "short")。
    # 之後如果使用者想恢復空方篩選，把下面這段迴圈（仿照多方那段，改成 weakest[:3] / "short"）
    # 加回來即可，不需要動 stock_screener.py 或 report.py 的邏輯。
    short_candidates: list = []
    for s in strongest[:3]:
        cands, excluded = screen_sector(snapshot, s.sector, "long")
        log.info("多方篩選 %s：候選 %d 檔，排除 %d 檔", s.sector, len(cands), len(excluded))
        long_candidates.extend(cands[:3])

    # 回測晉升門檻的真正串接（用 state/rule_registry.json 的歷史紀錄）留待累積夠多真實交易日資料後再接，
    # 目前累積的歷史天數如果還不夠 backtest.py 的 min_sample_size，一律誠實標示 unvalidated，
    # 不假裝已經驗證過。
    gate_results = {
        c.stock_id: GateResult(passed=False, confidence_tier="unvalidated", reasons=["尚在累積真實歷史資料，未達回測驗證門檻"])
        for c in long_candidates + short_candidates
    }

    all_candidates_sorted = sorted(long_candidates + short_candidates, key=lambda c: c.score, reverse=True)
    combos = build_all_combos(all_candidates_sorted)

    long_ee = _entry_exit_for(snapshot, long_candidates)
    short_ee = _entry_exit_for(snapshot, short_candidates)

    issues = self_check(snapshot) + endpoint_issues
    report_md = render_daily_report(
        as_of=as_of,
        snapshot=snapshot,
        strongest_sectors=strongest,
        weakest_sectors=weakest,
        long_candidates=long_candidates,
        short_candidates=short_candidates,
        gate_results=gate_results,
        combos=combos,
        issues=issues,
        long_entry_exit=long_ee,
        short_entry_exit=short_ee,
    )

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = REPORTS_DIR / f"{as_of.isoformat()}.md"
    out_path.write_text(report_md, encoding="utf-8")

    # 額外輸出一份結構化 JSON（方便之後接網頁前端顯示，不是必要品，報告本體以上面的 .md 為準）
    latest_json = {
        "as_of": as_of.isoformat(),
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "is_synthetic": snapshot.is_synthetic(),
        "issues": issues,
        "long_candidates": [
            {
                "stock_id": c.stock_id,
                "name": c.name,
                "sector": c.sector,
                "score": c.score,
                "entry": long_ee[c.stock_id].entry_reference if c.stock_id in long_ee else None,
                "stop": long_ee[c.stock_id].stop_price if c.stock_id in long_ee else None,
                "target": long_ee[c.stock_id].target_price if c.stock_id in long_ee else None,
            }
            for c in long_candidates
        ],
        "short_candidates": [
            {
                "stock_id": c.stock_id,
                "name": c.name,
                "sector": c.sector,
                "score": c.score,
                "entry": short_ee[c.stock_id].entry_reference if c.stock_id in short_ee else None,
                "stop": short_ee[c.stock_id].stop_price if c.stock_id in short_ee else None,
                "target": short_ee[c.stock_id].target_price if c.stock_id in short_ee else None,
            }
            for c in short_candidates
        ],
    }
    (DATA_DIR / "latest.json").write_text(json.dumps(latest_json, ensure_ascii=False, indent=2), encoding="utf-8")

    log.info("報告已寫出: %s", out_path)
    print(report_md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

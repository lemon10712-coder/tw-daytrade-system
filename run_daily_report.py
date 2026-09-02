#!/usr/bin/env python3
"""每日報告產生器的進入點腳本。

現在（網路權限尚未開通）只能用 --source fixture 執行，這會清楚印出「這是測試模式」，
不會被誤當成真實報告。網路權限開通、TwseOpenApiProvider/YFinanceIntlProvider 真正
串接完成後，改成 --source real 即可，其餘程式碼不需要更動。

用法:
    python scripts/run_daily_report.py --source fixture
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from stockSystem.backtest import GateResult  # noqa: E402
from stockSystem.data_sources import FixtureProvider, DataSourceUnavailableError  # noqa: E402
from stockSystem.entry_exit import compute_entry_exit  # noqa: E402
from stockSystem.logging_setup import get_logger, new_run_id  # noqa: E402
from stockSystem.position_sizing import build_all_combos  # noqa: E402
from stockSystem.report import render_daily_report, self_check  # noqa: E402
from stockSystem.sector_strength import apply_macro_overlay, compute_sector_scores, rank_sectors  # noqa: E402
from stockSystem.stock_screener import screen_sector  # noqa: E402

REPORTS_DIR = Path(__file__).resolve().parents[1] / "reports"


def main() -> int:
    parser = argparse.ArgumentParser(description="產生台股當沖每日報告")
    parser.add_argument("--source", choices=["fixture", "real"], default="fixture")
    parser.add_argument("--date", default=None, help="YYYY-MM-DD，預設今天")
    args = parser.parse_args()

    run_id = new_run_id()
    log = get_logger("run_daily_report", run_id)

    as_of = dt.date.fromisoformat(args.date) if args.date else dt.date.today()
    log.info("開始執行每日報告，資料日期=%s，資料源=%s", as_of, args.source)

    if args.source == "real":
        log.error("真實資料源尚未可用（見 KNOWN_ISSUES.md），請先用 --source fixture")
        from stockSystem.data_sources import TwseOpenApiProvider

        try:
            TwseOpenApiProvider().get_snapshot(as_of)
        except DataSourceUnavailableError as exc:
            log.error(str(exc))
        return 1

    provider = FixtureProvider()
    snapshot = provider.get_snapshot(as_of)
    log.info("取得資料快照，來源標記=%s，股票數=%d", snapshot.source_tag.value, len(snapshot.ohlcv))

    issues = self_check(snapshot)
    for issue in issues:
        log.warning("資料品質檢查: %s", issue)

    market_return_by_window = {3: 0.0, 5: 0.0, 10: 0.0}  # fixture 大盤基準先設為0，之後接真實大盤指數
    scores = compute_sector_scores(snapshot.ohlcv, market_return_by_window)
    scores = apply_macro_overlay(scores, snapshot.intl_snapshot, semiconductor_sectors={"半導體", "光通訊"})
    strongest, weakest = rank_sectors(scores)
    log.info("族群強度排名完成，最強=%s，最弱=%s", strongest[0].sector, weakest[0].sector)

    long_candidates = []
    short_candidates = []
    for s in strongest[:2]:
        cands, excluded = screen_sector(snapshot, s.sector, "long")
        log.info("多方篩選 %s：候選 %d 檔，排除 %d 檔", s.sector, len(cands), len(excluded))
        long_candidates.extend(cands[:3])
    for s in weakest[:2]:
        cands, excluded = screen_sector(snapshot, s.sector, "short")
        log.info("空方篩選 %s：候選 %d 檔，排除 %d 檔", s.sector, len(cands), len(excluded))
        short_candidates.extend(cands[:3])

    # 目前尚未有真實歷史資料可回測，所有候選一律標示為 unvalidated（誠實反映現狀，不假裝已驗證）
    gate_results = {
        c.stock_id: GateResult(passed=False, confidence_tier="unvalidated", reasons=["尚無真實歷史資料可回測"])
        for c in long_candidates + short_candidates
    }

    all_candidates_sorted = sorted(long_candidates + short_candidates, key=lambda c: c.score, reverse=True)
    combos = build_all_combos(all_candidates_sorted)

    def _entry_exit_for(candidates):
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

    long_ee = _entry_exit_for(long_candidates)
    short_ee = _entry_exit_for(short_candidates)
    log.info("進出場價位計算完成，多方=%d檔，空方=%d檔", len(long_ee), len(short_ee))

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
    out_path = REPORTS_DIR / f"{as_of.isoformat()}_{args.source}_{run_id}.md"
    out_path.write_text(report_md, encoding="utf-8")
    log.info("報告已寫出: %s", out_path)
    print(report_md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

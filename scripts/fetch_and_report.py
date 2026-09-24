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

from stockSystem import backtest_tracker as bt  # noqa: E402
from stockSystem.backtest import GateResult  # noqa: E402
from stockSystem.config import ACCOUNT, SCORING  # noqa: E402
from stockSystem.data_sources import DataSourceUnavailableError, DataValidationError  # noqa: E402
from stockSystem.entry_exit import compute_entry_exit  # noqa: E402
from stockSystem.logging_setup import get_logger, new_run_id  # noqa: E402
from stockSystem.position_sizing import build_all_combos  # noqa: E402
from stockSystem.real_providers import (  # noqa: E402
    RealTwseProvider,
    RealYFinanceIntlProvider,
    _fetch_tpex_day_all,
    _fetch_twse_day_all,
    _parse_market_rows,
)
from stockSystem.report import render_daily_report, self_check  # noqa: E402
from stockSystem.sector_strength import apply_macro_overlay, compute_sector_scores, market_breadth, rank_sectors  # noqa: E402
from stockSystem.stock_screener import screen_sector  # noqa: E402

DATA_DIR = REPO_ROOT / "data"
REPORTS_DIR = REPO_ROOT / "reports"
TAIPEI = ZoneInfo("Asia/Taipei")


def _run_backtest_review(as_of: dt.date, data_dir: Path, log) -> tuple[list, dict | None]:
    """自動回測：讀回「上一個有記錄候選股的交易日」，用當時已經正式公布的真實開高低收，
    檢查那天報告裡的進場/止損/停利參考價有沒有被觸及。任何一步失敗都不能讓整次報告掛掉——
    這是錦上添花的區塊，不是核心資料，失敗就記警告、回傳空結果，報告照樣產生
    （呼應 `fetch_daily_snapshot_dict` 對「核心 vs 非核心」失敗處理的一貫原則）。
    """
    try:
        prev_date = bt.find_last_recorded_date(data_dir, as_of)
        if prev_date is None:
            log.info("回測比對：找不到 %s 之前的候選股記錄（可能是系統剛啟用），本次報告不含回測區塊", as_of)
            return [], None

        records = bt.load_candidates(data_dir, prev_date)
        if not records:
            return [], None

        import requests

        session = requests.Session()
        try:
            twse_raw = _fetch_twse_day_all(session, prev_date)
        except Exception as exc:  # noqa: BLE001
            log.warning("回測比對：抓不到 %s 的 TWSE 正式收盤資料，略過回測區塊: %r", prev_date, exc)
            twse_raw = []
        try:
            tpex_raw = _fetch_tpex_day_all(session, prev_date)
        except Exception as exc:  # noqa: BLE001
            log.warning("回測比對：抓不到 %s 的 TPEx 正式收盤資料: %r", prev_date, exc)
            tpex_raw = []

        actual_rows = _parse_market_rows(twse_raw, "TWSE")
        actual_rows.update(_parse_market_rows(tpex_raw, "TPEx"))

        outcomes = []
        for record in records:
            actual = actual_rows.get(record["stock_id"])
            if actual is None:
                log.warning("回測比對：%s 當天(%s)的真實收盤資料裡找不到 %s，略過這一檔",
                            record["stock_id"], prev_date, record["stock_id"])
                continue
            outcomes.append(bt.evaluate_outcome(
                record, actual["open"], actual["high"], actual["low"], actual["close"]
            ))

        summary = bt.append_summary(data_dir, prev_date, outcomes) if outcomes else None
        log.info("回測比對完成：%s 的候選股共 %d 檔，比對到真實結果 %d 檔", prev_date, len(records), len(outcomes))
        return outcomes, summary
    except Exception as exc:  # noqa: BLE001
        log.warning("自動回測比對整體失敗，本次報告不含回測區塊（不影響報告其他部分）: %r", exc)
        return [], None


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

    # 2026-09-24 新增：大盤廣度風控（參考真實策略 FinLab 台股動能策略的設計，見
    # sector_strength.market_breadth 與 config.SCORING 裡新增的 breadth_* 參數）。
    # 用「篩選前的全市場股票池」（snapshot.ohlcv，包含所有族群，不只是選進候選清單的股票）
    # 算廣度，這樣才是真正的「大盤」訊號，而不是候選股自己的廣度（候選股本來就是篩出來的
    # 強勢股，拿候選股算廣度沒有意義）。
    breadth_pct = market_breadth(snapshot.ohlcv, window=SCORING.breadth_trend_window)
    if breadth_pct == breadth_pct and breadth_pct < SCORING.breadth_risk_off_threshold:
        risk_scale = SCORING.breadth_risk_off_scale
        log.info("大盤廣度風控觸發：站上%d日均線比例=%.1f%%，低於門檻%.0f%%，部位規模降為%.0f%%",
                 SCORING.breadth_trend_window, breadth_pct * 100, SCORING.breadth_risk_off_threshold * 100,
                 risk_scale * 100)
    else:
        risk_scale = 1.0
        breadth_str = f"{breadth_pct * 100:.1f}%" if breadth_pct == breadth_pct else "無法計算"
        log.info("大盤廣度風控未觸發：站上%d日均線比例=%s", SCORING.breadth_trend_window, breadth_str)

    long_candidates = []
    short_candidates = []
    for s in strongest[:3]:
        cands, excluded = screen_sector(snapshot, s.sector, "long")
        log.info("多方篩選 %s：候選 %d 檔，排除 %d 檔", s.sector, len(cands), len(excluded))
        long_candidates.extend(cands[:3])
    for s in weakest[:3]:
        cands, excluded = screen_sector(snapshot, s.sector, "short")
        log.info("空方篩選 %s：候選 %d 檔，排除 %d 檔", s.sector, len(cands), len(excluded))
        short_candidates.extend(cands[:3])

    # 回測晉升門檻的真正串接（用 state/rule_registry.json 的歷史紀錄）留待累積夠多真實交易日資料後再接，
    # 目前累積的歷史天數如果還不夠 backtest.py 的 min_sample_size，一律誠實標示 unvalidated，
    # 不假裝已經驗證過。
    gate_results = {
        c.stock_id: GateResult(passed=False, confidence_tier="unvalidated", reasons=["尚在累積真實歷史資料，未達回測驗證門檻"])
        for c in long_candidates + short_candidates
    }

    all_candidates_sorted = sorted(long_candidates + short_candidates, key=lambda c: c.score, reverse=True)

    # 2026-09-24：entry/stop/target 要先算出來，才能交給 build_all_combos 的風險%部位法
    # （第4種組合）用止損距離反推張數；原本 combos 是在算 entry_exit 之前就算好的，
    # 這裡把順序換過來，行為對原本三種組合完全沒有影響（它們不吃 entry_exit_map）。
    long_ee = _entry_exit_for(snapshot, long_candidates)
    short_ee = _entry_exit_for(snapshot, short_candidates)
    all_ee = {**long_ee, **short_ee}

    combos = build_all_combos(
        all_candidates_sorted,
        capital_cap=ACCOUNT.capital_cap_twd * risk_scale,
        entry_exit_map=all_ee,
    )

    # 自動回測（2026-09-03 使用者要求：「請妳回測」「以後都要自動回測」）：先比對上一個交易日
    # 的候選股有沒有真的觸價，再把「今天」的候選股存下來給明天用——順序不能顛倒，不然今天會
    # 拿自己比對自己。
    backtest_review, backtest_summary = _run_backtest_review(as_of, DATA_DIR, log)
    bt.save_candidates(DATA_DIR, as_of, long_candidates + short_candidates, all_ee)

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
        backtest_review=backtest_review,
        backtest_summary=backtest_summary,
        breadth_pct=breadth_pct,
        breadth_window=SCORING.breadth_trend_window,
        breadth_threshold=SCORING.breadth_risk_off_threshold,
        breadth_risk_scale=risk_scale,
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
        "breadth_pct": breadth_pct if breadth_pct == breadth_pct else None,
        "breadth_risk_scale": risk_scale,
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

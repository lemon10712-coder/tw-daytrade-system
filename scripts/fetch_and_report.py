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

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from stockSystem import backtest_tracker as bt  # noqa: E402
from stockSystem.backtest import GateResult  # noqa: E402
from stockSystem.config import ACCOUNT, FUTURES, SCORING  # noqa: E402
from stockSystem.data_sources import DataSourceUnavailableError, DataValidationError  # noqa: E402
from stockSystem.entry_exit import compute_entry_exit  # noqa: E402
from stockSystem.futures_data import IndexDailyStore, _fetch_twse_index_ohlc  # noqa: E402
from stockSystem.futures_position_sizing import build_futures_position  # noqa: E402
from stockSystem.futures_signals import compute_futures_entry_exit, decide_direction  # noqa: E402
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
REPORTS_JSON_DIR = DATA_DIR / "reports_json"
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


def _run_futures_section(as_of: dt.date, data_dir: Path, log, breadth_pct: float | None) -> dict:
    """2026-09-24 新增：微台指(MXF)當沖建議區塊的資料準備。整段包在 try/except 裡，
    任何一步失敗都只回傳「不可用＋原因」，不會讓股票報告的其他部分掛掉——這個區塊是
    在既有股票系統之上新增的獨立模組，錦上添花，不是核心資料（呼應 `_run_backtest_review`
    同一套「錦上添花區塊失敗不擋主流程」的原則）。

    用「台股加權指數(TAIEX)」日線 OHLC 近似微台指方向，見 futures_signals.py 開頭的誠實聲明。
    """
    try:
        import requests

        store = IndexDailyStore(data_dir)
        session = requests.Session()

        today_bar = store.load(as_of)
        if today_bar is None:
            try:
                today_bar = _fetch_twse_index_ohlc(session, as_of)
            except Exception as exc:  # noqa: BLE001
                log.warning("期貨模組：抓取台股加權指數當日OHLC失敗: %r", exc)
                today_bar = None
            if today_bar is not None:
                store.save(as_of, today_bar)

        if today_bar is None:
            return {"available": False, "reason": "今日台股加權指數OHLC抓取失敗，本次不提供期貨訊號"}

        history = store.load_recent(as_of, lookback_days=60)
        if len(history) < FUTURES.min_history_days:
            return {
                "available": False,
                "reason": f"大盤指數歷史資料尚不足（目前累積{len(history)}天，需要至少{FUTURES.min_history_days}天才能算均線/ATR）",
            }

        closes = np.array([h["close"] for h in history], dtype=float)
        highs = np.array([h["high"] for h in history], dtype=float)
        lows = np.array([h["low"] for h in history], dtype=float)

        direction, reason = decide_direction(
            closes,
            windows=FUTURES.ma_windows,
            breadth_pct=breadth_pct,
            breadth_risk_off_threshold=SCORING.breadth_risk_off_threshold,
        )
        result = {
            "available": True,
            "direction": direction,
            "direction_reason": reason,
            "as_of_close": float(closes[-1]),
        }
        if direction == "neutral":
            log.info("期貨模組：方向判斷為中性，不提供進出場價位（%s）", reason)
            return result

        ee = compute_futures_entry_exit(
            direction=direction,
            high_hist=highs,
            low_hist=lows,
            close_hist=closes,
            atr_window=FUTURES.atr_window,
            atr_multiple=FUTURES.atr_stop_multiple,
            risk_reward=FUTURES.target_risk_reward,
        )
        position = build_futures_position(
            direction=direction,
            entry_reference=ee.entry_reference,
            stop_price=ee.stop_price,
            target_price=ee.target_price,
        )
        result.update({
            "entry_reference": ee.entry_reference,
            "stop_price": ee.stop_price,
            "target_price": ee.target_price,
            "caveat": ee.caveat,
            "position": position,
        })
        log.info("期貨模組：方向=%s，進場參考=%.0f，止損=%.0f，停利=%.0f，口數=%s",
                 direction, ee.entry_reference, ee.stop_price, ee.target_price,
                 position.contracts if position else "無可行口數")
        return result
    except Exception as exc:  # noqa: BLE001
        log.warning("期貨模組整體失敗，本次報告不含期貨區塊（不影響報告其他部分）: %r", exc)
        return {"available": False, "reason": "期貨模組執行時發生未預期錯誤"}


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
    scores = apply_macro_overlay(
        scores, snapshot.intl_snapshot, semiconductor_sectors={"半導體業", "通信網路業", "電子零組件業"}
    )
    strongest, weakest = rank_sectors(scores)
    log.info("族群強度排名完成，最強=%s，最弱=%s", strongest[0].sector, weakest[0].sector)

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

    gate_results = {
        c.stock_id: GateResult(passed=False, confidence_tier="unvalidated", reasons=["尚在累積真實歷史資料，未達回測驗證門檻"])
        for c in long_candidates + short_candidates
    }

    all_candidates_sorted = sorted(long_candidates + short_candidates, key=lambda c: c.score, reverse=True)

    long_ee = _entry_exit_for(snapshot, long_candidates)
    short_ee = _entry_exit_for(snapshot, short_candidates)
    all_ee = {**long_ee, **short_ee}

    combos = build_all_combos(
        all_candidates_sorted,
        capital_cap=ACCOUNT.capital_cap_twd * risk_scale,
        entry_exit_map=all_ee,
    )

    backtest_review, backtest_summary = _run_backtest_review(as_of, DATA_DIR, log)
    bt.save_candidates(DATA_DIR, as_of, long_candidates + short_candidates, all_ee)

    # 2026-09-24 新增：微台指(MXF)當沖建議（獨立於股票模組，見 _run_futures_section 的
    # try/except 保護，失敗不影響股票報告本身）。
    futures_result = _run_futures_section(as_of, DATA_DIR, log, breadth_pct)

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
        futures_result=futures_result,
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
        "futures": {
            "available": futures_result.get("available", False),
            "direction": futures_result.get("direction"),
            "entry": futures_result.get("entry_reference"),
            "stop": futures_result.get("stop_price"),
            "target": futures_result.get("target_price"),
            "contracts": futures_result["position"].contracts if futures_result.get("position") else None,
        },
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

    # 2026-09-24 新增：除了覆寫 latest.json（只留「最新一天」），另外逐日存一份到
    # data/reports_json/YYYY-MM-DD.json——latest.json 每天會被蓋掉，沒辦法回頭查任何一天
    # 的結構化資料；這裡存的是同一份內容，只是多存一份不會被覆蓋的逐日副本，給之後
    # scripts/build_site.py 產生的部落格網站、以及 Claude Artifact 儀表板用，讓「今天以外
    # 的任一天」也能拿到結構化資料，不用只能解析 markdown 純文字。跟 latest.json 一樣，
    # 這是報告本體（上面的 .md）以外的附加品，寫入失敗不影響報告本身是否成功產生。
    try:
        REPORTS_JSON_DIR.mkdir(parents=True, exist_ok=True)
        (REPORTS_JSON_DIR / f"{as_of.isoformat()}.json").write_text(
            json.dumps(latest_json, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("逐日結構化 JSON 存檔失敗（不影響報告本身）: %r", exc)

    log.info("報告已寫出: %s", out_path)
    print(report_md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""族群強度分析（規劃書第 4 節）：相對強度、廣度、量能擴張，並套用第 3 節的國際情勢加權。

輸出明確區分「最強族群」與「最弱族群」，分別作為多方/空方候選的來源。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from stockSystem.technicals import moving_average, volume_expansion_ratio


@dataclass
class SectorScore:
    sector: str
    relative_strength_pct: float   # 族群近 N 日報酬 - 大盤近 N 日報酬（用等權重族群平均近似）
    breadth: float                 # 族群內近 N 日上漲家數比例
    volume_expansion: float        # 族群近期量能 / 過去量能
    macro_adjustment: float        # 國際情勢加權修正量（見 apply_macro_overlay）
    composite_score: float         # 綜合分數，用來排序


def _sector_return(rows: pd.DataFrame, window: int) -> float:
    rets = []
    for close_hist in rows["close_hist"]:
        if len(close_hist) <= window:
            continue
        rets.append(close_hist[-1] / close_hist[-1 - window] - 1)
    return float(np.mean(rets)) if rets else float("nan")


def _breadth(rows: pd.DataFrame, window: int) -> float:
    ups = 0
    total = 0
    for close_hist in rows["close_hist"]:
        if len(close_hist) <= window:
            continue
        total += 1
        if close_hist[-1] > close_hist[-1 - window]:
            ups += 1
    return ups / total if total else float("nan")


def compute_sector_scores(
    ohlcv: pd.DataFrame,
    market_return_by_window: dict,
    rs_windows=(3, 5, 10),
) -> list[SectorScore]:
    """`ohlcv` 需含 'sector'、'close_hist'、'volume_hist' 欄位（見 data_sources.FixtureProvider 的輸出格式）。"""
    scores = []
    for sector, rows in ohlcv.groupby("sector"):
        # 多個時間窗口的相對強度取平均，避免單一窗口被短期雜訊干擾（規劃書 4.1 節的設計理由）
        rs_values = []
        for w in rs_windows:
            sector_ret = _sector_return(rows, w)
            market_ret = market_return_by_window.get(w, 0.0)
            if not np.isnan(sector_ret):
                rs_values.append(sector_ret - market_ret)
        rs = float(np.mean(rs_values)) if rs_values else float("nan")

        breadth = _breadth(rows, rs_windows[0])

        vol_ratios = [
            volume_expansion_ratio(v) for v in rows["volume_hist"] if len(v) >= 20
        ]
        vol_ratios = [v for v in vol_ratios if not np.isnan(v)]
        # 2026-09-07 修正：改用中位數而不是平均數。族群動輒有幾十到上百檔股票，
        # 只要其中一檔的量能擴張比異常大，平均數會被單一極端值拖走，讓「一檔股票暴量」
        # 被誤判成「整個族群放量」；中位數對這種單點離群值有抵抗力，更能反映族群
        # 「大部分股票」的真實量能狀態（見 KNOWN_ISSUES.md 2026-09-07 診斷紀錄，
        # 生技醫療業原本平均數 26.40 倍幾乎全部來自單一檔 6649 台生材的異常比率）。
        vol_expansion = float(np.median(vol_ratios)) if vol_ratios else float("nan")

        composite = (
            (rs if not np.isnan(rs) else 0) * 100
            + (breadth if not np.isnan(breadth) else 0.5) * 10
            + (vol_expansion if not np.isnan(vol_expansion) else 1.0) * 5
        )

        scores.append(
            SectorScore(
                sector=sector,
                relative_strength_pct=rs,
                breadth=breadth,
                volume_expansion=vol_expansion,
                macro_adjustment=0.0,
                composite_score=composite,
            )
        )
    return scores


def apply_macro_overlay(scores: list[SectorScore], intl_snapshot: dict, semiconductor_sectors: set) -> list[SectorScore]:
    """用國際情勢（主要是費半 SOX、台積電ADR）修正半導體/科技相關族群的分數。

    這是「加權修正」而非「一票否決」：見規劃書 3.2 節。目前用一個簡單線性公式，
    實際權重大小應該由 backtest.py 的回測結果校準，這裡先給一個保守、透明、
    容易解釋的預設版本，而不是一個看似精巧但缺乏根據的複雜公式。
    """
    sox = intl_snapshot.get("SOX", 0.0)
    tsm_adr = intl_snapshot.get("TSM_ADR", 0.0)
    macro_signal = (sox + tsm_adr) / 2  # 簡單平均，單位：百分比

    adjusted = []
    for s in scores:
        adj = macro_signal * 0.5 if s.sector in semiconductor_sectors else 0.0
        adjusted.append(
            SectorScore(
                sector=s.sector,
                relative_strength_pct=s.relative_strength_pct,
                breadth=s.breadth,
                volume_expansion=s.volume_expansion,
                macro_adjustment=adj,
                composite_score=s.composite_score + adj,
            )
        )
    return adjusted


def rank_sectors(scores: list[SectorScore]) -> tuple[list[SectorScore], list[SectorScore]]:
    """回傳 (最強排序, 最弱排序)，皆由強到弱排列，供上層各取所需。"""
    ranked = sorted(scores, key=lambda s: s.composite_score, reverse=True)
    return ranked, list(reversed(ranked))


def market_breadth(ohlcv: pd.DataFrame, window: int = 60) -> float:
    """全市場（篩選後的股票池）裡，收盤價站上 `window` 日均線的比例。

    2026-09-24 新增：參考真實策略（FinLab 台股動能策略）的大盤廣度風控設計——那套策略
    用「全市場站上60日均線比例 <= 40%」當作系統性風險訊號，訊號觸發時把部位規模減半，
    而不是逐檔看技術面評分（族群/個股分數再高，如果多數股票都在均線之下，代表當下
    環境本身偏空，個股層級的訊號在這種環境下比較容易失效）。

    這裡刻意重用 ohlcv 裡本來就有的 close_hist（跟 compute_sector_scores 用的是同一份
    資料），不需要另外接資料源；window 預設 60，對應 SCORING.ma_windows 裡本來就有的
    第 4 個窗口，不是另外發明一個新的均線天數。

    資料不足（沒有任何股票有 >= window 天的收盤價）時回傳 nan，呼叫端應該把 nan
    視為「這次無法判斷廣度」，維持預設部位，而不是當成 0% 觸發風控。
    """
    ups = 0
    total = 0
    for close_hist in ohlcv["close_hist"]:
        if len(close_hist) < window:
            continue
        ma = moving_average(close_hist, window)
        if np.isnan(ma):
            continue
        total += 1
        if close_hist[-1] > ma:
            ups += 1
    return ups / total if total else float("nan")

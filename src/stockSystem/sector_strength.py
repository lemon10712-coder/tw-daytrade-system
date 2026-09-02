"""族群強度分析（規劃書第 4 節）：相對強度、廣度、量能擴張，並套用第 3 節的國際情勢加權。

輸出明確區分「最強族群」與「最弱族群」，分別作為多方/空方候選的來源。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from stockSystem.technicals import volume_expansion_ratio


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
        vol_expansion = float(np.mean(vol_ratios)) if vol_ratios else float("nan")

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

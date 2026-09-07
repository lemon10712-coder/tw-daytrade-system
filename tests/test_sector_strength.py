import datetime as dt

import numpy as np
import pandas as pd

from stockSystem.data_sources import FixtureProvider
from stockSystem.sector_strength import apply_macro_overlay, compute_sector_scores, rank_sectors


def _get_scores():
    snapshot = FixtureProvider(seed=42).get_snapshot(dt.date.today())
    market_return_by_window = {3: 0.0, 5: 0.0, 10: 0.0}
    return compute_sector_scores(snapshot.ohlcv, market_return_by_window), snapshot


def test_strongest_sector_matches_fixture_design():
    """FixtureProvider 刻意把「光通訊」設計成趨勢最強的族群（見 data_sources.py 的 _sector_trend），
    這裡驗證族群強度排序邏輯真的能把它排到最前面，而不是隨機結果。
    """
    scores, _ = _get_scores()
    ranked_strongest, _ = rank_sectors(scores)
    assert ranked_strongest[0].sector == "光通訊"


def test_weakest_sector_matches_fixture_design():
    scores, _ = _get_scores()
    _, ranked_weakest = rank_sectors(scores)
    assert ranked_weakest[0].sector == "航運"


def test_macro_overlay_only_adjusts_specified_sectors():
    scores, snapshot = _get_scores()
    adjusted = apply_macro_overlay(scores, {"SOX": 3.0, "TSM_ADR": 2.0}, semiconductor_sectors={"半導體"})
    for s in adjusted:
        if s.sector == "半導體":
            assert s.macro_adjustment != 0.0
        else:
            assert s.macro_adjustment == 0.0


def test_macro_overlay_is_additive_correction_not_override():
    scores, _ = _get_scores()
    before = {s.sector: s.composite_score for s in scores}
    adjusted = apply_macro_overlay(scores, {"SOX": 5.0, "TSM_ADR": 5.0}, semiconductor_sectors={"半導體"})
    after = {s.sector: s.composite_score for s in adjusted}
    # 修正是加權，不是完全否決重排：非半導體族群分數應該完全不變
    for sector, val in before.items():
        if sector != "半導體":
            assert after[sector] == val


def _make_stock_row(close_hist: list[float], volume_hist: list[float]) -> dict:
    return {
        "close_hist": np.array(close_hist, dtype=float),
        "volume_hist": np.array(volume_hist, dtype=float),
    }


def test_composite_score_not_dominated_by_single_outlier_stock():
    """2026-09-07 真實案例（見 KNOWN_ISSUES.md）：生技醫療業原本用平均數彙總族群量能擴張比，
    157檔股票裡只要 1 檔（6649台生材）算出離譜的 3907倍（基準期均量趨近於0），
    整個族群平均就被拉到 26.40倍，排到全市場第1名——但其實其他股票的量能都很正常。
    改成中位數之後，這種單一離群值不該再主宰整個族群的分數。"""
    normal_stocks = {
        f"stock_{i}": _make_stock_row(
            close_hist=[100.0] * 25,  # 平盤，relative_strength/breadth 中性
            volume_hist=[1000.0] * 25,  # 近5日均量 = 基準期均量，量能擴張比 = 1.0
        )
        for i in range(19)
    }
    # 第 20 檔股票：基準期均量趨近於 0（模擬 6649 台生材的情況），近期均量正常，
    # 比率會被除出離譜的倍數（若沒有 technicals.py 的門檻/若剛好卡在門檻邊界之外）
    outlier = _make_stock_row(
        close_hist=[100.0] * 25,
        volume_hist=[2.0] * 20 + [500.0] * 5,  # 基準期均量2張(>=1張門檻，不會被technicals.py擋掉)
    )
    rows = {**normal_stocks, "outlier_stock": outlier}
    ohlcv = pd.DataFrame(
        {
            "sector": ["測試族群"] * len(rows),
            "close_hist": [r["close_hist"] for r in rows.values()],
            "volume_hist": [r["volume_hist"] for r in rows.values()],
        },
        index=list(rows.keys()),
    )

    scores = compute_sector_scores(ohlcv, market_return_by_window={3: 0.0, 5: 0.0, 10: 0.0})
    assert len(scores) == 1
    sector_score = scores[0]

    # 離群值本身的比率是 500/2 = 250倍；平均數彙總會被它拉到 (19*1.0 + 250)/20 ≈ 13.45倍，
    # 中位數彙總應該幾乎不受影響，仍然貼近其他19檔股票的真實水準（1.0倍）。
    assert sector_score.volume_expansion < 2.0, (
        f"中位數彙總應該不受單一離群值主宰，但算出 {sector_score.volume_expansion}倍，"
        "看起來還是被單一股票拉走了"
    )

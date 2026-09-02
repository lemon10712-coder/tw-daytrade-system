import datetime as dt

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

import datetime as dt

from stockSystem.data_sources import FixtureProvider
from stockSystem.stock_screener import hard_filters, screen_sector


def test_hard_filters_excludes_disposition_stock():
    snapshot = FixtureProvider(seed=42).get_snapshot(dt.date.today())
    disp_stock = next(iter(snapshot.disposition_list))
    result = hard_filters(disp_stock, snapshot, "long")
    assert result.excluded is True
    assert "處置股票" in result.reasons


def test_hard_filters_excludes_full_delivery_stock():
    snapshot = FixtureProvider(seed=42).get_snapshot(dt.date.today())
    fd_stock = next(iter(snapshot.full_delivery_list))
    result = hard_filters(fd_stock, snapshot, "long")
    assert result.excluded is True
    assert "全額交割股" in result.reasons


def test_hard_filters_excludes_short_ineligible_stock():
    snapshot = FixtureProvider(seed=42).get_snapshot(dt.date.today())
    ineligible = snapshot.daytrade_long_eligible - snapshot.daytrade_short_eligible
    assert ineligible, "fixture 應該至少設計一檔只能做多不能放空的股票"
    stock_id = next(iter(ineligible))
    result = hard_filters(stock_id, snapshot, "short")
    assert result.excluded is True
    assert any("先賣後買" in r for r in result.reasons)


def test_screen_sector_never_returns_excluded_stock_as_candidate():
    """最重要的一條測試：硬性排除的股票，不管技術面分數多高，絕對不能出現在候選清單裡。"""
    snapshot = FixtureProvider(seed=42).get_snapshot(dt.date.today())
    for sector in {"光通訊", "半導體", "傳產", "航運"}:
        candidates, excluded = screen_sector(snapshot, sector, "long")
        excluded_ids = {e.stock_id for e in excluded}
        candidate_ids = {c.stock_id for c in candidates}
        assert excluded_ids.isdisjoint(candidate_ids)


def test_screen_sector_candidates_sorted_descending_by_score():
    snapshot = FixtureProvider(seed=42).get_snapshot(dt.date.today())
    candidates, _ = screen_sector(snapshot, "光通訊", "long")
    scores = [c.score for c in candidates]
    assert scores == sorted(scores, reverse=True)


def test_hard_filters_excludes_stock_priced_below_min_price_twd():
    """2026-09-03 使用者要求：只列股價100元以上的股票。"""
    snapshot = FixtureProvider(seed=42).get_snapshot(dt.date.today())
    cheap_stock = next(
        sid for sid in snapshot.industry_map if snapshot.ohlcv.loc[sid]["close"] < 100.0
    )
    result = hard_filters(cheap_stock, snapshot, "long")
    assert result.excluded is True
    assert any("股價低於100元門檻" in r for r in result.reasons)


def test_hard_filters_does_not_exclude_stock_priced_above_min_price_twd_on_price_grounds():
    snapshot = FixtureProvider(seed=42).get_snapshot(dt.date.today())
    pricey_stock = next(
        sid for sid in snapshot.industry_map if snapshot.ohlcv.loc[sid]["close"] >= 100.0
    )
    result = hard_filters(pricey_stock, snapshot, "long")
    assert not any("股價低於" in r for r in result.reasons)


def test_screen_sector_never_returns_sub_100_stock_as_candidate():
    snapshot = FixtureProvider(seed=42).get_snapshot(dt.date.today())
    for sector in {"光通訊", "半導體", "傳產", "航運"}:
        for direction in ("long", "short"):
            candidates, _ = screen_sector(snapshot, sector, direction)
            for c in candidates:
                assert c.price >= 100.0

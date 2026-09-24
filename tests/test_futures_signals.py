import numpy as np
import pytest

from stockSystem.futures_signals import compute_futures_entry_exit, decide_direction


def _trend_series(start, end, n=30):
    return np.linspace(start, end, n)


def test_decide_direction_returns_neutral_when_insufficient_history():
    direction, reason = decide_direction(np.array([100.0, 101.0, 102.0]))
    assert direction == "neutral"
    assert "不足" in reason


def test_decide_direction_returns_long_for_clear_uptrend():
    closes = _trend_series(15000, 18000, n=25)
    direction, reason = decide_direction(closes)
    assert direction == "long"
    assert "偏多" in reason


def test_decide_direction_returns_short_for_clear_downtrend():
    closes = _trend_series(18000, 15000, n=25)
    direction, reason = decide_direction(closes)
    assert direction == "short"
    assert "偏空" in reason


def test_decide_direction_downgrades_to_neutral_when_breadth_risk_off():
    """2026-09-24 新增：大盤廣度風控觸發時，就算均線判斷偏多，也不該給多方訊號，
    這是刻意重用股票系統既有的大盤情緒判斷，不是各管各的。"""
    closes = _trend_series(15000, 18000, n=25)
    direction, reason = decide_direction(closes, breadth_pct=0.25, breadth_risk_off_threshold=0.40)
    assert direction == "neutral"
    assert "風控" in reason


def test_decide_direction_keeps_long_when_breadth_not_triggered():
    closes = _trend_series(15000, 18000, n=25)
    direction, reason = decide_direction(closes, breadth_pct=0.70, breadth_risk_off_threshold=0.40)
    assert direction == "long"


def test_compute_futures_entry_exit_long_direction_has_stop_below_and_target_above():
    closes = _trend_series(15000, 18000, n=25)
    highs = closes + 30
    lows = closes - 30
    plan = compute_futures_entry_exit(direction="long", high_hist=highs, low_hist=lows, close_hist=closes)
    assert plan.stop_price < plan.entry_reference < plan.target_price
    assert plan.entry_reference == pytest.approx(closes[-1])


def test_compute_futures_entry_exit_short_direction_has_stop_above_and_target_below():
    closes = _trend_series(18000, 15000, n=25)
    highs = closes + 30
    lows = closes - 30
    plan = compute_futures_entry_exit(direction="short", high_hist=highs, low_hist=lows, close_hist=closes)
    assert plan.target_price < plan.entry_reference < plan.stop_price


def test_compute_futures_entry_exit_rejects_neutral_direction():
    closes = _trend_series(15000, 18000, n=25)
    with pytest.raises(ValueError):
        compute_futures_entry_exit(direction="neutral", high_hist=closes, low_hist=closes, close_hist=closes)


def test_compute_futures_entry_exit_wider_atr_multiple_gives_wider_stop_distance():
    closes = _trend_series(15000, 18000, n=25)
    highs = closes + 30
    lows = closes - 30
    tight = compute_futures_entry_exit(direction="long", high_hist=highs, low_hist=lows, close_hist=closes, atr_multiple=1.0)
    wide = compute_futures_entry_exit(direction="long", high_hist=highs, low_hist=lows, close_hist=closes, atr_multiple=2.0)
    assert (wide.entry_reference - wide.stop_price) > (tight.entry_reference - tight.stop_price)


def test_max_loss_for_contracts_matches_manual_calculation():
    closes = _trend_series(15000, 18000, n=25)
    highs = closes + 30
    lows = closes - 30
    plan = compute_futures_entry_exit(direction="long", high_hist=highs, low_hist=lows, close_hist=closes)
    stop_distance = plan.entry_reference - plan.stop_price
    expected = stop_distance * 10.0 * 2
    assert plan.max_loss_for_contracts(2, point_value_twd=10.0) == pytest.approx(expected)

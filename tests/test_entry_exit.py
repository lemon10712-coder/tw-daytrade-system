import numpy as np

from stockSystem.config import SCORING
from stockSystem.entry_exit import compute_entry_exit


def _rising_close_hist():
    return np.linspace(90, 100, 20)


def _falling_close_hist():
    return np.linspace(100, 90, 20)


def test_long_stop_is_below_entry_and_target_is_above():
    plan = compute_entry_exit(
        stock_id="TEST",
        direction="long",
        prev_close=98.0,
        open_price=99.0,
        high=101.0,
        low=98.0,
        close=100.0,
        close_hist=_rising_close_hist(),
    )
    assert plan.stop_price < plan.entry_reference < plan.target_price


def test_short_stop_is_above_entry_and_target_is_below():
    plan = compute_entry_exit(
        stock_id="TEST",
        direction="short",
        prev_close=102.0,
        open_price=101.0,
        high=102.0,
        low=99.0,
        close=100.0,
        close_hist=_falling_close_hist(),
    )
    assert plan.target_price < plan.entry_reference < plan.stop_price


def test_target_respects_configured_reward_risk_ratio():
    plan = compute_entry_exit(
        stock_id="TEST",
        direction="long",
        prev_close=98.0,
        open_price=99.0,
        high=101.0,
        low=98.0,
        close=100.0,
        close_hist=_rising_close_hist(),
        reward_risk_ratio=2.0,
    )
    risk = plan.entry_reference - plan.stop_price
    reward = plan.target_price - plan.entry_reference
    assert reward == pytest_approx(risk * 2.0)


def pytest_approx(x, rel=1e-6):
    import pytest

    return pytest.approx(x, rel=rel)


def test_max_loss_for_lots_matches_user_worked_example():
    # 呼應規劃書7.5節範例：進場487元、止損478.7元、1張(48.7萬)成本，最大預計虧損應約等於價差*1000股
    plan = compute_entry_exit(
        stock_id="A",
        direction="long",
        prev_close=480.0,
        open_price=495.0,
        high=490.0,
        low=486.0,
        close=488.0,
        close_hist=np.linspace(470, 488, 20),
    )
    max_loss = plan.max_loss_for_lots(1)
    expected = abs(plan.entry_reference - plan.stop_price) * 1000
    assert max_loss == pytest_approx(expected)


def test_caveat_always_present_and_marks_approximation():
    plan = compute_entry_exit(
        stock_id="TEST",
        direction="long",
        prev_close=98.0,
        open_price=99.0,
        high=101.0,
        low=98.0,
        close=100.0,
        close_hist=_rising_close_hist(),
    )
    assert plan.is_approximation is True
    assert "近似" in plan.caveat or "近似" in plan.caveat
    assert "VWAP" in plan.caveat


def test_atr_multiple_defaults_to_configured_value_and_is_recorded_on_plan():
    """2026-09-24 新增：ATR停損倍數改成可調參數（config.SCORING.atr_stop_multiple），
    沒有明確傳 atr_multiple 時應該用這個設定值，而且要把實際用的倍數存在回傳的計畫上，
    這樣 backtest_tracker.py 之後才能依倍數分組統計，不用去解析 stop_basis 字串。"""
    plan = compute_entry_exit(
        stock_id="TEST",
        direction="long",
        prev_close=98.0,
        open_price=99.0,
        high=101.0,
        low=98.0,
        close=100.0,
        close_hist=_rising_close_hist(),
    )
    assert plan.atr_multiple == SCORING.atr_stop_multiple


def test_atr_multiple_can_be_overridden_and_changes_stop_distance():
    """呼應使用者要求「重新檢視ATR停損倍數」：倍數要能被個別呼叫覆寫，而且真的要影響
    算出來的止損距離，不能只是存起來但沒用到。"""
    close_hist = _rising_close_hist()
    tight = compute_entry_exit(
        stock_id="TEST", direction="long", prev_close=98.0, open_price=99.0,
        high=101.0, low=98.0, close=100.0, close_hist=close_hist, atr_multiple=1.0,
    )
    wide = compute_entry_exit(
        stock_id="TEST", direction="long", prev_close=98.0, open_price=99.0,
        high=101.0, low=98.0, close=100.0, close_hist=close_hist, atr_multiple=2.0,
    )
    assert wide.atr_multiple == 2.0
    assert tight.atr_multiple == 1.0
    tight_risk = tight.entry_reference - tight.stop_price
    wide_risk = wide.entry_reference - wide.stop_price
    assert wide_risk == pytest_approx(tight_risk * 2.0)

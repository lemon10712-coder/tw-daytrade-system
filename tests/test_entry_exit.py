import numpy as np

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

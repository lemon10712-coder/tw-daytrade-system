import datetime as dt

import pytest

from stockSystem.backtest import (
    TradeResult,
    compute_metrics,
    evaluate_live_drift,
    promotion_gate,
    record_live_result,
    split_walk_forward,
)
from stockSystem.config import VALIDATION


def _make_trades(n, win_rate, ret_win=0.03, ret_loss=-0.02):
    trades = []
    n_wins = int(n * win_rate)
    for i in range(n):
        exit_ret = ret_win if i < n_wins else ret_loss
        trades.append(
            TradeResult(
                date=dt.date(2026, 1, 1) + dt.timedelta(days=i),
                stock_id="TEST",
                direction="long",
                entry_price=100.0,
                exit_price=100.0 * (1 + exit_ret),
                fee_and_tax_pct=0.003,
            )
        )
    return trades


def test_promotion_gate_rejects_small_sample():
    trades = _make_trades(10, win_rate=0.9)
    metrics = compute_metrics(trades)
    result = promotion_gate(metrics)
    assert result.passed is False
    assert result.confidence_tier == "unvalidated"
    assert "樣本不足" in result.reasons[0]


def test_promotion_gate_rejects_negative_expectancy_even_with_large_sample():
    trades = _make_trades(200, win_rate=0.3, ret_win=0.01, ret_loss=-0.02)
    metrics = compute_metrics(trades)
    result = promotion_gate(metrics)
    assert result.passed is False


def test_promotion_gate_marks_observing_when_sample_between_thresholds():
    n = VALIDATION.min_sample_size + 5
    assert n < VALIDATION.promotion_sample_size
    trades = _make_trades(n, win_rate=0.6)
    metrics = compute_metrics(trades)
    result = promotion_gate(metrics)
    assert result.passed is True
    assert result.confidence_tier == "observing"


def test_promotion_gate_marks_high_confidence_with_large_positive_sample():
    n = VALIDATION.promotion_sample_size * 2 + 10
    trades = _make_trades(n, win_rate=0.6)
    metrics = compute_metrics(trades)
    result = promotion_gate(metrics)
    assert result.passed is True
    assert result.confidence_tier == "high"


def test_walk_forward_split_never_leaks_future_dates_into_train():
    dates = [dt.date(2025, 1, 1) + dt.timedelta(days=i) for i in range(400)]
    splits = split_walk_forward(dates, train_window_days=100, test_window_days=50)
    assert len(splits) > 0
    for split in splits:
        assert split.train_end < split.test_start


def test_live_drift_flags_degradation_when_live_underperforms(tmp_path, monkeypatch):
    import stockSystem.backtest as bt

    monkeypatch.setattr(bt, "REGISTRY_PATH", tmp_path / "rule_registry.json")

    backtest_trades = _make_trades(100, win_rate=0.65)
    metrics = compute_metrics(backtest_trades)

    for i in range(30):
        record_live_result("rule_test", f"2026-02-{(i % 28) + 1:02d}", "long", -0.01)

    result = evaluate_live_drift("rule_test", metrics)
    assert result["status"] == "degraded"


def test_live_drift_ok_when_live_matches_backtest(tmp_path, monkeypatch):
    import stockSystem.backtest as bt

    monkeypatch.setattr(bt, "REGISTRY_PATH", tmp_path / "rule_registry.json")

    backtest_trades = _make_trades(100, win_rate=0.6)
    metrics = compute_metrics(backtest_trades)

    for i in range(30):
        ret = 0.03 if i % 10 < 6 else -0.02  # 約60%勝率，貼近回測預期
        record_live_result("rule_test2", f"2026-02-{(i % 28) + 1:02d}", "long", ret)

    result = evaluate_live_drift("rule_test2", metrics)
    assert result["status"] == "ok"

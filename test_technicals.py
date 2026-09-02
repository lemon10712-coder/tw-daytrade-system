import numpy as np
import pytest

from stockSystem.technicals import (
    atr_from_close_series,
    ma_alignment,
    moving_average,
    price_volume_health,
    relative_strength,
    volume_expansion_ratio,
)


def test_moving_average_insufficient_data_returns_nan():
    assert np.isnan(moving_average(np.array([1.0, 2.0]), window=5))


def test_moving_average_basic():
    assert moving_average(np.array([1.0, 2.0, 3.0, 4.0, 5.0]), window=5) == 3.0


def test_ma_alignment_detects_bullish():
    # 建構一個明確持續上漲的序列，5/10/20 日均線理應呈現多頭排列
    closes = np.linspace(100, 160, 40)
    assert ma_alignment(closes, windows=(5, 10, 20)) == "bullish"


def test_ma_alignment_detects_bearish():
    closes = np.linspace(160, 100, 40)
    assert ma_alignment(closes, windows=(5, 10, 20)) == "bearish"


def test_ma_alignment_flat_is_not_bullish_or_bearish():
    closes = np.full(40, 100.0)
    result = ma_alignment(closes, windows=(5, 10, 20))
    assert result in ("mixed", "insufficient_data")


def test_atr_increases_with_volatility():
    calm = np.cumsum(np.full(30, 1.0)) + 100
    volatile = 100 + np.cumsum(np.array([1, -1, 1, -1] * 8)[:30] * 5.0)
    atr_calm = atr_from_close_series(calm, window=14)
    atr_volatile = atr_from_close_series(volatile, window=14)
    assert atr_volatile > atr_calm


def test_volume_expansion_ratio_detects_spike():
    base = np.full(20, 1000)
    spike = np.concatenate([base, np.full(5, 3000)])
    ratio = volume_expansion_ratio(spike, recent_window=5, base_window=20)
    assert ratio > 1.5


def test_price_volume_health_healthy_up():
    closes = np.linspace(100, 110, 10)
    volumes = np.linspace(1000, 2000, 10)
    assert price_volume_health(closes, volumes, lookback=5) == "healthy_up"


def test_price_volume_health_weak_up():
    closes = np.linspace(100, 110, 10)
    volumes = np.linspace(2000, 1000, 10)
    assert price_volume_health(closes, volumes, lookback=5) == "weak_up"


def test_relative_strength_subtracts_market_return():
    result = relative_strength({"3d": 0.05, "5d": 0.08}, market_return=0.02)
    assert result["3d"] == pytest.approx(0.03)
    assert result["5d"] == pytest.approx(0.06)

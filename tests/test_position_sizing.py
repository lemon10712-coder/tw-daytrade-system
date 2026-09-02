from dataclasses import dataclass

from stockSystem.config import ACCOUNT
from stockSystem.position_sizing import (
    build_all_combos,
    build_concentrated,
    build_core_satellite,
    build_diversified,
    feasibility_check,
    lot_cost,
)


@dataclass
class FakeCandidate:
    stock_id: str
    direction: str
    price: float
    score: float


def test_lot_cost_matches_manual_calculation_from_user_example():
    # 使用者原始舉例：一張 500 元的股票 = 50 萬
    assert lot_cost(500) == 500_000
    assert lot_cost(100) == 100_000
    assert lot_cost(200) == 200_000


def test_feasibility_check_rejects_stock_priced_over_cap():
    assert feasibility_check(501) is False  # 一張要 50.1 萬，超過 50 萬額度
    assert feasibility_check(500) is True


def test_concentrated_never_exceeds_capital_cap():
    candidates = [FakeCandidate("A", "long", 137.0, 90)]
    combo = build_concentrated(candidates)
    assert combo.total_cost <= ACCOUNT.capital_cap_twd


def test_concentrated_returns_none_when_all_over_budget():
    candidates = [FakeCandidate("A", "long", 9999.0, 90)]
    assert build_concentrated(candidates) is None


def test_diversified_never_exceeds_capital_cap_and_uses_whole_lots():
    candidates = [
        FakeCandidate("A", "long", 100.0, 95),
        FakeCandidate("B", "long", 200.0, 85),
        FakeCandidate("C", "long", 150.0, 80),
    ]
    combo = build_diversified(candidates)
    assert combo.total_cost <= ACCOUNT.capital_cap_twd
    for line in combo.lines:
        assert line.lots >= 1
        assert float(line.lots).is_integer()


def test_core_satellite_never_exceeds_capital_cap():
    candidates = [
        FakeCandidate("A", "long", 300.0, 95),
        FakeCandidate("B", "long", 120.0, 85),
    ]
    combo = build_core_satellite(candidates)
    assert combo.total_cost <= ACCOUNT.capital_cap_twd


def test_build_all_combos_all_respect_cap_for_user_example_prices():
    # 呼應使用者原始例子：100 / 200 / 500 元的股票混合
    candidates = [
        FakeCandidate("A", "long", 500.0, 95),
        FakeCandidate("B", "long", 100.0, 85),
        FakeCandidate("C", "long", 200.0, 80),
    ]
    combos = build_all_combos(candidates)
    assert len(combos) > 0
    for combo in combos:
        assert combo.total_cost <= ACCOUNT.capital_cap_twd
        assert combo.remaining >= 0

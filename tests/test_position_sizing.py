from dataclasses import dataclass

from stockSystem.config import ACCOUNT
from stockSystem.position_sizing import (
    build_all_combos,
    build_concentrated,
    build_core_satellite,
    build_diversified,
    build_risk_based,
    feasibility_check,
    lot_cost,
)


@dataclass
class FakeCandidate:
    stock_id: str
    direction: str
    price: float
    score: float


@dataclass
class FakeEntryExit:
    entry_reference: float
    stop_price: float


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


def test_risk_based_never_exceeds_capital_cap():
    """2026-09-24 新增：風險%部位法（參考海龜交易系統真實方法論），呼應使用者核准的
    「新增風險%部位大小選項」需求。跟其他組合一樣，絕對不能湊出超過額度的組合。"""
    candidates = [
        FakeCandidate("A", "long", 300.0, 95),
        FakeCandidate("B", "long", 120.0, 85),
    ]
    entry_exit_map = {
        "A": FakeEntryExit(entry_reference=300.0, stop_price=298.0),   # 止損距離2元
        "B": FakeEntryExit(entry_reference=120.0, stop_price=118.0),   # 止損距離2元
    }
    combo = build_risk_based(candidates, entry_exit_map)
    assert combo is not None
    assert combo.total_cost <= ACCOUNT.capital_cap_twd
    assert combo.remaining >= 0


def test_risk_based_allocates_fewer_lots_to_wider_stop_distance():
    """核心邏輯驗證：止損距離(波動度)越遠的標的，同樣的風險預算下應該分配到越少張數——
    這正是海龜系統「1 unit = 固定風險金額 / 波動度」設計的核心精神，不是固定金額分配。"""
    candidates = [
        FakeCandidate("TIGHT", "long", 100.0, 90),  # 止損距離小
        FakeCandidate("WIDE", "long", 100.0, 90),   # 同價位，止損距離大
    ]
    entry_exit_map = {
        "TIGHT": FakeEntryExit(entry_reference=100.0, stop_price=98.0),   # 止損距離2元
        "WIDE": FakeEntryExit(entry_reference=100.0, stop_price=96.0),    # 止損距離4元
    }
    tight_only = build_risk_based([candidates[0]], entry_exit_map)
    wide_only = build_risk_based([candidates[1]], entry_exit_map)
    assert tight_only is not None and wide_only is not None
    assert tight_only.lines[0].lots > wide_only.lines[0].lots


def test_risk_based_skips_candidates_without_entry_exit_plan():
    candidates = [FakeCandidate("A", "long", 100.0, 90)]
    assert build_risk_based(candidates, entry_exit_map={}) is None


def test_build_all_combos_includes_risk_based_combo_when_entry_exit_map_provided():
    """呼應使用者核准的第4種組合：有提供 entry_exit_map 時，build_all_combos 應該多出
    風險%部位法這個組合，沒有提供時維持原本三種組合的行為（向後相容既有呼叫端/測試）。"""
    candidates = [FakeCandidate("A", "long", 100.0, 90)]
    entry_exit_map = {"A": FakeEntryExit(entry_reference=100.0, stop_price=95.0)}

    without_map = build_all_combos(candidates)
    with_map = build_all_combos(candidates, entry_exit_map=entry_exit_map)

    assert all(c.label != "風險%部位法(海龜式)" for c in without_map)
    assert any(c.label == "風險%部位法(海龜式)" for c in with_map)

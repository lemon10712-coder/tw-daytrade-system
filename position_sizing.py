"""資金與部位配置（規劃書第 6 節）：在使用者的 50 萬額度內，只用整張計算部位組合。

三個核心規則，全部來自使用者訪談時的明確確認，寫死在這裡而不是散落各處：
1. 額度定義 = 同一時間持有部位的「總成本」上限（ACCOUNT.capital_cap_twd）
2. 只用整張（1000股），不使用零股（ACCOUNT.allow_odd_lot 恆為 False）
3. 絕不湊出超過額度的組合，寧可留餘裕
"""

from __future__ import annotations

from dataclasses import dataclass

from stockSystem.config import ACCOUNT


@dataclass
class PositionLine:
    stock_id: str
    direction: str
    price: float
    lots: int
    cost: float
    max_loss_estimate: float | None = None  # 由 report.py 依止損%換算填入


@dataclass
class ComboPlan:
    label: str            # "集中單押" / "核心＋衛星" / "分散配置"
    lines: list
    total_cost: float
    remaining: float


def lot_cost(price: float) -> float:
    return price * ACCOUNT.lot_size


def feasibility_check(price: float, capital_cap: float = ACCOUNT.capital_cap_twd) -> bool:
    """一張的成本是否在額度內可行。"""
    return lot_cost(price) <= capital_cap


def _max_lots_within_budget(price: float, budget: float) -> int:
    if price <= 0:
        return 0
    return int(budget // lot_cost(price))


def build_concentrated(candidates: list, capital_cap: float = ACCOUNT.capital_cap_twd) -> ComboPlan | None:
    """集中單押：全部額度盡量放在信心分數最高的一檔（用整張湊到不超過額度為止）。"""
    feasible = [c for c in candidates if feasibility_check(c.price, capital_cap)]
    if not feasible:
        return None
    top = feasible[0]
    lots = _max_lots_within_budget(top.price, capital_cap)
    if lots < 1:
        return None
    cost = lots * lot_cost(top.price)
    line = PositionLine(stock_id=top.stock_id, direction=top.direction, price=top.price, lots=lots, cost=cost)
    return ComboPlan(label="集中單押", lines=[line], total_cost=cost, remaining=capital_cap - cost)


def build_diversified(candidates: list, capital_cap: float = ACCOUNT.capital_cap_twd, n: int = 3) -> ComboPlan | None:
    """分散配置：依信心分數由高到低，依序用「剩餘額度平均分配」的方式塞入最多 n 檔（都用整張）。"""
    feasible = [c for c in candidates if feasibility_check(c.price, capital_cap)][:n]
    if not feasible:
        return None
    lines = []
    remaining = capital_cap
    slots_left = len(feasible)
    for c in feasible:
        share = remaining / slots_left
        lots = _max_lots_within_budget(c.price, share)
        slots_left -= 1
        if lots < 1:
            continue
        cost = lots * lot_cost(c.price)
        lines.append(PositionLine(stock_id=c.stock_id, direction=c.direction, price=c.price, lots=lots, cost=cost))
        remaining -= cost
    if not lines:
        return None
    total = sum(l.cost for l in lines)
    return ComboPlan(label="分散配置", lines=lines, total_cost=total, remaining=capital_cap - total)


def build_core_satellite(
    candidates: list, capital_cap: float = ACCOUNT.capital_cap_twd, core_ratio: float = 0.65
) -> ComboPlan | None:
    """核心＋衛星：多數額度放最高信心標的，剩餘分給次高 1-2 檔。"""
    feasible = [c for c in candidates if feasibility_check(c.price, capital_cap)]
    if not feasible:
        return None
    core = feasible[0]
    core_budget = capital_cap * core_ratio
    core_lots = _max_lots_within_budget(core.price, core_budget)
    if core_lots < 1:
        # 核心標的價格太高，分配不到目標比例的整張，改用集中單押邏輯處理核心部分
        core_lots = _max_lots_within_budget(core.price, capital_cap)
        if core_lots < 1:
            return None
    core_cost = core_lots * lot_cost(core.price)
    lines = [PositionLine(stock_id=core.stock_id, direction=core.direction, price=core.price, lots=core_lots, cost=core_cost)]
    remaining = capital_cap - core_cost

    for c in feasible[1:3]:
        if remaining < lot_cost(c.price):
            continue
        lots = _max_lots_within_budget(c.price, remaining)
        if lots < 1:
            continue
        cost = lots * lot_cost(c.price)
        lines.append(PositionLine(stock_id=c.stock_id, direction=c.direction, price=c.price, lots=lots, cost=cost))
        remaining -= cost

    total = sum(l.cost for l in lines)
    return ComboPlan(label="核心＋衛星", lines=lines, total_cost=total, remaining=capital_cap - total)


def build_all_combos(candidates: list, capital_cap: float = ACCOUNT.capital_cap_twd) -> list[ComboPlan]:
    combos = [
        build_concentrated(candidates, capital_cap),
        build_core_satellite(candidates, capital_cap),
        build_diversified(candidates, capital_cap),
    ]
    return [c for c in combos if c is not None]

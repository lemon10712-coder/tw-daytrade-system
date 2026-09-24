"""資金與部位配置（規劃書第 6 節）：在使用者的 50 萬額度內，只用整張計算部位組合。

三個核心規則，全部來自使用者訪談時的明確確認，寫死在這裡而不是散落各處：
1. 額度定義 = 同一時間持有部位的「總成本」上限（ACCOUNT.capital_cap_twd）
2. 只用整張（1000股），不使用零股（ACCOUNT.allow_odd_lot 恆為 False）
3. 絕不湊出超過額度的組合，寧可留餘裕
"""

from __future__ import annotations

from dataclasses import dataclass

from stockSystem.config import ACCOUNT, RISK_SIZING


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
    label: str            # "集中單押" / "核心＋衛星" / "分散配置" / "風險%部位法(海龜式)"
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


def build_risk_based(
    candidates: list,
    entry_exit_map: dict,
    capital_cap: float = ACCOUNT.capital_cap_twd,
    risk_pct: float = RISK_SIZING.risk_pct_per_trade,
    n: int = RISK_SIZING.max_candidates,
) -> ComboPlan | None:
    """風險百分比部位法：參考海龜交易系統（Turtle Trading System）真實方法論。

    跟其他三個組合的差別：其他三個都是「先決定要花多少錢，再看能買幾張」；這個組合反過來，
    先決定「萬一看錯、真的觸及止損時最多願意虧總額度的 risk_pct（預設1%，跟海龜系統原始
    設計一致）」，再用「止損距離(entry-stop的價差) x 每張股數」反推可以買幾張。這樣波動大
    （止損距離遠）的標的會自動分配到比較少張數，波動小的可以分配到比較多張數，讓每一檔
    候選股「看錯的風險」盡量拉齊，而不是像集中單押/分散配置那樣，只看「花多少錢」、完全
    沒管每檔股票的波動度差異有多大。

    需要 entry_exit_map（stock_id -> EntryExitPlan，見 entry_exit.compute_entry_exit）才能
    算出止損距離；沒有進出場計畫的候選股直接跳過，不用其他價位硬湊一個假的風險距離。
    """
    risk_budget_total = capital_cap * risk_pct

    feasible = []
    for c in candidates:
        if not feasibility_check(c.price, capital_cap):
            continue
        ee = entry_exit_map.get(c.stock_id)
        if ee is None:
            continue
        risk_per_share = abs(ee.entry_reference - ee.stop_price)
        if risk_per_share <= 0:
            continue
        feasible.append((c, risk_per_share))
    feasible = feasible[:n]
    if not feasible:
        return None

    lines = []
    remaining = capital_cap
    slots_left = len(feasible)
    for c, risk_per_share in feasible:
        risk_budget_share = risk_budget_total / slots_left
        slots_left -= 1
        risk_per_lot = risk_per_share * ACCOUNT.lot_size
        lots_by_risk = int(risk_budget_share // risk_per_lot) if risk_per_lot > 0 else 0
        lots_by_cash = _max_lots_within_budget(c.price, remaining)
        lots = max(0, min(lots_by_risk, lots_by_cash))
        if lots < 1:
            continue
        cost = lots * lot_cost(c.price)
        lines.append(PositionLine(stock_id=c.stock_id, direction=c.direction, price=c.price, lots=lots, cost=cost))
        remaining -= cost
    if not lines:
        return None
    total = sum(l.cost for l in lines)
    return ComboPlan(label="風險%部位法(海龜式)", lines=lines, total_cost=total, remaining=capital_cap - total)


def build_all_combos(
    candidates: list,
    capital_cap: float = ACCOUNT.capital_cap_twd,
    entry_exit_map: dict | None = None,
) -> list[ComboPlan]:
    combos = [
        build_concentrated(candidates, capital_cap),
        build_core_satellite(candidates, capital_cap),
        build_diversified(candidates, capital_cap),
    ]
    if entry_exit_map:
        # 2026-09-24 新增第 4 種組合：只有在呼叫端有算好進出場計畫時才加進來，
        # 沒有 entry_exit_map（例如舊呼叫端、或測試只想看前三種組合）就保持原本行為不變。
        combos.append(build_risk_based(candidates, entry_exit_map, capital_cap))
    return [c for c in combos if c is not None]

"""微型臺指期貨(MXF)部位大小計算：用「保證金額度」跟「口數」，不是股票系統的「張數」與「現金額度」。

跟 position_sizing.build_risk_based() 同一套海龜交易系統精神：先決定「萬一看錯、觸及止損
時最多願意虧多少」（風險預算 = capital_cap_twd * risk_pct），再用止損距離（點數）反推口數；
同時口數也不能讓總保證金超過期貨資金額度，兩個限制取較小值，兩邊都不能超過。
"""

from __future__ import annotations

from dataclasses import dataclass

from stockSystem.config import FUTURES


@dataclass
class FuturesPositionPlan:
    contract_code: str
    direction: str
    contracts: int
    margin_used_twd: float
    max_loss_twd: float
    entry_reference: float
    stop_price: float
    target_price: float
    capital_cap_twd: float

    @property
    def margin_remaining_twd(self) -> float:
        return self.capital_cap_twd - self.margin_used_twd


def build_futures_position(
    direction: str,
    entry_reference: float,
    stop_price: float,
    target_price: float,
    capital_cap_twd: float = FUTURES.capital_cap_twd,
    margin_per_contract_twd: float = FUTURES.original_margin_twd,
    point_value_twd: float = FUTURES.point_value_twd,
    risk_pct: float = FUTURES.risk_pct_per_trade,
    contract_code: str = FUTURES.contract_code,
) -> FuturesPositionPlan | None:
    """算出在風險預算與保證金額度雙重限制下，最多可以下幾口。

    任何一個限制算出來是 0 口（例如止損距離太遠、或額度連 1 口保證金都不夠），
    就回傳 None，不會硬湊出一個超過限制的部位——跟股票系統 build_risk_based() 同樣的
    「絕不超過額度」原則。
    """
    stop_distance_points = abs(entry_reference - stop_price)
    if stop_distance_points <= 0:
        return None
    if margin_per_contract_twd <= 0 or capital_cap_twd <= 0:
        return None

    risk_budget_twd = capital_cap_twd * risk_pct
    contracts_by_risk = int(risk_budget_twd // (stop_distance_points * point_value_twd))
    contracts_by_margin = int(capital_cap_twd // margin_per_contract_twd)
    contracts = min(contracts_by_risk, contracts_by_margin)
    if contracts < 1:
        return None

    max_loss_twd = stop_distance_points * point_value_twd * contracts
    margin_used_twd = margin_per_contract_twd * contracts

    return FuturesPositionPlan(
        contract_code=contract_code,
        direction=direction,
        contracts=contracts,
        margin_used_twd=margin_used_twd,
        max_loss_twd=max_loss_twd,
        entry_reference=entry_reference,
        stop_price=stop_price,
        target_price=target_price,
        capital_cap_twd=capital_cap_twd,
    )

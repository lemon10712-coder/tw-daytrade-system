"""個股篩選（規劃書第 5 節）：硬性排除條件 + 多方/空方評分。

設計原則：硬性排除一律先做、而且不可被分數蓋過去——這是「防呆」的核心，
一檔被處置或不具當沖資格的股票，不管技術面分數多漂亮都不能出現在候選清單。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from stockSystem.config import SCORING
from stockSystem.data_sources import MarketSnapshot
from stockSystem.technicals import (
    ma_alignment,
    price_volume_health,
    volume_expansion_ratio,
)


@dataclass
class ExclusionResult:
    stock_id: str
    excluded: bool
    reasons: list = field(default_factory=list)


@dataclass
class Candidate:
    stock_id: str
    sector: str
    direction: str          # "long" or "short"
    price: float
    score: float
    reasons: list
    ma_state: str
    volume_expansion: float
    institutional_net: float


def hard_filters(stock_id: str, snapshot: MarketSnapshot, direction: str) -> ExclusionResult:
    """回傳這檔股票是否該被硬性排除，以及排除原因（可能不只一個，全部列出方便追查）。"""
    reasons = []
    if stock_id in snapshot.watch_list:
        reasons.append("注意股票")
    if stock_id in snapshot.disposition_list:
        reasons.append("處置股票")
    if stock_id in snapshot.full_delivery_list:
        reasons.append("全額交割股")

    if direction == "long" and stock_id not in snapshot.daytrade_long_eligible:
        reasons.append("當日不具現股當沖(買進)資格")
    if direction == "short" and stock_id not in snapshot.daytrade_short_eligible:
        reasons.append("當日不具先賣後買(放空)當沖資格")

    row = snapshot.ohlcv.loc[stock_id]
    avg_vol_lots = row["volume_hist"][-20:].mean() if len(row["volume_hist"]) >= 20 else 0
    if avg_vol_lots < SCORING.min_liquidity_avg_volume_lots:
        reasons.append(f"近20日均量過低({avg_vol_lots:.0f}張 < 門檻{SCORING.min_liquidity_avg_volume_lots}張)")

    return ExclusionResult(stock_id=stock_id, excluded=bool(reasons), reasons=reasons)


def _score_stock(stock_id: str, snapshot: MarketSnapshot, direction: str) -> Candidate | None:
    row = snapshot.ohlcv.loc[stock_id]
    close_hist = row["close_hist"]
    volume_hist = row["volume_hist"]

    ma_state = ma_alignment(close_hist, SCORING.ma_windows[:3])
    vol_exp = volume_expansion_ratio(volume_hist)
    pv_health = price_volume_health(close_hist, volume_hist)

    inst = snapshot.institutional_flow.loc[stock_id]
    inst_net = float(inst["foreign_net"] + inst["trust_net"] + inst["dealer_net"])

    margin_change = float(snapshot.margin_data.loc[stock_id]["margin_change"])

    reasons = []
    score = 0.0

    if direction == "long":
        if ma_state != "bullish":
            return None  # 不符合多方型態，不強行評分
        score += 40
        reasons.append("均線多頭排列")
        if pv_health == "healthy_up":
            score += 20
            reasons.append("價漲量增(健康)")
        elif pv_health == "weak_up":
            score -= 10
            reasons.append("價漲量縮(警訊)")
        if not pd.isna(vol_exp):
            score += min(vol_exp, 3.0) * 10
            reasons.append(f"量能擴張{vol_exp:.2f}倍")
        if inst_net > 0:
            score += min(inst_net / 1000, 20)
            reasons.append(f"三大法人買超{inst_net:.0f}張")
        if margin_change > 3000:
            score -= 15
            reasons.append("融資餘額短期暴增(風險扣分)")
    else:  # short
        if ma_state != "bearish":
            return None
        score += 40
        reasons.append("均線空頭排列")
        if pv_health == "healthy_down":
            score += 20
            reasons.append("價跌量增(趨勢確認)")
        elif pv_health == "weak_down":
            score -= 10
            reasons.append("價跌量縮(動能不足，當沖較難抓)")
        if not pd.isna(vol_exp):
            score += min(vol_exp, 3.0) * 10
            reasons.append(f"量能擴張{vol_exp:.2f}倍")
        if inst_net < 0:
            score += min(abs(inst_net) / 1000, 20)
            reasons.append(f"三大法人賣超{abs(inst_net):.0f}張")
        if margin_change > 3000:
            score += 10
            reasons.append("融資餘額高檔鬆動，可能引發多殺多(加分)")

    return Candidate(
        stock_id=stock_id,
        sector=row["sector"],
        direction=direction,
        price=float(row["close"]),
        score=round(score, 1),
        reasons=reasons,
        ma_state=ma_state,
        volume_expansion=float(vol_exp) if not pd.isna(vol_exp) else float("nan"),
        institutional_net=inst_net,
    )


def screen_sector(
    snapshot: MarketSnapshot,
    sector: str,
    direction: str,
) -> tuple[list[Candidate], list[ExclusionResult]]:
    """回傳 (通過硬性篩選並完成評分的候選人清單, 被排除的清單含原因)，依 score 由高到低排序。"""
    sector_stocks = [sid for sid, sec in snapshot.industry_map.items() if sec == sector]
    candidates: list[Candidate] = []
    excluded: list[ExclusionResult] = []

    for stock_id in sector_stocks:
        result = hard_filters(stock_id, snapshot, direction)
        if result.excluded:
            excluded.append(result)
            continue
        cand = _score_stock(stock_id, snapshot, direction)
        if cand is not None:
            candidates.append(cand)

    candidates.sort(key=lambda c: c.score, reverse=True)
    return candidates, excluded

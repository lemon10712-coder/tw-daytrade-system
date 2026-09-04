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
    ma_alignment_ratio,
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
    name: str
    sector: str
    direction: str          # "long" or "short"
    price: float
    score: float
    reasons: list
    narrative: str          # 把 reasons 的資料點組成的個股專屬分析段落，見 _narrate()
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

    if stock_id not in snapshot.ohlcv.index:
        # 產業分類名單裡有這檔股票，但當日價格資料缺漏（常見於新上市、暫停交易、
        # 或資料來源當天沒有這檔的成交資訊）——排除掉，避免整份報告因單一股票
        # 資料缺角而全部中斷（呼應本檔案開頭「硬性排除一律先做」的設計原則）。
        reasons.append("查無當日價格資料(可能為新上市/暫停交易/資料缺漏)")
        return ExclusionResult(stock_id=stock_id, excluded=True, reasons=reasons)

    row = snapshot.ohlcv.loc[stock_id]
    avg_vol_lots = row["volume_hist"][-20:].mean() if len(row["volume_hist"]) >= 20 else 0
    if avg_vol_lots < SCORING.min_liquidity_avg_volume_lots:
        reasons.append(f"近20日均量過低({avg_vol_lots:.0f}張 < 門檻{SCORING.min_liquidity_avg_volume_lots}張)")

    close_price = row["close"]
    if pd.notna(close_price) and close_price < SCORING.min_price_twd:
        reasons.append(f"股價低於{SCORING.min_price_twd:.0f}元門檻({close_price:.2f}元)")

    return ExclusionResult(stock_id=stock_id, excluded=bool(reasons), reasons=reasons)


def _narrate(
    name: str,
    direction: str,
    ma_state: str,
    ma_ratio: float,
    pv_health: str,
    vol_exp: float,
    inst_net: float,
    margin_change: float,
    score: float,
) -> str:
    """把 `_score_stock` 已經算出來的技術面/籌碼面資料點，組成一段這檔股票專屬的分析文字。

    這裡不引入任何新的判斷邏輯或數字——每一句話都直接對應到某個已經算出來、可以在
    `reasons` 裡找到來源的欄位，只是把「均線多頭排列; 價漲量增(健康); 量能擴張2.8倍」
    這種給機器看的標籤，換成人看得懂的完整句子，並且每一檔股票用的都是它自己的數字，
    不是套用同一段罐頭文字（這正是這次要修正的問題：使用者原本看到的「理由」其實是
    entry_exit.py 的資料限制警語，跟股票本身無關，見 report.py 的改動）。
    """
    parts = []
    if direction == "long":
        if ma_ratio >= 0.99:
            parts.append(f"{name}目前站上短中期均線且呈多頭排列，技術面偏多。")
        else:
            parts.append(f"{name}短中期均線大致呈多頭排列（一致度約{ma_ratio:.0%}，未達完全標準排列），技術面轉多但訊號強度較弱，信心較低。")
        if pv_health == "healthy_up":
            parts.append("股價上漲的同時成交量同步放大，屬於價量配合的健康上攻型態。")
        elif pv_health == "weak_up":
            parts.append("不過股價上漲時成交量反而縮小，追價意願不強，上漲動能可能不足，追高要留意。")
        if not pd.isna(vol_exp):
            if vol_exp >= 1.5:
                parts.append(f"今日成交量是近期均量的{vol_exp:.1f}倍，市場關注度明顯提高。")
            else:
                parts.append(f"今日量能約為近期均量的{vol_exp:.1f}倍，尚未明顯放大。")
        if inst_net > 0:
            parts.append(f"三大法人（外資＋投信＋自營商）合計買超約{inst_net:.0f}張，籌碼面偏多，跟技術面方向一致。")
        elif inst_net < 0:
            parts.append(f"但三大法人合計賣超約{abs(inst_net):.0f}張，籌碼面跟技術面方向不一致，需留意法人是否正在調節。")
        if margin_change > 3000:
            parts.append(f"融資餘額短期增加約{margin_change:.0f}張，若股價拉回，這批融資部位可能形成賣壓，已在評分中扣分反映。")
    else:  # short
        if ma_ratio >= 0.99:
            parts.append(f"{name}目前跌破短中期均線且呈空頭排列，技術面偏空。")
        else:
            parts.append(f"{name}短中期均線大致呈空頭排列（一致度約{ma_ratio:.0%}，未達完全標準排列），技術面轉空但訊號強度較弱，信心較低。")
        if pv_health == "healthy_down":
            parts.append("股價下跌的同時成交量同步放大，屬於賣壓確實出籠的趨勢確認型態。")
        elif pv_health == "weak_down":
            parts.append("不過股價下跌時成交量反而縮小，賣壓不算積極，當沖放空的動能可能不足。")
        if not pd.isna(vol_exp):
            if vol_exp >= 1.5:
                parts.append(f"今日成交量是近期均量的{vol_exp:.1f}倍，市場關注度明顯提高。")
            else:
                parts.append(f"今日量能約為近期均量的{vol_exp:.1f}倍，尚未明顯放大。")
        if inst_net < 0:
            parts.append(f"三大法人（外資＋投信＋自營商）合計賣超約{abs(inst_net):.0f}張，籌碼面偏空，跟技術面方向一致。")
        elif inst_net > 0:
            parts.append(f"但三大法人合計買超約{inst_net:.0f}張，籌碼面跟技術面方向不一致，需留意是否有主力進場承接。")
        if margin_change > 3000:
            parts.append(f"融資餘額短期增加約{margin_change:.0f}張，如果股價續跌可能引發融資追繳、多殺多，已在評分中加分反映。")
    parts.append(f"綜合評分 {score:.1f} 分（分數只反映符合的訊號多寡，不是漲跌幅預測，尚未通過回測驗證，僅供觀察）。")
    return "".join(parts)


def _score_stock(stock_id: str, snapshot: MarketSnapshot, direction: str) -> Candidate | None:
    row = snapshot.ohlcv.loc[stock_id]
    close_hist = row["close_hist"]
    volume_hist = row["volume_hist"]

    ma_state, ma_ratio = ma_alignment_ratio(close_hist, SCORING.ma_windows[:3])
    vol_exp = volume_expansion_ratio(volume_hist)
    pv_health = price_volume_health(close_hist, volume_hist)

    reasons = []

    if stock_id in snapshot.institutional_flow.index:
        inst = snapshot.institutional_flow.loc[stock_id]
        inst_net = float(inst["foreign_net"] + inst["trust_net"] + inst["dealer_net"])
    else:
        # 三大法人資料當天缺漏（可能是資料源解析失敗，不代表這檔股票真的沒有法人動向）——
        # 視為中性、不加分不扣分，而不是直接排除整檔股票。2026-09-04 就是活生生的教訓：
        # 一改成「hard_filters 裡缺資料就直接排除」，三大法人資料當天解析失敗導致全部
        # 股票被排除，多空候選清單雙雙掛零，比原本會 KeyError 崩潰還糟——這裡改成在
        # 評分層面優雅降級，缺資料只是這個次要訊號不計分，不影響其他技術面判斷。
        inst_net = 0.0
        reasons.append("三大法人買賣超資料缺漏，籌碼面本次未列入評分")

    if stock_id in snapshot.margin_data.index:
        margin_change = float(snapshot.margin_data.loc[stock_id]["margin_change"])
    else:
        margin_change = 0.0
        reasons.append("融資餘額資料缺漏，資券面本次未列入評分")

    score = 0.0

    if direction == "long":
        if ma_state != "bullish":
            return None  # 不符合多方型態，不強行評分
        if ma_ratio >= 0.99:
            score += 40
            reasons.append("均線多頭排列")
        else:
            score += 20
            reasons.append(f"均線大致偏多(一致度{ma_ratio:.0%})，未達完全標準排列，信心較低")
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
        if ma_ratio >= 0.99:
            score += 40
            reasons.append("均線空頭排列")
        else:
            score += 20
            reasons.append(f"均線大致偏空(一致度{ma_ratio:.0%})，未達完全標準排列，信心較低")
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

    score = round(score, 1)
    name = str(row["name"]).strip() if "name" in row and row["name"] else stock_id
    narrative = _narrate(
        name=name,
        direction=direction,
        ma_state=ma_state,
        ma_ratio=ma_ratio,
        pv_health=pv_health,
        vol_exp=vol_exp,
        inst_net=inst_net,
        margin_change=margin_change,
        score=score,
    )

    return Candidate(
        stock_id=stock_id,
        name=name,
        sector=row["sector"],
        direction=direction,
        price=float(row["close"]),
        score=score,
        reasons=reasons,
        narrative=narrative,
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

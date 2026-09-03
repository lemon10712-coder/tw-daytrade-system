"""進出場價位計算（規劃書第 7 節）：進場參考、止損、停利，一律用可計算的規則和統計推導，
不對股價做「預測」。這個檔案就是規劃書第 7.1 節「為什麼不能給你一個精確預測價」在程式碼裡的實踐。

**重要的資料限制，必須誠實標註**：目前不管是 FixtureProvider 還是之後要接的
TwseOpenApiProvider，能拿到的都是「日成交資訊」（每天一組 open/high/low/close），
不是逐筆成交或分鐘K。所以這裡的 VWAP 是用 (high+low+close)/3 的「典型價」去近似，
**不是真正的成交量加權平均價**，開盤區間（Opening Range）也无法用日資料算出來——
這兩點都在規劃書第 5 節就先提醒過："這一部分因為需要分鐘級資料才能做有意義的回測"。
在真正接上分鐘級/逐筆資料之前，這個模組只能提供「日線層級」的參考，不能拿來做真正
盤中分鐘級的當沖時機判斷；程式裡用 `is_approximation=True` 明確標記，report.py
呈現時必須把這個限制講清楚，不能讓使用者誤以為這是精確的當沖進場訊號。
"""

from __future__ import annotations

from dataclasses import dataclass

from stockSystem.config import ACCOUNT
from stockSystem.technicals import atr_from_close_series


@dataclass
class EntryExitPlan:
    stock_id: str
    direction: str
    prev_close: float
    open_price: float
    typical_price_approx: float   # (high+low+close)/3，VWAP 的日線近似值
    atr: float
    entry_reference: float
    stop_price: float
    stop_basis: str                # 說明止損怎麼算出來的，方便追查/教學
    target_price: float
    target_basis: str
    risk_pct: float
    reward_risk_ratio: float
    is_approximation: bool = True
    caveat: str = (
        "本模組目前只有日成交資訊可用，VWAP／進場拉回位置為日線近似值，非真實逐筆VWAP；"
        "開盤區間、費波那契回撤等真正的盤中結構位置需要分鐘級資料，尚未串接，"
        "此處數字僅供日線層級參考，不是精確的盤中進場訊號。"
        "【重要】報告在開盤前產生，進場/止損/停利三個價位都是用「前一個交易日」的收盤價"
        "算出的樞紐(pivot)參考位置，代表「如果今天股價拉回/反彈到這個位置」的假設價位，"
        "不是對今天走勢的預測，也不保證今天股價一定會到——今天可能開高走高或開低走低、"
        "全天都不會回到這個參考價，這是拉回進場策略下常見且正常的結果，不代表系統有誤；"
        "沒有觸價就代表「今天沒有出現這個進場條件」，正確做法是不進場，而不是用其他價位硬套。"
    )

    def max_loss_for_lots(self, lots: int) -> float:
        return abs(self.entry_reference - self.stop_price) * lots * ACCOUNT.lot_size


def compute_entry_exit(
    stock_id: str,
    direction: str,
    prev_close: float,
    open_price: float,
    high: float,
    low: float,
    close: float,
    close_hist,
    atr_multiple: float = 1.2,
    reward_risk_ratio: float = 2.0,
) -> EntryExitPlan:
    """規劃書 7.2-7.4 節的具體實作：

    進場參考：用日線近似 VWAP（(high+low+close)/3）當作拉回測試的參考位置——
        多方：預期拉回不破這個價位；空方：預期反彈不過這個價位。
    止損：用 ATR 的 atr_multiple 倍當作波動性止損距離（規劃書 7.3 節）。
    停利：用風險報酬比法（規劃書 7.4 節「最推薦、最機械化」的做法），
        不去預測「最高會到多少錢」，只維持贏面比輸面大的結構。
    """
    typical_price = (high + low + close) / 3
    atr = atr_from_close_series(close_hist)

    if direction == "long":
        entry = typical_price
        stop = entry - atr * atr_multiple if atr == atr else entry * 0.98  # atr是nan時退回保守的固定2%
        stop_basis = (
            f"進場參考{entry:.2f} - ATR({atr:.2f})*{atr_multiple}"
            if atr == atr
            else "ATR資料不足，暫用進場價-2%的保守估計"
        )
        risk = entry - stop
        target = entry + risk * reward_risk_ratio
        target_basis = f"風險報酬比 1:{reward_risk_ratio}，止損距離{risk:.2f}的{reward_risk_ratio}倍"
    else:  # short
        entry = typical_price
        stop = entry + atr * atr_multiple if atr == atr else entry * 1.02
        stop_basis = (
            f"進場參考{entry:.2f} + ATR({atr:.2f})*{atr_multiple}"
            if atr == atr
            else "ATR資料不足，暫用進場價+2%的保守估計"
        )
        risk = stop - entry
        target = entry - risk * reward_risk_ratio
        target_basis = f"風險報酬比 1:{reward_risk_ratio}，止損距離{risk:.2f}的{reward_risk_ratio}倍"

    risk_pct = risk / entry if entry else float("nan")

    return EntryExitPlan(
        stock_id=stock_id,
        direction=direction,
        prev_close=prev_close,
        open_price=open_price,
        typical_price_approx=typical_price,
        atr=atr,
        entry_reference=entry,
        stop_price=stop,
        stop_basis=stop_basis,
        target_price=target,
        target_basis=target_basis,
        risk_pct=risk_pct,
        reward_risk_ratio=reward_risk_ratio,
    )

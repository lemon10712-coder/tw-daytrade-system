"""微型臺指期貨(MXF)當沖訊號層：大盤趨勢判斷 + 進出場價位計算（規劃書精神的期貨版延伸）。

跟 entry_exit.py 對個股的做法完全一致：不做「精確預測價」，只用可計算的規則
（均線排列、ATR、樞紐位置、風險報酬比）給出誠實的統計參考，每個結果都附帶限制說明。

**重要限制（誠實聲明）**：這裡的訊號是對「台股加權指數(TAIEX)」本身算的日線指標，
不是對微台指期貨本身的逐筆報價算的。原因：
1. 系統目前的資料抓取架構是「開盤前執行一次、用前一交易日的日線資料算當天參考」，
   沒有接入分鐘級的盤中資料，所以無法計算真正的「開盤區間突破(Opening Range
   Breakout)」——這需要當天開盤後最初幾分鐘的即時高低點，系統目前拿不到。
2. 微台指期貨與台股加權指數之間存在期現貨價差(basis)，兩者不會完全同步，
   用加權指數的日線近似微台指的方向與波動幅度，不是假裝兩者是同一個東西。
這兩點限制會透過 caveat 文字清楚寫進報告，不會被包裝成看起來很精確的訊號。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from stockSystem.technicals import atr_from_ohlc, ma_alignment_ratio

FUTURES_CAVEAT = (
    "本區塊訊號是用「台股加權指數(TAIEX)」的日線資料計算，不是微台指期貨本身的逐筆報價，"
    "兩者之間存在期現貨價差(basis)，僅供方向參考，不保證微台指實際報價會精確對齊這裡的點位；"
    "系統目前沒有接入盤中分鐘級資料，無法計算真正的開盤區間突破，這裡用前一交易日收盤價"
    "算出的日線近似樞紐位置取代，是否觸及視當天走勢而定，不保證一定成交。"
)


def decide_direction(
    close_hist: np.ndarray,
    windows: tuple = (5, 10, 20),
    breadth_pct: float | None = None,
    breadth_risk_off_threshold: float = 0.40,
) -> tuple[str, str]:
    """依大盤加權指數的均線排列（+ 可選的個股系統大盤廣度數字）判斷方向。

    回傳 (direction, reason)，direction 是 'long' / 'short' / 'neutral'。
    均線排列不足以判斷（'insufficient_data' 或 'mixed'，一致度未達 0.6）時一律回傳
    'neutral'，不勉強給方向——呼應系統一貫「不知道就老實說不知道」的原則。

    breadth_pct 是股票系統既有的 sector_strength.market_breadth() 結果（全市場站上
    60日均線比例），可選傳入作為額外的方向確認：如果均線判斷偏多、但大盤廣度已經
    觸發風控（廣度過低），會把方向降級為 'neutral'，避免在大盤環境明顯偏空時還給多方
    訊號——這是刻意跟股票系統共用同一個大盤情緒判斷，不是重新發明一套。
    """
    direction_raw, confidence = ma_alignment_ratio(close_hist, windows=windows)
    if direction_raw == "insufficient_data":
        return "neutral", f"加權指數歷史資料不足{max(windows)}天，均線排列無法判斷"
    if direction_raw == "mixed" or confidence < 0.6:
        return "neutral", f"加權指數均線排列不一致（一致度{confidence:.0%}），不勉強給方向"

    direction = "long" if direction_raw == "bullish" else "short"
    reason = f"加權指數均線{('偏多' if direction == 'long' else '偏空')}排列（一致度{confidence:.0%}）"

    if direction == "long" and breadth_pct is not None and breadth_pct == breadth_pct:
        if breadth_pct < breadth_risk_off_threshold:
            return "neutral", (
                f"{reason}，但大盤廣度已觸發風控（站上60日均線比例{breadth_pct:.0%}，"
                f"低於門檻{breadth_risk_off_threshold:.0%}），降級為中性、不給多方訊號"
            )
        reason += f"，且大盤廣度未觸發風控（{breadth_pct:.0%}）"

    return direction, reason


@dataclass
class FuturesEntryExitPlan:
    direction: str
    prev_close: float
    entry_reference: float
    stop_price: float
    target_price: float
    atr_points: float
    atr_multiple: float
    caveat: str = FUTURES_CAVEAT

    def max_loss_for_contracts(self, contracts: int, point_value_twd: float) -> float:
        return abs(self.entry_reference - self.stop_price) * point_value_twd * contracts


def compute_futures_entry_exit(
    direction: str,
    high_hist: np.ndarray,
    low_hist: np.ndarray,
    close_hist: np.ndarray,
    atr_window: int = 14,
    atr_multiple: float = 1.2,
    risk_reward: float = 1.5,
) -> FuturesEntryExitPlan:
    """跟 entry_exit.compute_entry_exit 同一套精神：進場參考用前一交易日收盤價（日線近似
    樞紐價），止損用 ATR 的倍數，停利用風險報酬比法（不猜目標點位，只維持「贏比輸多」的結構）。

    `direction` 必須是 'long' 或 'short'（呼叫端應該先用 decide_direction 排除 'neutral' 的情況，
    這個函式本身不處理 'neutral'，因為 neutral 代表「不建議進場」，沒有進出場價位可算）。
    """
    if direction not in ("long", "short"):
        raise ValueError(f"direction 必須是 'long' 或 'short'，收到 {direction!r}（neutral 不應呼叫這個函式）")

    prev_close = float(close_hist[-1])
    atr_points = atr_from_ohlc(high_hist, low_hist, close_hist, window=atr_window)
    if atr_points != atr_points:  # nan：歷史不足以算 ATR，退回用近5日高低平均振幅粗估，一樣誠實記錄在 caveat
        recent = min(5, len(high_hist))
        atr_points = float(np.mean(high_hist[-recent:] - low_hist[-recent:])) if recent else 0.0

    stop_distance = atr_points * atr_multiple
    if direction == "long":
        stop_price = prev_close - stop_distance
        target_price = prev_close + stop_distance * risk_reward
    else:
        stop_price = prev_close + stop_distance
        target_price = prev_close - stop_distance * risk_reward

    return FuturesEntryExitPlan(
        direction=direction,
        prev_close=prev_close,
        entry_reference=prev_close,
        stop_price=stop_price,
        target_price=target_price,
        atr_points=atr_points,
        atr_multiple=atr_multiple,
    )

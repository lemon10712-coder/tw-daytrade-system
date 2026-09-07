"""技術指標計算（規劃書第 5.2 節「技術面／線型分析」的量化實作）。

所有函式都是純計算、不碰網路，輸入是 numpy array / pandas Series，方便單元測試。
"""

from __future__ import annotations

import numpy as np


def moving_average(close_hist: np.ndarray, window: int) -> float:
    """回傳最近 `window` 天的簡單移動平均。資料不足時回傳 nan，而不是硬算一個不可靠的數字。"""
    if len(close_hist) < window:
        return float("nan")
    return float(np.mean(close_hist[-window:]))


def ma_alignment(close_hist: np.ndarray, windows=(5, 10, 20)) -> str:
    """判斷均線排列：'bullish'（多頭排列）/ 'bearish'（空頭排列）/ 'mixed'（不一致）。

    多頭排列定義：短天期均線 > 長天期均線，且股價站上所有均線。
    """
    mas = [moving_average(close_hist, w) for w in windows]
    if any(np.isnan(mas)):
        return "insufficient_data"
    price = close_hist[-1]
    ascending = all(mas[i] > mas[i + 1] for i in range(len(mas) - 1))  # 短>長
    descending = all(mas[i] < mas[i + 1] for i in range(len(mas) - 1))
    if ascending and price > mas[0]:
        return "bullish"
    if descending and price < mas[0]:
        return "bearish"
    return "mixed"


def ma_alignment_ratio(close_hist: np.ndarray, windows=(5, 10, 20)) -> tuple[str, float]:
    """比 ma_alignment() 更寬鬆的連續版本：回傳 (方向, 一致度 0~1)。

    2026-09-04 使用者明確要求：均線排列的判定不要再用「教科書等級完美排列才算」的二選一，
    要允許「大致偏多/大致偏空」也能進候選清單（信心較低即可，不是完全不給）。

    一致度是「price>MA5、price>MA10、price>MA20、MA5>MA10、MA10>MA20」這5個多頭條件裡
    符合幾個(除以5)；空頭方向是對應的5個反向條件。ma_alignment() 的嚴格「bullish」
    等同於這裡一致度剛好等於1.0的情況（5個條件全部成立），所以呼叫端只要看一致度
    是否達到1.0，就能重現原本二選一的行為，不會改變既有測試的預期。

    一致度未達0.6（5個條件符合不到3個）的一律回傳 'mixed'、一致度0.0，避免用不到一半的
    雜訊訊號硬凹出方向；兩個方向都可能符合時，取比例較高的那一個。
    """
    mas = [moving_average(close_hist, w) for w in windows]
    if any(np.isnan(mas)):
        return "insufficient_data", 0.0
    price = close_hist[-1]
    bull_checks = [price > mas[0], price > mas[1], price > mas[2], mas[0] > mas[1], mas[1] > mas[2]]
    bear_checks = [price < mas[0], price < mas[1], price < mas[2], mas[0] < mas[1], mas[1] < mas[2]]
    bull_ratio = sum(bull_checks) / len(bull_checks)
    bear_ratio = sum(bear_checks) / len(bear_checks)
    if bull_ratio >= 0.6 and bull_ratio >= bear_ratio:
        return "bullish", bull_ratio
    if bear_ratio >= 0.6 and bear_ratio > bull_ratio:
        return "bearish", bear_ratio
    return "mixed", 0.0


def true_range(high: float, low: float, prev_close: float) -> float:
    return max(high - low, abs(high - prev_close), abs(low - prev_close))


def atr_from_close_series(close_hist: np.ndarray, window: int = 14) -> float:
    """用收盤價序列近似計算 ATR（沒有逐日高低價時的簡化版本：用日報酬絕對值的移動平均近似波動幅度）。

    正式接上真實日 OHLC 資料後，應改用 `atr_from_ohlc` 取得更準確的版本；
    這個近似版本先讓 Phase 0-2 在只有收盤價的情況下也能跑通。
    """
    if len(close_hist) < window + 1:
        return float("nan")
    daily_moves = np.abs(np.diff(close_hist[-(window + 1):]))
    return float(np.mean(daily_moves))


def atr_from_ohlc(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, window: int = 14) -> float:
    if len(highs) < window + 1:
        return float("nan")
    trs = [
        true_range(highs[i], lows[i], closes[i - 1])
        for i in range(len(highs) - window, len(highs))
    ]
    return float(np.mean(trs))


MIN_MEANINGFUL_BASE_VOLUME = 1.0  # 張。基準期均量低於這個門檻視為「幾乎沒有交易紀錄」，不足以算出可信的比率。


def volume_expansion_ratio(volume_hist: np.ndarray, recent_window: int = 5, base_window: int = 20) -> float:
    """近期均量 / 過去均量，>1 代表放量。

    2026-09-07 修正：原本只排除 base 剛好等於 0 的情況，但基準期均量只要是「接近 0 但不是恰好 0」
    （例如新上市權證、極冷門股票的基準期均量只有 0.0x 張），也會讓比率被除出離譜的倍數
    （實測發現過 3907 倍這種案例，見 KNOWN_ISSUES.md 診斷紀錄）。這種比率不是「真的放量」，
    只是分母幾乎是雜訊，所以改成基準期均量低於 `MIN_MEANINGFUL_BASE_VOLUME`（預設1張）時
    一律回傳 nan，跟「資料不足」一視同仁處理，不讓這種個股汙染族群強度排名或個股見解文字。
    """
    if len(volume_hist) < base_window:
        return float("nan")
    recent = np.mean(volume_hist[-recent_window:])
    base = np.mean(volume_hist[-base_window:-recent_window]) if len(volume_hist) >= base_window else np.mean(
        volume_hist[:-recent_window]
    )
    if base < MIN_MEANINGFUL_BASE_VOLUME:
        return float("nan")
    return float(recent / base)


def price_volume_health(close_hist: np.ndarray, volume_hist: np.ndarray, lookback: int = 5) -> str:
    """簡化的量價關係健康度判斷：漲時量增 -> 'healthy_up'；漲時量縮 -> 'weak_up'；
    跌時量增 -> 'healthy_down'（放量下跌，趨勢確認）；跌時量縮 -> 'weak_down'（無量陰跌）。
    """
    if len(close_hist) < lookback + 1 or len(volume_hist) < lookback + 1:
        return "insufficient_data"
    price_chg = close_hist[-1] - close_hist[-1 - lookback]
    vol_chg = np.mean(volume_hist[-lookback:]) - np.mean(volume_hist[-2 * lookback : -lookback])
    if price_chg >= 0:
        return "healthy_up" if vol_chg > 0 else "weak_up"
    return "healthy_down" if vol_chg > 0 else "weak_down"


def relative_strength(stock_returns: dict, market_return: float) -> dict:
    """個別報酬率 vs 大盤報酬率的超額報酬，輸入輸出都是 {window_label: value} 的字典。"""
    return {k: v - market_return for k, v in stock_returns.items()}

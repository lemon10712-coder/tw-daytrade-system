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


def volume_expansion_ratio(volume_hist: np.ndarray, recent_window: int = 5, base_window: int = 20) -> float:
    """近期均量 / 過去均量，>1 代表放量。"""
    if len(volume_hist) < base_window:
        return float("nan")
    recent = np.mean(volume_hist[-recent_window:])
    base = np.mean(volume_hist[-base_window:-recent_window]) if len(volume_hist) >= base_window else np.mean(
        volume_hist[:-recent_window]
    )
    if base == 0:
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

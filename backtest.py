"""回測與閉環驗證（規劃書第 8 節）：walk-forward 切分、績效指標、晉升門檻、
以及上線後的實測追蹤與異常降級（閉環的第二段）。

這是整份系統裡最重要、也最容易被抄捷徑跳過的模組——所以特別把「門檻檢查」寫成
獨立、可單元測試的純函式（`promotion_gate`），任何呼叫端都無法繞過它直接宣稱一條規則「有效」。
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np

from stockSystem.config import VALIDATION

STATE_DIR = Path(__file__).resolve().parents[2] / "state"
STATE_DIR.mkdir(parents=True, exist_ok=True)
REGISTRY_PATH = STATE_DIR / "rule_registry.json"


@dataclass
class WalkForwardSplit:
    train_start: dt.date
    train_end: dt.date
    test_start: dt.date
    test_end: dt.date


def split_walk_forward(
    all_dates: list, train_window_days: int = VALIDATION.train_window_days, test_window_days: int = VALIDATION.test_window_days
) -> list[WalkForwardSplit]:
    """把交易日清單切成一系列 (訓練期, 樣本外測試期) 組合，並往前滾動。

    刻意用「index 切片」而不是日曆天數，確保切出來的都是實際交易日，
    且測試期永遠緊接在訓練期之後、不會用到訓練期之後才出現的資料（避免未來函數 / look-ahead bias）。
    """
    all_dates = sorted(all_dates)
    splits = []
    start = 0
    while start + train_window_days + test_window_days <= len(all_dates):
        train_slice = all_dates[start : start + train_window_days]
        test_slice = all_dates[start + train_window_days : start + train_window_days + test_window_days]
        splits.append(
            WalkForwardSplit(
                train_start=train_slice[0],
                train_end=train_slice[-1],
                test_start=test_slice[0],
                test_end=test_slice[-1],
            )
        )
        start += test_window_days  # 往前滾動一個測試期的長度
    return splits


@dataclass
class TradeResult:
    date: dt.date
    stock_id: str
    direction: str
    entry_price: float
    exit_price: float
    fee_and_tax_pct: float  # 這筆交易扣掉的手續費+稅金佔進場成本的比例

    @property
    def raw_return_pct(self) -> float:
        if self.direction == "long":
            return self.exit_price / self.entry_price - 1
        return 1 - self.exit_price / self.entry_price

    @property
    def net_return_pct(self) -> float:
        return self.raw_return_pct - self.fee_and_tax_pct


@dataclass
class BacktestMetrics:
    sample_size: int
    win_rate: float
    expectancy_pct: float          # 扣成本後的平均每筆報酬率
    win_rate_ci95: tuple           # 勝率的粗略 95% 信賴區間（常態近似，樣本夠大才有意義）
    max_consecutive_losses: int


def compute_metrics(trades: list[TradeResult]) -> BacktestMetrics:
    n = len(trades)
    if n == 0:
        return BacktestMetrics(0, float("nan"), float("nan"), (float("nan"), float("nan")), 0)

    net_returns = np.array([t.net_return_pct for t in trades])
    wins = net_returns > 0
    win_rate = float(wins.mean())
    expectancy = float(net_returns.mean())

    # 常態近似的信賴區間；樣本數很小時這個近似本來就不可靠，
    # 所以 promotion_gate 會另外用 min_sample_size 把小樣本擋在門外，不是靠這個區間自己判斷。
    se = np.sqrt(win_rate * (1 - win_rate) / n) if n > 0 else float("nan")
    ci = (max(0.0, win_rate - 1.96 * se), min(1.0, win_rate + 1.96 * se))

    max_consec = 0
    cur = 0
    for w in wins:
        if not w:
            cur += 1
            max_consec = max(max_consec, cur)
        else:
            cur = 0

    return BacktestMetrics(
        sample_size=n,
        win_rate=win_rate,
        expectancy_pct=expectancy,
        win_rate_ci95=ci,
        max_consecutive_losses=max_consec,
    )


@dataclass
class GateResult:
    passed: bool
    confidence_tier: str   # "unvalidated" / "observing" / "medium" / "high"
    reasons: list = field(default_factory=list)


def promotion_gate(metrics: BacktestMetrics, cfg=VALIDATION) -> GateResult:
    """規劃書 8.2 節的晉升門檻，寫成一個純函式，方便單元測試覆蓋各種邊界情況。"""
    reasons = []

    if metrics.sample_size < cfg.min_sample_size:
        reasons.append(f"樣本數{metrics.sample_size}不足{cfg.min_sample_size}筆，樣本不足，暫不提供")
        return GateResult(passed=False, confidence_tier="unvalidated", reasons=reasons)

    if metrics.expectancy_pct < cfg.min_expectancy_pct:
        reasons.append(f"扣成本後期望值{metrics.expectancy_pct:.4f}未達門檻{cfg.min_expectancy_pct}")
        return GateResult(passed=False, confidence_tier="unvalidated", reasons=reasons)

    if metrics.sample_size < cfg.promotion_sample_size:
        reasons.append(f"樣本數{metrics.sample_size}足夠基本驗證但未達晉升門檻{cfg.promotion_sample_size}，列為觀察中")
        return GateResult(passed=True, confidence_tier="observing", reasons=reasons)

    tier = "high" if metrics.sample_size >= cfg.promotion_sample_size * 2 else "medium"
    reasons.append(f"通過驗證：樣本數{metrics.sample_size}，期望值{metrics.expectancy_pct:.4f}，勝率{metrics.win_rate:.2%}")
    return GateResult(passed=True, confidence_tier=tier, reasons=reasons)


# ---------------------------------------------------------------------------
# 閉環的第二段：上線後的實測追蹤與異常降級（規劃書第 8.3 節）
# ---------------------------------------------------------------------------

def _load_registry() -> dict:
    if REGISTRY_PATH.exists():
        return json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    return {}


def _save_registry(registry: dict) -> None:
    REGISTRY_PATH.write_text(json.dumps(registry, indent=2, ensure_ascii=False), encoding="utf-8")


def record_live_result(rule_id: str, date: str, predicted_direction: str, net_return_pct: float) -> None:
    """每天把「系統實際建議」對應的後續結果記錄下來（假設性追蹤），持久化到 state/rule_registry.json，
    這樣即使每次排程都是全新 session，也能接續累積實測歷史，不會每次都從零開始。
    """
    registry = _load_registry()
    rule = registry.setdefault(rule_id, {"live_trades": [], "status": "observing"})
    rule["live_trades"].append({"date": date, "direction": predicted_direction, "net_return_pct": net_return_pct})
    _save_registry(registry)


def evaluate_live_drift(rule_id: str, backtest_metrics: BacktestMetrics, cfg=VALIDATION) -> dict:
    """比對「實測表現」與「回測預期」，超出門檻就標記 degraded，回傳完整判斷過程方便追查
    （不是只回傳一個 True/False，而是把用到的每個數字都列出來，符合『有跡可循』的要求）。
    """
    registry = _load_registry()
    rule = registry.get(rule_id)
    if not rule or not rule["live_trades"]:
        return {"status": "no_live_data", "detail": "尚無實測資料可比對"}

    live_returns = np.array([t["net_return_pct"] for t in rule["live_trades"]])
    live_win_rate = float((live_returns > 0).mean())

    expected = backtest_metrics.win_rate
    n = len(live_returns)
    se = np.sqrt(expected * (1 - expected) / n) if n > 0 and 0 < expected < 1 else float("nan")
    if np.isnan(se) or se == 0:
        z = float("nan")
        drifted = False
    else:
        z = (live_win_rate - expected) / se
        drifted = z < -cfg.live_drift_std_threshold  # 只在「實測明顯比回測差」時才降級，變好不觸發降級

    result = {
        "status": "degraded" if drifted else "ok",
        "live_sample_size": n,
        "live_win_rate": live_win_rate,
        "backtest_win_rate": expected,
        "z_score": z,
        "threshold_std": cfg.live_drift_std_threshold,
        "detail": (
            f"實測{n}筆勝率{live_win_rate:.2%}，回測預期{expected:.2%}，"
            f"z分數{z:.2f}（低於-{cfg.live_drift_std_threshold}標準差視為異常）"
            if not np.isnan(z)
            else "樣本或分母不足，暫無法計算z分數"
        ),
    }

    rule["status"] = result["status"]
    rule["last_evaluated"] = result
    registry[rule_id] = rule
    _save_registry(registry)
    return result

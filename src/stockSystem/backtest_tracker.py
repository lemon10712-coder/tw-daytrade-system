"""逐日回測記錄（呼應使用者 2026-09-03 要求：「請妳回測」、「以後都要自動回測」）。

背景：使用者發現報告裡的進場參考價「今天根本沒到過」，追查後確認這是正常的（進場價是用
前一個交易日收盤價算出的樞紐參考位置，不是預測，不保證會觸及），但也讓使用者意識到「每次
都要我自己人工查一次」不是長久做法。這個模組把這個檢查自動化、每天都做，並且把結果存下來
累積成歷史紀錄：

1. 每天出報告時，把當天的候選股 + 進場/止損/停利價位存成一筆記錄（`save_candidates`）。
2. 隔天出報告前，先讀回「上一個有記錄的交易日」的候選股，用當時已經正式公布的真實成交
   高低價，檢查「進場參考價當天有沒有被觸及」「如果觸及，後續有沒有碰到止損或停利」
   （`evaluate_outcome`），結果會被放進當天報告的新增區塊，使用者不用再自己查一次。
3. 每次評測完，把結果累加進一份跨日的統計檔（`append_summary`），讓「進場觸價率」「觸價後
   止損/停利比例」這種數字有機會隨時間累積出有意義的樣本數——這也是 `backtest.py` 那套正式
   回測驗證閘門（`min_sample_size` 等門檻）將來要對接的資料來源，兩邊不是各自獨立的系統。

**已知限制，誠實標注（呼應整個專案「寧可誠實說不知道，也不要假裝精確」的原則）**：
目前只有日成交的開高低收，沒有分鐘級/逐筆資料，沒辦法知道「當天到底是先碰到止損還是先碰到
停利」的真實時間順序——如果同一天最高價超過停利、最低價也跌破止損，這裡老實標成「同一天內
止損/停利都被觸及，用日資料無法判斷實際先後順序」，不去猜。
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass
class BacktestOutcome:
    stock_id: str
    name: str
    direction: str  # "long" or "short"
    entry_reference: float
    stop_price: float
    target_price: float
    actual_open: float
    actual_high: float
    actual_low: float
    actual_close: float
    entry_touched: bool
    result: str  # 人類可讀的結果說明，見 evaluate_outcome()


def _backtest_dir(data_dir: Path) -> Path:
    d = Path(data_dir) / "backtest"
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_candidates(data_dir: Path, as_of: dt.date, candidates: list, entry_exit_map: dict) -> Path:
    """把當天的候選股 + 進出場計畫存起來，供下一個交易日回測比對用。

    只存「有算出進出場計畫」的候選股（entry_exit_map 裡找得到的），欄位刻意保持精簡、
    只留評測會用到的數字，方便之後直接看 JSON 內容除錯。
    """
    records = []
    for c in candidates:
        ee = entry_exit_map.get(c.stock_id)
        if ee is None:
            continue
        records.append({
            "stock_id": c.stock_id,
            "name": c.name,
            "direction": c.direction,
            "prev_close": ee.prev_close,
            "entry_reference": ee.entry_reference,
            "stop_price": ee.stop_price,
            "target_price": ee.target_price,
        })
    out_path = _backtest_dir(data_dir) / f"candidates_{as_of.isoformat()}.json"
    out_path.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    return out_path


def load_candidates(data_dir: Path, as_of: dt.date) -> list[dict]:
    path = _backtest_dir(data_dir) / f"candidates_{as_of.isoformat()}.json"
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


def find_last_recorded_date(data_dir: Path, before: dt.date) -> dt.date | None:
    """在 `before` 之前，找最近一個有存過候選股記錄的日期（自然跳過假日/系統沒執行的日子，
    不需要另外判斷交易日曆）。找不到就回傳 None（例如系統剛啟用、還沒有任何記錄）。
    """
    backtest_dir = _backtest_dir(data_dir)
    dates = []
    for p in backtest_dir.glob("candidates_*.json"):
        date_part = p.stem[len("candidates_"):]
        try:
            d = dt.date.fromisoformat(date_part)
        except ValueError:
            continue
        if d < before:
            dates.append(d)
    return max(dates) if dates else None


def evaluate_outcome(
    record: dict,
    actual_open: float,
    actual_high: float,
    actual_low: float,
    actual_close: float,
) -> BacktestOutcome:
    """拿當時存的進出場計畫，對照後來真實公布的當天開高低收，判斷結果。"""
    entry = record["entry_reference"]
    stop = record["stop_price"]
    target = record["target_price"]
    direction = record["direction"]

    entry_touched = actual_low <= entry <= actual_high

    if not entry_touched:
        result = "進場參考價當天未觸及，依規則屬於「今天沒有出現進場條件」，正確做法是不進場"
    else:
        if direction == "long":
            hit_stop = actual_low <= stop
            hit_target = actual_high >= target
        else:  # short
            hit_stop = actual_high >= stop
            hit_target = actual_low <= target

        if hit_stop and hit_target:
            result = "觸及進場參考價後，止損與停利價位同一天都被觸及，用日資料無法判斷實際先後順序"
        elif hit_stop:
            result = "觸及進場參考價後，當天即觸及止損參考價"
        elif hit_target:
            result = "觸及進場參考價後，當天即觸及停利參考價"
        else:
            result = "觸及進場參考價後，收盤前尚未觸及止損或停利"

    return BacktestOutcome(
        stock_id=record["stock_id"],
        name=record["name"],
        direction=direction,
        entry_reference=entry,
        stop_price=stop,
        target_price=target,
        actual_open=actual_open,
        actual_high=actual_high,
        actual_low=actual_low,
        actual_close=actual_close,
        entry_touched=entry_touched,
        result=result,
    )


def append_summary(data_dir: Path, evaluated_date: dt.date, outcomes: list[BacktestOutcome]) -> dict:
    """把這次評測的結果累加進跨日的統計檔（`data/backtest/summary.json`）。

    冪等設計：同一個 `evaluated_date` 重複呼叫不會重複累加（先移除舊的同日紀錄再加回去），
    這樣即使某天報告因故重跑一次，統計數字也不會被灌水。
    """
    summary_path = _backtest_dir(data_dir) / "summary.json"
    if summary_path.exists():
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
    else:
        summary = {"by_date": {}}

    summary.setdefault("by_date", {})
    summary["by_date"][evaluated_date.isoformat()] = {
        "total_candidates": len(outcomes),
        "entry_touched": sum(1 for o in outcomes if o.entry_touched),
        "hit_stop_only": sum(1 for o in outcomes if o.entry_touched and "止損參考價" in o.result and "同一天" not in o.result),
        "hit_target_only": sum(1 for o in outcomes if o.entry_touched and "停利參考價" in o.result and "同一天" not in o.result),
        "both_same_day": sum(1 for o in outcomes if "同一天都被觸及" in o.result),
        "neither": sum(1 for o in outcomes if o.entry_touched and o.result == "觸及進場參考價後，收盤前尚未觸及止損或停利"),
    }

    totals = {"total_candidates": 0, "entry_touched": 0, "hit_stop_only": 0, "hit_target_only": 0, "both_same_day": 0, "neither": 0}
    for day_stats in summary["by_date"].values():
        for k in totals:
            totals[k] += day_stats.get(k, 0)
    summary["cumulative"] = totals
    summary["cumulative"]["entry_touch_rate"] = (
        round(totals["entry_touched"] / totals["total_candidates"], 4) if totals["total_candidates"] else None
    )
    summary["cumulative"]["sample_trading_days"] = len(summary["by_date"])

    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary

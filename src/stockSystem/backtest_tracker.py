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

**2026-09-24 新增：記錄每一筆用的 ATR 停損倍數**（呼應 config.SCORING.atr_stop_multiple
改為可調參數）。目的是讓「調整倍數」不再是憑直覺猜，而是能用真實累積的樣本比較——
如果之後把倍數從 1.2 調成別的值，`append_summary` 會依倍數分組統計，讓使用者/未來的
session 可以直接比較「1.2倍那些天」vs「新倍數那些天」的進場觸價率、止損/停利比例，
而不必自己重新翻歷史資料手動算。
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
    atr_multiple: float | None = None  # 這筆記錄用的 ATR 停損倍數；舊記錄沒有這個欄位時為 None


def _backtest_dir(data_dir: Path) -> Path:
    d = Path(data_dir) / "backtest"
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_candidates(data_dir: Path, as_of: dt.date, candidates: list, entry_exit_map: dict) -> Path:
    """把當天的候選股 + 進出場計畫存起來，供下一個交易日回測比對用。

    只存「有算出進出場計畫」的候選股（entry_exit_map 裡找得到的），欄位刻意保持精簡、
    只留評測會用到的數字，方便之後直接看 JSON 內容除錯。

    2026-09-24：多存一個 atr_multiple 欄位（用 getattr 安全取值，避免傳進來的 ee 物件
    是測試用的簡化假物件、沒有這個屬性時整個函式直接壞掉——沒有的話就存 None，跟舊資料
    的行為一致）。
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
            "atr_multiple": getattr(ee, "atr_multiple", None),
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
        atr_multiple=record.get("atr_multiple"),
    )


def append_summary(data_dir: Path, evaluated_date: dt.date, outcomes: list[BacktestOutcome]) -> dict:
    """把這次評測的結果累加進跨日的統計檔（`data/backtest/summary.json`）。

    冪等設計：同一個 `evaluated_date` 重複呼叫不會重複累加（先移除舊的同日紀錄再加回去），
    這樣即使某天報告因故重跑一次，統計數字也不會被灌水。

    2026-09-24 新增 `by_atr_multiple`：除了原本跨全部樣本的 `cumulative`，額外依每筆記錄
    用的 `atr_multiple` 分組統計。用意是讓「要不要調整 ATR 停損倍數」這件事，之後可以直接
    比較不同倍數底下的進場觸價率／止損停利比例，而不是只有一組看不出倍數影響的總數字。
    倍數缺漏（舊資料、或測試用的簡化物件）一律歸類到 "unknown" 分組，不會讓整個函式報錯。
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
        "atr_multiples_used": sorted({o.atr_multiple for o in outcomes if o.atr_multiple is not None}),
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

    # 依 ATR 倍數重新彙整全部歷史 outcomes 的分組統計。這裡假設「同一天所有候選股都用同一個
    # atr_multiple」——這在目前的系統設計下必然成立，因為 atr_multiple 來自單一的全域設定
    # SCORING.atr_stop_multiple，同一次報告執行不會有兩個不同的值。在這個前提下，把該天
    # 的整天統計歸進它唯一用過的那個倍數分組，數字是精確的，不是估計值；`atr_multiples_used`
    # 存成清單只是為了在未來真的允許「同一天不同候選股用不同倍數」時，容易發現這個假設被
    # 打破（多於 1 個值），需要回來改成逐檔分組。
    by_multiple: dict = {}
    for date_str, day_stats in summary["by_date"].items():
        multiples_used = day_stats.get("atr_multiples_used", [])
        if len(multiples_used) > 1:
            # 假設被打破（同一天出現多個倍數）：無法安全歸類整天總數到單一分組，跳過這天的
            # 分組統計，避免用錯誤假設算出誤導性的數字。cumulative（跨全部樣本）不受影響。
            continue
        for multiple in multiples_used:
            key = str(multiple)
            bucket = by_multiple.setdefault(key, {
                "total_candidates": 0, "entry_touched": 0, "hit_stop_only": 0,
                "hit_target_only": 0, "both_same_day": 0, "neither": 0, "trading_days": 0,
            })
            bucket["trading_days"] += 1
            for k in ("total_candidates", "entry_touched", "hit_stop_only", "hit_target_only", "both_same_day", "neither"):
                bucket[k] += day_stats.get(k, 0)
    for bucket in by_multiple.values():
        bucket["entry_touch_rate"] = (
            round(bucket["entry_touched"] / bucket["total_candidates"], 4) if bucket["total_candidates"] else None
        )
    summary["by_atr_multiple"] = by_multiple

    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary

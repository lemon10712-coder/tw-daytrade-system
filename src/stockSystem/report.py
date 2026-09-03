"""每日報告組裝（規劃書第 9 節）+ 行為紀律檢查（第 9.1 節）+ 出報告前的品質自我檢查。

這個檔案是所有「防幻覺」原則真正落地執行的地方：
- 資料只要帶有 SYNTHETIC_TEST_DATA 標記，報告標題就會被強制改成測試模式，不會被誤認成正式報告。
- 沒通過回測驗證的候選股，一律歸類到「觀察中／尚未驗證」區塊，不混進正式建議。
- 任何數字都附上來源與時間戳記。
"""

from __future__ import annotations

import datetime as dt

from stockSystem.backtest import GateResult
from stockSystem.data_sources import MarketSnapshot
from stockSystem.position_sizing import ComboPlan


def self_check(snapshot: MarketSnapshot) -> list[str]:
    """出報告前的資料品質檢查清單（規劃書第 6.4 節 / 8 節呼應的防呆機制）。
    回傳發現的問題清單；呼叫端看到非空清單時，應該在報告最上方顯著提醒使用者。
    """
    issues = []
    if snapshot.ohlcv.isnull().values.any():
        issues.append("股價資料存在缺漏值")
    if (snapshot.ohlcv["close"] <= 0).any():
        issues.append("發現收盤價 <= 0 的異常資料")
    if (snapshot.ohlcv["volume"] < 0).any():
        issues.append("發現成交量為負的異常資料")
    if snapshot.is_synthetic():
        issues.append("本次資料來源為測試用合成資料（SYNTHETIC_TEST_DATA），非真實市場資料")
    return issues


def behavior_checklist(
    recent_live_win_streak: int,
    any_stop_loss_hit: bool,
    high_discussion_no_validation: list,
    no_validated_candidates_today: bool,
    extreme_move_candidates: list,
) -> list[str]:
    """規劃書 9.1 節「行為紀律檢查」——依系統目前狀態動態觸發，不是罐頭式警語。"""
    reminders = []
    if recent_live_win_streak >= 3:
        reminders.append(
            f"【過度自信提醒】近期已連續{recent_live_win_streak}筆判斷正確，"
            "這不代表接下來會持續，不建議因此放大部位。"
        )
    if any_stop_loss_hit:
        reminders.append(
            "【損失厭惡提醒】有部位已觸及止損參考價，規則要求出場；"
            "『再等一下應該會漲回來』是最容易讓小虧損變大虧損的心理陷阱之一。"
        )
    for name in high_discussion_no_validation:
        reminders.append(f"【社會認同提醒】{name} 討論度高，但尚未通過系統驗證門檻，討論度高不等於符合條件。")
    if no_validated_candidates_today:
        reminders.append(
            "【行動偏誤提醒】今天沒有通過驗證門檻的候選股，最誠實的做法是不出手；"
            "為了『想操作』而勉強找標的，本身就是常見的虧損來源。"
        )
    for name in extreme_move_candidates:
        reminders.append(f"【錯失恐懼提醒】{name} 漲幅已相當極端，此時追進場的風險報酬比通常已經變差。")
    return reminders


def format_intl_summary(intl_snapshot: dict) -> str:
    if not intl_snapshot:
        return "（本次無國際情勢資料）"
    parts = [f"{k} {v:+.2f}%" for k, v in intl_snapshot.items()]
    sentiment = "偏多" if sum(intl_snapshot.values()) > 1.0 else ("偏空" if sum(intl_snapshot.values()) < -1.0 else "中性")
    return "、".join(parts) + f" → 總經氣氛：{sentiment}"


def render_daily_report(
    as_of: dt.date,
    snapshot: MarketSnapshot,
    strongest_sectors: list,
    weakest_sectors: list,
    long_candidates: list,
    short_candidates: list,
    gate_results: dict,          # {candidate.stock_id: GateResult}
    combos: list,
    issues: list,
    long_entry_exit: dict | None = None,   # {stock_id: EntryExitPlan}
    short_entry_exit: dict | None = None,
) -> str:
    lines = []
    is_test = snapshot.is_synthetic()

    title = "【測試模式｜非真實市場資料】台股當沖每日報告" if is_test else "台股當沖每日報告"
    lines.append(f"# {title}")
    lines.append(f"資料日期: {as_of.isoformat()}　資料來源標記: {snapshot.source_tag.value}")
    lines.append("")

    if issues:
        lines.append("## ⚠️ 資料品質檢查發現以下問題，請先留意")
        for issue in issues:
            lines.append(f"- {issue}")
        lines.append("")

    lines.append("## 1. 國際情勢摘要")
    lines.append(format_intl_summary(snapshot.intl_snapshot))
    lines.append("")

    lines.append("## 2. 族群強度排行榜")
    lines.append("### 最強族群")
    for s in strongest_sectors[:5]:
        lines.append(
            f"- {s.sector}：相對強度 {s.relative_strength_pct:+.2%}，廣度 {s.breadth:.0%}，"
            f"量能擴張 {s.volume_expansion:.2f}倍，國際情勢修正 {s.macro_adjustment:+.2f}"
        )
    lines.append("### 最弱族群")
    for s in weakest_sectors[:5]:
        lines.append(
            f"- {s.sector}：相對強度 {s.relative_strength_pct:+.2%}，廣度 {s.breadth:.0%}，"
            f"量能擴張 {s.volume_expansion:.2f}倍"
        )
    lines.append("")

    def render_candidates(title: str, candidates: list, entry_exit_map: dict) -> None:
        lines.append(f"## {title}")
        if not candidates:
            lines.append("（今日無符合條件的候選股）")
            lines.append("")
            return
        # 進出場的資料限制警語（日線近似VWAP、無法用日資料算開盤區間等）每一檔都完全一樣，
        # 不是個股專屬的分析——之前每一檔候選股下面都完整重複這段警語，使用者反映看起來
        # 像是「每一檔的推薦理由都是這段罐頭文字」。改成只在本節開頭講一次，個股底下的
        # 進出場數字只留一個簡短的星號註記指回這裡，個股專屬的分析改看「見解」那一行
        # （見 stock_screener._narrate）。
        sample_ee = next((entry_exit_map.get(c.stock_id) for c in candidates if entry_exit_map.get(c.stock_id)), None)
        if sample_ee:
            lines.append(f"> ＊進出場價位的共同限制說明：{sample_ee.caveat}")
            lines.append("")
        for c in candidates:
            gate = gate_results.get(c.stock_id)
            tier = gate.confidence_tier if gate else "unvalidated"
            tier_label = {
                "unvalidated": "尚未驗證，僅供觀察",
                "observing": "觀察中(樣本不足以晉升)",
                "medium": "中信心",
                "high": "高信心",
            }.get(tier, tier)
            lines.append(f"### {c.stock_id}　{c.name}（{c.sector}，信心：{tier_label}）")
            lines.append(f"- 參考價: {c.price:.2f}　分數: {c.score}")
            lines.append(f"- 見解: {c.narrative}")
            lines.append(f"- 依據標籤: {'; '.join(c.reasons)}")
            if gate:
                lines.append(f"- 驗證狀態: {'; '.join(gate.reasons)}")
            ee = entry_exit_map.get(c.stock_id)
            if ee:
                lines.append(
                    f"- 進場參考: {ee.entry_reference:.2f}（日線近似VWAP，見本節開頭＊）"
                )
                lines.append(f"- 止損參考: {ee.stop_price:.2f}（{ee.stop_basis}）")
                lines.append(f"- 停利參考: {ee.target_price:.2f}（{ee.target_basis}）")
            lines.append("")

    render_candidates("3. 多方候選清單", long_candidates, long_entry_exit or {})
    render_candidates("4. 空方候選清單", short_candidates, short_entry_exit or {})

    lines.append("## 5. 資金配置建議組合")
    if not combos:
        lines.append("（無可行組合，可能是候選股皆超出你的額度或無合格候選股）")
    all_ee = {**(long_entry_exit or {}), **(short_entry_exit or {})}
    for combo in combos:
        lines.append(f"### {combo.label}　使用 {combo.total_cost:,.0f} 元／剩餘 {combo.remaining:,.0f} 元")
        for line in combo.lines:
            loss_str = ""
            ee = all_ee.get(line.stock_id)
            if ee:
                max_loss = ee.max_loss_for_lots(line.lots)
                loss_str = f"，若觸及止損參考價最大預計虧損約 {max_loss:,.0f} 元（未計手續費稅金）"
            lines.append(
                f"- {line.stock_id}（{line.direction}）：{line.lots} 張 @ {line.price:.2f}，成本 {line.cost:,.0f} 元{loss_str}"
            )
    lines.append("")

    lines.append("## 6. 名詞小教室")
    lines.append("見專案文件《台股當沖選股系統_規劃書》附錄名詞小辭典。")

    return "\n".join(lines)

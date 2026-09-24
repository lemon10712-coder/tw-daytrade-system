import datetime as dt

from stockSystem import backtest_tracker as bt
from stockSystem.backtest import GateResult
from stockSystem.data_sources import FixtureProvider
from stockSystem.entry_exit import compute_entry_exit
from stockSystem.position_sizing import build_all_combos
from stockSystem.report import behavior_checklist, format_breadth_summary, render_daily_report, self_check
from stockSystem.sector_strength import compute_sector_scores, rank_sectors
from stockSystem.stock_screener import screen_sector


def test_self_check_flags_synthetic_data():
    snapshot = FixtureProvider().get_snapshot(dt.date.today())
    issues = self_check(snapshot)
    assert any("SYNTHETIC_TEST_DATA" in i for i in issues)


def test_report_title_marks_test_mode_when_data_is_synthetic():
    """最關鍵的防幻覺測試：只要資料是合成的，報告標題就必須明確標示測試模式，
    不能讓使用者誤以為這是可以拿去下單的正式報告。
    """
    snapshot = FixtureProvider(seed=1).get_snapshot(dt.date.today())
    scores = compute_sector_scores(snapshot.ohlcv, {3: 0.0, 5: 0.0, 10: 0.0})
    strongest, weakest = rank_sectors(scores)
    long_c, _ = screen_sector(snapshot, strongest[0].sector, "long")
    combos = build_all_combos(long_c)
    report = render_daily_report(
        as_of=dt.date.today(),
        snapshot=snapshot,
        strongest_sectors=strongest,
        weakest_sectors=weakest,
        long_candidates=long_c,
        short_candidates=[],
        gate_results={},
        combos=combos,
        issues=self_check(snapshot),
    )
    assert "測試模式" in report.splitlines()[0]
    assert "非真實市場資料" in report.splitlines()[0]


def test_unvalidated_candidates_are_labeled_not_hidden():
    snapshot = FixtureProvider(seed=1).get_snapshot(dt.date.today())
    scores = compute_sector_scores(snapshot.ohlcv, {3: 0.0, 5: 0.0, 10: 0.0})
    strongest, _ = rank_sectors(scores)
    long_c, _ = screen_sector(snapshot, strongest[0].sector, "long")
    gate_results = {c.stock_id: GateResult(passed=False, confidence_tier="unvalidated", reasons=["尚無真實歷史資料"]) for c in long_c}
    report = render_daily_report(
        as_of=dt.date.today(),
        snapshot=snapshot,
        strongest_sectors=strongest,
        weakest_sectors=[],
        long_candidates=long_c,
        short_candidates=[],
        gate_results=gate_results,
        combos=[],
        issues=[],
    )
    if long_c:
        assert "尚未驗證，僅供觀察" in report


def test_report_includes_entry_stop_target_and_caveat_when_provided():
    """呼應使用者要求：報告要有具體進場/止損/停利數字，而且限制說明不能被省略。"""
    snapshot = FixtureProvider(seed=1).get_snapshot(dt.date.today())
    scores = compute_sector_scores(snapshot.ohlcv, {3: 0.0, 5: 0.0, 10: 0.0})
    strongest, weakest = rank_sectors(scores)
    long_c, _ = screen_sector(snapshot, strongest[0].sector, "long")

    long_ee = {}
    for c in long_c:
        row = snapshot.ohlcv.loc[c.stock_id]
        long_ee[c.stock_id] = compute_entry_exit(
            stock_id=c.stock_id,
            direction="long",
            prev_close=float(row["prev_close"]),
            open_price=float(row["open"]),
            high=float(row["high"]),
            low=float(row["low"]),
            close=float(row["close"]),
            close_hist=row["close_hist"],
        )

    report = render_daily_report(
        as_of=dt.date.today(),
        snapshot=snapshot,
        strongest_sectors=strongest,
        weakest_sectors=weakest,
        long_candidates=long_c,
        short_candidates=[],
        gate_results={},
        combos=build_all_combos(long_c),
        issues=self_check(snapshot),
        long_entry_exit=long_ee,
        short_entry_exit={},
    )
    if long_c:
        assert "進場參考" in report
        assert "止損參考" in report
        assert "停利參考" in report
        assert "VWAP" in report and "近似" in report


def test_report_includes_backtest_review_section_when_provided():
    """呼應使用者要求：「請妳回測」、「以後都要自動回測」——上一交易日候選股的真實觸價結果
    要出現在報告裡，不用使用者自己每次人工查一次。"""
    snapshot = FixtureProvider(seed=1).get_snapshot(dt.date.today())
    scores = compute_sector_scores(snapshot.ohlcv, {3: 0.0, 5: 0.0, 10: 0.0})
    strongest, weakest = rank_sectors(scores)
    long_c, _ = screen_sector(snapshot, strongest[0].sector, "long")

    outcome = bt.evaluate_outcome(
        {"stock_id": "1303", "name": "南亞", "direction": "long",
         "entry_reference": 239.17, "stop_price": 228.28, "target_price": 260.94},
        actual_open=243.0, actual_high=244.5, actual_low=219.5, actual_close=221.0,
    )

    report = render_daily_report(
        as_of=dt.date.today(),
        snapshot=snapshot,
        strongest_sectors=strongest,
        weakest_sectors=weakest,
        long_candidates=long_c,
        short_candidates=[],
        gate_results={},
        combos=build_all_combos(long_c),
        issues=self_check(snapshot),
        backtest_review=[outcome],
        backtest_summary={"cumulative": {
            "sample_trading_days": 1, "total_candidates": 1, "entry_touched": 1,
            "entry_touch_rate": 1.0, "hit_stop_only": 1, "hit_target_only": 0,
            "both_same_day": 0, "neither": 0,
        }},
    )
    assert "回測：上一交易日候選股表現" in report
    assert "南亞" in report and "有觸及" in report
    assert "止損參考價" in report
    assert "累積統計" in report


def test_report_backtest_section_has_honest_placeholder_when_no_review_available():
    snapshot = FixtureProvider(seed=1).get_snapshot(dt.date.today())
    scores = compute_sector_scores(snapshot.ohlcv, {3: 0.0, 5: 0.0, 10: 0.0})
    strongest, weakest = rank_sectors(scores)
    long_c, _ = screen_sector(snapshot, strongest[0].sector, "long")

    report = render_daily_report(
        as_of=dt.date.today(),
        snapshot=snapshot,
        strongest_sectors=strongest,
        weakest_sectors=weakest,
        long_candidates=long_c,
        short_candidates=[],
        gate_results={},
        combos=build_all_combos(long_c),
        issues=self_check(snapshot),
    )
    assert "回測：上一交易日候選股表現" in report
    assert "尚無上一交易日的候選股記錄" in report


def test_behavior_checklist_triggers_overconfidence_reminder():
    reminders = behavior_checklist(
        recent_live_win_streak=4,
        any_stop_loss_hit=False,
        high_discussion_no_validation=[],
        no_validated_candidates_today=False,
        extreme_move_candidates=[],
    )
    assert any("過度自信" in r for r in reminders)


def test_behavior_checklist_triggers_action_bias_reminder_when_no_candidates():
    reminders = behavior_checklist(
        recent_live_win_streak=0,
        any_stop_loss_hit=False,
        high_discussion_no_validation=[],
        no_validated_candidates_today=True,
        extreme_move_candidates=[],
    )
    assert any("行動偏誤" in r for r in reminders)


def test_behavior_checklist_empty_when_nothing_triggered():
    reminders = behavior_checklist(
        recent_live_win_streak=0,
        any_stop_loss_hit=False,
        high_discussion_no_validation=[],
        no_validated_candidates_today=False,
        extreme_move_candidates=[],
    )
    assert reminders == []


def test_format_breadth_summary_reports_unavailable_when_none():
    assert "無法計算" in format_breadth_summary(None)


def test_format_breadth_summary_reports_unavailable_when_nan():
    assert "無法計算" in format_breadth_summary(float("nan"))


def test_format_breadth_summary_flags_risk_off_when_scale_below_one():
    text = format_breadth_summary(0.25, window=60, threshold=0.40, risk_scale=0.5)
    assert "25%" in text
    assert "風控" in text and "50%" in text


def test_format_breadth_summary_notes_no_trigger_when_scale_is_one():
    text = format_breadth_summary(0.80, window=60, threshold=0.40, risk_scale=1.0)
    assert "未觸發" in text


def test_report_includes_breadth_summary_when_provided():
    """2026-09-24 新增：呼應使用者核准的「大盤廣度」風控開關——報告要能顯示廣度狀態，
    不是算完就丟掉。"""
    snapshot = FixtureProvider(seed=1).get_snapshot(dt.date.today())
    scores = compute_sector_scores(snapshot.ohlcv, {3: 0.0, 5: 0.0, 10: 0.0})
    strongest, weakest = rank_sectors(scores)
    long_c, _ = screen_sector(snapshot, strongest[0].sector, "long")

    report = render_daily_report(
        as_of=dt.date.today(),
        snapshot=snapshot,
        strongest_sectors=strongest,
        weakest_sectors=weakest,
        long_candidates=long_c,
        short_candidates=[],
        gate_results={},
        combos=build_all_combos(long_c),
        issues=self_check(snapshot),
        breadth_pct=0.25,
        breadth_window=60,
        breadth_threshold=0.40,
        breadth_risk_scale=0.5,
    )
    assert "大盤環境摘要" in report
    assert "25%" in report
    assert "風控" in report


def test_report_breadth_section_has_honest_placeholder_when_not_provided():
    snapshot = FixtureProvider(seed=1).get_snapshot(dt.date.today())
    scores = compute_sector_scores(snapshot.ohlcv, {3: 0.0, 5: 0.0, 10: 0.0})
    strongest, weakest = rank_sectors(scores)
    long_c, _ = screen_sector(snapshot, strongest[0].sector, "long")

    report = render_daily_report(
        as_of=dt.date.today(),
        snapshot=snapshot,
        strongest_sectors=strongest,
        weakest_sectors=weakest,
        long_candidates=long_c,
        short_candidates=[],
        gate_results={},
        combos=build_all_combos(long_c),
        issues=self_check(snapshot),
    )
    assert "無法計算大盤廣度" in report

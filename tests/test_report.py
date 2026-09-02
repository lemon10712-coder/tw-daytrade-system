import datetime as dt

from stockSystem.backtest import GateResult
from stockSystem.data_sources import FixtureProvider
from stockSystem.entry_exit import compute_entry_exit
from stockSystem.position_sizing import build_all_combos
from stockSystem.report import behavior_checklist, render_daily_report, self_check
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

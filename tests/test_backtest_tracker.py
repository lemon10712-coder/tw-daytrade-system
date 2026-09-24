import datetime as dt

from stockSystem import backtest_tracker as bt


class _FakeEntryExit:
    def __init__(self, prev_close, entry, stop, target):
        self.prev_close = prev_close
        self.entry_reference = entry
        self.stop_price = stop
        self.target_price = target


class _FakeCandidate:
    def __init__(self, stock_id, name, direction):
        self.stock_id = stock_id
        self.name = name
        self.direction = direction


def test_evaluate_outcome_matches_real_1303_case_2026_09_03():
    """用 2026-09-03 南亞(1303) 的真實案例鎖住邏輯：開243/高244.5/低219.5/收221，
    進場239.17有觸及、且當天最低價跌破止損228.28（南亞當天確實被巴出場）。"""
    record = {
        "stock_id": "1303", "name": "南亞", "direction": "long",
        "prev_close": 237.50, "entry_reference": 239.17,
        "stop_price": 228.28, "target_price": 260.94,
    }
    outcome = bt.evaluate_outcome(record, actual_open=243.0, actual_high=244.5, actual_low=219.5, actual_close=221.0)
    assert outcome.entry_touched is True
    assert "止損參考價" in outcome.result
    assert "同一天" not in outcome.result


def test_evaluate_outcome_entry_not_touched():
    record = {
        "stock_id": "2812", "name": "台中銀", "direction": "long",
        "prev_close": 20.0, "entry_reference": 20.5,
        "stop_price": 19.5, "target_price": 22.5,
    }
    # 開盤即跳空走高，全天都沒拉回到進場參考價
    outcome = bt.evaluate_outcome(record, actual_open=21.5, actual_high=22.0, actual_low=21.2, actual_close=21.8)
    assert outcome.entry_touched is False
    assert "未觸及" in outcome.result


def test_evaluate_outcome_both_stop_and_target_same_day_is_labeled_ambiguous():
    record = {
        "stock_id": "9999", "name": "測試", "direction": "long",
        "prev_close": 100.0, "entry_reference": 100.0,
        "stop_price": 95.0, "target_price": 105.0,
    }
    outcome = bt.evaluate_outcome(record, actual_open=100.0, actual_high=106.0, actual_low=94.0, actual_close=100.0)
    assert outcome.entry_touched is True
    assert "無法判斷實際先後順序" in outcome.result


def test_evaluate_outcome_short_direction_uses_mirrored_conditions():
    record = {
        "stock_id": "1111", "name": "測試空", "direction": "short",
        "prev_close": 50.0, "entry_reference": 50.0,
        "stop_price": 53.0, "target_price": 44.0,
    }
    outcome = bt.evaluate_outcome(record, actual_open=50.0, actual_high=53.5, actual_low=48.0, actual_close=49.0)
    assert outcome.entry_touched is True
    assert "止損參考價" in outcome.result


def test_save_and_load_candidates_roundtrip(tmp_path):
    candidates = [_FakeCandidate("1303", "南亞", "long")]
    ee_map = {"1303": _FakeEntryExit(237.50, 239.17, 228.28, 260.94)}
    as_of = dt.date(2026, 9, 3)

    bt.save_candidates(tmp_path, as_of, candidates, ee_map)
    loaded = bt.load_candidates(tmp_path, as_of)

    assert len(loaded) == 1
    assert loaded[0]["stock_id"] == "1303"
    assert loaded[0]["entry_reference"] == 239.17


def test_load_candidates_returns_empty_list_when_no_record_exists(tmp_path):
    assert bt.load_candidates(tmp_path, dt.date(2099, 1, 1)) == []


def test_find_last_recorded_date_skips_dates_on_or_after_before(tmp_path):
    candidates = [_FakeCandidate("1303", "南亞", "long")]
    ee_map = {"1303": _FakeEntryExit(237.50, 239.17, 228.28, 260.94)}
    bt.save_candidates(tmp_path, dt.date(2026, 9, 1), candidates, ee_map)
    bt.save_candidates(tmp_path, dt.date(2026, 9, 3), candidates, ee_map)

    assert bt.find_last_recorded_date(tmp_path, dt.date(2026, 9, 4)) == dt.date(2026, 9, 3)
    assert bt.find_last_recorded_date(tmp_path, dt.date(2026, 9, 2)) == dt.date(2026, 9, 1)
    assert bt.find_last_recorded_date(tmp_path, dt.date(2026, 9, 1)) is None


def test_find_last_recorded_date_returns_none_when_no_history(tmp_path):
    assert bt.find_last_recorded_date(tmp_path, dt.date(2026, 9, 3)) is None


def test_append_summary_accumulates_cumulative_counts_across_days(tmp_path):
    outcome_touch = bt.evaluate_outcome(
        {"stock_id": "1303", "name": "南亞", "direction": "long",
         "entry_reference": 239.17, "stop_price": 228.28, "target_price": 260.94},
        actual_open=243.0, actual_high=244.5, actual_low=219.5, actual_close=221.0,
    )
    outcome_no_touch = bt.evaluate_outcome(
        {"stock_id": "2812", "name": "台中銀", "direction": "long",
         "entry_reference": 20.5, "stop_price": 19.5, "target_price": 22.5},
        actual_open=21.5, actual_high=22.0, actual_low=21.2, actual_close=21.8,
    )

    bt.append_summary(tmp_path, dt.date(2026, 9, 2), [outcome_no_touch])
    summary = bt.append_summary(tmp_path, dt.date(2026, 9, 3), [outcome_touch])

    assert summary["cumulative"]["total_candidates"] == 2
    assert summary["cumulative"]["entry_touched"] == 1
    assert summary["cumulative"]["sample_trading_days"] == 2


def test_append_summary_is_idempotent_for_same_date(tmp_path):
    outcome = bt.evaluate_outcome(
        {"stock_id": "1303", "name": "南亞", "direction": "long",
         "entry_reference": 239.17, "stop_price": 228.28, "target_price": 260.94},
        actual_open=243.0, actual_high=244.5, actual_low=219.5, actual_close=221.0,
    )
    bt.append_summary(tmp_path, dt.date(2026, 9, 3), [outcome])
    summary = bt.append_summary(tmp_path, dt.date(2026, 9, 3), [outcome])

    assert summary["cumulative"]["total_candidates"] == 1


class _FakeEntryExitWithMultiple(_FakeEntryExit):
    def __init__(self, prev_close, entry, stop, target, atr_multiple):
        super().__init__(prev_close, entry, stop, target)
        self.atr_multiple = atr_multiple


def test_save_candidates_records_atr_multiple_when_present(tmp_path):
    """2026-09-24 新增：呼應使用者核准的「重新檢視ATR停損倍數」需求——記錄每一筆用的
    倍數，未來才能依倍數分組比較真實表現。"""
    candidates = [_FakeCandidate("1303", "南亞", "long")]
    ee_map = {"1303": _FakeEntryExitWithMultiple(237.50, 239.17, 228.28, 260.94, atr_multiple=1.2)}
    as_of = dt.date(2026, 9, 3)

    bt.save_candidates(tmp_path, as_of, candidates, ee_map)
    loaded = bt.load_candidates(tmp_path, as_of)

    assert loaded[0]["atr_multiple"] == 1.2


def test_save_candidates_defaults_atr_multiple_to_none_when_ee_lacks_attribute(tmp_path):
    """向後相容：傳進來的 entry_exit 物件（例如舊呼叫端或簡化測試假物件）沒有 atr_multiple
    屬性時，不應該讓整個存檔動作報錯，應該老實存 None。"""
    candidates = [_FakeCandidate("1303", "南亞", "long")]
    ee_map = {"1303": _FakeEntryExit(237.50, 239.17, 228.28, 260.94)}  # 沒有 atr_multiple 屬性
    as_of = dt.date(2026, 9, 3)

    bt.save_candidates(tmp_path, as_of, candidates, ee_map)
    loaded = bt.load_candidates(tmp_path, as_of)

    assert loaded[0]["atr_multiple"] is None


def test_evaluate_outcome_carries_atr_multiple_through_from_record():
    record = {
        "stock_id": "1303", "name": "南亞", "direction": "long",
        "prev_close": 237.50, "entry_reference": 239.17,
        "stop_price": 228.28, "target_price": 260.94, "atr_multiple": 1.5,
    }
    outcome = bt.evaluate_outcome(record, actual_open=243.0, actual_high=244.5, actual_low=219.5, actual_close=221.0)
    assert outcome.atr_multiple == 1.5


def test_evaluate_outcome_atr_multiple_defaults_to_none_when_missing_from_record():
    # 呼應既有測試裡不帶 atr_multiple 的 record（見本檔案其他測試），不應該報 KeyError
    record = {
        "stock_id": "9999", "name": "測試", "direction": "long",
        "prev_close": 100.0, "entry_reference": 100.0,
        "stop_price": 95.0, "target_price": 105.0,
    }
    outcome = bt.evaluate_outcome(record, actual_open=100.0, actual_high=101.0, actual_low=99.0, actual_close=100.5)
    assert outcome.atr_multiple is None


def test_append_summary_groups_cumulative_stats_by_atr_multiple(tmp_path):
    """核心驗證：累積統計要能依 ATR 倍數分組，讓使用者/未來的 session 可以比較「用1.2倍
    的那些交易日」跟「用其他倍數的那些交易日」的進場觸價率等指標，而不是只有一個看不出
    倍數影響的總數字。"""
    outcome_1_2 = bt.evaluate_outcome(
        {"stock_id": "1303", "name": "南亞", "direction": "long",
         "entry_reference": 239.17, "stop_price": 228.28, "target_price": 260.94, "atr_multiple": 1.2},
        actual_open=243.0, actual_high=244.5, actual_low=219.5, actual_close=221.0,
    )
    outcome_2_0 = bt.evaluate_outcome(
        {"stock_id": "2812", "name": "台中銀", "direction": "long",
         "entry_reference": 20.5, "stop_price": 19.5, "target_price": 22.5, "atr_multiple": 2.0},
        actual_open=21.5, actual_high=22.0, actual_low=21.2, actual_close=21.8,
    )

    bt.append_summary(tmp_path, dt.date(2026, 9, 2), [outcome_1_2])
    summary = bt.append_summary(tmp_path, dt.date(2026, 9, 3), [outcome_2_0])

    assert summary["by_atr_multiple"]["1.2"]["total_candidates"] == 1
    assert summary["by_atr_multiple"]["1.2"]["entry_touched"] == 1
    assert summary["by_atr_multiple"]["2.0"]["total_candidates"] == 1
    assert summary["by_atr_multiple"]["2.0"]["entry_touched"] == 0

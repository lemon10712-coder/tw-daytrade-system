import datetime as dt

from stockSystem.futures_data import IndexDailyStore


def test_save_and_load_roundtrip(tmp_path):
    store = IndexDailyStore(tmp_path)
    bar = {"as_of": "2026-09-24", "open": 18000.0, "high": 18100.0, "low": 17950.0, "close": 18050.0}
    store.save(dt.date(2026, 9, 24), bar)
    loaded = store.load(dt.date(2026, 9, 24))
    assert loaded == bar


def test_load_returns_none_when_missing(tmp_path):
    store = IndexDailyStore(tmp_path)
    assert store.load(dt.date(2099, 1, 1)) is None


def test_load_recent_skips_missing_dates_and_orders_oldest_to_newest(tmp_path):
    store = IndexDailyStore(tmp_path)
    for day, close in [(1, 100.0), (2, 101.0), (4, 103.0)]:
        store.save(dt.date(2026, 9, day), {"as_of": f"2026-09-0{day}", "open": close, "high": close,
                                            "low": close, "close": close})
    history = store.load_recent(dt.date(2026, 9, 4), lookback_days=10)
    assert [h["close"] for h in history] == [100.0, 101.0, 103.0]


def test_load_recent_respects_lookback_days_limit(tmp_path):
    store = IndexDailyStore(tmp_path)
    for day in range(1, 6):
        store.save(dt.date(2026, 9, day), {"as_of": f"2026-09-0{day}", "open": day, "high": day,
                                            "low": day, "close": day})
    history = store.load_recent(dt.date(2026, 9, 5), lookback_days=2)
    assert len(history) == 2
    assert [h["close"] for h in history] == [4, 5]

import datetime as dt

import pytest

from stockSystem.data_sources import DataValidationError
from stockSystem.futures_data import IndexDailyStore, _fetch_twse_index_ohlc, _roc_date_str


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


def test_roc_date_str_converts_western_year_to_minguo():
    assert _roc_date_str(dt.date(2026, 8, 3)) == "115/08/03"
    assert _roc_date_str(dt.date(2026, 9, 24)) == "115/09/24"


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _FakeSession:
    """模擬 requests.Session，回傳寫死的 payload，不對外發送任何真實請求。"""

    def __init__(self, payload):
        self._payload = payload
        self.last_url = None

    def get(self, url, timeout=30, headers=None):
        self.last_url = url
        return _FakeResponse(self._payload)


_REAL_MONTH_PAYLOAD = {
    "stat": "OK",
    "title": "115年08月 發行量加權股價指數歷史資料",
    "date": "20260801",
    "fields": ["日期", "開盤指數", "最高指數", "最低指數", "收盤指數"],
    "data": [
        ["115/08/03", "42,780.42", "43,784.19", "42,780.42", "43,386.41"],
        ["115/08/04", "43,400.00", "43,900.50", "43,100.10", "43,850.25"],
    ],
    "total": 2,
}


def test_fetch_twse_index_ohlc_picks_matching_row_from_whole_month_response():
    """2026-09-24 真實 CI 執行後修正：這個端點一次回傳整個月的資料，不是單日一筆，
    這裡鎖住「必須從整月資料裡挑出目標日期那一列，不是隨便取最後一列」這個行為，
    也驗證千分位逗號的數字會被正確轉換成 float。"""
    session = _FakeSession(_REAL_MONTH_PAYLOAD)
    bar = _fetch_twse_index_ohlc(session, dt.date(2026, 8, 4))
    assert bar == {"as_of": "2026-08-04", "open": 43400.00, "high": 43900.50, "low": 43100.10, "close": 43850.25}


def test_fetch_twse_index_ohlc_returns_none_when_date_not_in_month_data():
    session = _FakeSession(_REAL_MONTH_PAYLOAD)
    assert _fetch_twse_index_ohlc(session, dt.date(2026, 8, 15)) is None  # 週末，不在資料裡


def test_fetch_twse_index_ohlc_returns_none_when_stat_not_ok():
    session = _FakeSession({"stat": "查詢日期大於今日，請重新查詢!"})
    assert _fetch_twse_index_ohlc(session, dt.date(2099, 1, 1)) is None


def test_fetch_twse_index_ohlc_raises_clear_error_when_fields_missing():
    session = _FakeSession({"stat": "OK"})
    with pytest.raises(DataValidationError):
        _fetch_twse_index_ohlc(session, dt.date(2026, 8, 4))

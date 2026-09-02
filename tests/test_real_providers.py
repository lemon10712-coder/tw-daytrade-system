"""real_providers.py 的測試。因為這個沙盒連不到真實的 TWSE/TPEx 端點（見 KNOWN_ISSUES.md），
這裡全部用假的 HTTP session（回傳寫死的、符合「文件記載欄位名稱」的 JSON）來測試解析邏輯本身
是否正確、以及「找不到欄位就清楚報錯」這個防呆機制是否真的有效——**不是**用來證明真實端點的
欄位名稱一定是這樣，那件事只有在 GitHub Actions 真正執行一次之後才能確認。
"""

from __future__ import annotations

import datetime as dt
import json

import pytest

from stockSystem.data_sources import DataValidationError
from stockSystem.real_providers import (
    DailySnapshotStore,
    _fetch_institutional_flow_with_lookback,
    _find_key,
    _parse_market_rows,
    _rows_from_fields_data,
    _to_float,
    _to_int,
    build_snapshot,
)


def test_find_key_matches_all_tokens():
    row = {"Code": "2330", "ClosingPrice": "2385.0000"}
    assert _find_key(row, "Code") == "Code"
    assert _find_key(row, "Closing") == "ClosingPrice"


def test_find_key_raises_with_diagnostic_when_missing():
    row = {"Foo": "bar"}
    with pytest.raises(DataValidationError) as exc_info:
        _find_key(row, "Code")
    assert "Foo" in str(exc_info.value)  # 錯誤訊息裡要看得到「目前實際有的欄位」


def test_to_float_handles_placeholder_values():
    assert _to_float("2,385.00") == 2385.0
    assert _to_float("--") != _to_float("--")  # nan != nan
    assert _to_float(None) != _to_float(None)


def test_to_int_rounds_down_from_float_string():
    assert _to_int("19783.0") == 19783


def _fake_twse_row(code, close, volume_shares=19783000):
    return {
        "Code": code,
        "Name": "測試股",
        "TradeVolume": str(volume_shares),
        "OpeningPrice": str(close - 5),
        "HighestPrice": str(close + 5),
        "LowestPrice": str(close - 10),
        "ClosingPrice": str(close),
        "Change": "5.0000",
    }


def test_parse_market_rows_converts_volume_shares_to_lots():
    rows = [_fake_twse_row("2330", 2385.0, volume_shares=19783000)]
    parsed = _parse_market_rows(rows, "TWSE")
    assert parsed["2330"]["close"] == 2385.0
    assert parsed["2330"]["volume"] == pytest.approx(19783.0)  # 股 -> 張，除以1000


def test_build_snapshot_accumulates_multi_day_close_history(tmp_path):
    store = DailySnapshotStore(tmp_path)
    dates = [dt.date(2026, 9, 1), dt.date(2026, 9, 2)]
    closes = {dates[0]: 100.0, dates[1]: 105.0}
    for d in dates:
        snap = {
            "as_of": d.isoformat(),
            "twse_daily": [_fake_twse_row("2330", closes[d])],
            "tpex_daily": [],
            "twse_industry": [{"公司代號": "2330", "產業別": "半導體業"}],
            "twse_institutional": None,
            "twse_margin": None,
            "twse_watch": None,
            "twse_disposition": None,
        }
        store.save(d, snap)

    history = store.load_recent(dates[-1], lookback_days=60)
    assert len(history) == 2

    snapshot = build_snapshot(dates[-1], history)
    assert "2330" in snapshot.ohlcv.index
    row = snapshot.ohlcv.loc["2330"]
    assert list(row["close_hist"]) == pytest.approx([100.0, 105.0])
    assert row["sector"] == "半導體業"
    assert row["prev_close"] == pytest.approx(100.0)  # 用歷史序列的前一天，不是用 Change 反推


def test_build_snapshot_raises_when_last_history_entry_is_not_as_of(tmp_path):
    store = DailySnapshotStore(tmp_path)
    d = dt.date(2026, 9, 1)
    store.save(d, {"as_of": d.isoformat(), "twse_daily": [], "tpex_daily": []})
    history = store.load_recent(d, lookback_days=10)
    with pytest.raises(DataValidationError):
        build_snapshot(dt.date(2026, 9, 2), history)  # as_of 跟 history 最後一筆對不上


def test_daily_snapshot_store_round_trip(tmp_path):
    store = DailySnapshotStore(tmp_path)
    d = dt.date(2026, 9, 2)
    store.save(d, {"as_of": d.isoformat(), "hello": "world"})
    loaded = store.load(d)
    assert loaded["hello"] == "world"
    assert store.load(dt.date(2026, 1, 1)) is None  # 沒存過的日期回傳 None，不是拋例外


def test_rows_from_fields_data_converts_twse_legacy_format_to_object_array():
    """2026-09-02 第一次在 GitHub Actions 真正執行時發現：三大法人買賣超（T86）不在
    openapi.twse.com.tw 上，要打證交所舊系統 www.twse.com.tw/rwd/zh/fund/T86，
    回傳格式是 {"stat","fields","data"}（欄位名跟資料分開兩個陣列），不是其他端點
    那種「每筆資料是一個物件」的格式，這個測試鎖住轉換邏輯不能再壞掉。
    """
    raw = {
        "stat": "OK",
        "fields": ["證券代號", "證券名稱", "三大法人買賣超股數"],
        "data": [["2330", "台積電", "12345"], ["2454", "聯發科", "-6789"]],
    }
    rows = _rows_from_fields_data(raw, "TWSE 三大法人買賣超(T86)")
    assert rows == [
        {"證券代號": "2330", "證券名稱": "台積電", "三大法人買賣超股數": "12345"},
        {"證券代號": "2454", "證券名稱": "聯發科", "三大法人買賣超股數": "-6789"},
    ]


def test_rows_from_fields_data_raises_clear_error_when_stat_not_ok():
    """stat 不是 "OK"（例如非交易日、資料還沒公布）時要清楚報錯，讓呼叫端當成
    「今天沒抓到」處理，而不是悄悄回傳空清單假裝三大法人今天沒買賣超。
    """
    raw = {"stat": "很抱歉，沒有符合條件的資料!", "fields": [], "data": []}
    with pytest.raises(DataValidationError) as exc_info:
        _rows_from_fields_data(raw, "TWSE 三大法人買賣超(T86)")
    assert "stat" in str(exc_info.value)


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _FakeSessionByDate:
    """模擬 T86：只有指定的某幾天有「已公布」的資料，其他天回傳 stat 不是 OK。

    用來測試 `_fetch_institutional_flow_with_lookback` 是否真的會往前找，而不是
    排程在開盤前執行、當天資料還沒公布時就直接放棄。
    """

    def __init__(self, available_dates: set[str]):
        self._available_dates = available_dates
        self.requested_urls: list[str] = []

    def get(self, url, timeout=30, headers=None):
        self.requested_urls.append(url)
        date_str = url.split("date=")[1].split("&")[0]
        if date_str in self._available_dates:
            payload = {
                "stat": "OK",
                "fields": ["證券代號", "三大法人買賣超股數"],
                "data": [["2330", "1000"]],
            }
        else:
            payload = {"stat": "很抱歉，沒有符合條件的資料!", "fields": [], "data": []}
        return _FakeResponse(payload)


def test_fetch_institutional_flow_with_lookback_skips_to_last_published_day():
    """2026-09-03 第二次在 GitHub Actions 真正執行時發現：排程在開盤前(台北時間 08:15)跑，
    當天(as_of)的三大法人資料根本還沒公布，用 as_of 當天查永遠只會拿到「沒資料」。
    這個測試鎖住「自動往前找最近一個已公布資料的交易日」這個行為不能再壞掉。
    """
    as_of = dt.date(2026, 9, 3)  # 當天沒資料
    session = _FakeSessionByDate(available_dates={"20260902"})  # 前一天有資料
    rows = _fetch_institutional_flow_with_lookback(session, as_of, max_lookback_days=7)
    assert rows == [{"證券代號": "2330", "三大法人買賣超股數": "1000"}]
    # 應該先試 as_of 當天(20260903)，沒資料才試前一天(20260902)，不能跳過 as_of 直接查前一天
    assert session.requested_urls[0].endswith("date=20260903&selectType=ALL&response=json")
    assert session.requested_urls[1].endswith("date=20260902&selectType=ALL&response=json")


def test_fetch_institutional_flow_with_lookback_gives_up_after_max_days():
    """連續好幾天都沒有已公布的資料（例如長假）時，要在試完 max_lookback_days 天後放棄回傳
    None，而不是無限往前找卡住整個流程。"""
    as_of = dt.date(2026, 9, 3)
    session = _FakeSessionByDate(available_dates=set())  # 完全沒有任何一天有資料
    rows = _fetch_institutional_flow_with_lookback(session, as_of, max_lookback_days=3)
    assert rows is None
    assert len(session.requested_urls) == 3


def test_daily_snapshot_store_load_recent_skips_missing_days(tmp_path):
    """遇到沒有存檔的日子（假日、系統中斷）要自動跳過，不能讓歷史序列裡出現「空的一天」。"""
    store = DailySnapshotStore(tmp_path)
    present_dates = [dt.date(2026, 9, 1), dt.date(2026, 9, 3)]  # 9/2 故意沒存
    for d in present_dates:
        store.save(d, {"as_of": d.isoformat()})
    history = store.load_recent(dt.date(2026, 9, 3), lookback_days=60)
    assert [h["as_of"] for h in history] == ["2026-09-01", "2026-09-03"]

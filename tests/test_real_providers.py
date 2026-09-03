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
    _fetch_tpex_day_all,
    _fetch_twse_day_all,
    _find_key,
    _parse_market_rows,
    _rows_from_fields_data,
    _to_float,
    _to_int,
    build_snapshot,
    sector_name_for_code,
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
    assert parsed["2330"]["name"] == "測試股"


def test_parse_market_rows_handles_tpex_openapi_short_english_field_names():
    """2026-09-03 第三次真實環境查證後發現：TPEx 新版 openapi
    （tpex_mainboard_daily_close_quotes，每天正式產生報告時用來抓上櫃股票「今天」資料的
    端點）用的英文欄位名稱是 Open/High/Low/Close/TradingShares/CompanyName 這種短名稱，
    跟 TWSE STOCK_DAY_ALL 的 OpeningPrice/HighestPrice/.../TradeVolume（"-ing"/"-est" 字尾）
    長得很像但其實不一樣——修正前這裡完全沒有處理這組欄位，導致每天的上櫃股票 today 資料
    全部被解析成 nan，這個測試鎖住修正後的行為不能再壞掉。
    """
    row = {
        "Date": "1150902",
        "SecuritiesCompanyCode": "6488",
        "CompanyName": "环球晶",
        "Close": "500.00",
        "Change": "-0.19 ",
        "Open": "495.00",
        "High": "505.00",
        "Low": "490.00",
        "Average": "498.00",
        "TradingShares": "3000000",
        "TransactionAmount": "1500000000",
    }
    parsed = _parse_market_rows([row], "TPEx")
    assert parsed["6488"]["name"] == "环球晶"
    assert parsed["6488"]["open"] == 495.0
    assert parsed["6488"]["high"] == 505.0
    assert parsed["6488"]["low"] == 490.0
    assert parsed["6488"]["close"] == 500.0
    assert parsed["6488"]["volume"] == pytest.approx(3000.0)  # 股 -> 張


def test_sector_name_for_code_resolves_official_names():
    """代碼表是從 TWSE 官方查詢工具 isin.twse.com.tw/isin/class_i.jsp?kind=4 的下拉選單
    對照真實回應驗證過的（見 real_providers.SECTOR_CODE_NAME 的說明），這裡鎖住幾個
    報告裡實際出現過、使用者反映「看不懂」的代碼一定要能正確轉換。
    """
    assert sector_name_for_code("17") == "金融保險業"
    assert sector_name_for_code("24") == "半導體業"
    assert sector_name_for_code("30") == "資訊服務業"
    assert sector_name_for_code("37") == "運動休閒"


def test_sector_name_for_code_falls_back_gracefully_for_unknown_code():
    """代碼表以外的代碼（例如已停用的代碼）不該讓整個解析流程壞掉，也不該假裝成一個
    正常名稱誤導使用者，而是要在顯示的文字裡保留代碼本身方便追查。"""
    assert "代碼99" in sector_name_for_code("99")
    assert sector_name_for_code("") == "未分類"


def test_build_snapshot_accumulates_multi_day_close_history(tmp_path):
    store = DailySnapshotStore(tmp_path)
    dates = [dt.date(2026, 9, 1), dt.date(2026, 9, 2)]
    closes = {dates[0]: 100.0, dates[1]: 105.0}
    for d in dates:
        snap = {
            "as_of": d.isoformat(),
            "twse_daily": [_fake_twse_row("2330", closes[d])],
            "tpex_daily": [],
            # "產業別" 真實回應給的是代碼（見 real_providers.SECTOR_CODE_NAME 的說明），
            # 不是名稱——用真代碼 "24"，斷言解析後的 sector 是對照表轉出來的全名。
            "twse_industry": [{"公司代號": "2330", "產業別": "24"}],
            "tpex_industry": [],
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
    assert row["sector"] == "半導體業"  # 代碼 "24" 轉換後的全名，不是代碼本身
    assert row["name"] == "測試股"  # 來自 _fake_twse_row 的 "Name" 欄位
    assert row["prev_close"] == pytest.approx(100.0)  # 用歷史序列的前一天，不是用 Change 反推
    assert snapshot.industry_map["2330"] == "半導體業"


def test_build_snapshot_resolves_tpex_sector_from_tpex_industry(tmp_path):
    """2026-09-03 第三次真實環境查證後發現：之前只抓 TWSE 的產業別，完全沒抓 TPEx 的，
    導致所有上櫃股票的族群永遠是「未分類」。這個測試鎖住 tpex_industry 也要被用上。
    """
    store = DailySnapshotStore(tmp_path)
    d = dt.date(2026, 9, 2)
    tpex_row = {
        "SecuritiesCompanyCode": "6488",
        "CompanyName": "环球晶",
        "Close": "500.00",
        "Open": "495.00",
        "High": "505.00",
        "Low": "490.00",
        "TradingShares": "3000000",
    }
    store.save(d, {
        "as_of": d.isoformat(),
        "twse_daily": [],
        "tpex_daily": [tpex_row],
        "twse_industry": [],
        "tpex_industry": [{"SecuritiesCompanyCode": "6488", "SecuritiesIndustryCode": "25"}],
        "twse_institutional": None,
        "twse_margin": None,
        "twse_watch": None,
        "twse_disposition": None,
    })
    history = store.load_recent(d, lookback_days=10)
    snapshot = build_snapshot(d, history)
    row = snapshot.ohlcv.loc["6488"]
    assert row["sector"] == "電腦及週邊設備業"
    assert row["name"] == "环球晶"
    assert row["close"] == 500.0  # 也順便鎖住 TPEx 短字尾英文欄位不會被解析成 nan


def test_build_snapshot_converts_institutional_flow_shares_to_lots(tmp_path):
    """2026-09-03 第三次真實環境查證後發現：T86 的買賣超欄位單位是股，但
    MarketSnapshot.institutional_flow 的欄位說明跟 FixtureProvider 的合成資料都是「張」，
    且 stock_screener 的評分公式也是照「張」的量級校準的——這裡少做股->張的換算會讓
    報告上顯示的「三大法人買超」數字誇大1000倍，也會讓法人籌碼分數失真。
    """
    store = DailySnapshotStore(tmp_path)
    d = dt.date(2026, 9, 2)
    store.save(d, {
        "as_of": d.isoformat(),
        "twse_daily": [_fake_twse_row("2330", 100.0)],
        "tpex_daily": [],
        "twse_industry": [],
        "tpex_industry": [],
        "twse_institutional": [
            {
                "代號": "2330",
                "外陸資買賣超股數(不含外資自營商)": "3000000",
                "外資自營商買賣超股數": "500000",
                "投信買賣超股數": "1000000",
                "自營商買賣超股數": "200000",
            }
        ],
        "twse_margin": None,
        "twse_watch": None,
        "twse_disposition": None,
    })
    history = store.load_recent(d, lookback_days=10)
    snapshot = build_snapshot(d, history)
    inst = snapshot.institutional_flow.loc["2330"]
    assert inst["foreign_net"] == pytest.approx(3500.0)  # (3,000,000+500,000)股 -> 3,500張
    assert inst["trust_net"] == pytest.approx(1000.0)
    assert inst["dealer_net"] == pytest.approx(200.0)


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


class _FakeSessionForBackfill:
    """模擬 MI_INDEX（TWSE 指定日期全市場行情）跟 TPEx 舊系統的每日收盤行情，
    用來測試 `backfill_history.py` 用的兩個回補函式（`_fetch_twse_day_all` /
    `_fetch_tpex_day_all`）在正常情況、以及格式不符時的行為。
    """

    def __init__(self, twse_ok_dates: set[str], tpex_ok_dates: set[str], tpex_bad_format_dates: set[str] = frozenset()):
        self._twse_ok_dates = twse_ok_dates
        self._tpex_ok_dates = tpex_ok_dates
        self._tpex_bad_format_dates = tpex_bad_format_dates
        self.requested_urls: list[str] = []

    def get(self, url, timeout=30, headers=None):
        self.requested_urls.append(url)
        if "afterTrading/MI_INDEX" in url:
            date_str = url.split("date=")[1].split("&")[0]
            if date_str in self._twse_ok_dates:
                payload = {
                    "stat": "OK",
                    "tables": [
                        {
                            "title": f"{date_str} 價格指數(臺灣證券交易所)",
                            "fields": ["指數", "收盤指數"],
                            "data": [["發行量加權股價指數", "20000"]],
                        },
                        {
                            "title": f"{date_str} 每日收盤行情(全部)",
                            "fields": ["證券代號", "證券名稱", "開盤價", "最高價", "最低價", "收盤價", "成交股數"],
                            "data": [["2330", "台積電", "900", "910", "895", "905", "19783000"]],
                        },
                    ],
                }
            else:
                payload = {"stat": "很抱歉，沒有符合條件的資料!", "tables": []}
            return _FakeResponse(payload)
        if "daily_close_quotes" in url:
            date_str = url.split("d=")[1].split("&")[0]
            if date_str in self._tpex_bad_format_dates:
                return _FakeResponse({"unexpectedKey": []})
            if date_str in self._tpex_ok_dates:
                payload = {
                    "date": date_str.replace("/", ""),
                    "tables": [
                        {
                            "title": "上櫃股票行情",
                            "fields": ["代號", "名稱", "收盤", "漲跌", "開盤", "最高", "最低", "成交股數"],
                            "data": [
                                ["6488", "环球晶", "500", "5", "495", "505", "490", "3000000"],
                            ],
                        }
                    ],
                    "flagField": "",
                    "stat": "ok",
                }
            else:
                payload = {"date": date_str.replace("/", ""), "tables": [], "flagField": "", "stat": "ok"}
            return _FakeResponse(payload)
        raise AssertionError(f"unexpected URL in test fake session: {url}")


def test_fetch_twse_day_all_converts_legacy_mi_index_format():
    """2026-09-03 第二次真實執行後確認的實際格式：MI_INDEX（帶 type=ALL）回傳的是
    `{"stat","tables":[{...}, ...]}`，資料被拆成好幾個表格（指數、報酬指數、每日收盤行情…），
    不是原本猜測的扁平 `{"stat","fields","data"}`——這裡鎖住「從 tables 裡找出標題含
    『每日收盤行情』的那個表格」的邏輯，且轉出來的中文欄位要能被 _parse_market_rows 認得
    （見該函式的英文/中文 fallback 設計，不需要另外改介面）。
    """
    session = _FakeSessionForBackfill(twse_ok_dates={"20260902"}, tpex_ok_dates=set())
    rows = _fetch_twse_day_all(session, dt.date(2026, 9, 2))
    assert rows == [
        {
            "證券代號": "2330", "證券名稱": "台積電", "開盤價": "900", "最高價": "910",
            "最低價": "895", "收盤價": "905", "成交股數": "19783000",
        }
    ]
    parsed = _parse_market_rows(rows, "TWSE")
    assert parsed["2330"]["close"] == 905.0
    assert parsed["2330"]["volume"] == pytest.approx(19783.0)  # 股 -> 張


def test_fetch_twse_day_all_raises_clearly_when_non_trading_day():
    """非交易日（假日/週末）查 MI_INDEX 會拿到 stat 不是 OK，要清楚報錯而不是回傳空清單，
    這樣呼叫端（backfill_history.py）才能正確判斷「這天跳過」而不是「這天沒有任何股票交易」。
    """
    session = _FakeSessionForBackfill(twse_ok_dates=set(), tpex_ok_dates=set())
    with pytest.raises(DataValidationError):
        _fetch_twse_day_all(session, dt.date(2026, 9, 6))  # 週日


def test_fetch_tpex_day_all_converts_tables_format():
    """2026-09-03 真實執行後確認的實際格式：{"tables": [{"fields": [...], "data": [[...]]}]}，
    不是原本猜測的 {"aaData": [...]}——這裡鎖住對照真實回應改寫後的轉換邏輯。"""
    session = _FakeSessionForBackfill(twse_ok_dates=set(), tpex_ok_dates={"115/09/02"})
    rows = _fetch_tpex_day_all(session, dt.date(2026, 9, 2))
    assert rows[0]["代號"] == "6488"
    assert rows[0]["收盤"] == "500"
    # 民國年轉換要正確：2026 - 1911 = 115
    assert any("d=115/09/02" in u for u in session.requested_urls)


def test_fetch_tpex_day_all_raises_clearly_when_tables_empty():
    """非交易日/假日時 tables 會是空陣列，不該被解析成『0 檔股票』悄悄放行，要清楚報錯，
    讓呼叫端（backfill_history.py）當成『這天沒抓到』處理。"""
    session = _FakeSessionForBackfill(twse_ok_dates=set(), tpex_ok_dates=set())
    with pytest.raises(DataValidationError):
        _fetch_tpex_day_all(session, dt.date(2026, 9, 6))  # 週日，tpex_ok_dates 沒有這天


def test_fetch_tpex_day_all_raises_clearly_when_format_unexpected():
    """TPEx 舊系統的回傳格式沒有機會在沙盒環境驗證過，格式不符時要清楚報錯、列出實際拿到的
    key，讓 backfill_history.py 把這天的 TPEx 資料當成『沒抓到』處理，而不是悄悄用錯資料
    (見 _fetch_tpex_day_all 的可信度聲明)。
    """
    session = _FakeSessionForBackfill(
        twse_ok_dates=set(), tpex_ok_dates=set(), tpex_bad_format_dates={"115/09/02"}
    )
    with pytest.raises(DataValidationError) as exc_info:
        _fetch_tpex_day_all(session, dt.date(2026, 9, 2))
    assert "unexpectedKey" in str(exc_info.value)


def test_daily_snapshot_store_load_recent_skips_missing_days(tmp_path):
    """遇到沒有存檔的日子（假日、系統中斷）要自動跳過，不能讓歷史序列裡出現「空的一天」。"""
    store = DailySnapshotStore(tmp_path)
    present_dates = [dt.date(2026, 9, 1), dt.date(2026, 9, 3)]  # 9/2 故意沒存
    for d in present_dates:
        store.save(d, {"as_of": d.isoformat()})
    history = store.load_recent(dt.date(2026, 9, 3), lookback_days=60)
    assert [h["as_of"] for h in history] == ["2026-09-01", "2026-09-03"]

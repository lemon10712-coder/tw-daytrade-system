"""tests/test_futures_intraday.py — 用 2026-09-27 從 TAIFEX 官網
quotesApi/getQuotes 端點實際觀察到的真實回應格式，鎖住解析與計算邏輯。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from stockSystem import futures_intraday as fi


class _FakeResponse:
      def __init__(self, payload):
                self._payload = payload

      def raise_for_status(self):
                pass

      def json(self):
                return self._payload


class _FakeSession:
      """依 objId 回傳固定的合成資料，格式跟真實觀察到的一模一樣。"""

    def __init__(self, responses: dict[int, object]):
              self._responses = responses
              self.requested_urls: list[str] = []

    def get(self, url, headers=None, timeout=None):
              self.requested_urls.append(url)
              for obj_id, payload in self._responses.items():
                            if f"objId={obj_id}" in url:
                                              return _FakeResponse(payload)
                                      raise AssertionError(f"未預期的請求: {url}")


# 2026-09-27 實際觀察到的 objId=2 回應（節錄）
REAL_SNAPSHOT_PAYLOAD = [
      {"contract": "TX106", "price": "48,123", "ttlvol": "65,343", "contractName": "臺股期貨", "updown": "-189"},
      {"contract": "TE106", "price": "3,065.00", "ttlvol": "166", "contractName": "電子期貨", "updown": "-15.60"},
      {"contract": "TF106", "price": "3,587.2", "ttlvol": "138", "contractName": "金融期貨", "updown": "-25.0"},
      {"contract": "XIF106", "price": "16,445.00", "ttlvol": "71", "contractName": "非金電期", "updown": "-49.00"},
      {"contract": "SOF106", "price": "-1", "ttlvol": "23", "contractName": "半導體30期貨", "updown": "-17,038"},
      {"contract": "SXF126", "price": "12,622.0", "ttlvol": "81", "contractName": "費城半導體期貨", "updown": "-146.0"},
]

# 2026-09-27 實際觀察到的 objId=3（日盤分鐘序列）開頭節錄
REAL_DAY_SERIES_PAYLOAD = [
      {"time": "0845", "price": "47850"},
      {"time": "0846", "price": "47818"},
      {"time": "0847", "price": "47828"},
      {"time": "0848", "price": "47802"},
      {"time": "0849", "price": "47797"},
      {"time": "0850", "price": "47808"},
      {"time": "0851", "price": "47821"},
      {"time": "0852", "price": "47869"},
      {"time": "0853", "price": "47853"},
      {"time": "0854", "price": "47874"},
      {"time": "0855", "price": "47868"},
      {"time": "0856", "price": "47868"},
      {"time": "0857", "price": "47903"},
      {"time": "0858", "price": "47931"},
      {"time": "0859", "price": "47924"},
      {"time": "0900", "price": "47928"},
]


def test_fetch_snapshot_parses_real_format():
      session = _FakeSession({2: REAL_SNAPSHOT_PAYLOAD})
      quotes = fi.fetch_snapshot(session=session)
      assert len(quotes) == 6
      tx = quotes[0]
      assert tx.contract == "TX106"
      assert tx.contract_name == "臺股期貨"
      assert tx.price == 48123.0
      assert tx.change == -189.0
      assert tx.total_volume == 65343


def test_fetch_snapshot_handles_placeholder_negative_price():
      """半導體30期貨在觀察當下是 "-1"（尚未開始交易的佔位值），要能正常解析成
          -1.0 而不是整筆跳過或丟例外——呼叫端自己決定要不要用這個值。"""
      session = _FakeSession({2: REAL_SNAPSHOT_PAYLOAD})
      quotes = fi.fetch_snapshot(session=session)
      sof = next(q for q in quotes if q.contract_name == "半導體30期貨")
      assert sof.price == -1.0


def test_get_tx_snapshot_finds_tx_by_contract_name():
      session = _FakeSession({2: REAL_SNAPSHOT_PAYLOAD})
      quotes = fi.fetch_snapshot(session=session)
      tx = fi.get_tx_snapshot(quotes)
      assert tx is not None
      assert tx.contract == "TX106"


def test_get_tx_snapshot_returns_none_when_absent():
      assert fi.get_tx_snapshot([]) is None


def test_fetch_minute_series_parses_real_format_and_preserves_order():
      session = _FakeSession({3: REAL_DAY_SERIES_PAYLOAD})
      series = fi.fetch_minute_series("day", session=session)
      assert series[0] == ("0845", 47850.0)
      assert series[-1] == ("0900", 47928.0)
      assert len(series) == len(REAL_DAY_SERIES_PAYLOAD)


def test_fetch_minute_series_uses_night_obj_id():
      session = _FakeSession({13: [{"time": "1500", "price": "47915"}]})
      series = fi.fetch_minute_series("night", session=session)
      assert series == [("1500", 47915.0)]
      assert any("objId=13" in u for u in session.requested_urls)


def test_compute_opening_range_uses_first_n_minutes_only():
      series = [(f"08{45+i:02d}", float(100 + i)) for i in range(30)]
      high, low = fi.compute_opening_range(series, minutes=15)
      assert high == 100 + 14
      assert low == 100.0


def test_compute_opening_range_handles_short_series():
      series = [("0845", 100.0), ("0846", 105.0)]
      high, low = fi.compute_opening_range(series, minutes=15)
      assert high == 105.0
      assert low == 100.0


def test_compute_opening_range_empty_series_returns_none():
      assert fi.compute_opening_range([], minutes=15) is None


def test_compute_camarilla_pivots_matches_known_formula():
      pivots = fi.compute_camarilla_pivots(prev_close=48000.0, prev_high=48500.0, prev_low=47500.0)
      rng = 1000.0
      assert pivots["r4"] == 48000.0 + rng * 1.1 / 2
      assert pivots["r3"] == 48000.0 + rng * 1.1 / 4
      assert pivots["s3"] == 48000.0 - rng * 1.1 / 4
      assert pivots["s4"] == 48000.0 - rng * 1.1 / 2
      assert pivots["s4"] < pivots["s3"] < 48000.0 < pivots["r3"] < pivots["r4"]


def test_compute_twap_is_simple_average_of_prices():
      series = [("0845", 100.0), ("0846", 200.0), ("0847", 300.0)]
      assert fi.compute_twap(series) == 200.0


def test_compute_twap_empty_series_returns_none():
      assert fi.compute_twap([]) is None


def test_compute_realized_volatility_returns_none_when_series_too_short():
      series = [(f"08{45+i:02d}", 100.0) for i in range(5)]
      assert fi.compute_realized_volatility_points(series) is None


def test_compute_realized_volatility_zero_for_flat_series():
      series = [(f"08{45+i:02d}", 100.0) for i in range(15)]
      assert fi.compute_realized_volatility_points(series) == 0.0


def test_intraday_range_prediction_bullish_when_breaking_above_orb():
      # 前15分鐘在 100~110 之間震盪，之後一路噴到 130，站上開盤區間高點 -> 判定偏多
      # 開盤前15分鐘(orb_minutes預設值)在100~110之間震盪，之後才噴到130，
      # 確保突破發生在ORB視窗「之後」，不會被算進開盤區間本身裡面。
      series = [(f"08{45+i:02d}", 100.0 + (i % 5)) for i in range(15)]
      series += [(f"09{i:02d}", 130.0) for i in range(10)]
      pred = fi.compute_intraday_range_prediction(
          series, prev_close=100.0, prev_high=105.0, prev_low=95.0, daily_direction_bias="中性"
      )
      assert pred is not None
      assert pred.direction_bias == "偏多"
      assert pred.latest_price == 130.0
      # 進出場範圍都應該是 (低,高) 排序過的合法區間
      assert pred.entry_range[0] <= pred.entry_range[1]
    assert pred.stop_range[0] <= pred.stop_range[1]
    assert pred.target_range[0] <= pred.target_range[1]
    # 停損應該在進場區間之下、停利應該在進場區間之上（多方邏輯）
    assert pred.stop_range[1] <= pred.entry_range[1]
    assert pred.target_range[0] >= pred.entry_range[0]


def test_intraday_range_prediction_bearish_when_breaking_below_orb():
      series = [(f"08{45+i:02d}", 100.0 - (i % 5)) for i in range(15)]
      series += [(f"09{i:02d}", 70.0) for i in range(10)]
      pred = fi.compute_intraday_range_prediction(
          series, prev_close=100.0, prev_high=105.0, prev_low=95.0, daily_direction_bias="中性"
      )
      assert pred is not None
      assert pred.direction_bias == "偏空"
      assert pred.latest_price == 70.0


def test_intraday_range_prediction_neutral_inside_orb_falls_back_to_daily_bias():
      # 全部價格都在開盤區間(100~110)裡面震盪，沒有突破 -> 沿用傳入的日線方向判斷
      series = [(f"08{45+i:02d}", 100.0 + (i % 5)) for i in range(20)]
      pred = fi.compute_intraday_range_prediction(
          series, prev_close=100.0, prev_high=105.0, prev_low=95.0, daily_direction_bias="偏空"
      )
      assert pred is not None
      assert pred.direction_bias == "偏空"


def test_intraday_range_prediction_none_when_series_empty():
      assert fi.compute_intraday_range_prediction([], prev_close=100.0, prev_high=105.0, prev_low=95.0) is None


def test_caveat_mentions_tx_proxy_and_not_tick_and_disclaimer():
      caveat = fi.FUTURES_INTRADAY_CAVEAT
      assert "TX" in caveat
      assert "MXF" in caveat or "微台指" in caveat
      assert "tick" in caveat or "逐筆" in caveat
      assert "不構成投資建議" in caveat


def test_prediction_carries_caveat_by_default():
      series = [(f"08{45+i:02d}", 100.0) for i in range(20)]
      pred = fi.compute_intraday_range_prediction(series, prev_close=100.0, prev_high=105.0, prev_low=95.0)
      assert pred.caveat == fi.FUTURES_INTRADAY_CAVEAT
  

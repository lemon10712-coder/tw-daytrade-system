"""微台指(MXF)盤中監控層：抓 TAIFEX 官方免費即時報價，算開盤區間突破(ORB)/
Camarilla樞紐點/已實現波動度，產出「進場價範圍、停損價範圍、停利價範圍」。

背景（2026-09-27 新增，誠實記錄取捨過程供之後維護者理解）：
使用者要求期貨模組要「即時」、要能自動抓資料、給進出場價，並且要求参考「高手」做法。
先確認過技術現實：這個 Cowork 雲端沙盒本身完全連不到任何外部網站（見專案
KNOWN_ISSUES.md 記錄超過6次的網路層阻擋），真正的「逐筆tick」資料本來就需要付費
券商/資訊商的API，這種持續連線這個環境本來就做不到——不管排程設多密都一樣，因為
Cowork 排程一次只會啟動一個短命的 session，不是一個能整天掛著吃tick的常駐連線。

跟使用者討論後決定：比照這個專案一貫的架構——所有真正需要連外部網路抓資料的工作，
都放進 GitHub Actions（有完整網路權限，不受 Cowork 沙盒限制），不是想辦法讓 Cowork
自己去連。這支模組就是給 GitHub Actions 用的。

資料來源：`https://www.taifex.com.tw/cht/quotesApi/getQuotes?<timestamp>&objId=<N>`——
這是台灣期貨交易所官網首頁本身用來畫「日盤商品行情表」跟走勢圖的公開JSON端點，
不需要登入、不需要任何API金鑰，用 Claude in Chrome 開官網首頁、攔截它實際呼叫的
API 找到的（2026-09-27 驗證），已用瀏覽器直接冷啟動導航測試過(不帶任何cookie)一樣
能拿到正確資料，適合 GitHub Actions 用單純的 requests.get() 呼叫。目前確認可用的
objId：
  2  近月主要指數期貨即時快照（含「臺股期貨」contractName，即TX大台，附成交量/漲跌）
  3  今日日盤(0845-1345)近月台股期貨(TX) 每分鐘價格序列
  13 今日夜盤(1500-次日0500) 每分鐘價格序列
  9  三大法人淨部位歷史序列（未在這支模組使用，留待之後需要時再接）

【重要誠實限制，寫進 FUTURES_INTRADAY_CAVEAT 附在每一次輸出裡】：
1. 這是「臺股期貨(TX，大台)」的分鐘價格，不是微台指(MXF)自己的報價，也不是逐筆tick。
   TX 與 MXF 追蹤同一個台指期貨標的、同一組到期月份，報價點數在正常市況下幾乎完全
   一致（差別只在每點價值：TX每點新台幣200元、MXF每點10元），這裡直接把 TX 的分鐘
   價格序列當作 MXF 方向與價位的代理，但不保證兩者在極端流動性情況下完全沒有微小
   價差(basis)。
2. GitHub Actions 排程盤中每5分鐘觸發一次，不是持續不間斷的連線，兩次更新之間的
   價格變化不會被捕捉到；GitHub 排程本身也可能因平台負載延遲數分鐘執行，不保證
   精確到秒。
3. 這個免費端點只給價格、沒有給逐分鐘成交量，沒辦法算真正的成交量加權(VWAP)，
   這裡老實用時間加權(TWAP)代替，不假裝是VWAP。
4. 以下算出的「範圍」是用開盤區間突破(ORB)、Camarilla樞紐點、當日已實現波動度
   這些公開、被日內交易者廣泛使用的技術分析方法計算，不是對台指期貨走勢的預測
   保證，也不構成投資建議。
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import requests

TAIFEX_QUOTES_API = "https://www.taifex.com.tw/cht/quotesApi/getQuotes"

OBJ_ID_SNAPSHOT = 2
OBJ_ID_DAY_SERIES = 3
OBJ_ID_NIGHT_SERIES = 13

_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.taifex.com.tw/cht/index",
}

FUTURES_INTRADAY_CAVEAT = (
    "【重要】本區塊使用台灣期貨交易所官網公開、免登入的 quotesApi 端點"
    "(www.taifex.com.tw/cht/quotesApi/getQuotes)，資料是「臺股期貨(TX，大台)」"
    "每分鐘更新一次的參考價，不是逐筆(tick)成交明細，也不是微台指(MXF)自己的報價——"
    "TX 與 MXF 追蹤同一個台指期貨標的、同一組到期月份，報價點數在正常市況下幾乎完全"
    "一致(差別只在每點價值：TX每點新台幣200元、MXF每點10元)，這裡直接把 TX 的分鐘"
    "價格序列當作 MXF 方向與價位的代理，但不保證兩者在極端流動性情況下完全沒有微小"
    "價差(basis)。本區塊由 GitHub Actions 於盤中每 5 分鐘重新整理一次，不是持續不間斷"
    "的即時連線，兩次更新之間發生的價格變化不會被捕捉到；GitHub 排程本身也可能因平台"
    "負載而延遲數分鐘執行，不保證精確到秒。這個免費端點只給價格、沒有逐分鐘成交量，"
    "無法計算真正的成交量加權(VWAP)，這裡用時間加權(TWAP)代替。以下的進場/停損/停利"
    "「範圍」是用開盤區間突破(ORB)、Camarilla樞紐點、當日已實現波動度等公開技術分析"
    "方法計算出的參考區間，不是對台指期貨走勢的預測保證，也不構成投資建議，請自行"
    "判斷風險。"
)


@dataclass
class IntradayQuote:
    contract: str
    contract_name: str
    price: float
    change: float
    total_volume: int


def _parse_number(value) -> float:
    return float(str(value).replace(",", ""))


def fetch_snapshot(session=None, timeout: float = 10.0) -> list[IntradayQuote]:
    """呼叫 objId=2，回傳目前六個近月主要指數期貨的即時快照。"""
    sess = session or requests
    ts = int(datetime.now().timestamp() * 1000)
    resp = sess.get(
        f"{TAIFEX_QUOTES_API}?{ts}&objId={OBJ_ID_SNAPSHOT}",
        headers=_BROWSER_HEADERS,
        timeout=timeout,
    )
    resp.raise_for_status()
    data = resp.json()
    out = []
    for row in data:
        try:
            price = _parse_number(row.get("price", ""))
        except ValueError:
            continue
        out.append(
            IntradayQuote(
                contract=row.get("contract", ""),
                contract_name=row.get("contractName", ""),
                price=price,
                change=_parse_number(row.get("updown", "0") or "0"),
                total_volume=int(_parse_number(row.get("ttlvol", "0") or "0")),
            )
        )
    return out


def fetch_minute_series(session_type: str = "day", session=None, timeout: float = 10.0) -> list[tuple[str, float]]:
    """呼叫 objId=3(日盤)或13(夜盤)，回傳 [(HHMM字串, price), ...]，依時間排序（端點
    本身就是照時間順序回傳，這裡不重新排序，避免萬一端點格式改變時掩蓋掉真實順序）。
    """
    obj_id = OBJ_ID_DAY_SERIES if session_type == "day" else OBJ_ID_NIGHT_SERIES
    sess = session or requests
    ts = int(datetime.now().timestamp() * 1000)
    resp = sess.get(
        f"{TAIFEX_QUOTES_API}?{ts}&objId={obj_id}",
        headers=_BROWSER_HEADERS,
        timeout=timeout,
    )
    resp.raise_for_status()
    data = resp.json()
    out = []
    for row in data:
        t, p = row.get("time"), row.get("price")
        if t is None or p is None:
            continue
        try:
            out.append((str(t), _parse_number(p)))
        except ValueError:
            continue
    return out


def get_tx_snapshot(quotes: list[IntradayQuote]) -> Optional[IntradayQuote]:
    for q in quotes:
        if q.contract_name == "臺股期貨":
            return q
    return None


# ---------------------------------------------------------------------------
# 技術分析：開盤區間突破 (ORB) / Camarilla 樞紐點 / TWAP / 已實現波動度
# ---------------------------------------------------------------------------


def compute_opening_range(series: list[tuple[str, float]], minutes: int = 15) -> Optional[tuple[float, float]]:
    """回傳(開盤區間高, 開盤區間低)，取序列最前面 `minutes` 筆（端點本身照時間
    順序回傳，第一筆就是當天開盤那一分鐘）。序列筆數不足 `minutes` 時，用目前
    有的全部筆數計算（開盤沒多久重新整理時本來就不會有滿15筆）。
    """
    if not series:
        return None
    window = [p for _, p in series[:minutes]]
    return max(window), min(window)


def compute_camarilla_pivots(prev_close: float, prev_high: float, prev_low: float) -> dict:
    """Camarilla 樞紐點——日內交易者常用的支撐壓力算法（公開公式）：
    range = 前一日高低價差
    R4 = 收盤 + range*1.1/2，R3 = 收盤 + range*1.1/4
    S3 = 收盤 - range*1.1/4，S4 = 收盤 - range*1.1/2
    R4/S4 常被當作單日極端突破的參考位；R3/S3 常被當作區間交易的進出場參考。
    """
    rng = prev_high - prev_low
    return {
        "r4": prev_close + rng * 1.1 / 2,
        "r3": prev_close + rng * 1.1 / 4,
        "s3": prev_close - rng * 1.1 / 4,
        "s4": prev_close - rng * 1.1 / 2,
    }


def compute_twap(series: list[tuple[str, float]]) -> Optional[float]:
    """時間加權平均價。這個免費端點只有價格、沒有逐分鐘成交量，沒辦法算真正
    的成交量加權(VWAP)，老實用TWAP代替，不假裝是VWAP。"""
    if not series:
        return None
    prices = [p for _, p in series]
    return sum(prices) / len(prices)


def compute_realized_volatility_points(series: list[tuple[str, float]]) -> Optional[float]:
    """用分鐘價格序列的一階差分標準差，估計「今天到目前為止」的已實現波動度
    （單位：點）。序列筆數不足10筆（剛開盤沒多久）時回傳 None，不硬算一個
    沒有統計意義的數字。
    """
    if len(series) < 10:
        return None
    diffs = [series[i][1] - series[i - 1][1] for i in range(1, len(series))]
    return statistics.pstdev(diffs)


@dataclass
class IntradayRangePrediction:
    as_of_time: str
    latest_price: float
    direction_bias: str  # "偏多" / "偏空" / "中性"
    entry_range: tuple[float, float]
    stop_range: tuple[float, float]
    target_range: tuple[float, float]
    basis: dict
    caveat: str = FUTURES_INTRADAY_CAVEAT


def _sorted_pair(pair: tuple[float, float]) -> tuple[float, float]:
    return (min(pair), max(pair))


def compute_intraday_range_prediction(
    series: list[tuple[str, float]],
    prev_close: float,
    prev_high: float,
    prev_low: float,
    daily_direction_bias: str = "中性",
    orb_minutes: int = 15,
) -> Optional[IntradayRangePrediction]:
    """整合 ORB + Camarilla + 已實現波動度，算出「進場價範圍」「停損價範圍」
    「停利價範圍」——回傳範圍而不是單一價位，因為使用者明確要的是「預測範圍」，
    不是假裝能精準預測單一成交價。

    邏輯（比照 entry_exit.py / futures_signals.py 一貫的「規則透明、可回推」精神，
    不是黑盒模型）：
    1. 用最新價相對開盤區間(ORB)高低點的位置判斷「盤中方向」：站上ORB高點視為
       偏多突破、跌破ORB低點視為偏空突破，都在區間內則沿用「當天既有的日線方向
       判斷」(daily_direction_bias，由 futures_signals.decide_direction() 算出)
       當預設立場。
    2. 偏多時：進場範圍抓「Camarilla S3支撐 ~ 目前價」的拉回承接區間，停損放在
       S4之下，停利看Camarilla R3~R4（壓力區）。偏空時方向相反。中性時，用已實現
       波動度(或序列太短時退回用前一日振幅估計)在目前價上下抓一個對稱區間，
       不給方向性建議。
    """
    if not series:
        return None
    latest_time, latest_price = series[-1]

    orb = compute_opening_range(series, minutes=orb_minutes)
    pivots = compute_camarilla_pivots(prev_close, prev_high, prev_low)
    vol = compute_realized_volatility_points(series)
    vol_buffer = vol * 3 if vol else max((prev_high - prev_low) * 0.1, 1.0)

    if orb:
        orb_high, orb_low = orb
        if latest_price > orb_high:
            bias = "偏多"
        elif latest_price < orb_low:
            bias = "偏空"
        else:
            bias = daily_direction_bias
    else:
        bias = daily_direction_bias

    if bias == "偏多":
        entry_range = _sorted_pair((pivots["s3"], latest_price))
        stop_range = _sorted_pair((pivots["s4"], pivots["s3"] - vol_buffer * 0.3))
        target_range = _sorted_pair((pivots["r3"], pivots["r4"]))
    elif bias == "偏空":
        entry_range = _sorted_pair((latest_price, pivots["r3"]))
        stop_range = _sorted_pair((pivots["r3"] + vol_buffer * 0.3, pivots["r4"]))
        target_range = _sorted_pair((pivots["s4"], pivots["s3"]))
    else:
        entry_range = _sorted_pair((latest_price - vol_buffer, latest_price + vol_buffer))
        stop_range = _sorted_pair((latest_price - vol_buffer * 2, latest_price - vol_buffer))
        target_range = _sorted_pair((latest_price + vol_buffer, latest_price + vol_buffer * 2))

    return IntradayRangePrediction(
        as_of_time=latest_time,
        latest_price=latest_price,
        direction_bias=bias,
        entry_range=entry_range,
        stop_range=stop_range,
        target_range=target_range,
        basis={
            "orb_high": orb[0] if orb else None,
            "orb_low": orb[1] if orb else None,
            "camarilla": pivots,
            "realized_vol_points": vol,
            "twap": compute_twap(series),
        },
    )

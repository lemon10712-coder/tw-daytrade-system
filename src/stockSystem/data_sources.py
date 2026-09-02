"""資料層：定義資料提供者的共同介面，並提供兩種實作：

1. `FixtureProvider`：**測試用合成資料**，完全不對外連線，用來驗證下游邏輯正確性。
2. `TwseOpenApiProvider` / `YFinanceIntlProvider`：**真實資料**，需要對外網路連線。
   目前這個雲端環境的網路被組織政策擋下（診斷過程與解法見專案 KNOWN_ISSUES.md），
   所以這兩個 provider 目前呼叫任何方法都會拋出 `DataSourceUnavailableError`，
   訊息裡會直接告訴你去看 KNOWN_ISSUES.md，而不是讓你看到一串不知所云的連線錯誤。

**最關鍵的防幻覺設計**：每一筆資料都帶有 `source_tag` 欄位（見 `SourceTag`），
下游的 report.py 在組裝報告前一定會檢查這個標記——只要資料裡出現
`SourceTag.SYNTHETIC_TEST_DATA`，整份報告就會被強制標示為「測試模式，非真實市場資料」，
不可能不小心把假資料包裝成正式報告呈現給使用者。這是寫在程式邏輯裡的保險，
不是只靠文件提醒或人工記得。
"""

from __future__ import annotations

import abc
import datetime as dt
import enum
from dataclasses import dataclass, field

import numpy as np
import pandas as pd


class DataSourceUnavailableError(RuntimeError):
    """對外資料源無法連線時拋出。訊息會指向 KNOWN_ISSUES.md 的診斷結果。"""

    def __init__(self, source_name: str, underlying: Exception | None = None):
        msg = (
            f"資料源「{source_name}」目前無法連線。"
            "這是已知問題：此雲端環境的網路對外存取（egress）被組織政策擋下，"
            "不是程式碼或這次呼叫本身的問題。"
            "診斷細節與解法請見專案文件 KNOWN_ISSUES.md（Organization settings -> "
            "Capabilities -> Code execution 開通對應網域）。"
            "在網路權限開通前，請改用 stockSystem.data_sources.FixtureProvider 做開發與測試。"
        )
        if underlying is not None:
            msg += f" 原始錯誤: {underlying!r}"
        super().__init__(msg)


class DataValidationError(ValueError):
    """資料通過連線但內容有明顯異常（缺漏、負成交量、價格為0等）時拋出。

    對應規劃書「出報告前的品質檢查清單」——資料異常寧可丟例外、讓上層決定跳過該筆，
    也不要讓異常資料悄悄流進評分邏輯，算出一個看起來正常但其實是錯的結果。
    """


class SourceTag(str, enum.Enum):
    """資料來源標記，用來讓下游明確知道這筆資料能不能被當作「真實市場資料」呈現。"""

    SYNTHETIC_TEST_DATA = "SYNTHETIC_TEST_DATA"   # 測試用合成資料，絕對不可呈現為正式報告
    TWSE_OPENAPI = "TWSE_OPENAPI"                 # 台灣證券交易所官方公開資料
    TPEX_OPENAPI = "TPEX_OPENAPI"                 # 證券櫃檯買賣中心官方公開資料
    YFINANCE = "YFINANCE"                         # Yahoo Finance（經 yfinance）


@dataclass
class MarketSnapshot:
    """單次資料抓取的結果容器，強制附帶來源標記與資料時間戳記。"""

    as_of: dt.date
    source_tag: SourceTag
    ohlcv: pd.DataFrame                 # index: stock_id, columns: open/high/low/close/volume(股)
    industry_map: dict                  # stock_id -> 產業/族群名稱
    institutional_flow: pd.DataFrame    # index: stock_id, columns: foreign/trust/dealer 買賣超(張)
    margin_data: pd.DataFrame           # index: stock_id, columns: margin_balance, margin_change
    watch_list: set                     # 注意股票代碼集合
    disposition_list: set               # 處置股票代碼集合
    full_delivery_list: set             # 全額交割股代碼集合
    daytrade_long_eligible: set         # 可現股當沖（買進）標的
    daytrade_short_eligible: set        # 可先賣後買（放空）標的
    intl_snapshot: dict = field(default_factory=dict)  # 國際情勢：{"SOX": pct_chg, "TSM_ADR": pct_chg, ...}

    def is_synthetic(self) -> bool:
        return self.source_tag == SourceTag.SYNTHETIC_TEST_DATA


class MarketDataProvider(abc.ABC):
    """所有資料提供者的共同介面。之後要接真實資料源，只要實作這個介面就好，
    上游的 sector_strength / stock_screener 完全不需要跟著改。
    """

    @abc.abstractmethod
    def get_snapshot(self, as_of: dt.date) -> MarketSnapshot:
        ...


class TwseOpenApiProvider(MarketDataProvider):
    """真實資料：台灣證券交易所 / 證券櫃檯買賣中心 OpenAPI。

    網路一旦開通，把下面 `_fetch_json` 裡的 TODO 補完（目前先把端點路徑列好，
    對應規劃書第 2.1 節列出的資料集），其餘程式碼不需要更動。
    """

    BASE_URL = "https://openapi.twse.com.tw/v1"

    ENDPOINTS = {
        "daily_quotes": "/exchangeReport/STOCK_DAY_ALL",       # 個股日成交資訊
        "institutional": "/fund/T86",                          # 三大法人買賣超
        "margin": "/margin/MI_MARGN",                          # 融資融券
        "watch_list": "/announcement/notice",                  # 注意股票（實際端點待核對）
        "disposition": "/announcement/punish",                 # 處置股票（實際端點待核對）
        "daytrade": "/exchangeReport/TWTB4U",                  # 當日沖銷交易標的
    }

    def __init__(self, session=None):
        import requests  # 延遲 import，避免在完全用 FixtureProvider 的測試環境也要求 requests 以外的東西
        self._session = session or requests.Session()

    def get_snapshot(self, as_of: dt.date) -> MarketSnapshot:
        try:
            # TODO: 網路開通後在這裡實作真正的多端點抓取與整併邏輯。
            # 刻意先讓它明確失敗，而不是回傳一個看似正常、實際是空殼的 MarketSnapshot——
            # 空殼資料一旦被下游誤用，會產生「有跑出結果但其實毫無根據」的假象，
            # 這正是整個系統設計上最忌諱的事。
            raise NotImplementedError("尚未串接真實端點，見 KNOWN_ISSUES.md")
        except Exception as exc:  # noqa: BLE001 - 這裡刻意攔截所有例外並轉譯成明確訊息
            raise DataSourceUnavailableError("TWSE OpenAPI", exc) from exc


class YFinanceIntlProvider:
    """真實資料：國際情勢（美股大盤/龍頭股、日股、韓股），透過 yfinance。"""

    TICKERS = {
        "NASDAQ": "^IXIC",
        "SP500": "^GSPC",
        "SOX": "^SOX",
        "MICRON": "MU",
        "NVIDIA": "NVDA",
        "TSM_ADR": "TSM",
        "NIKKEI225": "^N225",
        "KOSPI": "^KS11",
    }

    def get_intl_snapshot(self, as_of: dt.date) -> dict:
        try:
            import yfinance  # noqa: F401  # 目前環境未安裝，且 pip 被組織網路政策擋下
            raise NotImplementedError("yfinance 已可 import，但尚未實作抓取與整併邏輯")
        except Exception as exc:  # noqa: BLE001
            raise DataSourceUnavailableError("yfinance (國際情勢資料)", exc) from exc


class FixtureProvider(MarketDataProvider):
    """**測試用合成資料**，完全不對外連線，用固定亂數種子產生可重現的資料，
    讓所有下游模組（族群強度、選股、資金配置、回測）都能在網路權限開通之前
    被完整測試過一次。回傳的每一筆資料都標記為 SourceTag.SYNTHETIC_TEST_DATA。
    """

    def __init__(self, seed: int = 42, n_stocks_per_sector: int = 12):
        self._rng = np.random.default_rng(seed)
        self._n_per_sector = n_stocks_per_sector
        # 刻意設計幾個「族群強度」有明顯差異的假族群，方便測試排序邏輯是否正確：
        # 光通訊＝強勢（近期放量上漲）、傳產＝中性、航運＝弱勢（放量下跌，適合測放空邏輯）
        # 趨勢差距刻意拉大、雜訊縮小（見下方 daily_ret 的 scale），確保這是「訊號夠強、
        # 隨機種子不同也應該穩定排序」的測試情境，而不是每次跑結果都不一樣的不穩定測試。
        self._sector_trend = {
            "光通訊": 0.060,
            "半導體": 0.020,
            "傳產": 0.0,
            "航運": -0.055,
        }

    def get_snapshot(self, as_of: dt.date) -> MarketSnapshot:
        rows = []
        industry_map = {}
        watch_list: set = set()
        disposition_list: set = set()
        full_delivery_list: set = set()
        daytrade_long: set = set()
        daytrade_short: set = set()
        inst_rows = []
        margin_rows = []

        stock_idx = 1000
        for sector, trend in self._sector_trend.items():
            for i in range(self._n_per_sector):
                stock_id = str(stock_idx)
                stock_idx += 1
                industry_map[stock_id] = sector

                base_price = float(self._rng.uniform(30, 600))
                days = 30
                # 用帶趨勢的隨機漫步模擬近 30 日收盤價，最後一天就是「今天」
                daily_ret = self._rng.normal(loc=trend / 5, scale=0.010, size=days)
                closes = base_price * np.cumprod(1 + daily_ret)
                volumes = self._rng.integers(200, 5000, size=days)  # 單位：張

                rows.append(
                    {
                        "stock_id": stock_id,
                        "sector": sector,
                        "close": closes[-1],
                        "prev_close": closes[-2],
                        "open": closes[-2] * (1 + self._rng.normal(0.001, 0.006)),
                        "high": max(closes[-1], closes[-2]) * (1 + abs(self._rng.normal(0.005, 0.004))),
                        "low": min(closes[-1], closes[-2]) * (1 - abs(self._rng.normal(0.005, 0.004))),
                        "volume": int(volumes[-1]),
                        "close_hist": closes,       # 近30日收盤價序列，供技術指標計算
                        "volume_hist": volumes,     # 近30日成交量序列（張）
                    }
                )

                # 少數股票標記為警示股，測試硬性排除邏輯是否確實生效
                if i == 0 and sector == "航運":
                    disposition_list.add(stock_id)
                if i == 1 and sector == "傳產":
                    full_delivery_list.add(stock_id)
                if i == 2 and sector == "半導體":
                    watch_list.add(stock_id)

                # 大部分股票都有雙向當沖資格，留幾檔只能做多，測試放空資格過濾
                daytrade_long.add(stock_id)
                if not (sector == "航運" and i == 3):
                    daytrade_short.add(stock_id)

                inst_rows.append(
                    {
                        "stock_id": stock_id,
                        "foreign_net": int(self._rng.normal(trend * 20000, 500)),
                        "trust_net": int(self._rng.normal(trend * 8000, 300)),
                        "dealer_net": int(self._rng.normal(trend * 3000, 200)),
                    }
                )
                margin_rows.append(
                    {
                        "stock_id": stock_id,
                        "margin_balance": int(self._rng.uniform(500, 20000)),
                        "margin_change": int(self._rng.normal(trend * 3000, 400)),
                    }
                )

        ohlcv = pd.DataFrame(rows).set_index("stock_id")
        institutional = pd.DataFrame(inst_rows).set_index("stock_id")
        margin = pd.DataFrame(margin_rows).set_index("stock_id")

        intl_snapshot = {
            "SOX": float(self._rng.normal(0.5, 1.5)),
            "TSM_ADR": float(self._rng.normal(0.4, 1.3)),
            "NASDAQ": float(self._rng.normal(0.3, 1.0)),
            "SP500": float(self._rng.normal(0.2, 0.8)),
            "NIKKEI225": float(self._rng.normal(0.2, 1.1)),
            "KOSPI": float(self._rng.normal(0.1, 1.0)),
        }

        return MarketSnapshot(
            as_of=as_of,
            source_tag=SourceTag.SYNTHETIC_TEST_DATA,
            ohlcv=ohlcv,
            industry_map=industry_map,
            institutional_flow=institutional,
            margin_data=margin,
            watch_list=watch_list,
            disposition_list=disposition_list,
            full_delivery_list=full_delivery_list,
            daytrade_long_eligible=daytrade_long,
            daytrade_short_eligible=daytrade_short,
            intl_snapshot=intl_snapshot,
        )

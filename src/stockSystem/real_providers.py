"""真實資料提供者：直接用 `requests` 打證交所（TWSE）／櫃買中心（TPEx）OpenAPI 與 Yahoo Finance，
給 GitHub Actions（或任何有正常對外網路的環境）執行——**不是**給這個 Claude Cowork 雲端沙盒執行的，
這個沙盒的網路被組織政策擋死，細節見 KNOWN_ISSUES.md「問題1」。

## 這個檔案的核心設計：逐日累積歷史，不做昂貴的個股回補查詢

TWSE/TPEx 的「單日全市場」端點（STOCK_DAY_ALL 等）一次呼叫就能拿到當天所有股票的資料，
但沒有「一次拿全部股票近30日歷史」這種端點——要拿歷史只能一檔一檔查，對上千檔股票來說
不可行（會被視為濫用、也太慢）。

所以這裡採用的做法是：**每天執行一次，把當天的全市場快照存成一個檔案（`data/daily/YYYY-MM-DD.json`），
存進 git repo，長期累積**。族群強度、ATR 等需要多天序列的計算，用「讀取最近 N 個每日快照檔案、
組出每檔股票的 close_hist/volume_hist」的方式取得——這與規劃書設計的技術指標函式（`technicals.py`）
本來就相容：資料不足 30 天時，那些函式會回傳 `nan`（見 `technicals.py` 的設計），下游的
`entry_exit.py`／`sector_strength.py` 已經有「資料不足時退回保守估計」的邏輯，**不需要改介面**，
只是剛上線的頭幾週，均線/ATR 相關數字會因為歷史還不夠長而比較不精準，這點會誠實寫進報告的
資料品質提示裡（見 `self_check`）。

## 欄位名稱的可信度聲明（非常重要，請讀）

這個雲端沙盒本身連不到這些端點（見 KNOWN_ISSUES.md），所以下面的欄位名稱**是根據 TWSE/TPEx OpenAPI
的公開文件與慣例寫的，沒有機會在這個環境裡對著真實回應驗證過**。因此每個解析函式都刻意寫成「防禦性」：
用關鍵字比對而不是死板的完全比對欄位名稱，而且只要找不到關鍵欄位就明確拋出 `DataValidationError`，
把「目前實際拿到的欄位有哪些」印在錯誤訊息裡——**這是刻意設計成第一次在 GitHub Actions 真正執行時，
如果欄位對不上，會清楚地失敗並告訴你差在哪裡，而不是悄悄用錯的欄位算出一個看起來正常但其實是錯的結果**。
第一次真正執行後，請把 Actions 執行紀錄裡的錯誤訊息（如果有）回報，才能一次把欄位名稱對正確，
不需要靠反覆猜測。
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import time
from pathlib import Path

import numpy as np
import pandas as pd

from stockSystem.data_sources import (
    DataSourceUnavailableError,
    DataValidationError,
    MarketDataProvider,
    MarketSnapshot,
    SourceTag,
)

logger = logging.getLogger("stockSystem.real_providers")

TWSE_BASE = "https://openapi.twse.com.tw/v1"
TPEX_BASE = "https://www.tpex.org.tw/openapi/v1"
# 三大法人買賣超（T86）不在新版開放資料平台（openapi.twse.com.tw）上，只能從證交所
# 舊版「盤後資訊」系統取得，回傳格式也不同（見下面 _rows_from_fields_data 的說明）。
TWSE_LEGACY_BASE = "https://www.twse.com.tw/rwd/zh"

# 2026-09-02 第一次在 GitHub Actions 真正執行後發現：requests 預設的 User-Agent 會被
# Yahoo Finance 判定為爬蟲、對 GitHub Actions 的共用 IP 回傳 429 Too Many Requests。
# 帶一個一般瀏覽器的 User-Agent 可以大幅降低被擋的機率（見 KNOWN_ISSUES.md）。
_BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


# ---------------------------------------------------------------------------
# 共用工具：防禦性欄位比對，找不到就丟出清楚的錯誤而不是悄悄用錯資料
# ---------------------------------------------------------------------------

def _find_key(row: dict, *must_contain: str) -> str:
    """在 `row` 的 key 裡找出「同時包含所有 must_contain 子字串」的那一個 key。

    找不到就拋 DataValidationError，並把目前有的 key 全部列出來，方便一次對正確。
    """
    for k in row.keys():
        if all(token in k for token in must_contain):
            return k
    raise DataValidationError(
        f"找不到符合條件 {must_contain} 的欄位。目前這筆資料實際的欄位有：{sorted(row.keys())}。"
        "代表 TWSE/TPEx 的欄位名稱跟這個檔案裡假設的不一樣，需要對照本次錯誤訊息更新 real_providers.py "
        "裡對應的 _find_key 呼叫，而不是重試或忽略——這個檔案的欄位名稱寫這份程式時沒有機會用真實回應驗證過，"
        "見 real_providers.py 檔案開頭的說明。"
    )


def _to_float(value, default: float = float("nan")) -> float:
    if value is None:
        return default
    try:
        s = str(value).strip().replace(",", "")
        if s in ("", "--", "X", "N/A"):
            return default
        return float(s)
    except (TypeError, ValueError):
        return default


def _to_int(value, default: int = 0) -> int:
    f = _to_float(value, float(default))
    return int(f) if f == f else default  # f==f 排除 nan


# ---------------------------------------------------------------------------
# HTTP 抓取（薄封裝，方便測試時 mock）
# ---------------------------------------------------------------------------

def _get_json(session, url: str, timeout: int = 30):
    resp = session.get(
        url,
        timeout=timeout,
        headers={"Accept": "application/json", "User-Agent": _BROWSER_USER_AGENT},
    )
    resp.raise_for_status()
    return resp.json()


def _rows_from_fields_data(raw: dict, endpoint_label: str) -> list[dict]:
    """把 TWSE 舊版「盤後資訊」系統（www.twse.com.tw/rwd/zh/...）回傳的
    `{"stat": "OK", "fields": [...], "data": [[...], ...]}` 格式，轉成跟
    openapi.twse.com.tw 一致的「物件陣列」（每筆資料是一個 dict），這樣下游的
    `_find_key` / `_parse_market_rows` 完全不用區分資料來源、不用改介面。

    2026-09-02 第一次在 GitHub Actions 真正執行時發現：原本以為三大法人買賣超
    也在 openapi.twse.com.tw（新版開放資料平台）上、用跟其他端點一樣的物件陣列格式，
    結果那個端點根本不存在（404），真正的資料在這個完全不同的舊系統上，且格式也不同。
    """
    stat = raw.get("stat")
    if stat != "OK":
        raise DataValidationError(
            f"{endpoint_label} 回應的 stat 不是 'OK'（實際是 {stat!r}），"
            "可能是非交易日、假日、或當天資料還沒公布，不應該當作解析失敗直接崩潰，"
            "但也不該假裝有資料——呼叫端要把這個當成『今天沒抓到』處理。"
        )
    fields = raw.get("fields") or []
    data_rows = raw.get("data") or []
    return [dict(zip(fields, row)) for row in data_rows]


# ---------------------------------------------------------------------------
# 每日快照抓取：把 TWSE + TPEx 當天的全市場資料整理成一個可以存檔的 dict
# ---------------------------------------------------------------------------

def fetch_daily_snapshot_dict(session, as_of: dt.date) -> dict:
    """對外發出這次執行需要的所有 HTTP 請求，回傳一個「可以直接 json.dump 存檔」的 dict。

    任何一個「全市場核心資料」端點（個股日成交、產業別）失敗都視為整次抓取失敗（fail loud）；
    非核心端點（三大法人、融資融券、注意/處置股清單）失敗則記錄警告、該部分留空，
    讓報告本身可以照樣產生，但會在 `self_check` 的品質提示裡誠實告知使用者「這部分今天沒抓到」，
    不會假裝有資料。
    """
    result: dict = {"as_of": as_of.isoformat(), "fetched_at": dt.datetime.now(dt.timezone.utc).isoformat()}

    # --- 核心：個股日成交資訊（上市 TWSE + 上櫃 TPEx），沒有這個就沒辦法產報告，直接失敗 ---
    try:
        twse_daily = _get_json(session, f"{TWSE_BASE}/exchangeReport/STOCK_DAY_ALL")
    except Exception as exc:  # noqa: BLE001
        raise DataSourceUnavailableError("TWSE STOCK_DAY_ALL", exc) from exc
    result["twse_daily"] = twse_daily

    try:
        tpex_daily = _get_json(session, f"{TPEX_BASE}/tpex_mainboard_daily_close_quotes")
    except Exception as exc:  # noqa: BLE001
        logger.warning("TPEx 上櫃每日收盤行情抓取失敗，本次快照只會有上市(TWSE)資料: %r", exc)
        tpex_daily = []
    result["tpex_daily"] = tpex_daily

    # --- 產業別對照（用來做族群分類，規劃書第4節「族群強度」的基礎） ---
    try:
        result["twse_industry"] = _get_json(session, f"{TWSE_BASE}/opendata/t187ap03_L")
    except Exception as exc:  # noqa: BLE001
        logger.warning("TWSE 產業別資料抓取失敗: %r", exc)
        result["twse_industry"] = []

    # --- 三大法人買賣超 ---
    # 注意：這個資料集不在 openapi.twse.com.tw（新版開放資料平台）上，要用證交所舊版
    # 「盤後資訊」系統的 www.twse.com.tw/rwd/zh/fund/T86（見 TWSE_LEGACY_BASE 說明），
    # 回傳格式也跟其他端點不同，用 _rows_from_fields_data 轉換成一致的物件陣列。
    try:
        date_str = as_of.strftime("%Y%m%d")
        raw = _get_json(
            session, f"{TWSE_LEGACY_BASE}/fund/T86?date={date_str}&selectType=ALL&response=json"
        )
        result["twse_institutional"] = _rows_from_fields_data(raw, "TWSE 三大法人買賣超(T86)")
    except Exception as exc:  # noqa: BLE001
        logger.warning("TWSE 三大法人買賣超抓取失敗: %r", exc)
        result["twse_institutional"] = None  # None 代表「沒抓到」，區別於「抓到但是空清單」

    # --- 融資融券 ---
    try:
        result["twse_margin"] = _get_json(session, f"{TWSE_BASE}/exchangeReport/MI_MARGN")
    except Exception as exc:  # noqa: BLE001
        logger.warning("TWSE 融資融券資料抓取失敗: %r", exc)
        result["twse_margin"] = None

    # --- 注意股票／處置股票（端點路徑尚未在真實環境驗證過，見檔案開頭聲明） ---
    for key, path in (("twse_watch", "/announcement/notice"), ("twse_disposition", "/announcement/punish")):
        try:
            result[key] = _get_json(session, f"{TWSE_BASE}{path}")
        except Exception as exc:  # noqa: BLE001
            logger.warning("TWSE %s 抓取失敗（端點路徑可能需要核對）: %r", key, exc)
            result[key] = None

    return result


# ---------------------------------------------------------------------------
# 歷史快照存取（逐日累積）
# ---------------------------------------------------------------------------

class DailySnapshotStore:
    """把每日快照存成 `<data_dir>/daily/YYYY-MM-DD.json`，並提供「讀最近 N 天」的功能。"""

    def __init__(self, data_dir: str | Path):
        self.data_dir = Path(data_dir)
        self.daily_dir = self.data_dir / "daily"
        self.daily_dir.mkdir(parents=True, exist_ok=True)

    def save(self, as_of: dt.date, snapshot_dict: dict) -> Path:
        path = self.daily_dir / f"{as_of.isoformat()}.json"
        path.write_text(json.dumps(snapshot_dict, ensure_ascii=False), encoding="utf-8")
        return path

    def load(self, as_of: dt.date) -> dict | None:
        path = self.daily_dir / f"{as_of.isoformat()}.json"
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def load_recent(self, as_of: dt.date, lookback_days: int = 60) -> list[dict]:
        """讀出 `as_of` 當天（含）往前最多 `lookback_days` 個「有存到檔案」的快照，依日期由舊到新排序。

        用「往前找存在的檔案」而不是「往前數 N 個日曆天」，這樣遇到假日／系統中斷沒跑的日子會自動跳過，
        不會讓歷史序列裡出現缺洞。
        """
        candidates = sorted(self.daily_dir.glob("*.json"))
        cutoff_files = [p for p in candidates if p.stem <= as_of.isoformat()]
        selected = cutoff_files[-lookback_days:]
        return [json.loads(p.read_text(encoding="utf-8")) for p in selected]


# ---------------------------------------------------------------------------
# 把「一天的原始快照 dict」整理成當天各股的 row（供組 DataFrame 用）
# ---------------------------------------------------------------------------

def _parse_market_rows(raw_rows: list[dict], market_label: str) -> dict:
    """回傳 {stock_id: {open, high, low, close, volume, name}}，成交量已經從「股」換算成「張」
    （跟 FixtureProvider 的慣例一致，見 data_sources.py 裡 ScoringConfig.min_liquidity_avg_volume_lots
    的單位假設）。
    """
    out = {}
    for row in raw_rows:
        try:
            code_key = _find_key(row, "Code") if any("Code" in k for k in row.keys()) else _find_key(row, "代號")
        except DataValidationError:
            code_key = _find_key(row, "代號")
        code = str(row[code_key]).strip()
        if not code:
            continue

        def _pick(*tokens):
            try:
                return _to_float(row[_find_key(row, *tokens)])
            except DataValidationError:
                return float("nan")

        volume_shares = _pick("Volume") if any("Volume" in k for k in row.keys()) else _pick("成交股數")
        out[code] = {
            "open": _pick("Opening") if any("Opening" in k for k in row.keys()) else _pick("開盤"),
            "high": _pick("Highest") if any("Highest" in k for k in row.keys()) else _pick("最高"),
            "low": _pick("Lowest") if any("Lowest" in k for k in row.keys()) else _pick("最低"),
            "close": _pick("Closing") if any("Closing" in k for k in row.keys()) else _pick("收盤"),
            "volume": (volume_shares / 1000.0) if volume_shares == volume_shares else float("nan"),  # 股->張
            "market": market_label,
        }
    return out


def build_snapshot(as_of: dt.date, history: list[dict]) -> MarketSnapshot:
    """把 `DailySnapshotStore.load_recent` 讀回來的一串原始快照 dict，組成 `MarketSnapshot`。

    `history` 最後一筆必須是 `as_of` 當天的快照（若當天還沒存進去，呼叫端要自己先呼叫
    `fetch_daily_snapshot_dict` + `DailySnapshotStore.save`，再把完整的 history 傳進來）。
    """
    if not history:
        raise DataValidationError("history 是空的，至少要有當天這一筆快照才能組出 MarketSnapshot")
    today_raw = history[-1]
    if today_raw.get("as_of") != as_of.isoformat():
        raise DataValidationError(
            f"history 最後一筆的日期 {today_raw.get('as_of')} 跟要求的 as_of={as_of.isoformat()} 不符，"
            "呼叫端邏輯有誤：build_snapshot 假設 history[-1] 就是今天。"
        )

    # --- 逐日組出每檔股票的 close_hist / volume_hist ---
    close_hist_by_stock: dict[str, list[float]] = {}
    volume_hist_by_stock: dict[str, list[float]] = {}
    for day_raw in history:
        day_rows = _parse_market_rows(day_raw.get("twse_daily") or [], "TWSE")
        day_rows.update(_parse_market_rows(day_raw.get("tpex_daily") or [], "TPEx"))
        for stock_id, r in day_rows.items():
            close_hist_by_stock.setdefault(stock_id, []).append(r["close"])
            volume_hist_by_stock.setdefault(stock_id, []).append(r["volume"])

    today_rows = _parse_market_rows(today_raw.get("twse_daily") or [], "TWSE")
    today_rows.update(_parse_market_rows(today_raw.get("tpex_daily") or [], "TPEx"))

    # --- 產業別 ---
    industry_map: dict[str, str] = {}
    for row in today_raw.get("twse_industry") or []:
        try:
            code = str(row[_find_key(row, "代號")]).strip()
            sector = str(row[_find_key(row, "產業")]).strip()
            if code and sector:
                industry_map[code] = sector
        except DataValidationError:
            continue  # 產業別抓取失敗時不擋整體流程，見 self_check 的品質提示

    # --- 組 ohlcv DataFrame ---
    ohlcv_rows = []
    for stock_id, r in today_rows.items():
        closes = np.array(close_hist_by_stock.get(stock_id, [r["close"]]), dtype=float)
        volumes = np.array(volume_hist_by_stock.get(stock_id, [r["volume"]]), dtype=float)
        prev_close = closes[-2] if len(closes) >= 2 else float("nan")
        ohlcv_rows.append(
            {
                "stock_id": stock_id,
                "sector": industry_map.get(stock_id, "未分類"),
                "close": r["close"],
                "prev_close": prev_close,
                "open": r["open"],
                "high": r["high"],
                "low": r["low"],
                "volume": r["volume"],
                "close_hist": closes,
                "volume_hist": volumes,
            }
        )
    if not ohlcv_rows:
        raise DataValidationError("今天的快照解析後沒有任何一檔股票的資料，可能是欄位解析全部失敗，見上面的錯誤")
    ohlcv = pd.DataFrame(ohlcv_rows).set_index("stock_id")

    # --- 三大法人 ---
    inst_rows = []
    if today_raw.get("twse_institutional"):
        for row in today_raw["twse_institutional"]:
            try:
                code = str(row[_find_key(row, "代號")]).strip()
                foreign = _to_int(row[_find_key(row, "外", "買賣超")]) if any(
                    "外" in k and "買賣超" in k for k in row.keys()
                ) else 0
                trust = _to_int(row[_find_key(row, "投信", "買賣超")]) if any(
                    "投信" in k and "買賣超" in k for k in row.keys()
                ) else 0
                dealer_keys = [k for k in row.keys() if "自營商買賣超股數" == k]
                dealer = _to_int(row[dealer_keys[0]]) if dealer_keys else 0
                inst_rows.append({"stock_id": code, "foreign_net": foreign, "trust_net": trust, "dealer_net": dealer})
            except (DataValidationError, KeyError):
                continue
    institutional_flow = (
        pd.DataFrame(inst_rows).set_index("stock_id")
        if inst_rows
        else pd.DataFrame(columns=["foreign_net", "trust_net", "dealer_net"])
    )

    # --- 融資融券 ---
    margin_rows = []
    if today_raw.get("twse_margin"):
        for row in today_raw["twse_margin"]:
            try:
                code = str(row[_find_key(row, "股票代號")]).strip()
                balance = _to_int(row[_find_key(row, "融資", "今日餘額")])
                prev_balance = _to_int(row[_find_key(row, "融資", "前日餘額")])
                margin_rows.append(
                    {"stock_id": code, "margin_balance": balance, "margin_change": balance - prev_balance}
                )
            except (DataValidationError, KeyError):
                continue
    margin_data = (
        pd.DataFrame(margin_rows).set_index("stock_id")
        if margin_rows
        else pd.DataFrame(columns=["margin_balance", "margin_change"])
    )

    # --- 注意／處置／全額交割／當沖資格 ---
    # 端點尚未在真實環境驗證過（見檔案開頭聲明），抓不到就回傳空集合，並且一定要在 self_check
    # 的品質提示裡誠實告知「今天沒能取得注意/處置股清單」，不能悄悄當作「今天沒有注意股」。
    watch_list: set = set()
    if today_raw.get("twse_watch"):
        for row in today_raw["twse_watch"]:
            try:
                watch_list.add(str(row[_find_key(row, "代號")]).strip())
            except DataValidationError:
                break

    disposition_list: set = set()
    if today_raw.get("twse_disposition"):
        for row in today_raw["twse_disposition"]:
            try:
                disposition_list.add(str(row[_find_key(row, "代號")]).strip())
            except DataValidationError:
                break

    full_delivery_list: set = set()  # 全額交割股清單端點尚待找到，先留空並在報告品質提示裡註明

    all_stock_ids = set(today_rows.keys())
    # 當沖資格是保守近似：排除處置股與全額交割股之後的其餘股票視為可「現股當沖(先買後賣)」；
    # 「先賣後買」(放空)的資格規則更嚴格（平盤以下不得放空等），這裡先用同一份排除清單做保守近似，
    # 這個近似之後應該優先找 TWTB4U（當日沖銷交易標的）端點來取代，見 KNOWN_ISSUES.md。
    daytrade_long_eligible = all_stock_ids - disposition_list - full_delivery_list
    daytrade_short_eligible = set(daytrade_long_eligible)  # 保守近似，見上一行說明

    return MarketSnapshot(
        as_of=as_of,
        source_tag=SourceTag.TWSE_OPENAPI,
        ohlcv=ohlcv,
        industry_map=industry_map,
        institutional_flow=institutional_flow,
        margin_data=margin_data,
        watch_list=watch_list,
        disposition_list=disposition_list,
        full_delivery_list=full_delivery_list,
        daytrade_long_eligible=daytrade_long_eligible,
        daytrade_short_eligible=daytrade_short_eligible,
        intl_snapshot={},  # 由 YFinanceIntlProvider 另外填入，見 fetch_and_report.py
    )


class RealTwseProvider(MarketDataProvider):
    """組合 `fetch_daily_snapshot_dict` + `DailySnapshotStore` + `build_snapshot` 的完整流程。

    這是給 `scripts/fetch_and_report.py`（GitHub Actions 執行）用的高階介面；
    這個 Cowork 沙盒裡呼叫 `get_snapshot` 一樣會因為連不到網路而失敗，這是預期行為。
    """

    def __init__(self, data_dir: str | Path, lookback_days: int = 60, session=None):
        import requests

        self._store = DailySnapshotStore(data_dir)
        self._lookback_days = lookback_days
        self._session = session or requests.Session()

    @property
    def store(self) -> DailySnapshotStore:
        return self._store

    def get_snapshot(self, as_of: dt.date) -> MarketSnapshot:
        today_dict = self._store.load(as_of)
        if today_dict is None:
            today_dict = fetch_daily_snapshot_dict(self._session, as_of)
            self._store.save(as_of, today_dict)
        history = self._store.load_recent(as_of, self._lookback_days)
        return build_snapshot(as_of, history)


class RealYFinanceIntlProvider:
    """國際情勢資料：直接打 Yahoo Finance 的公開 chart JSON 端點（不依賴 yfinance 套件，減少相依）。

    這個端點在這個 Cowork 沙盒裡會被 WebFetch 的 robots.txt 檢查擋下（見 KNOWN_ISSUES.md 問題3），
    但那是 WebFetch 工具自己的政策，不是端點本身的限制——用一般的 `requests` 直接呼叫應該沒問題，
    GitHub Actions 環境下第一次執行時務必確認一次。

    2026-09-02 第一次在 GitHub Actions 真正執行後發現：全部 8 個 ticker 都收到 429 Too Many
    Requests——Yahoo Finance 會用 User-Agent 判斷是不是爬蟲，對沒有 User-Agent（requests 預設值）
    又短時間內連續打好幾個 ticker 的請求特別容易擋。修正方式有兩個：(1) `_get_json` 現在會帶一個
    一般瀏覽器的 User-Agent（見檔案開頭 `_BROWSER_USER_AGENT`）；(2) 下面每個 ticker 之間加一個
    小延遲，不要在同一瞬間連續發 8 個請求。
    """

    REQUEST_INTERVAL_SECONDS = 1.0

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

    def __init__(self, session=None):
        import requests

        self._session = session or requests.Session()

    def get_intl_snapshot(self, as_of: dt.date) -> dict:
        out = {}
        for i, (label, symbol) in enumerate(self.TICKERS.items()):
            if i > 0:
                time.sleep(self.REQUEST_INTERVAL_SECONDS)  # 避免短時間連續打 8 個請求被判定成爬蟲
            try:
                url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=5d&interval=1d"
                data = _get_json(self._session, url)
                result = data["chart"]["result"][0]
                meta = result["meta"]
                prev_close = meta.get("chartPreviousClose") or meta.get("previousClose")
                last_close = meta.get("regularMarketPrice")
                if prev_close and last_close:
                    out[label] = (last_close - prev_close) / prev_close * 100.0
                else:
                    logger.warning("%s (%s) 缺少 chartPreviousClose 或 regularMarketPrice，略過", label, symbol)
            except Exception as exc:  # noqa: BLE001
                logger.warning("國際情勢資料抓取失敗 %s (%s): %r", label, symbol, exc)
        return out

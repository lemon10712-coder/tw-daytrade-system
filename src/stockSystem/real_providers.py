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

# 產業分類代碼 -> 全名對照表。
#
# 背景：t187ap03_L（TWSE 上市公司基本資料）跟 mopsfin_t187ap03_O（TPEx 上櫃公司基本資料）
# 的「產業別」欄位給的其實是兩碼的代碼（例如 "17"），不是可讀的族群名稱——這是
# 2026-09-03 第三次真實環境查證才發現的（之前 build_snapshot 直接把這個代碼當成
# sector 存進 ohlcv，導致報告上的族群欄位只會顯示「17」「03」這種代碼，使用者根本
# 看不懂是什麼族群）。這份對照表是直接從 TWSE 官方查詢工具
# https://isin.twse.com.tw/isin/class_i.jsp?kind=4 的「產業別」下拉選單抓下來、
# 對照真實回應驗證過的（同一份代碼表上市/上櫃共用，已用 isin.twse.com.tw/isin/C_public.jsp
# 的股票資料交叉核對過多檔上市股票，代碼與名稱一致）。
SECTOR_CODE_NAME: dict[str, str] = {
    "01": "水泥工業", "02": "食品工業", "03": "塑膠工業", "04": "紡織纖維",
    "05": "電機機械", "06": "電器電纜", "08": "玻璃陶瓷", "09": "造紙工業",
    "10": "鋼鐵工業", "11": "橡膠工業", "12": "汽車工業", "13": "電子工業",
    "14": "建材營造業", "15": "航運業", "16": "觀光餐旅", "17": "金融保險業",
    "18": "貿易百貨業", "19": "綜合", "20": "其他業", "21": "化學工業",
    "22": "生技醫療業", "23": "油電燃氣業", "24": "半導體業", "25": "電腦及週邊設備業",
    "26": "光電業", "27": "通信網路業", "28": "電子零組件業", "29": "電子通路業",
    "30": "資訊服務業", "31": "其他電子業", "32": "文化創意業", "33": "農業科技業",
    "35": "綠能環保", "36": "數位雲端", "37": "運動休閒", "38": "居家生活",
}


def sector_name_for_code(code: str) -> str:
    """把產業分類代碼轉成可讀名稱；代碼不在對照表裡（例如已停用的代碼）就保留代碼本身，
    讓使用者至少看得出「這是一個代碼」而不是誤以為是正常名稱，也方便回報。
    """
    code = (code or "").strip()
    if not code:
        return "未分類"
    return SECTOR_CODE_NAME.get(code, f"未分類(代碼{code})")
# 三大法人買賣超（T86）不在新版開放資料平台（openapi.twse.com.tw）上，只能從證交所
# 舊版「盤後資訊」系統取得，回傳格式也不同（見下面 _rows_from_fields_data 的說明）。
TWSE_LEGACY_BASE = "https://www.twse.com.tw/rwd/zh"
# TPEx（櫃買中心）舊版系統，用來回補歷史每日行情（新版 openapi 的 daily_close_quotes
# 沒有日期參數，只能查「今天」，見 _fetch_tpex_day_all 說明）。
TPEX_LEGACY_BASE = "https://www.tpex.org.tw/web/stock/aftertrading/daily_close_quotes"

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


def _fetch_institutional_flow_with_lookback(
    session, as_of: dt.date, max_lookback_days: int = 7
) -> list[dict] | None:
    """查 T86（三大法人買賣超），從 `as_of` 往前找，遇到第一個「已經公布資料」的交易日就回傳那天的。

    背景：T86 是收盤後才會公布當天資料的端點，如果排程在開盤前執行（例如台北時間 08:15），
    `as_of`（今天）當天的資料根本還不存在，一定會拿到 stat 不是 "OK" 的回應。這不是端點壞掉，
    是「該查哪一天」的邏輯問題——所以這裡改成往前試，遇到週末、假日、或當天還沒公布都自動跳到
    再前一天，最多試 `max_lookback_days` 天，找不到就回傳 None（呼叫端會照樣把這個當成
    「今天沒抓到」處理，不會假裝有資料）。
    """
    for delta in range(max_lookback_days):
        query_date = as_of - dt.timedelta(days=delta)
        date_str = query_date.strftime("%Y%m%d")
        try:
            raw = _get_json(
                session, f"{TWSE_LEGACY_BASE}/fund/T86?date={date_str}&selectType=ALL&response=json"
            )
            return _rows_from_fields_data(raw, f"TWSE 三大法人買賣超(T86, {date_str})")
        except Exception as exc:  # noqa: BLE001
            logger.info("TWSE 三大法人買賣超 %s 沒有已公布的資料，往前找上一個交易日: %r", date_str, exc)
            continue
    return None


def _fetch_twse_day_all(session, as_of: dt.date) -> list[dict]:
    """查 TWSE 舊版「盤後資訊」系統的每日收盤行情（MI_INDEX），拿「指定某一天」全部上市股票的資料。

    背景：`fetch_daily_snapshot_dict` 平常用的 `openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL`
    只能查「今天」，沒有日期參數，沒辦法拿來回補過去的歷史快照。MI_INDEX 是同一套舊系統
    （跟 T86 三大法人一樣，見 TWSE_LEGACY_BASE 說明）的「指定日期」版本。

    **2026-09-03 第二次真實執行後更新（第一次的假設也是錯的，已對照真實回應驗證過）**：
    原本以為跟 T86 一樣是扁平的 `{"stat","fields","data"}`，結果第一次修好 TPEx 之後重新產生
    報告，發現族群強度全部是 nan%——追下去才發現 MI_INDEX 這個端點（帶 `type=ALL` 時）回傳的
    其實是 `{"stat","tables":[{...}, ...]}`，資料被拆成 10 個左右的表格（大盤指數、報酬指數、
    漲跌證券數…），**不是**扁平的 fields/data，所以 `_rows_from_fields_data` 原本的假設完全
    找不到 "fields"/"data"，安靜地回傳空陣列（沒有拋錯，因為 `stat` 仍然是 "OK"）——這是回補
    第一輪時「沒報錯但資料其實是空的」的根因。真正要的是 `tables` 陣列裡標題包含
    「每日收盤行情」的那一個表格，格式才是每檔股票一列的 `{"fields","data"}`。
    """
    date_str = as_of.strftime("%Y%m%d")
    raw = _get_json(
        session, f"{TWSE_LEGACY_BASE}/afterTrading/MI_INDEX?date={date_str}&type=ALL&response=json"
    )
    stat = raw.get("stat") if isinstance(raw, dict) else None
    if stat is not None and stat != "OK":
        raise DataValidationError(
            f"TWSE 每日收盤行情(MI_INDEX, {date_str}) 回應的 stat 不是 'OK'（實際是 {stat!r}），"
            "可能是非交易日/假日/當天資料還沒公布，呼叫端要把這天當成『沒抓到』處理。"
        )
    tables = raw.get("tables") if isinstance(raw, dict) else None
    quote_table = None
    if isinstance(tables, list):
        for table in tables:
            title = (table or {}).get("title") or ""
            if "每日收盤行情" in title:
                quote_table = table
                break
    if quote_table is not None:
        fields = quote_table.get("fields") or []
        data_rows = quote_table.get("data") or []
        if fields and data_rows:
            return [dict(zip(fields, row)) for row in data_rows]
    raise DataValidationError(
        f"TWSE 每日收盤行情(MI_INDEX, {date_str}) 回應裡找不到標題含「每日收盤行情」且有資料的表格，"
        f"實際回應的 top-level key 有：{sorted(raw.keys()) if isinstance(raw, dict) else type(raw)}，"
        f"tables 的標題有：{[((t or {}).get('title')) for t in tables] if isinstance(tables, list) else None}。"
        "可能是非交易日/假日、或格式又跟這裡假設的不一樣，需要對照這次錯誤訊息確認。"
    )


def _fetch_tpex_day_all(session, as_of: dt.date) -> list[dict]:
    """查 TPEx（櫃買中心）舊版系統的每日收盤行情，拿「指定某一天」全部上櫃股票的資料。

    背景同 `_fetch_twse_day_all`：平常用的 `tpex_mainboard_daily_close_quotes` 只能查今天。
    TPEx 舊系統的日期格式是民國年（西元年 - 1911）的 YYY/MM/DD。

    **2026-09-03 第一次在 GitHub Actions 真正執行後更新（已對照真實回應驗證過）**：
    原本猜測的 `{"aaData": [...]}` 格式是錯的，實際回應長這樣：

        {"date": "20260902",
         "tables": [{"title": "上櫃股票行情", "fields": ["代號","名稱","收盤",...],
                      "data": [["00411A","主動統一前沿科技","9.50",...], ...], ...}],
         "flagField": ..., "stat": ...}

    跟 TWSE 舊系統的 `{"fields","data"}` 形狀很像，只是多包了一層 "tables" 陣列——
    這裡直接攤平每個 table 的 fields/data 成物件陣列（邏輯跟 `_rows_from_fields_data`
    一樣，只是要先從 "tables" 裡取出來，所以沒有直接共用那個函式）。非交易日/假日時
    "tables" 會是空陣列或缺欄位，一樣會被下面的防禦邏輯抓到、清楚報錯。
    """
    roc_year = as_of.year - 1911
    date_str = f"{roc_year}/{as_of.month:02d}/{as_of.day:02d}"
    raw = _get_json(session, f"{TPEX_LEGACY_BASE}/stk_quote_result.php?l=zh-tw&d={date_str}&se=EW")
    tables = raw.get("tables") if isinstance(raw, dict) else None
    if isinstance(tables, list) and tables:
        rows: list[dict] = []
        for table in tables:
            fields = table.get("fields") or []
            data_rows = table.get("data") or []
            if not fields or not data_rows:
                continue
            rows.extend(dict(zip(fields, row)) for row in data_rows)
        if rows:
            return rows
    raise DataValidationError(
        f"TPEx 每日收盤行情（{date_str}）回應裡沒有可用的 tables 資料，"
        f"實際回應的 top-level key 有：{sorted(raw.keys()) if isinstance(raw, dict) else type(raw)}。"
        "可能是非交易日/假日、或格式又跟這裡假設的不一樣，需要對照這次錯誤訊息確認。"
    )


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
    # 注意：t187ap03_L／mopsfin_t187ap03_O 的「產業別」欄位給的是兩碼代碼（例如 "17"），
    # 不是可讀名稱，要另外用 SECTOR_CODE_NAME 轉換（見該常數的說明），build_snapshot 會做這件事。
    try:
        result["twse_industry"] = _get_json(session, f"{TWSE_BASE}/opendata/t187ap03_L")
    except Exception as exc:  # noqa: BLE001
        logger.warning("TWSE 產業別資料抓取失敗: %r", exc)
        result["twse_industry"] = []

    # 2026-09-03 第三次真實環境查證後新增：之前只抓 TWSE（上市）的產業別，完全沒抓 TPEx
    # （上櫃）的，導致所有上櫃股票的族群永遠是「未分類」。TPEx openapi 裡對應 t187ap03_L
    # 的端點是 mopsfin_t187ap03_O（欄位是英文：SecuritiesCompanyCode／SecuritiesIndustryCode，
    # 已對照真實回應驗證過，見該端點 swagger）。
    try:
        result["tpex_industry"] = _get_json(session, f"{TPEX_BASE}/mopsfin_t187ap03_O")
    except Exception as exc:  # noqa: BLE001
        logger.warning("TPEx 產業別資料抓取失敗: %r", exc)
        result["tpex_industry"] = []

    # --- 三大法人買賣超 ---
    # 注意：這個資料集不在 openapi.twse.com.tw（新版開放資料平台）上，要用證交所舊版
    # 「盤後資訊」系統的 www.twse.com.tw/rwd/zh/fund/T86（見 TWSE_LEGACY_BASE 說明），
    # 回傳格式也跟其他端點不同，用 _rows_from_fields_data 轉換成一致的物件陣列。
    #
    # 2026-09-03 第二次在 GitHub Actions 真正執行後發現：排程是台北時間 08:15（開盤前）執行，
    # 這時候 as_of（今天）的三大法人資料根本還沒公布（要收盤後才有），用 as_of 當天的日期查
    # 永遠只會拿到 stat 不是 "OK" 的「今天沒資料」——不是端點壞掉，是查詢的日期邏輯本來就錯了。
    # 改成從 as_of 往前找，遇到第一個「已經公布資料」的交易日就用那天的（週末／假日／還沒公布
    # 都會自動跳過，最多往前找 7 天，避免連假期間無限往前找）。
    try:
        result["twse_institutional"] = _fetch_institutional_flow_with_lookback(session, as_of)
        if result["twse_institutional"] is None:
            logger.warning("TWSE 三大法人買賣超：往前找了 7 天都沒有已公布的資料")
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

def _pick_first_matching(row: dict, *token_groups: tuple) -> float:
    """依序試過每一組 tokens，回傳第一組能在 row 裡找到欄位的解析結果；全部都找不到就回傳 nan。

    這是為了同時兼容三種曾經在真實回應裡出現過的欄位命名慣例（見下面的呼叫端說明），
    而不是只猜一種就假設一定對——這個檔案已經因為只猜一種格式吃過兩次虧了。
    """
    for tokens in token_groups:
        if any(all(tok in k for tok in tokens) for k in row.keys()):
            try:
                return _to_float(row[_find_key(row, *tokens)])
            except DataValidationError:
                continue
    return float("nan")


def _parse_market_rows(raw_rows: list[dict], market_label: str) -> dict:
    """回傳 {stock_id: {open, high, low, close, volume, name, market}}，成交量已經從「股」
    換算成「張」（跟 FixtureProvider 的慣例一致，見 data_sources.py 裡
    ScoringConfig.min_liquidity_avg_volume_lots 的單位假設）。

    **2026-09-03 第三次真實環境查證後更新**：目前實際會流進這個函式的資料有三種欄位命名慣例，
    缺一種都會讓那個來源的股票被悄悄解析成 nan（不會報錯，因為外層的 `_pick`/`_pick_first_matching`
    找不到欄位時是回傳 nan，不是拋例外——這是刻意的，因為單一欄位缺漏不該讓整筆資料作廢）：

    1. TWSE 新版 openapi（`STOCK_DAY_ALL`，每天的即時資料）：英文、`-ing`/`-est` 字尾，
       例如 `OpeningPrice`／`ClosingPrice`／`TradeVolume`／`Name`。
    2. TPEx 新版 openapi（`tpex_mainboard_daily_close_quotes`，每天的即時資料）：英文但是
       **短字尾**，例如 `Open`／`Close`／`TradingShares`／`CompanyName`——這組跟第1種长得像
       但欄位名稱其實不一樣，**之前這裡完全沒有處理這組，導致每天正式產生報告時，所有上櫃
       股票的今日 open/high/low/close/volume 全部是 nan、均線/ATR 計算會被污染，這是候選清單
       裡從來沒出現過上櫃股票的根因**，2026-09-03 直接對照真實回應才抓到。
    3. TWSE／TPEx 舊版「盤後資訊」系統（回補歷史用，見 `_fetch_twse_day_all`／
       `_fetch_tpex_day_all`）：中文欄位，例如 `開盤`／`收盤`／`成交股數`／`名稱`。

    股票代號的判斷（`Code`／`SecuritiesCompanyCode`都含有 "Code" 這個子字串，`代號`則是中文
    慣例)本來就已經涵蓋這三種來源，不需要改。
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

        def _name_pick():
            if "Name" in row:  # 精確比對，避免不小心吃到 "CompanyName" 這種也含有 Name 的欄位
                return str(row["Name"]).strip()
            if any("CompanyName" in k for k in row.keys()):
                return str(row[_find_key(row, "CompanyName")]).strip()
            try:
                return str(row[_find_key(row, "名稱")]).strip()
            except DataValidationError:
                return ""

        out[code] = {
            "name": _name_pick(),
            "open": _pick_first_matching(row, ("Opening",), ("Open",), ("開盤",)),
            "high": _pick_first_matching(row, ("Highest",), ("High",), ("最高",)),
            "low": _pick_first_matching(row, ("Lowest",), ("Low",), ("最低",)),
            "close": _pick_first_matching(row, ("Closing",), ("Close",), ("收盤",)),
            "volume": (
                lambda shares: (shares / 1000.0) if shares == shares else float("nan")  # 股->張
            )(_pick_first_matching(row, ("Volume",), ("TradingShares",), ("成交股數",))),
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

    # --- 產業別：分別解析 TWSE(上市)／TPEx(上櫃) 的公司基本資料，兩邊欄位名稱不一樣
    # （見 SECTOR_CODE_NAME 常數與 fetch_daily_snapshot_dict 的說明），代碼一律轉成可讀名稱
    # 再存，這樣 ohlcv 的 "sector" 欄位跟這裡的 industry_map 才會一致（screen_sector 是拿
    # sector_strength 算出來的 sector 名稱回頭去 industry_map 找同名的股票，兩邊沒對齊會
    # 篩出 0 檔股票）。
    industry_map: dict[str, str] = {}
    for row in today_raw.get("twse_industry") or []:
        try:
            code = str(row[_find_key(row, "代號")]).strip()
            industry_code = str(row[_find_key(row, "產業")]).strip()
            if code and industry_code:
                industry_map[code] = sector_name_for_code(industry_code)
        except DataValidationError:
            continue  # 產業別抓取失敗時不擋整體流程，見 self_check 的品質提示

    for row in today_raw.get("tpex_industry") or []:
        try:
            code = str(row[_find_key(row, "SecuritiesCompanyCode")]).strip()
            industry_code = str(row[_find_key(row, "SecuritiesIndustryCode")]).strip()
            if code and industry_code:
                industry_map[code] = sector_name_for_code(industry_code)
        except DataValidationError:
            continue  # 同上，TPEx 產業別抓取失敗時不擋整體流程

    # --- 組 ohlcv DataFrame ---
    ohlcv_rows = []
    for stock_id, r in today_rows.items():
        closes = np.array(close_hist_by_stock.get(stock_id, [r["close"]]), dtype=float)
        volumes = np.array(volume_hist_by_stock.get(stock_id, [r["volume"]]), dtype=float)
        prev_close = closes[-2] if len(closes) >= 2 else float("nan")
        ohlcv_rows.append(
            {
                "stock_id": stock_id,
                "name": r.get("name") or stock_id,  # 抓不到名稱時至少顯示代號，不要顯示空字串
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
    # 2026-09-03 第三次真實環境查證後修正：T86 回傳的買賣超「股數」欄位單位是股，不是張，
    # 但 `MarketSnapshot.institutional_flow` 的欄位說明跟 FixtureProvider 的合成資料（見
    # data_sources.py，量級是幾千到幾萬）都是以「張」為單位，stock_screener.py 的評分公式
    # 也是照「張」的量級校準的——這裡少做了股->張（除以1000）的換算，導致：
    # (1) 報告上顯示的「三大法人買超xxx張」其實是股數，數字誇大了1000倍；
    # (2) 評分公式 `min(inst_net/1000, 20)` 對真實資料而言幾乎必定瞬間打滿20分上限
    #     （因為 inst_net 還是股數量級），法人籌碼分數形同虛設。
    # 換算成張之後，兩邊都會回到原本設計時預期的量級，不需要再改 stock_screener.py。
    # 外資淨買賣超也一併把「外陸資(不含外資自營商)」跟「外資自營商」兩個子項加總，
    # 原本只取第一個子項，會低估真正的外資合計買賣超。
    inst_rows = []
    if today_raw.get("twse_institutional"):
        for row in today_raw["twse_institutional"]:
            try:
                code = str(row[_find_key(row, "代號")]).strip()
                # 注意：「外陸資買賣超股數(不含外資自營商)」這個欄位名稱本身也含有
                # "外資自營商" 這個子字串（在括號的排除說明裡），如果 foreign_dealer 用
                # 一般的「包含子字串」比對，會兩個都比對到同一個「外陸資」欄位、把它算兩次、
                # 完全漏掉真正的「外資自營商買賣超股數」欄位——這裡改用精確比對欄位名稱
                # （已對照 T86 真實回應驗證過的確切欄位名稱），跟 dealer_keys 的作法一致。
                foreign_ordinary_keys = [k for k in row.keys() if "外陸資買賣超股數" in k]
                foreign_ordinary = _to_int(row[foreign_ordinary_keys[0]]) if foreign_ordinary_keys else 0
                foreign_dealer_keys = [k for k in row.keys() if "外資自營商買賣超股數" == k]
                foreign_dealer = _to_int(row[foreign_dealer_keys[0]]) if foreign_dealer_keys else 0
                foreign = (foreign_ordinary + foreign_dealer) / 1000.0  # 股->張
                trust = (
                    _to_int(row[_find_key(row, "投信", "買賣超")]) if any(
                        "投信" in k and "買賣超" in k for k in row.keys()
                    ) else 0
                ) / 1000.0  # 股->張
                dealer_keys = [k for k in row.keys() if "自營商買賣超股數" == k]
                dealer = (_to_int(row[dealer_keys[0]]) if dealer_keys else 0) / 1000.0  # 股->張
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

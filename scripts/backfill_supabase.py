#!/usr/bin/env python3
"""一次性歷史回補：把 data/daily/*.json 這些已經存在的每日快照，解析後寫進 Supabase。

設計原則：不重新刻一份解析邏輯，直接沿用 stockSystem.real_providers 裡
DailySnapshotStore / build_snapshot 這兩個正式報告產生流程本來就在用的函式——
確保「資料庫裡看到的」跟「report.py 產生的正式報告」永遠是同一套產出，不會兩邊對不上。

只回補 data/daily/ 底下已經有的檔案（目前是 2026-07-24 ~ 2026-09-03），完全不對外發任何
HTTP 請求，純粹是本地檔案讀取 + 寫進 Supabase，所以在 GitHub Actions 執行只需要 checkout
這個 repo，不需要額外的網路權限（除了連 Supabase 本身）。

用法（在 repo 根目錄，也就是 .github/workflows/backfill-supabase.yml 執行的方式）：
    export SUPABASE_URL=https://xxxx.supabase.co
    export SUPABASE_SERVICE_ROLE_KEY=xxxx   # Project Settings -> API -> service_role key
    export PYTHONPATH=src
    python scripts/backfill_supabase.py

注意：這裡故意用 service_role key 而不是 anon key——写入這幾張表本來就不該開放給匯名/
前端角色，也是為什麼 Supabase 那邊這些表已經打開 RLS、且沒有加任何 policy（service_role
本來就會繞過 RLS，其餘角色一律讀寫不到，見 Supabase migration `enable_rls_all_tables`）。
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
from pathlib import Path

import requests

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from stockSystem.real_providers import DailySnapshotStore, build_snapshot  # noqa: E402

DATA_DIR = REPO_ROOT / "data"


def _clean_float(value) -> float | None:
    """把 nan 轉成 None——Postgres/PostgREST 的 JSON 解析不吃字面上的 NaN token。"""
    try:
        if value is None:
            return None
        f = float(value)
        return None if f != f else f  # f != f 只在 nan 時成立
    except (TypeError, ValueError):
        return None


def _clean_int(value) -> int | None:
    f = _clean_float(value)
    return int(f) if f is not None else None


class SupabaseWriter:
    def __init__(self, base_url: str, service_role_key: str):
        self.base_url = base_url.rstrip("/")
        self.headers = {
            "apikey": service_role_key,
            "Authorization": f"Bearer {service_role_key}",
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates,return=minimal",
        }

    def upsert(self, table: str, rows: list[dict], on_conflict: str, batch_size: int = 500) -> int:
        if not rows:
            return 0
        url = f"{self.base_url}/rest/v1/{table}?on_conflict={on_conflict}"
        written = 0
        for i in range(0, len(rows), batch_size):
            batch = rows[i : i + batch_size]
            resp = requests.post(url, headers=self.headers, data=json.dumps(batch), timeout=60)
            if resp.status_code >= 300:
                raise RuntimeError(
                    f"寫入 {table} 失敗（狀態碼 {resp.status_code}，這批第 {i} 筆起）：{resp.text[:800]}"
                )
            written += len(batch)
        return written


def _row_get(row, key, default=None):
    """pandas Series 沒有這個欄位時回傳 default，而不是丟 KeyError——用來相容之後
    build_snapshot 可能新增/移除欄位的情況（防禦性寫法，呼應這個專案一貫的風格）。
    """
    try:
        return row[key]
    except KeyError:
        return default


def main() -> int:
    supabase_url = os.environ.get("SUPABASE_URL")
    service_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not supabase_url or not service_key:
        print("錯誤：請設定環境變數 SUPABASE_URL 與 SUPABASE_SERVICE_ROLE_KEY（見本檔案開頭用法說明）")
        return 1

    writer = SupabaseWriter(supabase_url, service_key)
    store = DailySnapshotStore(DATA_DIR)

    files = sorted((DATA_DIR / "daily").glob("*.json"))
    dates = [dt.date.fromisoformat(p.stem) for p in files]
    if not dates:
        print("data/daily/ 底下沒有任何快照檔案，沒有東西可以回補")
        return 0

    print(f"共 {len(dates)} 個交易日待回補：{dates[0].isoformat()} ~ {dates[-1].isoformat()}")

    total_ok = 0
    total_failed = 0
    for as_of in dates:
        history = store.load_recent(as_of, lookback_days=60)
        if not history or history[-1].get("as_of") != as_of.isoformat():
            print(f"[跳過] {as_of.isoformat()}：找不到當天快照檔案")
            continue
        try:
            snapshot = build_snapshot(as_of, history)
        except Exception as exc:  # noqa: BLE001
            print(f"[解析失敗] {as_of.isoformat()}：{exc!r}")
            total_failed += 1
            continue

        date_str = as_of.isoformat()

        ohlcv_rows = [
            {
                "trade_date": date_str,
                "stock_id": stock_id,
                "sector": _row_get(row, "sector"),
                "name": _row_get(row, "name"),
                "open": _clean_float(_row_get(row, "open")),
                "high": _clean_float(_row_get(row, "high")),
                "low": _clean_float(_row_get(row, "low")),
                "close": _clean_float(_row_get(row, "close")),
                "prev_close": _clean_float(_row_get(row, "prev_close")),
                "volume_lots": _clean_float(_row_get(row, "volume")),
            }
            for stock_id, row in snapshot.ohlcv.iterrows()
        ]

        inst_rows = [
            {
                "trade_date": date_str,
                "stock_id": stock_id,
                "foreign_net": _clean_int(row.get("foreign_net")),
                "trust_net": _clean_int(row.get("trust_net")),
                "dealer_net": _clean_int(row.get("dealer_net")),
            }
            for stock_id, row in snapshot.institutional_flow.iterrows()
        ]

        margin_rows = [
            {
                "trade_date": date_str,
                "stock_id": stock_id,
                "margin_balance": _clean_int(row.get("margin_balance")),
                "margin_change": _clean_int(row.get("margin_change")),
            }
            for stock_id, row in snapshot.margin_data.iterrows()
        ]

        flag_rows = [
            {
                "trade_date": date_str,
                "stock_id": stock_id,
                "is_watch": stock_id in snapshot.watch_list,
                "is_disposition": stock_id in snapshot.disposition_list,
                "is_full_delivery": stock_id in snapshot.full_delivery_list,
                "daytrade_long_eligible": stock_id in snapshot.daytrade_long_eligible,
                "daytrade_short_eligible": stock_id in snapshot.daytrade_short_eligible,
            }
            for stock_id in snapshot.ohlcv.index
        ]

        intl_rows = [
            {"trade_date": date_str, "ticker_label": label, "pct_change": _clean_float(pct)}
            for label, pct in (snapshot.intl_snapshot or {}).items()
        ]

        try:
            n1 = writer.upsert("daily_ohlcv", ohlcv_rows, "trade_date,stock_id")
            n2 = writer.upsert("institutional_flow", inst_rows, "trade_date,stock_id")
            n3 = writer.upsert("margin_data", margin_rows, "trade_date,stock_id")
            n4 = writer.upsert("stock_flags", flag_rows, "trade_date,stock_id")
            n5 = writer.upsert("intl_snapshot", intl_rows, "trade_date,ticker_label")
        except Exception as exc:  # noqa: BLE001
            print(f"[寫入失敗] {date_str}：{exc}")
            total_failed += 1
            continue

        print(
            f"[完成] {date_str}：daily_ohlcv={n1} institutional_flow={n2} "
            f"margin_data={n3} stock_flags={n4} intl_snapshot={n5}"
        )
        total_ok += 1

    print(f"\n回補結束：成功 {total_ok} 天，失敗/跳過 {total_failed} 天")
    return 1 if total_failed and not total_ok else 0


if __name__ == "__main__":
    raise SystemExit(main())

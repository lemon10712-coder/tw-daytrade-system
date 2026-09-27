#!/usr/bin/env python3
"""盤中期貨(微台指MXF)監控排程進入點。

由 `.github/workflows/intraday-futures-monitor.yml` 在交易日盤中每 5 分鐘呼叫一次。
這個 workflow 完全獨立於 `daily-report.yml`（開盤前的每日選股報告），互不影響：
這支腳本失敗不會讓每日報告掛掉，每日報告的任何改動也不會動到這支腳本。

設計取捨（2026-09-27，誠實記錄）：這個 workflow 的唯一工作就是「盤中期貨監控」，
不像 daily-report.yml 裡的回測/期貨區塊是「錦上添花、失敗只警告」——這裡失敗了就
直接用非零結束碼結束，讓 GitHub Actions 顯示這次執行失敗，方便之後排查，不假裝
安靜跳過。

輸出：
- `data/futures_intraday/YYYY-MM-DD.json`：當天目前為止抓到的分鐘序列 + 最新一次
  算出的進出場範圍預測，每次執行覆寫（不是逐筆append，因為 objId=3/13 端點本身
  每次都回傳「當天到目前為止的完整序列」，覆寫最新的就是最準確的）。
- `docs/futures_live.html`：獨立的靜態頁面（不依賴 build_site.py 的樣式/邏輯），
  給 GitHub Pages 直接服務，網址是
  https://lemon10712-coder.github.io/tw-daytrade-system/futures_live.html。
- `docs/data/futures_intraday_latest.json`：跟 docs/futures_live.html 同樣內容的
  結構化版本，之後如果 Claude Artifact 儀表板想要抓最新一筆快照可以用這個路徑。
"""

from __future__ import annotations

import html
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from stockSystem import futures_intraday as fi  # noqa: E402
from stockSystem.futures_data import IndexDailyStore  # noqa: E402
from stockSystem.futures_signals import decide_direction  # noqa: E402

TAIPEI_TZ = timezone(timedelta(hours=8))

_BIAS_MAP = {"long": "偏多", "short": "偏空", "neutral": "中性"}


def _today_taipei():
    return datetime.now(TAIPEI_TZ).date()


def _load_prev_and_history(today):
    """回傳 (prev_bar_dict, close_hist_list)。prev_bar_dict 是「今天以前最近一筆」
    的 TAIEX 日線 OHLC dict；close_hist_list 是給 decide_direction() 用的收盤價
    歷史序列（由舊到新，不含今天，因為今天的日K線這個時間點根本還沒收盤）。
    找不到任何歷史資料時兩者都回傳 None/[]，呼叫端要自己處理這個情況。
    """
    store = IndexDailyStore(REPO_ROOT / "data")
    bars = store.load_recent(today, lookback_days=60)
    bars = [b for b in bars if b.get("as_of") != today.isoformat()]
    if not bars:
        return None, []
    return bars[-1], [b["close"] for b in bars]


def _daily_direction_bias(close_hist: list[float]) -> str:
    if len(close_hist) < 20:
        return "中性"
    import numpy as np

    direction, _reason = decide_direction(np.array(close_hist, dtype=float))
    return _BIAS_MAP.get(direction, "中性")


def main() -> int:
    today = _today_taipei()
    today_str = today.isoformat()
    print(f"[intraday-futures] 開始處理 {today_str} 的盤中期貨監控")

    try:
        snapshot = fi.fetch_snapshot()
    except Exception as exc:  # noqa: BLE001 - 印出明確原因後直接失敗，不掩蓋
        print(f"[intraday-futures] 抓取即時快照失敗：{exc!r}")
        return 1

    tx = fi.get_tx_snapshot(snapshot)
    if tx is None:
        names = [q.contract_name for q in snapshot]
        print(f"[intraday-futures] 快照裡找不到「臺股期貨」，實際拿到的 contractName 有：{names}")
        return 1

    try:
        day_series = fi.fetch_minute_series("day")
    except Exception as exc:  # noqa: BLE001
        print(f"[intraday-futures] 抓取日盤分鐘序列失敗：{exc!r}")
        return 1

    if not day_series:
        print("[intraday-futures] 日盤分鐘序列目前是空的（可能還沒開盤，或已經是非交易時段），僅記錄快照")
        _write_outputs(today_str, tx, [], None)
        return 0

    prev_bar, close_hist = _load_prev_and_history(today)
    if prev_bar is None:
        print("[intraday-futures] 找不到前一交易日的 TAIEX 日線資料，無法算 Camarilla 樞紐點，僅記錄分鐘序列")
        _write_outputs(today_str, tx, day_series, None)
        return 0

    daily_bias = _daily_direction_bias(close_hist)
    prediction = fi.compute_intraday_range_prediction(
        day_series,
        prev_close=prev_bar["close"],
        prev_high=prev_bar["high"],
        prev_low=prev_bar["low"],
        daily_direction_bias=daily_bias,
    )
    _write_outputs(today_str, tx, day_series, prediction)

    if prediction:
        print(
            f"[intraday-futures] {prediction.as_of_time} 最新價 {prediction.latest_price} "
            f"偏向 {prediction.direction_bias}；進場範圍 {prediction.entry_range}，"
            f"停損範圍 {prediction.stop_range}，停利範圍 {prediction.target_range}"
        )
    print("[intraday-futures] 完成")
    return 0


def _payload(today_str, tx, day_series, prediction) -> dict:
    payload = {
        "date": today_str,
        "updated_at": datetime.now(TAIPEI_TZ).isoformat(timespec="seconds"),
        "tx_snapshot": {
            "contract": tx.contract,
            "price": tx.price,
            "change": tx.change,
            "total_volume": tx.total_volume,
        },
        "minute_series": [{"time": t, "price": p} for t, p in day_series],
        "prediction": None,
        "caveat": fi.FUTURES_INTRADAY_CAVEAT,
    }
    if prediction:
        payload["prediction"] = {
            "as_of_time": prediction.as_of_time,
            "latest_price": prediction.latest_price,
            "direction_bias": prediction.direction_bias,
            "entry_range": list(prediction.entry_range),
            "stop_range": list(prediction.stop_range),
            "target_range": list(prediction.target_range),
            "basis": prediction.basis,
        }
    return payload


def _write_outputs(today_str, tx, day_series, prediction) -> None:
    payload = _payload(today_str, tx, day_series, prediction)

    intraday_dir = REPO_ROOT / "data" / "futures_intraday"
    intraday_dir.mkdir(parents=True, exist_ok=True)
    (intraday_dir / f"{today_str}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    docs_data_dir = REPO_ROOT / "docs" / "data"
    docs_data_dir.mkdir(parents=True, exist_ok=True)
    (docs_data_dir / "futures_intraday_latest.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    docs_dir = REPO_ROOT / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "futures_live.html").write_text(_render_html(payload), encoding="utf-8")


def _fmt(n) -> str:
    if n is None:
        return "—"
    return f"{n:,.0f}"


def _render_html(payload: dict) -> str:
    tx = payload["tx_snapshot"]
    pred = payload.get("prediction")
    series = payload.get("minute_series") or []

    change_class = "down" if tx["change"] < 0 else ("up" if tx["change"] > 0 else "flat")
    change_sign = "+" if tx["change"] > 0 else ""

    if pred:
        bias = pred["direction_bias"]
        bias_class = {"偏多": "good", "偏空": "critical"}.get(bias, "neutral")
        entry_lo, entry_hi = pred["entry_range"]
        stop_lo, stop_hi = pred["stop_range"]
        target_lo, target_hi = pred["target_range"]
        prediction_html = f"""
      <div class="tile tone-{bias_class}">
        <p class="tile-label">盤中方向偏向</p>
        <p class="tile-value">{html.escape(bias)}</p>
        <p class="tile-sub">依 {html.escape(pred['as_of_time'])} 最新價 {_fmt(pred['latest_price'])} 判斷</p>
      </div>
      <div class="tile">
        <p class="tile-label">進場價範圍</p>
        <p class="tile-value num">{_fmt(entry_lo)} ~ {_fmt(entry_hi)}</p>
      </div>
      <div class="tile">
        <p class="tile-label">停損價範圍</p>
        <p class="tile-value num">{_fmt(stop_lo)} ~ {_fmt(stop_hi)}</p>
      </div>
      <div class="tile">
        <p class="tile-label">停利價範圍</p>
        <p class="tile-value num">{_fmt(target_lo)} ~ {_fmt(target_hi)}</p>
      </div>"""
    else:
        prediction_html = """
      <div class="tile">
        <p class="tile-label">盤中範圍預測</p>
        <p class="tile-value" style="font-size:16px;">目前資料不足以計算（可能還沒開盤，或缺前一交易日資料）</p>
      </div>"""

    # 簡單的 SVG 折線圖：只畫價格序列，不追求跟 dataviz skill 同等複雜度的互動圖，
    # 這個頁面每5分鐘就會整頁重新產生，保持產生邏輯單純、不易壞掉優先。
    chart_svg = _render_series_svg(series)

    return f"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>微台指盤中即時監控｜台股當沖選股系統</title>
<meta name="description" content="微台指(MXF)盤中即時方向與進出場範圍參考，每5分鐘自動更新">
<style>
  :root {{
    --accent: #2a78d6; --accent-2: #7d5fd6;
    --good: #0ca30c; --critical: #d03b3b; --warning: #eda100;
    --page-plane: #f9f9f7; --surface: #fcfcfb; --border: #e4e2dd;
    --text-primary: #1a1a19; --text-secondary: #55534d; --text-muted: #8a8780;
    --good-bg: #e3f6e0; --critical-bg: #fbe7e5; --neutral-bg: #eeece7;
  }}
  @media (prefers-color-scheme: dark) {{
    :root {{
      --accent: #3987e5; --accent-2: #a690f0;
      --good: #26b354; --critical: #e66767; --warning: #c98500;
      --page-plane: #0d0d0d; --surface: #1a1a19; --border: #2c2b27;
      --text-primary: #f3f2ee; --text-secondary: #b7b4ac; --text-muted: #7a776f;
      --good-bg: #16301c; --critical-bg: #3a1f1f; --neutral-bg: #26251f;
    }}
  }}
  * {{ box-sizing: border-box; }}
  body {{
    background: var(--page-plane); color: var(--text-primary);
    font-family: "Noto Sans TC","PingFang TC","Microsoft JhengHei",sans-serif;
    padding: 20px 16px 40px; line-height: 1.6; max-width: 780px; margin: 0 auto;
  }}
  .num {{ font-variant-numeric: tabular-nums; }}
  h1 {{ font-size: 22px; margin: 0 0 4px; }}
  .tagline {{ color: var(--text-secondary); font-size: 13px; margin: 0 0 4px; }}
  .updated {{ color: var(--text-muted); font-size: 12px; margin: 0 0 20px; }}
  .hero-grid {{ display: grid; grid-template-columns: repeat(auto-fit,minmax(150px,1fr)); gap: 10px; margin-bottom: 20px; }}
  .tile {{ background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 14px; }}
  .tile-label {{ font-size: 12px; color: var(--text-secondary); margin: 0 0 6px; }}
  .tile-value {{ font-size: 22px; font-weight: 600; margin: 0; }}
  .tile-sub {{ font-size: 12px; color: var(--text-muted); margin: 4px 0 0; }}
  .tone-good .tile-value {{ color: var(--good); }}
  .tone-critical .tile-value {{ color: var(--critical); }}
  .price-row {{ display: flex; align-items: baseline; gap: 10px; margin-bottom: 20px; }}
  .price-row .price {{ font-size: 34px; font-weight: 700; }}
  .price-row .change.up {{ color: var(--good); }}
  .price-row .change.down {{ color: var(--critical); }}
  .chart-card {{ background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 14px; margin-bottom: 20px; overflow-x: auto; }}
  svg.chart {{ display: block; min-width: 600px; }}
  svg.chart .line {{ fill: none; stroke: var(--accent); stroke-width: 1.5; }}
  svg.chart .grid {{ stroke: var(--border); stroke-width: 1; }}
  svg.chart .axis-label {{ font-size: 9px; fill: var(--text-muted); font-family: monospace; }}
  .caveat {{ font-size: 12px; color: var(--text-muted); line-height: 1.8; padding: 14px; background: var(--neutral-bg); border-radius: 10px; }}
  .back {{ display:inline-block; margin-bottom: 16px; font-size: 13px; color: var(--accent); text-decoration: none; }}
</style>
</head>
<body>
  <a class="back" href="index.html">← 回部落格首頁</a>
  <h1>微台指(MXF)盤中即時監控</h1>
  <p class="tagline">臺股期貨(TX)分鐘報價代理 · 開盤區間突破 + Camarilla樞紐點 · GitHub Actions 盤中每5分鐘更新</p>
  <p class="updated">資料日期 {html.escape(payload['date'])}　更新時間 {html.escape(payload['updated_at'])}</p>

  <div class="price-row">
    <span class="price num">{_fmt(tx['price'])}</span>
    <span class="change {change_class} num">{change_sign}{_fmt(tx['change'])}</span>
    <span style="color:var(--text-muted);font-size:13px;">臺股期貨(TX) 近月 · 成交量 {_fmt(tx['total_volume'])} 口</span>
  </div>

  <div class="hero-grid">{prediction_html}
  </div>

  <div class="chart-card">
    {chart_svg}
  </div>

  <p class="caveat">{html.escape(payload['caveat'])}</p>
</body>
</html>
"""


def _render_series_svg(series: list[dict]) -> str:
    if not series:
        return '<p style="color:var(--text-muted);font-size:13px;">目前沒有分鐘序列資料。</p>'

    prices = [p["price"] for p in series]
    lo, hi = min(prices), max(prices)
    if hi == lo:
        hi = lo + 1
    width, height = 640, 160
    left_margin, right_margin, top_margin, bottom_margin = 44, 10, 10, 20
    plot_w = width - left_margin - right_margin
    plot_h = height - top_margin - bottom_margin

    def x_of(i):
        if len(series) == 1:
            return left_margin
        return left_margin + plot_w * i / (len(series) - 1)

    def y_of(price):
        return top_margin + plot_h * (1 - (price - lo) / (hi - lo))

    points = " ".join(f"{x_of(i):.1f},{y_of(p):.1f}" for i, p in enumerate(prices))

    labels = []
    for frac, val in ((0.0, hi), (1.0, lo)):
        y = top_margin + plot_h * frac
        labels.append(
            f'<line x1="{left_margin}" y1="{y:.1f}" x2="{width-right_margin}" y2="{y:.1f}" class="grid" />'
            f'<text x="{left_margin-4}" y="{y+3:.1f}" class="axis-label" text-anchor="end">{val:,.0f}</text>'
        )

    x_labels = []
    step = max(1, len(series) // 6)
    for i in range(0, len(series), step):
        x_labels.append(
            f'<text x="{x_of(i):.1f}" y="{height-4}" class="axis-label" text-anchor="middle">{series[i]["time"]}</text>'
        )

    return (
        f'<svg class="chart" viewBox="0 0 {width} {height}" role="img" '
        f'aria-label="今日臺股期貨分鐘價格走勢">'
        + "".join(labels)
        + f'<polyline class="line" points="{points}" />'
        + "".join(x_labels)
        + "</svg>"
    )


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""把 reports/*.md 轉成美化的靜態部落格網站，輸出到 docs/（給 GitHub Pages 用）。

2026-09-24 新增。使用者要求「美化優化」系統、要一個像部落格一樣可以往回翻閱歷史報告的
「打開方式」。設計原則：

- **不改動任何既有的資料/報告產生邏輯**——這支腳本只讀 `reports/*.md`（既有的每日報告，
  格式完全由 `report.py` 的 `render_daily_report()` 決定，見該檔案開頭聲明）跟
  `data/latest.json` / `data/reports_json/*.json`（若存在），純粹是「同一份資料的另一種
  呈現方式」，不是另一套資料來源，不會有「網站跟報告數字對不上」的風險。
- 產生的 `docs/` 目錄結構固定給 GitHub Pages 用「main 分支 /docs 資料夾」這個 source 設定：
    docs/index.html              -- 部落格列表（全部歷史報告，最新在最上面）
    docs/reports/YYYY-MM-DD.html -- 單篇報告詳情頁（含上一篇/下一篇導覽）
    docs/assets/style.css        -- 共用樣式（色票依 dataviz 技能的驗證色票）
    docs/data/manifest.json      -- 給 Claude Artifact 儀表板抓的摘要清單（每天一筆，含
                                     大盤氣氛/微台指方向/候選股數等，不含完整內文）
    docs/data/latest.json        -- data/latest.json 的原樣複製（若存在），給儀表板抓最新
                                     一天的完整結構化資料
    docs/data/reports/YYYY-MM-DD.json -- data/reports_json/ 底下的逐日結構化資料原樣複製
                                     （若存在；這個檔案是 2026-09-24 之後才開始存，更早的
                                     日期不會有，儀表板端要能處理缺漏）
    docs/.nojekyll                -- 停用 GitHub Pages 預設的 Jekyll 處理

- 全部重新產生（不是只增量處理新的一天）：報告篇數目前是幾十篇的量級，全部重建一次的
  成本可忽略，換來的是「改了樣式或這支腳本的邏輯，舊的頁面也會一起套用新樣式」，不用
  另外寫「重建全部」跟「只建今天」兩套邏輯。
"""

from __future__ import annotations

import html
import json
import re
import shutil
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
REPORTS_DIR = REPO_ROOT / "reports"
DATA_DIR = REPO_ROOT / "data"
DOCS_DIR = REPO_ROOT / "docs"
ASSET_SRC = REPO_ROOT / "scripts" / "site_assets" / "style.css"

SITE_TITLE = "台股當沖選股系統"
SITE_TAGLINE = "每日自動選股報告（含微台指(MXF)期貨模組）"


# ---------------------------------------------------------------------------
# 1. 讀取與正規化 markdown
# ---------------------------------------------------------------------------

def load_reports() -> list[tuple[str, str]]:
    """回傳 [(date_str, raw_markdown), ...]，依日期由舊到新排序。"""
    if not REPORTS_DIR.exists():
        return []
    out = []
    for p in sorted(REPORTS_DIR.glob("*.md")):
        date_str = p.stem
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", date_str):
            continue  # 忽略非日期檔名的雜項檔案，不假設目錄下只有報告
        out.append((date_str, p.read_text(encoding="utf-8")))
    out.sort(key=lambda t: t[0])
    return out


def normalize_markdown(text: str) -> str:
    """在標題(#)前面補上空行，避免緊接在前一段文字/清單後面時，某些 markdown
    parser 會誤判成同一段落。report.py 產生的原始文字大部分章節之間已經有空行，
    但「### 最強族群」「### 最弱族群」這種同一個 `##` 底下的子標題之間沒有空行，
    這裡統一補齊，不依賴特定 parser 的容錯行為。
    """
    lines = text.split("\n")
    out = []
    for i, line in enumerate(lines):
        if line.startswith("#") and out and out[-1].strip() != "":
            out.append("")
        out.append(line)
    return "\n".join(out)


# ---------------------------------------------------------------------------
# 2. 從原始 markdown 擷取摘要（給部落格列表卡片 / manifest.json 用）
# ---------------------------------------------------------------------------

def _extract_section(lines: list[str], heading_pattern: str) -> list[str]:
    """用內容關鍵字（而不是寫死的章節編號）找到一個 `## ` 區塊。章節編號會隨著
    功能增加而往後移動（例如回測區塊是 2026-09-07 才加的第1節、微台指是
    2026-09-24 才加的第7節），舊報告的編號跟現在的報告不一樣，用編號比對舊報告
    會直接找不到區塊、統計數字整批掛零——用標題文字裡的關鍵字比對，不管是第幾節
    都找得到，同一支腳本才能正確處理從系統上線第一天到現在的所有歷史報告。
    """
    pat = re.compile(heading_pattern)
    start = None
    for i, l in enumerate(lines):
        if l.startswith("## ") and pat.search(l):
            start = i
            break
    if start is None:
        return []
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if lines[i].startswith("## "):
            end = i
            break
    return lines[start:end]


def extract_summary(date_str: str, text: str) -> dict:
    lines = text.split("\n")
    is_test = text.startswith("# 【測試模式")

    sentiment = None
    m = re.search(r"總經氣氛：(\S+)", text)
    if m:
        sentiment = m.group(1)

    issues_section = _extract_section(lines, r"⚠")
    issues_count = sum(1 for l in issues_section if l.startswith("- "))

    long_section = _extract_section(lines, r"多方候選清單")
    short_section = _extract_section(lines, r"空方候選清單")
    long_count = sum(1 for l in long_section if l.startswith("### "))
    short_count = sum(1 for l in short_section if l.startswith("### "))

    futures_section = _extract_section(lines, r"微台指")
    futures_text = "\n".join(futures_section)
    futures_available = False
    futures_direction = None
    futures_has_position = False
    fm = re.search(r"方向判斷：([^—\s]+)", futures_text)
    if fm:
        futures_available = True
        futures_direction = fm.group(1)
        if "建議口數" in futures_text:
            futures_has_position = True

    return {
        "date": date_str,
        "url": f"reports/{date_str}.html",
        "is_test": is_test,
        "sentiment": sentiment,
        "issues_count": issues_count,
        "long_count": long_count,
        "short_count": short_count,
        "futures_available": futures_available,
        "futures_direction": futures_direction,
        "futures_has_position": futures_has_position,
    }


# ---------------------------------------------------------------------------
# 3. markdown -> HTML（用 python-markdown，再做一次輕量的區塊包裝後製）
# ---------------------------------------------------------------------------

def markdown_to_html(text: str) -> str:
    import markdown as md_lib

    body = md_lib.markdown(normalize_markdown(text), extensions=["extra", "sane_lists"])
    # 把「⚠️ 資料品質檢查」跟「7. 微台指」這兩個 h2 區塊包一層特殊樣式的 div，
    # 用正規表示式在 <h2> 邊界切段落即可，因為 report.py 產生的結構保證 h2
    # 不會出現在清單/引用區塊裡面，不需要真的解析 HTML tree。
    parts = re.split(r"(?=<h2[ >])", body)
    out = []
    for part in parts:
        if not part.strip():
            out.append(part)
            continue
        if re.match(r"<h2[^>]*>\s*⚠", part):
            out.append(f'<div class="quality-warning">{part}</div>')
        elif re.match(r"<h2[^>]*>\s*(\d+\.\s*)?微台指", part):
            out.append(f'<div class="futures-section">{part}</div>')
        else:
            out.append(part)
    return "".join(out)


# ---------------------------------------------------------------------------
# 4. HTML 頁面模板
# ---------------------------------------------------------------------------

def _page_shell(title: str, body: str, depth: int = 0) -> str:
    prefix = "../" * depth
    return f"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<meta name="description" content="{html.escape(SITE_TAGLINE)}">
<link rel="stylesheet" href="{prefix}assets/style.css">
</head>
<body>
<div class="wrap">
<header class="site-header">
  <a class="brand" href="{prefix}index.html">{html.escape(SITE_TITLE)}</a>
  <span class="tagline">{html.escape(SITE_TAGLINE)}</span>
</header>
{body}
<footer class="site-footer">
  <p>本網站由 GitHub Actions 每個交易日自動產生，內容為系統性技術面/籌碼面篩選結果，
  不構成投資建議；進出場參考價位皆為日線近似值，詳見各篇報告內的限制說明。
  原始碼與完整歷史：<a href="https://github.com/lemon10712-coder/tw-daytrade-system" target="_blank" rel="noopener">GitHub repo</a>。</p>
</footer>
</div>
</body>
</html>
"""


def _badge(label: str, kind: str) -> str:
    return f'<span class="badge badge-{kind}">{html.escape(label)}</span>'


def sentiment_badge(sentiment: str | None) -> str:
    if sentiment is None:
        return _badge("大盤：無資料", "neutral")
    kind = {"偏多": "good", "偏空": "critical"}.get(sentiment, "neutral")
    return _badge(f"大盤：{sentiment}", kind)


def futures_badge(summary: dict) -> str | None:
    if not summary["futures_available"]:
        return None
    direction = summary["futures_direction"]
    kind = {"偏多": "good", "偏空": "critical"}.get(direction, "neutral")
    return _badge(f"微台指：{direction}", kind)


def build_index_html(summaries: list[dict]) -> str:
    """summaries 依日期新到舊排序。"""
    if not summaries:
        items = '<p class="empty-note">目前還沒有任何報告。</p>'
    else:
        cards = []
        for s in summaries:
            badges = [sentiment_badge(s["sentiment"])]
            fb = futures_badge(s)
            if fb:
                badges.append(fb)
            if s["is_test"]:
                badges.append(_badge("測試模式", "test"))
            excerpt_bits = [
                f"<span>多方候選 {s['long_count']} 檔</span>",
                f"<span>空方候選 {s['short_count']} 檔</span>",
            ]
            if s["issues_count"]:
                excerpt_bits.append(f"<span>⚠ {s['issues_count']} 項資料品質提醒</span>")
            cards.append(f"""<a class="day-card" href="{s['url']}">
  <div class="day-card-top">
    <span class="day-date">{s['date']}</span>
    <span class="day-badges">{''.join(badges)}</span>
  </div>
  <div class="day-excerpt">{''.join(excerpt_bits)}</div>
</a>""")
        items = f'<ul class="day-list">{"".join(f"<li>{c}</li>" for c in cards)}</ul>'

    body = f"""<main>
<p style="color:var(--text-secondary);font-size:14px;margin-bottom:20px;">
共 {len(summaries)} 篇報告，最新的排在最上面。點進任一天可以看完整內容。
</p>
{items}
</main>"""
    return _page_shell(SITE_TITLE, body, depth=0)


def build_day_html(date_str: str, report_html: str, prev_date: str | None, next_date: str | None, is_test: bool) -> str:
    pager_bits = []
    if prev_date:
        pager_bits.append(f'<a href="{prev_date}.html">← 較舊：{prev_date}</a>')
    else:
        pager_bits.append('<span></span>')
    pager_bits.append('<span class="spacer"></span>')
    if next_date:
        pager_bits.append(f'<a href="{next_date}.html">較新：{next_date} →</a>')
    else:
        pager_bits.append('<span></span>')

    body = f"""<main>
<a class="back-link" href="../index.html">← 回部落格列表</a>
<article class="report">
{report_html}
</article>
<nav class="pager">{''.join(pager_bits)}</nav>
</main>"""
    title = f"{date_str}｜{SITE_TITLE}"
    return _page_shell(title, body, depth=1)


# ---------------------------------------------------------------------------
# 5. 主流程
# ---------------------------------------------------------------------------

def main() -> int:
    reports = load_reports()  # 舊 -> 新

    reports_out_dir = DOCS_DIR / "reports"
    reports_out_dir.mkdir(parents=True, exist_ok=True)
    (DOCS_DIR / "assets").mkdir(parents=True, exist_ok=True)
    (DOCS_DIR / "data" / "reports").mkdir(parents=True, exist_ok=True)

    summaries = []
    for i, (date_str, raw_text) in enumerate(reports):
        summary = extract_summary(date_str, raw_text)
        summaries.append(summary)

        report_html = markdown_to_html(raw_text)
        prev_date = reports[i - 1][0] if i > 0 else None
        next_date = reports[i + 1][0] if i < len(reports) - 1 else None
        page = build_day_html(date_str, report_html, prev_date, next_date, summary["is_test"])
        (reports_out_dir / f"{date_str}.html").write_text(page, encoding="utf-8")

    summaries_desc = list(reversed(summaries))  # 新 -> 舊，給列表頁跟 manifest 用
    (DOCS_DIR / "index.html").write_text(build_index_html(summaries_desc), encoding="utf-8")

    if ASSET_SRC.exists():
        shutil.copyfile(ASSET_SRC, DOCS_DIR / "assets" / "style.css")

    (DOCS_DIR / "data" / "manifest.json").write_text(
        json.dumps(
            {"site_title": SITE_TITLE, "tagline": SITE_TAGLINE, "reports": summaries_desc},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    latest_json = DATA_DIR / "latest.json"
    if latest_json.exists():
        shutil.copyfile(latest_json, DOCS_DIR / "data" / "latest.json")

    reports_json_dir = DATA_DIR / "reports_json"
    if reports_json_dir.exists():
        for p in reports_json_dir.glob("*.json"):
            shutil.copyfile(p, DOCS_DIR / "data" / "reports" / p.name)

    (DOCS_DIR / ".nojekyll").write_text("", encoding="utf-8")

    print(f"建站完成：{len(reports)} 篇報告 -> {DOCS_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""build_site.py 的單元測試，只測純邏輯（摘要擷取/markdown 正規化），不測檔案 I/O 或
python-markdown 本身的轉換結果（那是第三方套件的行為，不是我們要鎖住的邏輯）。

2026-09-24 新增。重點鎖住兩件容易犯錯的事：
1. 章節擷取要用「標題關鍵字」而不是「寫死的章節編號」——舊報告的編號跟現在的編號不一樣
   （回測區塊是 2026-09-07 才加的、微台指是 2026-09-24 才加的），用編號找章節在處理
   歷史報告時會整批找不到、統計數字掛零，這裡用不同編號的合成報告鎖住這個行為。
2. 微台指方向擷取要在遇到「——」全形破折號或空白時就停止，不能把後面「加權指數均線
   偏多排列（一致度100%）」這種說明文字也吃進去。
"""

import sys
from pathlib import Path

# build_site.py 放在 scripts/（跟其他一次性/建置用的腳本同一個資料夾），不是
# src/stockSystem/ 底下的套件模組，既有的 PYTHONPATH=$GITHUB_WORKSPACE/src 設定
# 找不到它，這裡直接把 scripts/ 加進 sys.path，跟 scripts/backfill_index_history.py
# 那些腳本 import src/stockSystem 時的做法（手動 insert 到 sys.path）是同一種模式。
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from build_site import extract_summary, normalize_markdown  # noqa: E402


def _report(body: str) -> str:
    return "# 台股當沖每日報告\n資料日期: 2026-09-24 資料來源標記: TWSE_OPENAPI\n\n" + body


def test_extract_summary_counts_candidates_by_keyword_not_section_number():
    # 舊版編號：多方候選清單是「## 3.」不是「## 4.」，一樣要抓得到
    text = _report(
        "## 3. 多方候選清單\n"
        "### 2330 台積電（半導體業，信心：尚未驗證）\n- 參考價: 500\n\n"
        "### 2454 聯發科（半導體業，信心：尚未驗證）\n- 參考價: 1200\n\n"
        "## 4. 空方候選清單\n（今日無符合條件的候選股）\n"
    )
    s = extract_summary("2026-08-01", text)
    assert s["long_count"] == 2
    assert s["short_count"] == 0


def test_extract_summary_handles_missing_futures_section():
    text = _report("## 4. 多方候選清單\n（今日無符合條件的候選股）\n")
    s = extract_summary("2026-08-01", text)
    assert s["futures_available"] is False
    assert s["futures_direction"] is None


def test_extract_summary_futures_direction_stops_before_dash_reason():
    text = _report(
        "## 7. 微台指(MXF)當沖建議\n"
        "加權指數參考收盤：48025 方向判斷：偏多——加權指數均線偏多排列（一致度100%） "
        "進場參考：48025 止損參考：47192 停利參考：49274\n"
        "建議口數：2 口（MXF）\n"
    )
    s = extract_summary("2026-09-24", text)
    assert s["futures_available"] is True
    assert s["futures_direction"] == "偏多"
    assert s["futures_has_position"] is True


def test_extract_summary_detects_test_mode_title():
    text = "# 【測試模式｜非真實市場資料】台股當沖每日報告\n資料日期: 2026-01-01\n\n## 4. 多方候選清單\n（今日無符合條件的候選股）\n"
    s = extract_summary("2026-01-01", text)
    assert s["is_test"] is True


def test_extract_summary_counts_quality_issues():
    text = _report(
        "## ⚠️ 資料品質檢查發現以下問題，請先留意\n"
        "- 股價資料存在缺漏值\n"
        "- 全額交割股清單目前尚未串接資料源\n\n"
        "## 4. 多方候選清單\n（今日無符合條件的候選股）\n"
    )
    s = extract_summary("2026-09-24", text)
    assert s["issues_count"] == 2


def test_extract_summary_sentiment():
    text = _report("## 2. 大盤環境摘要\nNASDAQ +1.0% → 總經氣氛：偏空\n\n## 4. 多方候選清單\n（今日無符合條件的候選股）\n")
    s = extract_summary("2026-09-24", text)
    assert s["sentiment"] == "偏空"


def test_normalize_markdown_inserts_blank_line_before_heading():
    text = "- 半導體業：相對強度 +1%\n### 最弱族群\n- 水泥工業：相對強度 -1%"
    out = normalize_markdown(text)
    lines = out.split("\n")
    idx = lines.index("### 最弱族群")
    assert lines[idx - 1] == ""


def test_normalize_markdown_does_not_duplicate_existing_blank_line():
    text = "## 1. 節\n\n## 2. 節"
    out = normalize_markdown(text)
    assert "\n\n\n" not in out

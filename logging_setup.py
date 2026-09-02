"""集中式 logging 設定。

目的：任何一次執行（不管是互動測試還是每日排程）都要留下「有跡可循」的紀錄——
每次執行有一個 run_id，每個模組的每個關鍵步驟都寫進同一份 log，
以後出問題時可以直接看 log 定位是哪個模組、哪個函式、帶什麼資料出錯，
而不是要重新用猜的重跑。
"""

from __future__ import annotations

import logging
import sys
import uuid
from pathlib import Path

LOG_DIR = Path(__file__).resolve().parents[2] / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)


def new_run_id() -> str:
    return uuid.uuid4().hex[:8]


def get_logger(name: str, run_id: str | None = None) -> logging.Logger:
    """取得一個設定好的 logger。

    同時輸出到 console（方便互動除錯）和 logs/stockSystem.log（方便事後追溯，
    每一行都帶 run_id，可以用 run_id 把同一次執行的所有模組 log 串起來）。
    """
    logger = logging.getLogger(name)
    if logger.handlers:
        # 避免重複呼叫 get_logger 造成 handler 疊加、log 重複輸出
        return logger

    logger.setLevel(logging.INFO)
    fmt = logging.Formatter(
        fmt=f"%(asctime)s | run={run_id or '-'} | %(name)s | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(fmt)
    logger.addHandler(stream_handler)

    file_handler = logging.FileHandler(LOG_DIR / "stockSystem.log", encoding="utf-8")
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    logger.propagate = False
    return logger

"""系統設定常數。

所有「會影響金額計算、風險判斷」的關鍵數字都集中在這裡，並附上出處，
方便之後使用者確認實際數字（例如元大手續費折扣）後直接回來改這一個檔案，
不需要滿專案找散落的魔術數字（呼應使用者要求的「可追溯」）。
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class AccountConfig:
    """使用者帳戶相關設定 —— 2026-09-02 訪談確認，之後有變動只需改這裡。"""

    # 當沖信用額度：定義為「同一時間持有部位的總成本上限」（使用者已於訪談確認此定義）
    capital_cap_twd: float = 500_000.0

    # 手續費折扣：使用者不確定實際折數，暫用市場常見的 6 折估算
    # TODO(使用者確認後更新): 跟元大證券確認實際折扣後修改此值，並在下方 fee_discount_confirmed 設為 True
    fee_discount: float = 0.6
    fee_discount_confirmed: bool = False

    # 台股交易單位：1 張 = 1000 股。系統明確不支援零股（使用者已要求排除）
    lot_size: int = 1000
    allow_odd_lot: bool = False


@dataclass(frozen=True)
class MarketConfig:
    """市場層級的稅費常數 —— 均有公開資料佐證，見規劃書第 11 節。"""

    fee_rate: float = 0.001425          # 證券商手續費牌告費率
    fee_min_twd: float = 20.0           # 手續費下限（不足 20 元以 20 元計收）
    tax_rate_daytrade: float = 0.0015   # 現股當沖證交稅（優惠稅率，延長至 2027-12-31）
    tax_rate_normal: float = 0.003      # 一般證交稅（賣出時課徵）
    daytrade_tax_sunset: str = "2027-12-31"


@dataclass(frozen=True)
class ValidationConfig:
    """回測驗證閘門的門檻 —— 對應規劃書第 8 節「晉升門檻」。

    這些是初始建議值，之後應該用實際回測結果去校準，不是憑感覺定的終版數字，
    所以每個欄位都附上「為什麼先這樣設」的說明。
    """

    # 樣本數門檻：少於這個數字，一律標示「樣本不足」，不進正式報告
    min_sample_size: int = 30
    # 晉升為「正式候選」規則所需的最小樣本數（比 min_sample_size 更嚴格，用於分級）
    promotion_sample_size: int = 60
    # 期望值（扣成本後）門檻：必須為正，且要求一個最小值避免「勉強打平」的規則被視為有效
    min_expectancy_pct: float = 0.001
    # walk-forward 切分：訓練期與測試期的天數（先給一組合理預設，之後可調）
    train_window_days: int = 126   # 約半年交易日
    test_window_days: int = 63     # 約一季交易日
    # 實測績效與回測預期的偏離超過這個標準差倍數，觸發降級警示
    live_drift_std_threshold: float = 2.0


@dataclass(frozen=True)
class ScoringConfig:
    """族群強度與個股評分的時間窗口設定。"""

    rs_windows_days: tuple = (3, 5, 10)
    ma_windows: tuple = (5, 10, 20, 60)
    atr_window: int = 14
    min_liquidity_avg_volume_lots: int = 200  # 近20日均量門檻（單位：張），排除流動性太差的股票
    min_price_twd: float = 100.0  # 2026-09-03 使用者要求：只列股價100元以上的股票（用當天收盤價判斷）

    # 2026-09-24 使用者要求：參考真實策略（FinLab 台股動能策略、海龜交易系統）幫系統加參數，
    # 這裡新增兩組可調參數，不是憑感覺、也不是照抄別人的預設值，而是把「值」跟「機制」分開：
    # 機制先做出來、可以調，實際要用多少由 backtest_tracker.py 之後累積的真實樣本決定。

    # 止損距離 = ATR(14) * 這個倍數（見 entry_exit.py compute_entry_exit）。
    # 預設沿用原本就在用的 1.2 倍，刻意不因為海龜交易系統用 2 倍就跟著改——
    # 真實台股回測（FinLab，月頻動能策略、2018-2026）顯示把 ATR 停損倍數放寬到 2-3 倍，
    # 反而讓 CAGR 從 8.0% 掉到 -3.6%/-0.4%、MaxDD 惡化到 -57.4%/-55.3%（洗出場、
    # 賣在阿呆谷、換手率大增），可見「別人用的倍數」不能直接照搬到不同的策略/市場條件。
    # 所以先把它做成可調參數，讓 backtest_tracker.py 逐日累積「這個倍數實際被觸發後的
    # 真實表現」，未來用數據而不是直覺決定要不要調整。
    atr_stop_multiple: float = 1.2

    # 大盤廣度風控（參考 FinLab 真實台股動能策略：全市場站上60日均線比例 <= 40% 時減碼）。
    # breadth_trend_window 沿用 ma_windows 裡本來就有的第4個窗口(60)，不是另外發明新窗口。
    breadth_trend_window: int = 60
    breadth_risk_off_threshold: float = 0.40  # 廣度低於這個比例視為「大盤環境偏空」
    breadth_risk_off_scale: float = 0.5       # 觸發風控時，部位規模（額度上限）乘上這個係數


@dataclass(frozen=True)
class RiskSizingConfig:
    """風險百分比部位法的參數（見 position_sizing.build_risk_based）。

    2026-09-24 新增，方法論參考海龜交易系統（Turtle Trading System，Richard Dennis）
    真實文獻的核心設計：每一個 unit 的風險預算 = 帳戶規模的固定百分比（原始設計為 1%），
    不是像「集中單押/核心＋衛星/分散配置」那樣先決定要花多少錢，而是先決定「萬一看錯、
    觸及止損時最多願意虧多少」，再反推張數——波動大（止損距離遠）的部位會自動配置得少，
    波動小的可以配置得多，讓每一檔候選股「看錯的風險」盡量一致。
    """

    risk_pct_per_trade: float = 0.01  # 每檔部位的風險預算 = capital_cap * 這個比例；1% 為海龜系統原始設計值
    max_candidates: int = 3           # 最多分配到幾檔候選股，避免過度分散成太多小部位


ACCOUNT = AccountConfig()
MARKET = MarketConfig()
VALIDATION = ValidationConfig()
SCORING = ScoringConfig()
RISK_SIZING = RiskSizingConfig()

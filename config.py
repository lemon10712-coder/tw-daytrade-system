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


ACCOUNT = AccountConfig()
MARKET = MarketConfig()
VALIDATION = ValidationConfig()
SCORING = ScoringConfig()

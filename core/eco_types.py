"""Economy とその部品（eco_*.py）が共有する定数と、結果を返す入れ物。"""

from dataclasses import dataclass

INITIAL_BALANCE = 3000
INSURANCE_CAP = 1_000  # 保険券で戻る額の上限（core/shop.py の説明と同じ値）

# 消耗品 → 効果の列。呼び出し側の指定ではなく、ここで対応を固定する
CONSUMABLE_EFFECTS = {"daily_double": "daily_boost", "insurance": "insurance"}


@dataclass(frozen=True)
class DailyResult:
    ok: bool
    amount: int = 0
    streak: int = 0
    balance: int = 0
    boosted: bool = False  # デイリー2倍券を使った
    repaid: int = 0  # 受け取った額から、借金の返済として引かれた額


@dataclass(frozen=True)
class PurchaseResult:
    ok: bool
    reason: str = ""
    balance: int = 0


@dataclass(frozen=True)
class CryptoResult:
    ok: bool
    reason: str = ""
    units: int = 0  # 売買した口数
    money: int = 0  # 買い: 支払った総額 / 売り: 受け取った額（どちらも手数料込み）
    fee: int = 0
    balance: int = 0
    profit: int = 0  # 売り: 買った額に対する損益


@dataclass(frozen=True)
class TransferResult:
    ok: bool
    reason: str = ""
    received: int = 0
    fee: int = 0
    sender_balance: int = 0

"""Economy とその部品（eco_*.py）が共有する定数と、結果を返す入れ物。"""

from dataclasses import dataclass

from core import catalogs

_WALLET = catalogs.economy().wallet
INITIAL_BALANCE = _WALLET.initial_balance
INSURANCE_CAP = catalogs.shop().insurance_cap  # 保険券で戻る額の上限（catalogs/shop.yaml と同じ値）

# 消耗品 → 効果の列。呼び出し側の指定ではなく、ここで対応を固定する
CONSUMABLE_EFFECTS = dict(_WALLET.consumable_effects)


@dataclass(frozen=True)
class DailyResult:
    ok: bool
    amount: int = 0
    streak: int = 0
    balance: int = 0
    boosted: bool = False  # デイリー2倍券を使った
    repaid: int = 0  # 受け取った額から、借金の返済として天引きされた額
    skimmed: int = 0  # 帝愛が没収した額（借金中のデイリーのピンハネ。借金は減らない）
    seed: int = 0  # 帝愛が貸し付けた種銭（手元が枠に満たなかったとき）
    guarded: int = 0  # 連続守り券で埋めた、受け取れなかった日数


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
    profit: int = 0  # 売り: 買った額に対する損益（税の前）
    tax: int = 0  # 売り・決済で利益が出たときに、帝愛が源泉徴収した税（money からは、引いてある）


@dataclass(frozen=True)
class TransferResult:
    ok: bool
    reason: str = ""
    received: int = 0
    fee: int = 0
    sender_balance: int = 0

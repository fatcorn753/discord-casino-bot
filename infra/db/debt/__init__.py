"""帝愛ファイナンス: 借金・強制執行・種銭・身分・自己破産。数字と考え方は domain/teiai.py。

表の定義は infra/db/schema/tables.py（users の debt・bankrupt_at・blacklist_until）。
差し押さえ（IAssetSeizureStep）・換金（ILiquidationStep）・競売査定（ISeizureValueStep）は
拡張に開き修正に閉じるため（OCP）独立戦略クラスとして構成する。

逐次継承チェーンで base.py（コンストラクタ・身分と借金の読み取り）→ lifecycle.py（利息・後始末・天引き）
→ ops.py（強制執行と借金の発生）→ repay.py（返済）→ escape.py（海外逃亡）→ bankrupt.py（自己破産）。
`__init__.py` が最終クラス `DebtRepo` と戦略ステップ群を再エクスポートし、
`from infra.db.debt import DebtRepo` は従来どおり使える。
"""

from .bankrupt import DebtRepo
from .liquidation import (
    BondLiquidationStep,
    CryptoLiquidationStep,
    DepositLiquidationStep,
    KyosaiLiquidationStep,
    MarginLiquidationStep,
    OptionLiquidationStep,
)
from .seizure import (
    BondSeizureStep,
    CryptoSeizureStep,
    DepositSeizureStep,
    MarginSeizureStep,
    OptionSeizureStep,
)
from .seizure_value import ShopAndHorseSeizureStep

__all__ = [
    "BondLiquidationStep",
    "BondSeizureStep",
    "CryptoLiquidationStep",
    "CryptoSeizureStep",
    "DebtRepo",
    "DepositLiquidationStep",
    "DepositSeizureStep",
    "KyosaiLiquidationStep",
    "MarginLiquidationStep",
    "MarginSeizureStep",
    "OptionLiquidationStep",
    "OptionSeizureStep",
    "ShopAndHorseSeizureStep",
]

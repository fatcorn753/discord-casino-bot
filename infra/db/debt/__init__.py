"""帝愛ファイナンス: 借金・強制執行・種銭・身分・自己破産。数字と考え方は domain/teiai.py。

表の定義は infra/db/schema/tables.py（users の debt・bankrupt_at・blacklist_until）。
差し押さえ（AssetSeizureStep）・換金（LiquidationStep）・競売査定（SeizureValueStep）は
拡張に開き修正に閉じるため（OCP）ファクトリ関数が返す Callable ステップとして構成する。

逐次継承チェーンで base.py（コンストラクタ・身分と借金の読み取り）→ lifecycle.py（利息・後始末・天引き）
→ ops.py（強制執行と借金の発生）→ repay.py（返済）→ escape.py（海外逃亡）→ bankrupt.py（自己破産）。
`__init__.py` が最終クラス `DebtRepo` と戦略ステップ群を再エクスポートし、
`from infra.db.debt import DebtRepo` は従来どおり使える。
"""

from .bankrupt import DebtRepo
from .liquidation import (
    bond_liquidation_step,
    crypto_liquidation_step,
    deposit_liquidation_step,
    kyosai_liquidation_step,
    margin_liquidation_step,
    option_liquidation_step,
)
from .seizure import (
    bond_seizure_step,
    crypto_seizure_step,
    deposit_seizure_step,
    margin_seizure_step,
    option_seizure_step,
)
from .seizure_value import shop_and_horse_seizure_value_step

__all__ = [
    "DebtRepo",
    "bond_liquidation_step",
    "bond_seizure_step",
    "crypto_liquidation_step",
    "crypto_seizure_step",
    "deposit_liquidation_step",
    "deposit_seizure_step",
    "kyosai_liquidation_step",
    "margin_liquidation_step",
    "margin_seizure_step",
    "option_liquidation_step",
    "option_seizure_step",
    "shop_and_horse_seizure_value_step",
]

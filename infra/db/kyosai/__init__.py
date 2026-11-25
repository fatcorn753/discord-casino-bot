"""帝愛共済（#34）の記録。口・配当・超過損失の請求・回収と帝愛の持ち口の競り・譲渡・動議。

表の定義は infra/db/schema/ddl/teiai.py（kyosai_units・kyosai_auctions・kyosai_offers・kyosai_motions・kyosai_votes・
kyosai_state、teiai_trust.premium）。数字と考え方は domain/kyosai.py。

逐次継承チェーンで base.py（コンストラクタ・口の管理と引き戻し）→ excess.py（超過損失の請求）→ daily.py（毎日の配当と休眠回収）
→ auction.py（帝愛の持ち口の競り）→ offers.py（口の譲渡）→ motions.py（動議と投票）→ status.py（表示用まとめ）。
`__init__.py` が最終クラス `KyosaiRepo` を再エクスポートし、`from infra.db.kyosai import KyosaiRepo` は従来どおり使える。
"""

from .base import DAY
from .status import KyosaiRepo

__all__ = ["DAY", "KyosaiRepo"]

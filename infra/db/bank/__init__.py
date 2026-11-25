"""帝愛銀行（帝愛信託・準備率・取り付け騒ぎ）、破産猶予、CDS（破綻保証）。

表の定義は infra/db/schema/ddl/teiai.py（teiai_bank・teiai_thermostat・teiai_trust・teiai_deposits・cds、teiai_grace）。数字と考え方は domain/teiai.py。
"""

from .cds import BankRepo

__all__ = ["BankRepo"]

"""帝愛銀行（帝愛信託・準備率・取り付け騒ぎ）、破産猶予、CDS（破綻保証）。

表の定義は infra/db/schema/tables.py（teiai_bank・teiai_deposits・cds、users.grace_until）。数字と考え方は domain/teiai.py。
"""

from .cds import BankRepo

__all__ = ["BankRepo"]

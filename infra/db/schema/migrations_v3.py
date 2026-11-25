"""v2 から v3 への移行処理（teiai_bank・teiai_house を責務別 6 表へ 5NF 分離）。"""

import sqlite3

from infra.db.schema.ddl import (
    TEIAI_BANK_DDL,
    TEIAI_HOUSE_DDL,
    TEIAI_HOUSE_GRANTS_DDL,
    TEIAI_THERMOSTAT_DDL,
    TEIAI_TRUST_DDL,
    V3_TEIAI_SPLIT_STATEMENTS,
)
from infra.db.schema.ddl.teiai import KYOSAI_STATE_V3_DDL


def _split_teiai_bank(conn: sqlite3.Connection) -> None:
    tb_cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(teiai_bank)")}
    if "thermo_load" not in tb_cols:
        conn.execute(TEIAI_THERMOSTAT_DDL)
        conn.execute(TEIAI_TRUST_DDL)
        conn.execute(KYOSAI_STATE_V3_DDL)
        return
    # 子テーブルの FOREIGN KEY (teiai_bank) が RENAME で teiai_bank_v2 に書き換わらないよう、先に親を再作成する
    conn.execute("ALTER TABLE teiai_bank RENAME TO teiai_bank_v2")
    conn.execute(TEIAI_BANK_DDL)
    conn.execute(TEIAI_THERMOSTAT_DDL)
    conn.execute(TEIAI_TRUST_DDL)
    conn.execute(KYOSAI_STATE_V3_DDL)
    conn.execute(
        "INSERT OR IGNORE INTO teiai_bank (guild_id, cash, loaned, ts) SELECT guild_id, cash, loaned, ts FROM teiai_bank_v2"
    )
    conn.execute(
        "INSERT OR IGNORE INTO teiai_thermostat (guild_id, load, capacity, ts, burned, bailed) "
        "SELECT guild_id, thermo_load, thermo_capacity, thermo_ts, thermo_burned, thermo_bailed FROM teiai_bank_v2"
    )
    conn.execute(
        "INSERT OR IGNORE INTO teiai_trust (guild_id, rf, rf_ts, trust_ts, accrued, paid, excess_unpaid, premium) "
        "SELECT guild_id, rf, rf_ts, trust_ts, trust_accrued, trust_paid, excess_unpaid, trust_premium FROM teiai_bank_v2"
    )
    conn.execute(
        "INSERT OR IGNORE INTO kyosai_state (guild_id, div_ts, paid, charged) "
        "SELECT guild_id, kyosai_div_ts, kyosai_paid, kyosai_charged FROM teiai_bank_v2"
    )
    conn.execute("DROP TABLE teiai_bank_v2")


def _split_teiai_house(conn: sqlite3.Connection) -> None:
    th_cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(teiai_house)")}
    if "grant_day" not in th_cols:
        conn.execute(TEIAI_HOUSE_GRANTS_DDL)
        return
    conn.execute("ALTER TABLE teiai_house RENAME TO teiai_house_v2")
    conn.execute(TEIAI_HOUSE_DDL)
    conn.execute(TEIAI_HOUSE_GRANTS_DDL)
    conn.execute(
        "INSERT OR IGNORE INTO teiai_house (guild_id, balance, skimmed) "
        "SELECT guild_id, MAX(0, balance), MAX(0, skimmed) FROM teiai_house_v2"
    )
    conn.execute(
        "INSERT OR IGNORE INTO teiai_house_grants (guild_id, day, used) "
        "SELECT guild_id, grant_day, MAX(0, grant_used) FROM teiai_house_v2 WHERE grant_day != '' AND grant_used > 0"
    )
    conn.execute("DROP TABLE teiai_house_v2")


def migrate_v2_to_v3(conn: sqlite3.Connection) -> None:
    """旧 teiai_bank / teiai_house から v3 の責務別 6 表へ無損失移行する。"""
    _split_teiai_bank(conn)
    _split_teiai_house(conn)
    for stmt in V3_TEIAI_SPLIT_STATEMENTS:
        conn.execute(stmt)

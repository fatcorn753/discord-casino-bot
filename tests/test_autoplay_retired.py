"""廃止した自動プレイ: 預け中の分は、起動時に残高へ戻って表が消える。"""

import sqlite3

import pytest

from core.economy import INITIAL_BALANCE, Economy


@pytest.mark.real_scope
def test_leftover_pots_are_refunded_and_the_table_is_dropped(tmp_path):
    path = str(tmp_path / "t.db")
    eco = Economy(path)
    eco.for_guild(5).balance(7)
    eco._conn.commit()
    eco._conn.close()
    c = sqlite3.connect(path)
    c.executescript("""CREATE TABLE autoplay (guild_id INTEGER, user_id INTEGER, pot INTEGER);
        INSERT INTO autoplay VALUES (5, 7, 1150), (5, 8, 0);""")
    c.commit()
    c.close()
    again = Economy(path)  # bot を再起動した想定
    assert again.for_guild(5).balance(7) == INITIAL_BALANCE + 1150
    names = {r[0] for r in again._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "autoplay" not in names
    assert Economy(path).for_guild(5).balance(7) == INITIAL_BALANCE + 1150  # 二重に戻らない

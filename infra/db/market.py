"""証券の市場価格・ストップ高安・チャート掲示（Economy に混ぜ込む部品）。"""

from typing import Optional

from domain import market as market_mod
from domain.errors import Failure
from domain.market import DEFAULT
from infra.db.base import EcoBase


class MarketMixin(EcoBase):
    """銘柄の価格履歴・理論価格・張り付き状態・チャート投稿管理。"""

    def market_latest(self, symbol: str = DEFAULT) -> Optional[tuple]:
        """いまの価格 (時刻, 価格, 出来事)。まだ無ければ None。時計が戻っても、最後に記録した行を最新とする。"""
        with self._lock:
            r = self._conn.execute(
                "SELECT ts, price, event FROM market_prices WHERE symbol=? ORDER BY rowid DESC LIMIT 1", (symbol,)
            ).fetchone()
            return (r["ts"], r["price"], r["event"]) if r else None

    def latest_prices(self) -> dict:
        """いまの全銘柄の価格 {銘柄: 価格}。まだ価格がない銘柄は入らない。"""
        with self._lock:
            return {k: p for k in market_mod.BY_KEY if (p := (self.market_latest(k) or (0, 0, 0))[1])}

    def market_set(self, ts: float, price: float, event: int = 0, symbol: str = DEFAULT) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO market_prices (symbol, ts, price, event) VALUES (?, ?, ?, ?)",
                (symbol, ts, price, event),
            )

    def market_fair_get(self, symbol: str) -> Optional[float]:
        """裏の理論価格。まだ無ければ None。"""
        with self._lock:
            r = self._conn.execute("SELECT fair FROM market_fair WHERE symbol=?", (symbol,)).fetchone()
            return r["fair"] if r else None

    def market_fair_set(self, symbol: str, fair: float, pin: int, ts: float) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO market_fair (symbol, fair, pin, ts) VALUES (?, ?, ?, ?)",
                (symbol, fair, pin, ts),
            )

    def market_pin(self, symbol: str) -> int:
        """いま張り付いているか（1 ストップ高 / -1 ストップ安 / 0 なし）。"""
        with self._lock:
            r = self._conn.execute("SELECT pin FROM market_fair WHERE symbol=?", (symbol,)).fetchone()
            return r["pin"] if r else 0

    def _pin_reason(self, symbol: str, action: str) -> Optional[Failure]:
        """張り付き中で、この売買が約定しないなら Failure を返す。通るなら None。"""
        return market_mod.pin_lock_failure(symbol, self.market_pin(symbol), action)

    def market_history(self, since: float, symbol: str = DEFAULT) -> list:
        """since（UNIX時間）以降の価格の列 [(時刻, 価格, 出来事), ...]。古い順。"""
        with self._lock:
            return [
                (r["ts"], r["price"], r["event"])
                for r in self._conn.execute(
                    "SELECT ts, price, event FROM market_prices WHERE symbol=? AND ts>=? ORDER BY ts", (symbol, since)
                )
            ]

    def market_prune(self, before: float) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM market_prices WHERE ts<?", (before,))

    def swap_chart_post(
        self, channel_id: int, message_id: int, guild_id: int, period: str, ts: float, symbol: str = DEFAULT
    ) -> Optional[int]:
        """そのチャンネルのチャートを新しいものに入れ替え（更新中にする）、前のメッセージID（無ければ None）を返す。"""
        with self._lock, self._conn:
            prev = self._conn.execute("SELECT message_id FROM chart_posts WHERE channel_id=?", (channel_id,)).fetchone()
            self._conn.execute(
                "INSERT INTO chart_posts (channel_id, message_id, guild_id, period, ts, live, symbol) VALUES (?, ?, ?, ?, ?, 1, ?) "
                "ON CONFLICT(channel_id) DO UPDATE SET message_id=excluded.message_id, guild_id=excluded.guild_id, "
                "period=excluded.period, ts=excluded.ts, live=1, symbol=excluded.symbol",
                (channel_id, message_id, guild_id, period, ts, symbol),
            )
            return prev["message_id"] if prev else None

    def live_chart_posts(self) -> list:
        """自動で更新中のチャート。"""
        with self._lock:
            return [dict(r) for r in self._conn.execute("SELECT * FROM chart_posts WHERE live=1")]

    def end_chart_post(self, channel_id: int) -> None:
        with self._lock, self._conn:
            self._conn.execute("UPDATE chart_posts SET live=0 WHERE channel_id=?", (channel_id,))

    def delete_chart_post(self, channel_id: int) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM chart_posts WHERE channel_id=?", (channel_id,))

    def delete_guild_chart_posts(self, guild_id: int) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM chart_posts WHERE guild_id=?", (guild_id,))

    def prune_chart_posts(self, before_ts: float) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM chart_posts WHERE ts<? AND live=0", (before_ts,))

    # 昔の「チャート掲示」（廃止）の後始末用
    def list_chart_boards(self) -> list:
        with self._lock:
            return [dict(r) for r in self._conn.execute("SELECT * FROM chart_boards")]

    def delete_chart_board(self, guild_id: int) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM chart_boards WHERE guild_id=?", (guild_id,))

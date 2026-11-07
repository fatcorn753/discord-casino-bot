"""CSトークンの売買・空売り・チャート掲示（Economy に混ぜ込む部品）。

表の定義は core/economy.py の _init_schema にある。
"""

import math
from typing import Optional

from core import market as market_mod
from core.eco_types import CryptoResult


class CryptoMixin:
    """CSトークンの売買・空売り・チャート掲示（Economy に混ぜ込む部品）。"""

    # ------------------------------------------------------------------ #
    # CSトークン                                                          #
    # ------------------------------------------------------------------ #
    def market_latest(self) -> Optional[tuple]:
        """いまの価格 (時刻, 価格, 出来事)。まだ無ければ None。時計が戻っても、最後に記録した行を最新とする。"""
        with self._lock:
            r = self._conn.execute("SELECT ts, price, event FROM market_prices ORDER BY rowid DESC LIMIT 1").fetchone()
            return (r["ts"], r["price"], r["event"]) if r else None

    def market_set(self, ts: float, price: float, event: int = 0) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO market_prices (ts, price, event) VALUES (?, ?, ?)", (ts, price, event)
            )

    def market_history(self, since: float) -> list:
        """since（UNIX時間）以降の価格の列 [(時刻, 価格, 出来事), ...]。古い順。"""
        with self._lock:
            return [
                (r["ts"], r["price"], r["event"])
                for r in self._conn.execute(
                    "SELECT ts, price, event FROM market_prices WHERE ts>=? ORDER BY ts", (since,)
                )
            ]

    def market_prune(self, before: float) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM market_prices WHERE ts<?", (before,))

    def crypto_holding(self, user_id: int) -> tuple:
        """(保有口数, 買うのにかかった総額)。読み取り専用。"""
        with self._lock:
            r = self._conn.execute(
                "SELECT units, cost FROM crypto WHERE guild_id=? AND user_id=?", (self._g(), user_id)
            ).fetchone()
            return (r["units"], r["cost"]) if r else (0, 0)

    def crypto_holders(self, limit: int = 10) -> list:
        """このサーバーの保有者 [(user_id, 口数, 買った総額), ...]。買った額の大きい順。"""
        with self._lock:
            return [
                (r["user_id"], r["units"], r["cost"])
                for r in self._conn.execute(
                    "SELECT user_id, units, cost FROM crypto WHERE guild_id=? AND units>0 ORDER BY cost DESC, user_id LIMIT ?",
                    (self._g(), limit),
                )
            ]

    def crypto_buy(self, user_id: int, amount: int, price: float) -> "CryptoResult":
        """amount ペリカの範囲で、price の価格で買えるだけ買う（手数料込み）。"""
        with self._lock, self._conn:
            self._ensure(user_id)
            if amount <= 0 or price <= 0:
                return CryptoResult(False, "金額が正しくありません。")
            if self._debt(user_id) > 0:
                return CryptoResult(
                    False,
                    f"借金が {self._debt(user_id):,}ペリカ 残っているので、買えません。",
                    balance=self._balance(user_id),
                )
            if self._balance(user_id) < amount:
                return CryptoResult(
                    False,
                    f"ペリカが足りません（残高 {self._balance(user_id):,}ペリカ）。",
                    balance=self._balance(user_id),
                )
            units, total, fee = market_mod.buy_quote(amount, price)
            if units < 1:
                return CryptoResult(
                    False, "その金額では 0.001枚も買えません。金額を増やしてください。", balance=self._balance(user_id)
                )
            self._apply(user_id, -total, f"{market_mod.NAME}購入:{market_mod.lots(units)}@{market_mod.fmt(price)}")
            self._conn.execute(
                "INSERT INTO crypto (guild_id, user_id, units, cost) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(guild_id, user_id) DO UPDATE SET units=units+excluded.units, cost=cost+excluded.cost",
                (self._g(), user_id, units, total),
            )
            return CryptoResult(True, units=units, money=total, fee=fee, balance=self._balance(user_id))

    def crypto_sell(self, user_id: int, units: Optional[int], price: float) -> "CryptoResult":
        """units 口を売る（None なら全部）。受け取る額は手数料を引いたもの。"""
        with self._lock, self._conn:
            have, cost = self.crypto_holding(user_id)
            if have <= 0:
                return CryptoResult(False, f"{market_mod.NAME}を持っていません。")
            units = have if units is None else units
            if units < 1 or units > have:
                return CryptoResult(False, f"売れるのは 0.001〜{market_mod.lots(have)} です（持っている分）。")
            proceeds, fee = market_mod.sell_quote(units, price)
            if proceeds < 1 and units != have:
                return CryptoResult(False, "その数量では受け取れる額が 1ペリカ未満です。もう少し多く売ってください。")
            removed = (
                cost if units == have else round(cost * units / have)
            )  # 端数の按分は四捨五入（切り捨てだと取得額が少しずつ残る）
            self._ensure(user_id)
            self._apply(user_id, proceeds, f"{market_mod.NAME}売却:{market_mod.lots(units)}@{market_mod.fmt(price)}")
            if units == have:
                self._conn.execute("DELETE FROM crypto WHERE guild_id=? AND user_id=?", (self._g(), user_id))
            else:
                self._conn.execute(
                    "UPDATE crypto SET units=units-?, cost=cost-? WHERE guild_id=? AND user_id=?",
                    (units, removed, self._g(), user_id),
                )
            return CryptoResult(
                True, units=units, money=proceeds, fee=fee, balance=self._balance(user_id), profit=proceeds - removed
            )

    # ------------------------------------------------------------------ #
    # CSトークンの空売り                                                  #
    # ------------------------------------------------------------------ #
    SHORT_LEVERAGES = (1, 2, 3, 5)
    LOSSCUT = 0.5  # 損失が担保のこの割合に達したら、強制決済（1分に動く最大幅 ×5倍でも、担保を超えない）
    SHORT_MIN_MARGIN = 10

    def short_position(self, user_id: int) -> tuple:
        """(口数, 建てた総額, 担保)。建てていなければ (0, 0.0, 0)。"""
        with self._lock:
            r = self._conn.execute(
                "SELECT units, entry_value, margin FROM crypto_short WHERE guild_id=? AND user_id=?",
                (self._g(), user_id),
            ).fetchone()
            return (r["units"], r["entry_value"], r["margin"]) if r else (0, 0.0, 0)

    @staticmethod
    def short_pnl(units: int, entry_value: float, price: float) -> int:
        """空売りの含み損益（価格が下がるとプラス）。"""
        return round(entry_value - units * price / market_mod.UNIT)

    def short_holders(self, limit: int = 10) -> list:
        """[(ユーザーID, 口数, 建てた総額)] 建てている口数が多い順。チャートの表示用。"""
        with self._lock:
            return [
                (r["user_id"], r["units"], r["entry_value"])
                for r in self._conn.execute(
                    "SELECT user_id, units, entry_value FROM crypto_short WHERE guild_id=? AND units>0 ORDER BY units DESC, user_id LIMIT ?",
                    (self._g(), limit),
                )
            ]

    def short_equity(self) -> dict:
        """このサーバーの {ユーザーID: 空売りの評価額（担保 + 含み損益。0未満にはしない）}。総資産に足す。"""
        with self._lock:
            latest = self.market_latest()
            if latest is None:
                return {}
            return {
                r["user_id"]: max(0, r["margin"] + self.short_pnl(r["units"], r["entry_value"], latest[1]))
                for r in self._conn.execute(
                    "SELECT user_id, units, entry_value, margin FROM crypto_short WHERE guild_id=?", (self._g(),)
                )
            }

    def short_open(self, user_id: int, units: int, price: float, leverage: int) -> "CryptoResult":
        """units 口を売り建てる。担保 = 総額 ÷ レバレッジ（切り上げ）を、残高から預かる。"""
        with self._lock, self._conn:
            self._ensure(user_id)
            if leverage not in self.SHORT_LEVERAGES or units < 1 or price <= 0:
                return CryptoResult(False, "指定が正しくありません。")
            if self._debt(user_id) > 0:
                return CryptoResult(
                    False,
                    f"借金が {self._debt(user_id):,}ペリカ 残っているので、空売りはできません。",
                    balance=self._balance(user_id),
                )
            notional = units * price / market_mod.UNIT
            margin = math.ceil(round(notional / leverage, 6))
            if margin < self.SHORT_MIN_MARGIN:
                return CryptoResult(
                    False,
                    f"担保が {self.SHORT_MIN_MARGIN}ペリカ 未満になる量です。枚数を増やしてください。",
                    balance=self._balance(user_id),
                )
            if self._balance(user_id) < margin:
                return CryptoResult(
                    False,
                    f"担保が足りません（{margin:,}ペリカ必要・残高 {self._balance(user_id):,}ペリカ）。",
                    balance=self._balance(user_id),
                )
            self._apply(
                user_id, -margin, f"空売り担保:{market_mod.lots(units)}@{market_mod.fmt(price)}（{leverage}倍）"
            )
            self._conn.execute(
                "INSERT INTO crypto_short (guild_id, user_id, units, entry_value, margin) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(guild_id, user_id) DO UPDATE SET units=units+excluded.units, entry_value=entry_value+excluded.entry_value, margin=margin+excluded.margin",
                (self._g(), user_id, units, notional, margin),
            )
            return CryptoResult(True, units=units, money=margin, balance=self._balance(user_id))

    def _short_close_locked(self, user_id: int, units: Optional[int], price: float, label: str) -> "CryptoResult":
        have, entry_value, margin = self.short_position(user_id)
        if have <= 0:
            return CryptoResult(False, "空売りを建てていません。")
        units = have if units is None else units
        if units < 1 or units > have:
            return CryptoResult(False, f"買い戻せるのは 0.001〜{market_mod.lots(have)} です（建てている分）。")
        whole = units == have
        part_margin = margin if whole else round(margin * units / have)
        part_entry = entry_value if whole else entry_value * units / have
        pnl = self.short_pnl(units, part_entry, price)
        payout = max(0, part_margin + pnl)
        if payout:
            self._apply(user_id, payout, f"{label}:{market_mod.lots(units)}@{market_mod.fmt(price)}")
        if whole:
            self._conn.execute("DELETE FROM crypto_short WHERE guild_id=? AND user_id=?", (self._g(), user_id))
        else:
            self._conn.execute(
                "UPDATE crypto_short SET units=units-?, entry_value=entry_value-?, margin=margin-? WHERE guild_id=? AND user_id=?",
                (units, part_entry, part_margin, self._g(), user_id),
            )
        return CryptoResult(
            True, units=units, money=payout, balance=self._balance(user_id), profit=payout - part_margin
        )

    def short_close(self, user_id: int, units: Optional[int], price: float) -> "CryptoResult":
        """units 口を買い戻して決済する（None なら全部）。担保 + 損益が戻る（0未満にはならない）。"""
        with self._lock, self._conn:
            self._ensure(user_id)
            return self._short_close_locked(user_id, units, price, "空売り決済")

    def short_losscut(self, price: float) -> list:
        """全サーバーの建玉のうち、損失が担保の LOSSCUT に達したものを強制決済する。[(サーバー, ユーザー, 損失)]。"""
        done = []
        with self._lock, self._conn:
            for r in self._conn.execute(
                "SELECT guild_id, user_id, units, entry_value, margin FROM crypto_short"
            ).fetchall():
                if -self.short_pnl(r["units"], r["entry_value"], price) < self.LOSSCUT * r["margin"]:
                    continue
                try:
                    view = self.for_guild(r["guild_id"])
                except ValueError:
                    print(f"出どころ不明の空売りは、ロスカットできません: guild={r['guild_id']} user={r['user_id']}")
                    continue
                res = view._short_close_locked(r["user_id"], None, price, "空売りロスカット")
                if res.ok:
                    done.append((r["guild_id"], r["user_id"], -res.profit))
        return done

    def swap_chart_post(self, channel_id: int, message_id: int, guild_id: int, period: str, ts: float) -> Optional[int]:
        """そのチャンネルのチャートを新しいものに入れ替え（更新中にする）、前のメッセージID（無ければ None）を返す。"""
        with self._lock, self._conn:
            prev = self._conn.execute("SELECT message_id FROM chart_posts WHERE channel_id=?", (channel_id,)).fetchone()
            self._conn.execute(
                "INSERT INTO chart_posts (channel_id, message_id, guild_id, period, ts, live) VALUES (?, ?, ?, ?, ?, 1) "
                "ON CONFLICT(channel_id) DO UPDATE SET message_id=excluded.message_id, guild_id=excluded.guild_id, "
                "period=excluded.period, ts=excluded.ts, live=1",
                (channel_id, message_id, guild_id, period, ts),
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

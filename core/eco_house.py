"""運営口座（サーバーごとに1つの、システムの口座）。Economy に混ぜ込む部品。

- ユーザーの負けは口座に入り、勝ち分・イベントの上乗せ・クイズや債券の支払い・付与などは口座から出る（口座は残高がマイナスになってもよい）。
- デイリー・ミッション・初期支給は、システムの「発行」で、口座の残高は動かさない（house_issue に記録して、毎日の公開に別に出す）。
- お金を動かす場所（Economy._apply の house 引数）が、帳簿 house_ledger に分類つきで記録する。ユーザーの帳簿 ledger とは別。
- 口座は、そのサーバーで最初にお金が動いたとき（または最初の確認のとき）に、初期資金つきで開く。
- 持ち主は、資金を足す（house_add_funds。ペリカが新しく増える）ことも、自分の遊ぶ口座から入金する（house_deposit）ことも、口座から自分の遊ぶ口座へ移す（house_embezzle）こともできる。
- ユーザー同士のお金（送金、みんなのクイズ、抽選）と、CSトークンの売買は、口座を通さない。

表の定義は core/economy.py の _init_schema にある。
"""

import time

INITIAL_FUNDS = 100_000          # 口座を開くときの資金（サーバーごとに同じ額）
NON_OPERATING = ("初期資金", "資金追加", "入金", "横領")      # 営業の収支には数えない動き


class HouseMixin:
    """運営口座の残高と帳簿。"""

    def _house_open(self) -> int:
        """このサーバーの運営口座の残高。無ければ初期資金つきで開く（呼び出し側のトランザクション内で使う）。"""
        row = self._conn.execute("SELECT balance FROM house WHERE guild_id=?", (self._g(),)).fetchone()
        if row is not None:
            return row["balance"]
        now = time.time()
        self._conn.execute("INSERT INTO house (guild_id, balance, created, last_report) VALUES (?, ?, ?, '')", (self._g(), INITIAL_FUNDS, now))
        self._conn.execute("INSERT INTO house_ledger (guild_id, ts, category, delta, balance_after) VALUES (?, ?, '初期資金', ?, ?)",
                           (self._g(), now, INITIAL_FUNDS, INITIAL_FUNDS))
        return INITIAL_FUNDS

    def _house_move(self, category: str, delta: int) -> None:
        """運営口座に delta を足す（負なら出す）。category は帳簿の分類（「ゲーム:スロット」のように「:」で細分できる）。"""
        if not delta:
            return
        balance = self._house_open() + delta
        self._conn.execute("UPDATE house SET balance=? WHERE guild_id=?", (balance, self._g()))
        self._conn.execute("INSERT INTO house_ledger (guild_id, ts, category, delta, balance_after) VALUES (?, ?, ?, ?, ?)",
                           (self._g(), time.time(), category, delta, balance))

    def _house_issue(self, category: str, amount: int) -> None:
        """システムが発行したペリカを記録する（運営口座の残高は動かさない）。"""
        if amount > 0:
            self._conn.execute("INSERT INTO house_issue (guild_id, ts, category, amount) VALUES (?, ?, ?, ?)", (self._g(), time.time(), category, amount))

    def house_balance(self) -> int:
        with self._lock, self._conn:
            return self._house_open()

    def house_info(self) -> dict:
        """残高・開いた時刻・最後に公開した日（'' なら未公開）。"""
        with self._lock, self._conn:
            self._house_open()
            return dict(self._conn.execute("SELECT balance, created, last_report FROM house WHERE guild_id=?", (self._g(),)).fetchone())

    def house_add_funds(self, amount: int) -> int:
        """資金を足す（持ち主の操作）。新しい残高を返す。"""
        if amount <= 0:
            raise ValueError("amount は正の数にしてください")
        with self._lock, self._conn:
            self._house_move("資金追加", amount)
            return self._house_open()

    def house_deposit(self, user_id: int, amount: int) -> tuple:
        """自分の遊ぶ口座から、運営口座へ入金する（横領の反対）。(成功か, 理由か入金した額) を返す。ペリカは新しく増えない。"""
        with self._lock, self._conn:
            if amount <= 0:
                return False, "金額は1以上にしてください。"
            self._ensure(user_id)
            if self._balance(user_id) < amount:
                return False, f"残高が足りません（いまの残高: {self._balance(user_id):,}ペリカ）。"
            self._apply(user_id, -amount, "運営口座へ入金")
            self._house_move("入金", amount)
            return True, amount

    def house_embezzle(self, user_id: int, amount: int) -> tuple:
        """運営口座から、持ち主の遊ぶ口座へ移す（横領）。口座の正の残高の範囲まで。(成功か, 理由か移した額) を返す。"""
        with self._lock, self._conn:
            balance = self._house_open()
            if amount <= 0:
                return False, "金額は1以上にしてください。"
            if balance <= 0:
                return False, f"運営口座に残高がありません（いまの残高: {balance:,}ペリカ）。"
            if amount > balance:
                return False, f"運営口座の残高（{balance:,}ペリカ）を超えています。"
            self._ensure(user_id)
            self._apply(user_id, amount, "横領（運営口座から）")
            self._house_move("横領", -amount)
            return True, amount

    def house_summary(self, start: float, end: float) -> dict:
        """start 以上 end 未満の帳簿の集計。{"rows": {分類: (件数, 合計)}, "opening": 期間の前の残高, "closing": 期間の終わりの残高, "issued": {分類: (件数, 合計)}（システムの発行）}。"""
        with self._lock, self._conn:
            self._house_open()
            rows = {r["category"]: (r["n"], r["total"]) for r in self._conn.execute(
                "SELECT category, COUNT(*) AS n, SUM(delta) AS total FROM house_ledger WHERE guild_id=? AND ts>=? AND ts<? GROUP BY category", (self._g(), start, end))}
            last = self._conn.execute("SELECT balance_after FROM house_ledger WHERE guild_id=? AND ts<? ORDER BY id DESC LIMIT 1", (self._g(), end)).fetchone()
            first = self._conn.execute("SELECT balance_after, delta FROM house_ledger WHERE guild_id=? AND ts>=? ORDER BY id LIMIT 1", (self._g(), start)).fetchone()
            closing = last["balance_after"] if last else 0
            if not last and not self._conn.execute("SELECT 1 FROM house_ledger WHERE guild_id=? LIMIT 1", (self._g(),)).fetchone():
                closing = self._conn.execute("SELECT balance FROM house WHERE guild_id=?", (self._g(),)).fetchone()["balance"]       # 帳簿を消したあとは、いまの残高から始める
            opening = (first["balance_after"] - first["delta"]) if first else closing
            issued = {r["category"]: (r["n"], r["total"]) for r in self._conn.execute(
                "SELECT category, COUNT(*) AS n, SUM(amount) AS total FROM house_issue WHERE guild_id=? AND ts>=? AND ts<? GROUP BY category", (self._g(), start, end))}
            return {"rows": rows, "opening": opening, "closing": closing, "issued": issued}

    def house_mark_reported(self, day: str) -> None:
        with self._lock, self._conn:
            self._house_open()
            self._conn.execute("UPDATE house SET last_report=? WHERE guild_id=?", (day, self._g()))

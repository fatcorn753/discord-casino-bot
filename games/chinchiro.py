"""チンチロ（チンチロリン）のルールと勝敗計算。Discord に依存しない純粋なロジック。

サイコロ3個を振り、出目の「役」で勝負する。目が出なければ最大3回まで振り直せる。

役の強さ（強い順）:
  ピンゾロ(1-1-1) > 嵐(2〜6のゾロ目) > シゴロ(4-5-6) > 目(ペア＋1つ。1つの数が目) > 目なし > ヒフミ(1-2-3)
倍率（勝ったときに受け取る倍率。賭け金に対して）:
  ピンゾロ 5倍 / 嵐 3倍 / シゴロ 2倍 / 目 1倍
  ヒフミは負け役で、負けると 2倍払い。3回振って目が出なければ「目なし」で最弱（ヒフミよりは強い）
"""

import random
from dataclasses import dataclass

PINZORO, ARASHI, SHIGORO, ME, MENASHI, HIFUMI = "pinzoro", "arashi", "shigoro", "me", "menashi", "hifumi"

MAX_ROLLS = 3
DICE_FACES = {1: "⚀", 2: "⚁", 3: "⚂", 4: "⚃", 5: "⚄", 6: "⚅"}

# 役ごとの表示名と、役の倍率
ROLE_NAME = {PINZORO: "ピンゾロ", ARASHI: "嵐（アラシ）", SHIGORO: "シゴロ", ME: "目", MENASHI: "目なし", HIFUMI: "ヒフミ"}
ROLE_MULT = {PINZORO: 5, ARASHI: 3, SHIGORO: 2, ME: 1, MENASHI: 1, HIFUMI: 1}


@dataclass(frozen=True)
class Hand:
    kind: str
    value: int = 0       # 目の数（kind が me のとき）／嵐の数字

    @property
    def strength(self) -> int:
        """大きいほど強い。同じ値なら引き分け。"""
        if self.kind == HIFUMI:
            return 0
        if self.kind == MENASHI:
            return 1
        if self.kind == ME:
            return 10 + self.value
        if self.kind == SHIGORO:
            return 30
        if self.kind == ARASHI:
            return 40 + self.value
        return 100

    @property
    def name(self) -> str:
        if self.kind == ME:
            return f"{self.value}の目"
        if self.kind == ARASHI:
            return f"嵐（{self.value}のゾロ目）"
        return ROLE_NAME[self.kind]


def classify(dice) -> Hand:
    """3つの出目から役を決める。"""
    a, b, c = sorted(dice)
    if a == b == c:
        return Hand(PINZORO) if a == 1 else Hand(ARASHI, a)
    if (a, b, c) == (4, 5, 6):
        return Hand(SHIGORO)
    if (a, b, c) == (1, 2, 3):
        return Hand(HIFUMI)
    if a == b:
        return Hand(ME, c)
    if b == c:
        return Hand(ME, a)
    return Hand(MENASHI)


def roll_dice(rng: random.Random) -> tuple:
    return tuple(rng.randint(1, 6) for _ in range(3))


def play_hand(rng: random.Random) -> list:
    """目が出る（またはヒフミ・3回）まで振る。[(出目, 役), ...] を返す。最後の要素が確定した役。"""
    rolls = []
    for _ in range(MAX_ROLLS):
        dice = roll_dice(rng)
        hand = classify(dice)
        rolls.append((dice, hand))
        if hand.kind != MENASHI:
            break
    return rolls


@dataclass(frozen=True)
class Outcome:
    result: str          # "win" / "lose" / "draw"
    multiplier: int      # 賭け金に対する倍率（勝ち・負けの大きさ）。引き分けは 0


def compare(player: Hand, dealer: Hand) -> Outcome:
    """子（プレイヤー）と親（ディーラー）の役を比べる。

    勝った側の役の倍率が適用される。負けた側がヒフミなら、倍率は最低でも2倍。
    同じ強さは引き分け（返金）。ただし目なし同士は親の勝ち。
    """
    if player.strength == dealer.strength:
        # 目なし同士（どちらも3回振って目が出なかった）の引き分けは親の勝ち。これが胴元の取り分になる
        if player.kind == MENASHI:
            return Outcome("lose", 1)
        return Outcome("draw", 0)
    if player.strength > dealer.strength:
        mult = max(ROLE_MULT[player.kind], 2 if dealer.kind == HIFUMI else 1)
        return Outcome("win", mult)
    mult = max(ROLE_MULT[dealer.kind], 2 if player.kind == HIFUMI else 1)
    return Outcome("lose", mult)


def payout(bet: int, outcome: Outcome) -> tuple:
    """(預かり金から払い戻す総額, 預かり金以外から追加で引く額) を返す。

    - 勝ち: 賭け金 + 賭け金×倍率 を受け取る
    - 引き分け: 賭け金を返す
    - 負け: 賭け金は没収。倍率が2以上なら、その分（倍率-1）×賭け金を残高から追加で引く（残高の範囲まで）
    """
    if outcome.result == "win":
        return bet + bet * outcome.multiplier, 0
    if outcome.result == "draw":
        return bet, 0
    return 0, bet * (outcome.multiplier - 1)


def dice_text(dice) -> str:
    return " ".join(DICE_FACES[d] for d in dice)


def expected_value(trials: int = 200_000, seed: int = 1) -> float:
    """シミュレーションで、1ペリカ賭けたときの損益の平均を求める（親に有利な差がないかの確認用）。"""
    rng = random.Random(seed)
    total = 0.0
    for _ in range(trials):
        player = play_hand(rng)[-1][1]
        dealer = play_hand(rng)[-1][1]
        out = compare(player, dealer)
        total += {"win": out.multiplier, "lose": -out.multiplier, "draw": 0}[out.result]
    return total / trials

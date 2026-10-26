"""ブラックジャック。1デッキ、ディーラーは17でスタンド（ソフト17も）。スプリット・保険なし。"""

import random
from dataclasses import dataclass, field

SUITS = ["♠", "♥", "♦", "♣"]
RANKS = ["A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K"]


def new_deck(rng: random.Random) -> list:
    deck = [(r, s) for s in SUITS for r in RANKS]
    rng.shuffle(deck)
    return deck


def card_text(card) -> str:
    return f"{card[1]}{card[0]}"


def hand_text(cards) -> str:
    return " ".join(f"`{card_text(c)}`" for c in cards)


def value(cards) -> int:
    """手札の合計。Aは1か11のうち、バーストしない最大の方。"""
    total, aces = 0, 0
    for rank, _ in cards:
        if rank == "A":
            aces += 1
            total += 11
        elif rank in ("J", "Q", "K"):
            total += 10
        else:
            total += int(rank)
    while total > 21 and aces:
        total -= 10
        aces -= 1
    return total


def is_blackjack(cards) -> bool:
    return len(cards) == 2 and value(cards) == 21


@dataclass
class Game:
    rng: random.Random
    deck: list = field(default_factory=list)
    player: list = field(default_factory=list)
    dealer: list = field(default_factory=list)
    doubled: bool = False
    finished: bool = False

    def __post_init__(self):
        self.deck = new_deck(self.rng)
        self.player = [self.deck.pop(), self.deck.pop()]
        self.dealer = [self.deck.pop(), self.deck.pop()]
        # 最初の2枚で決着（ナチュラル）なら、すぐ終わる
        if is_blackjack(self.player) or is_blackjack(self.dealer):
            self.finished = True

    def hit(self) -> None:
        self.player.append(self.deck.pop())
        if value(self.player) >= 21:
            self.stand()

    def double(self) -> None:
        """賭け金を倍にして、1枚だけ引いて終わる。"""
        self.doubled = True
        self.player.append(self.deck.pop())
        self.stand()

    def stand(self) -> None:
        while value(self.dealer) < 17 and value(self.player) <= 21:
            self.dealer.append(self.deck.pop())
        self.finished = True

    def result(self) -> str:
        """"blackjack" / "win" / "push" / "lose" / "bust"。"""
        p, d = value(self.player), value(self.dealer)
        if is_blackjack(self.player):
            return "push" if is_blackjack(self.dealer) else "blackjack"
        if is_blackjack(self.dealer):
            return "lose"
        if p > 21:
            return "bust"
        if d > 21 or p > d:
            return "win"
        return "push" if p == d else "lose"


def payout(result: str, bet: int) -> int:
    """受け取る総額（bet は、ダブルダウンした場合は倍になった後の賭け金）。ブラックジャックは3:2。"""
    return {"blackjack": bet * 5 // 2, "win": bet * 2, "push": bet}.get(result, 0)

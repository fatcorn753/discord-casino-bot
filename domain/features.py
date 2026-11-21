"""機能のオン・オフ（サーバーごと）。管理者が、賭け事や仕組みを一時的に止めるためのもの。

止めるのは「新しく始めること」だけ。止めた時点で途中のもの（預かった賭け金・開催中の大会やレース・建玉）は、
いつもどおり精算する（止めると、預かったペリカが戻らなくなるため）。
"""

from typing import Optional

# 機能のキー（DB に入る値。変えないこと）→ 表示名。並びは管理コマンドの選択肢の順
FEATURES: dict[str, str] = {
    "chinchiro": "チンチロ",
    "pachinko": "パチンコ",
    "slot": "スロット",
    "blackjack": "ブラックジャック",
    "roulette": "ルーレット",
    "arena": "地下闘技場",
    "horse": "競馬",
    "market": "証券（株・信用・オプション）",
    "bonds": "債券",
    "kyosai": "帝愛共済",
    "loan": "個人間の貸付",
    "company": "会社",
    "labor": "地下労働",
    "quiz": "クイズ",
    "raffle": "抽選",
    "poll": "投票",
    "p2p": "P2P の出品・売買",
    "shop": "ショップ",
}

# カジノのゲーム名（GameContext の game）→ 機能のキー
CASINO_GAMES: dict[str, str] = {
    "チンチロ": "chinchiro",
    "パチンコ": "pachinko",
    "スロット": "slot",
    "ブラックジャック": "blackjack",
    "ルーレット": "roulette",
}

# スラッシュコマンドの名前（親から順に空白区切り）→ 機能のキー。長い名前が優先される
COMMANDS: dict[str, str] = {
    **{f"カジノ {name}": key for name, key in CASINO_GAMES.items()},
    "闘技場": "arena",
    "競馬": "horse",
    "証券 注文": "market",
    "証券 注文 債券": "bonds",
    "共済": "kyosai",
    "貸付": "loan",
    "会社": "company",
    "地下": "labor",
    "クイズ": "quiz",
    "投票": "poll",
    "ショップ": "shop",
}

# 止めていても使えるコマンド（見るだけ・手じまい・返済。止めた機能から抜けられるように）
ALWAYS_ALLOWED: frozenset[str] = frozenset(
    {
        "共済 状況",
        "共済 取消",
        "貸付 返済",
        "貸付 一覧",
        "会社 状況",
        "会社 持分",
        "クイズ 取り下げ",
    }
)


def feature_of_command(qualified_name: str) -> Optional[str]:
    """コマンドが属する機能のキー（止められない・止める対象でないコマンドは None）。"""
    if qualified_name in ALWAYS_ALLOWED:
        return None
    parts = qualified_name.split()
    for n in range(len(parts), 0, -1):
        key = COMMANDS.get(" ".join(parts[:n]))
        if key is not None:
            return key
    return None


def label(key: str) -> str:
    return FEATURES.get(key, key)

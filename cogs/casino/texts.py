"""カジノ各ゲーム・大勝ち祝福・演出台本の静的定義（プレゼンテーション静的テキスト層）。"""

from cogs.shared import ui

# ------------------------------------------------------------------ #
# スロット・ルーレット共通
# ------------------------------------------------------------------ #
SIMPLE_STEP_SECONDS = 0.9

ROULETTE_COLOR_EMOJI: dict[str, str] = {
    "緑": "🟢",
    "赤": "🔴",
    "黒": "⚫",
}


def roulette_color_emoji(color: str) -> str:
    return ROULETTE_COLOR_EMOJI[color]


# ------------------------------------------------------------------ #
# ブラックジャック
# ------------------------------------------------------------------ #
BLACKJACK_TITLE = "🃏 ブラックジャック"

BLACKJACK_RESULT_TEXT: dict[str, tuple[str, int]] = {
    "blackjack": ("ブラックジャック！ 3:2 で払い戻し🎉", ui.GOLD),
    "win": ("勝ち🎉", ui.OK),
    "push": ("引き分け（賭け金が戻る）", ui.INFO),
    "lose": ("負け…", ui.ERROR),
    "bust": ("バースト…（21を超えた）", ui.ERROR),
}

BLACKJACK_FLAIR_BLACKJACK = "🃏✨ **ナチュラルブラックジャック！** 最初の2枚で21！"
BLACKJACK_FLAIR_DOUBLED = "💥 **ダブルダウン成功！** 倍賭けが当たった！"
BLACKJACK_FLAIR_TWENTY_ONE = "🎯 **21ぴったり！**"


# ------------------------------------------------------------------ #
# チンチロ
# ------------------------------------------------------------------ #
CHINCHIRO_TITLE = "🎲 地下チンチロ"
CHINCHIRO_STEP_SECONDS = 0.9

DICE_FACES: dict[int, str] = {
    1: "⚀",
    2: "⚁",
    3: "⚂",
    4: "⚃",
    5: "⚄",
    6: "⚅",
}


def dice_text(dice, mark: str = "") -> str:
    """出目の表示。mark はサイコロごとの飾り。"""
    return " ".join(mark + DICE_FACES[d] for d in dice)


CHINCHIRO_WIN_LINE: dict[str, str] = {
    "pinzoro": "ピンゾロ…ッ！ 5倍…！ 場の空気が凍りつく…",
    "arashi": "嵐…ッ！ 3倍…！ ざわ…ざわ…",
    "shigoro": "シゴロ…！ 2倍…。流れが来ている…",
    "me": "目で押し切った…！ 静かな勝利…",
    "menashi": "相手のヒフミに救われた…！",
}

CHINCHIRO_LOSE_LINE: dict[str, str] = {
    "pinzoro": "親のピンゾロ…！ 5倍払い…。血の気が引いていく…",
    "arashi": "親の嵐…！ 3倍払い…。地下に悲鳴が響く…",
    "shigoro": "親のシゴロ…2倍払い…。",
    "me": "親の目が上だった…。",
    "menashi": "目なし同士は親の勝ち…。それが地下のルール…",
    "hifumi": "ヒフミ…ッ！ 倍払い…！ 地獄だ…",
}


# ------------------------------------------------------------------ #
# 連勝・大勝ち祝福（全ゲーム共通）
# ------------------------------------------------------------------ #
WIN_TIERS: tuple[tuple[float, str], ...] = (
    (50, "🎰💥 **超特大ジャックポット級！！** 💥🎰"),
    (10, "💎✨ **特大勝ち！！** ✨💎"),
    (3, "🎉 **大勝ち！** 🎉"),
)

JACKPOT_ART = """```
 *  .  *  .  *  .  *  .  *
 +=======================+
 |   J A C K P O T !!!   |
 |      7   7   7        |
 +=======================+
 *  .  *  .  *  .  *  .  *
```"""


def streak_line(streak: int, new_best: bool) -> str:
    """連勝の表示。2連勝から出す。"""
    if streak < 2:
        return ""
    fire = "🔥" * min(streak // 2 + 1, 4)
    line = f"{fire} **{streak}連勝中！**"
    if new_best and streak >= 3:
        line += "　🏅 自己ベスト更新！"
    return line


def big_win_line(bet: int, net: int) -> str:
    """賭け金に対する利益の大きさに応じた、勝ちの派手な一言。"""
    if net <= 0 or bet <= 0:
        return ""
    ratio = net / bet
    for min_ratio, message in WIN_TIERS:
        if ratio >= min_ratio:
            return message
    return ""


# ------------------------------------------------------------------ #
# パチンコ演出・表示定義
# ------------------------------------------------------------------ #
PACHINKO_TITLE = "🎰 地下パチンコ"
PACHINKO_STEP_SECONDS = 1.1
PACHINKO_FRAME_BUDGET = 60
PACHINKO_SHOWN_ROUNDS = 6
PACHINKO_EXTRA_SETS = (5, 10)
PACHINKO_SPINNING = "🌀"

PACHINKO_BANNER: dict[int, str] = {
    2: "",
    10: "# 🎊 大当たり！！ 🎊",
    30: "# 🔥🔥 激アツ確変！！！ 🔥🔥",
}

PACHINKO_RANKS: tuple[tuple[int, str], ...] = (
    (100, "👑 伝説級"),
    (30, "🏆 大勝利"),
    (10, "🎊 好調"),
    (0, "✨ 小当たり"),
)

PACHINKO_OMEN_TEXT: dict[str, str] = {
    "blue": "（青）ざわ…",
    "red": "（赤）ゾワッ…！ 予告だ…！",
    "gold": "（金）⚡ 激アツ予告 ⚡ これは来る…！",
}

PACHINKO_STEP_TEXT = ["ざわ…", "ゾワッ…！", "⚡ 激アツ ⚡"]
PACHINKO_STEP_ICON = ["🔵", "🔴", "🟡"]
PACHINKO_STEPS_OF: dict[str, int] = {
    "blue": 1,
    "red": 2,
    "gold": 3,
}

PACHINKO_CUTIN: dict[str, list[str]] = {
    "super": ["「チャンスだ！！」", "「いけるぞ！！」", "「ここからが本番だ！」", "「見せてやる！！」"],
    "premium": ["「これで終わりだ！！」", "「全部まとめて持っていけ！！」", "「運命を変えろ！！」", "「勝負だ！！」"],
}

PACHINKO_STORIES: dict[str, list[tuple[str, str]]] = {
    "normal": [
        ("🌊 **波の予告**", "画面が揺れている…"),
        ("🐟 **魚群が横切った**", "なにか起きる…？"),
        ("🎞️ **ロングリーチ**", "ゆっくり回っている…"),
    ],
    "super": [
        ("🎬 **ストーリーリーチ**", "主人公が立ち上がる…！"),
        ("🚄 **ダッシュリーチ**", "絵柄が走り出した…！"),
        ("⚡ **スーパーリーチ**", "背景が変わった…！"),
    ],
    "premium": [
        ("🐉 **龍が現れた**", "地鳴りがする…！！"),
        ("🌠 **流星群**", "夜空が光る…！"),
        ("🔥 **プレミアムリーチ**", "画面が燃えている…！！"),
    ],
}

PACHINKO_NORMAL_WHISPERS = ["ざわ… ざわ…", "じわ… じわ…", "…どっちだ…"]

# 当たりの演出の格（今回の当たり＝1ラウンド目の出玉の下限, 予告の色の候補, リーチの格の候補）。上から順に見て、最初に合うものを使う。
# 演出の強さを出玉に比例させるため、当たりの見せ方は抽選の omen / tier ではなく、今回の当たりの出玉から決める（当落と還元率は変えない）。
# 連チャンの継続は、まだ起きていない抽選なので、ここには混ぜない。
PACHINKO_GRADES: tuple[tuple[int, tuple[str, ...], tuple[str, ...]], ...] = (
    (30, ("red", "gold", "gold"), ("super", "premium", "premium")),
    (10, ("blue", "red"), ("normal", "super", "super")),
    (0, ("", "", "blue"), ("", "normal", "normal")),
)
# 外れるリーチ（ガセ）を、1セットで何回まで見せるか。連続投入では、全体で何回までか
PACHINKO_SHOWN_MISSES = 1
PACHINKO_MULTI_SHOWN_MISSES = 2
PACHINKO_CHAIN_WAIT = ["継続抽選… 🎲", "…続くか…！？", "ざわ… 次は…！？"]  # 連チャンの継続を待つ間のひと言

PACHINKO_REVIVE_TEXT: list[tuple[str, str]] = [
    ("それでも!!!", "…諦めない…！！"),
    ("と、思っていたのか", "…画面が震えている…！！"),
    ("まだ終わっていない…！", "…リールが動き出す…！"),
    ("ここからだ…！！", "…運命が書き換わる…！"),
]

PACHINKO_FLAVOR: dict[str, str] = {
    "ハズレ": "…沈黙。ペリカが静かに溶けていく…",
    "小当たり": "ッ…！ 拾った…少しだけ…",
    "大当たり": "ざわ…ざわ…ざわ…！ 大当たり…！ 見ろ、この出玉…！",
    "激アツ確変": "どよめき…！ 確変突入…！ 地下が揺れる…！！",
}

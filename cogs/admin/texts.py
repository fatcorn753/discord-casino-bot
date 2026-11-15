"""運営コマンド（/運営）の静的ラベル定義（プレゼンテーション静的テキスト層）。"""

ANNOUNCE_TITLE = "📢 お知らせ"

OMEN_NAME: dict[str, str] = {
    "": "なし",
    "blue": "🔵 青",
    "red": "🔴 赤",
    "gold": "🟡 金",
}

TIER_NAME: dict[str, str] = {
    "": "なし",
    "normal": "ノーマル",
    "super": "スーパー",
    "premium": "プレミアム",
}

LEVEL_NAME: dict[int, str] = {
    1: "かなり渋い",
    2: "渋い",
    3: "標準",
    4: "甘い",
    5: "かなり甘い",
    6: "大盤振る舞い",
}

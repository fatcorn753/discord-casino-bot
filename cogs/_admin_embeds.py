"""運営コマンド（`/運営`）が出す埋め込みの組み立て。Discord への送受信はしない。"""

import discord

from core import ui
from core.duration import format_minutes
from games import pachinko, rtplevel

MAX_IMAGE_BYTES = 8 * 1024 * 1024
ANNOUNCE_TITLE = "📢 お知らせ"


def announce_embed(title: str, body: str, filename: str = "") -> discord.Embed:
    """お知らせの見た目。画像があれば、添付ファイルを埋め込みの中に出す。"""
    e = ui.embed(title or ANNOUNCE_TITLE, body, ui.GOLD)
    if filename:
        e.set_image(url=f"attachment://{filename}")
    return e


def bulk_preview_lines(quizzes: list) -> str:
    lines = []
    for i, q in enumerate(quizzes, start=1):
        text = q["question"].replace("\n", " ")
        lines.append(
            f"`{i:>2}.` {text[:40]}{'…' if len(text) > 40 else ''}　正解「{q['choices'][0][:20]}」　{q['reward']:,}ペリカ　"
            + (format_minutes(q["minutes"]) if q["minutes"] else "無期限")
        )
    return "\n".join(lines)


OMEN_NAME = {"": "なし", "blue": "🔵 青", "red": "🔴 赤", "gold": "🟡 金"}
TIER_NAME = {"": "なし", pachinko.NORMAL: "ノーマル", pachinko.SUPER: "スーパー", pachinko.PREMIUM: "プレミアム"}
SMALL_SAMPLE = 100  # これより少ない回転数の行には「参考」を付ける（当たり率がまだ安定しないため）


def pachinko_stats_embed(data: dict, scope: str) -> discord.Embed:
    """設計した数字と、実際の記録を並べる（セット全体と、予告・リーチごとの回転）。"""
    sets, hit_sets, bet, back, balls, starts, chain_max = data["sets"]
    if not sets:
        return ui.embed(f"🎰 パチンコ統計（{scope}）", "まだ記録がありません。", ui.INFO)
    spins, spin_hits = data["spins"]

    def rows(group: dict, names: dict, designed: dict) -> str:
        lines = []
        for key, name in names.items():
            n, h = group.get(key, (0, 0))
            actual = f"{h / n:.1%}" if n else "—"
            lines.append(
                f"{name}: {n:,}回転　実績 **{actual}** ／ 設計 {designed[key]:.1%}"
                + ("（参考）" if n < SMALL_SAMPLE else "")
            )
        return "\n".join(lines)

    per_set_design = pachinko.BALLS_PER_SET * pachinko.P_START
    e = ui.embed(
        f"🎰 パチンコ統計（{scope}）",
        f"{sets:,}セット（{balls:,}発）　スタート入賞: 1セットあたり 実績 **{starts / sets:.2f}回** ／ 設計 {per_set_design:.2f}回\n"
        f"当たったセット: 実績 **{hit_sets / sets:.1%}** ／ 設計 {pachinko.hit_rate():.1%}\n"
        f"還元率: 実績 **{back / bet:.1%}** ／ 設計 {pachinko.rtp():.1%}（賭け金 {ui.pelica(bet)}）　最大連チャン {chain_max}連\n"
        f"1回転の当たり率: 実績 **{spin_hits / spins:.2%}** ／ 設計 {pachinko.mean_hit_prob():.2%}（{spins:,}回転）",
        ui.INFO,
    )
    e.add_field(
        name="予告の色ごとの当たり率（1回転あたり）",
        value=rows(data["omen"], OMEN_NAME, pachinko.hit_rate_by_omen()),
        inline=False,
    )
    e.add_field(
        name="リーチの格ごとの当たり率（1回転あたり）",
        value=rows(data["tier"], TIER_NAME, pachinko.hit_rate_by_tier()),
        inline=False,
    )
    e.set_footer(
        text=f"{SMALL_SAMPLE}回転に満たない行は参考値です。還元率は連チャンの偏りで大きくぶれます（還元率レベルをかける前の数字です）"
    )
    return e


LEVEL_NAME = {1: "かなり渋い", 2: "渋い", 3: "標準", 4: "甘い", 5: "かなり甘い", 6: "大盤振る舞い"}


def rtp_table_embed(levels: dict, title: str) -> discord.Embed:
    """ゲームごとの、いまのレベルと、還元率の目安。"""
    lines = []
    for game in rtplevel.GAMES:
        level = levels[game]
        shown = rtplevel.approx_rtp(game, level)
        mark = "" if level == rtplevel.DEFAULT_LEVEL else "　⚠️ 標準から変更中"
        extra = (
            "（赤・黒などの目安。数字賭けは別）"
            if game == "ルーレット"
            else ("（目安）" if game in ("チンチロ", "ブラックジャック") else "")
        )
        lines.append(f"**{game}**　レベル **{level}**（{LEVEL_NAME[level]}）　還元率 約{shown:.1%}{extra}{mark}")
    e = ui.embed(title, "\n".join(lines), ui.INFO)
    e.set_footer(
        text="レベル1〜6。3が標準。1段階で還元率が約4ポイント動きます。勝ち分だけが増減します（ユーザーには見せません）"
    )
    return e

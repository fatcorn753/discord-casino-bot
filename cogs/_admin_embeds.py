"""運営コマンド（`/運営`）が出す埋め込みの組み立て。Discord への送受信はしない。"""

import datetime

import discord

from core import approval, catalogs, ui
from core import labor as lab
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


_PK_CFG = catalogs.games().pachinko
OMEN_NAME = dict(_PK_CFG.omen_names)
TIER_NAME = dict(_PK_CFG.tier_names)
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


LEVEL_NAME = dict(catalogs.games().rtplevel.level_names)


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


def click_history_embed(name: str, report: dict, tz_hours: float, hours: int) -> discord.Embed:
    """「働く」を押した記録（1時間ごと）。自動操作の疑いを、持ち主が見て判断するための数字。決めつけはしない。"""
    tz = datetime.timezone(datetime.timedelta(hours=tz_hours))
    rows = report["rows"]
    if not rows:
        return ui.embed(f"🕵️ {name} の「働く」の記録", f"過去{hours}時間に、押した記録はありません。", ui.INFO)

    def is_mech(r: dict) -> bool:
        return r["clicks"] >= lab.MECH_MIN_CLICKS and r["cv"] is not None and r["cv"] < lab.MECH_CV_MAX

    mech = sum(1 for r in rows if is_mech(r))
    lines = [
        f"押した回数 **{report['clicks']:,}回**　稼ぎ **{report['pay']:,}ペリカ**",
        f"押した時間 **{report['active_hours']}時間**（過去{hours}時間のうち）　休まず押し続けた最長 **{report['longest_run']}時間**",
        f"押す間隔が機械的に規則正しい時間（{lab.MECH_MIN_CLICKS}回以上・ばらつき {lab.MECH_CV_MAX} 未満）: **{mech}時間**",
        f"（これが **{lab.SUSPECT_HOURS}時間連続**すると、自動で、その日のクリックの稼ぎが打ち止めになります）",
        "",
        "**1時間ごと**（間隔のばらつき = 標準偏差 ÷ 平均。人間が手で押すと、ふつう 0.3 以上）",
    ]
    for r in rows[-24:]:
        at = datetime.datetime.fromtimestamp(r["hour"] * 3600, tz)
        gap = f"平均{r['mean_gap']:.2f}秒" if r["mean_gap"] else "—"
        cv = f"ばらつき{r['cv']:.2f}" if r["cv"] is not None else "—"
        low = f"最短{r['min_gap']:.2f}秒" if r["min_gap"] is not None else ""
        mark = " ⚠️" if is_mech(r) else ""
        lines.append(f"`{at:%m/%d %H時}` {r['clicks']:>4}回　{gap}　{cv}　{low}{mark}")
    e = ui.embed(f"🕵️ {name} の「働く」の記録", "\n".join(lines)[:4000], ui.INFO)
    e.set_footer(
        text="目安です。人間でも、規則的に押す人はいます。24時間ずっと押し続けていないか、時間のあき方を見てください。"
    )
    return e


def approval_request_embed(guild: discord.Guild) -> discord.Embed:
    """新しいサーバーに追加されたときの、持ち主向け承認依頼。"""
    embed = ui.embed("🆕 新しいサーバーに追加されました", color=ui.GOLD)
    embed.add_field(name="サーバー", value=f"{guild.name}\n`{guild.id}`", inline=True)
    embed.add_field(name="持ち主", value=f"<@{guild.owner_id}>", inline=True)
    embed.add_field(name="メンバー数", value=f"{guild.member_count or '?'}人", inline=True)
    embed.set_footer(text="許可するまで、このサーバーではコマンドが使えません")
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    return embed


def approved_notice_embed() -> discord.Embed:
    """サーバーが承認されたときの、サーバー側への挨拶。"""
    return ui.ok_embed(
        "遊べるようになりました",
        "Bot の持ち主に承認されました。`/help` で遊び方、`/ペリカ デイリー` からどうぞ。",
    )


def server_list_embed(bot_guilds: list, status_rows: list, config: dict) -> discord.Embed:
    """許可・承認待ち・断ったサーバーの一覧。"""
    joined = {g.id: g for g in bot_guilds}
    rows = {r["guild_id"]: r for r in status_rows}
    pre = approval.preapproved(config)
    groups = {"✅ 許可": [], "⏳ 承認待ち": [], "🚫 断った": []}
    for gid in pre:
        groups["✅ 許可"].append((gid, joined[gid].name if gid in joined else "（設定ファイルで許可）", gid in joined))
    for gid, r in rows.items():
        if gid in pre:
            continue
        key = {approval.APPROVED: "✅ 許可", approval.PENDING: "⏳ 承認待ち", approval.DENIED: "🚫 断った"}[r["status"]]
        groups[key].append((gid, joined[gid].name if gid in joined else (r["name"] or "?"), gid in joined))
    e = ui.embed(
        "🌐 サーバーの許可状況",
        f"承認制: **{'ON' if approval.requires_approval(config) else 'OFF'}**　参加中: {len(joined)}サーバー",
        ui.INFO,
    )
    for title, items in groups.items():
        e.add_field(
            name=f"{title}（{len(items)}）",
            value="\n".join(f"{'🟢' if on else '⚪'} {name}　`{gid}`" for gid, name, on in items)[:1024] or "なし",
            inline=False,
        )
    e.set_footer(text="🟢 = いま参加中 / ⚪ = 参加していない")
    return e

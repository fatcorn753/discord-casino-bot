"""`/地下 作業`・`/地下 ランキング` の画面の組み立て（作業場・雇用・採掘ランキング）と文言。"""

import discord

from cogs.shared import ui
from domain import labor as lab
from domain import relics, shop
from domain.ports.contracts import LaborState, LaborTop


def fatigue_label(fatigue: float) -> str:
    return "元気" if fatigue < 25 else "ふつう" if fatigue < 50 else "疲れ気味" if fatigue < 75 else "ぐったり"


def ore_text(found: dict) -> str:
    """掘った鉱石の一覧（価値の高い順）。"""
    order = sorted(lab.ORE_VALUE, key=lambda k: -lab.ORE_VALUE[k])  # 高い順
    return "　".join(f"{lab.ORE_MARK[k]}{lab.ORE_NAME[k]}×{found[k]}" for k in order if found.get(k))


def relic_text(found: dict) -> str:
    """見つけた発掘品の一覧（目安の価値の高い順）。"""
    order = sorted(found, key=lambda i: -shop.RELIC_VALUE.get(i, 0))
    return "　".join(f"{shop.BY_ID[i].mark}{shop.BY_ID[i].name}×{found[i]}" for i in order if found.get(i))


def _find_label(f: dict) -> str:
    """掘り出し1つの表示（鉱石は売値、発掘品は目安の価値）。"""
    if f["ore"] in shop.RELIC_VALUE:
        item = shop.BY_ID[f["ore"]]
        return f"{item.mark}{item.name}（目安 {shop.RELIC_VALUE[f['ore']]:,}）"
    return f"{lab.ORE_MARK[f['ore']]}{lab.ORE_NAME[f['ore']]}（{lab.ORE_VALUE[f['ore']]:,}）"


def finds_text(finds: list) -> str:
    """最近の掘り出し（石炭は除く）。いつ・どの作業員が・何を掘ったか。"""
    return "\n".join(
        f"<t:{int(f['at'])}:R>　{lab.WORKER_MARK[f['kind']]}{lab.WORKER_NAME[f['kind']]} が {_find_label(f)}"
        for f in finds
    )


def pot_text(state: LaborState) -> str:
    """未受け取りの額。作業員がいるときは、そのうち作業員が稼いだ分（鉱石の売値を含む）も見せる。"""
    text = f"**{ui.pelica(state['pot_total'])}**"
    if state["workers"] or state["pot_workers_total"]:
        text += f"\n（うち作業員 {ui.pelica(state['pot_workers_total'])}）"
    return text


def panel_embed(state: LaborState, note: str = "") -> discord.Embed:
    """作業の画面。上に、直前の結果。つぎに、お金と疲労。"""
    fat = state["fatigue"]
    e = ui.embed("⛏ 地下作業", note or "「働く」を押すと、ペリカが貯まります。", ui.DARK)
    e.add_field(name="💴 手元", value=ui.pelica(state["balance"]), inline=True)
    e.add_field(name="🎒 未受け取り", value=pot_text(state), inline=True)
    capped = state["day_click"] >= lab.MAX_CLICKS_PER_DAY
    e.add_field(
        name="📅 今日の稼ぎ",
        value=f"{ui.pelica(state['day_earned'])}\n（クリック {state['day_click']:,}/{lab.MAX_CLICKS_PER_DAY:,}回）",
        inline=True,
    )
    e.add_field(
        name="⛏ 1回の稼ぎ",
        value=f"**{ui.pelica(lab.OVER_CAP_PAY if capped else lab.click_pay(state['pick'], fat))}**"
        + (f"\n（上限に達しました。{lab.OVER_CAP_PAY}ペリカずつ）" if capped else ""),
        inline=True,
    )
    e.add_field(
        name=f"😮\u200d💨 疲労（{fatigue_label(fat)}）",
        value=f"{lab.bar(fat)}\n{fat:.0f}%　稼ぎ {lab.pay_rate(fat):.0%}",
        inline=True,
    )
    e.add_field(
        name="👷 作業員",
        value=f"{state['workers']}/{state['slots']}人　{state['rate']:,.0f}/時" if state["workers"] else "いません",
        inline=True,
    )
    e.add_field(
        name="⛏ 鉱脈ヒット率",
        value=f"**{state['crit_chance']:.0%}**（当たると稼ぎ×{lab.CRIT_MULT}）",
        inline=True,
    )
    e.add_field(
        name="🧰 工具",
        value="　".join(f"{lab.TOOL_MARK[t]} {lab.TOOL_NAME[t]} Lv{state[t]}/{lab.MAX_LEVEL}" for t in lab.TOOLS),
        inline=False,
    )
    if state.get("ores"):
        e.add_field(name="💎 掘り出した鉱石", value=ore_text(state["ores"]), inline=False)
    if state.get("relics"):
        e.add_field(name="🏺 発掘品（受け取ると持ち物に入る）", value=relic_text(state["relics"]), inline=False)
    if state.get("held_relics"):
        e.add_field(
            name="🎒 持っている発掘品",
            value=relic_text(state["held_relics"])
            + f"\n（同じ品を{relics.MERGE_COUNT}個集めると、★が1つ増えた1個に合体できます）",
            inline=False,
        )
    if state.get("finds"):
        e.add_field(name="📜 最近の掘り出し", value=finds_text(state["finds"]), inline=False)
    e.set_footer(
        text=f"🪨 今日の地層: {lab.terrain_label(state['terrain'])}\n"
        f"疲労は時間で回復します。「受け取る」で、貯まった分をまとめて手元に移します。\n"
        f"総寄付金額：{state.get('donated_all', 0):,}ペリカ"
    )
    return e


def hire_embed(state: LaborState, note: str = "") -> discord.Embed:
    """雇用の画面。作業員の一覧（人数・稼ぎ・次の値段）、寮。"""
    full = state["workers"] >= state["slots"]
    head = [x for x in (note, "⚠ 人数がいっぱいです。寮を強化するか、誰かを解雇しよう。" if full else "") if x]
    e = ui.embed(
        "👷 作業員の雇用",
        "\n".join(head)
        if head
        else "雇った作業員は、放置で働き、1時間ごとに鉱石を掘ります。レア鉱石を引けば、大きく黒字です。",
        ui.DARK,
    )
    e.add_field(name="💴 手元", value=ui.pelica(state["balance"]), inline=True)
    e.add_field(name="🎒 未受け取り", value=pot_text(state), inline=True)
    e.add_field(name="📈 作業員の稼ぎ", value=f"**{state['rate']:,.1f}/時**", inline=True)
    e.add_field(
        name="🏠 人数",
        value=f"**{state['workers']} / {state['slots']}人**（寮 Lv{state['dorm']}/{lab.DORM_MAX}）",
        inline=True,
    )
    e.add_field(
        name="🔁 自動更新",
        value="**オン**\n切れたら、貯まった稼ぎから契約料を払う" if state["renew"] else "**オフ**\n切れたら、退職する",
        inline=True,
    )
    for k in lab.WORKERS:
        if k == "foreman":
            gain = f"レア鉱石・発掘品が出やすい +{lab.FOREMAN_LUCK:.0%}"
        else:
            gain = f"稼ぎ {lab.WORKER_RATE[k]:g}/時 ＋ 鉱石 ＋ まれに発掘品"
        until = f"\n契約が切れる: <t:{int(state['expires'][k])}:R>" if state["expires"].get(k) else ""
        e.add_field(
            name=f"{lab.WORKER_MARK[k]} {lab.WORKER_NAME[k]} ×{state[k]}",
            value=f"{gain}\n契約料 **{lab.contract_fee(k):,}**（{lab.CONTRACT_DAYS}日）{until}",
            inline=True,
        )
    e.set_footer(
        text=f"契約は{lab.CONTRACT_DAYS}日ごと。作業員の稼ぎだけでは赤字で、平均でも契約料に届きません。当たり次第です。貯まるのは最大{lab.STOCK_HOURS:.0f}時間分。"
    )
    return e


MEDALS = ui.MEDALS


def ranking_embed(data: LaborTop, daily: bool) -> discord.Embed:
    """採掘ランキング。稼いだ額（ペリカ）の多い順。"""
    title = "⛏ 採掘ランキング（今日）" if daily else "⛏ 採掘ランキング（通算）"
    if not data["rows"]:
        return ui.embed(title, "まだ、だれも稼いでいません。`/地下 作業` で、1番乗りを狙おう。", ui.DARK)
    lines = [
        f"{MEDALS.get(rank, f'`{rank}`')} <@{uid}>　**{ui.pelica(earned)}**　（{clicks:,}回）"
        for rank, uid, earned, clicks in data["rows"]
    ]
    if data["me"] and data["me"][0] > len(data["rows"]):
        rank, earned, clicks, total = data["me"]
        lines += ["…", f"`{rank}` あなた　**{ui.pelica(earned)}**　（{clicks:,}回）"]
    e = ui.embed(title, "\n".join(lines), ui.DARK)
    e.set_footer(text="稼いだ額 = 働いて貯めたペリカ（作業員の稼ぎも含む。通算は、今日までの合計）")
    return e

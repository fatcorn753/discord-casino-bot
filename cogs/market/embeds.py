"""`/証券` の埋め込み・表示文の組み立て。"""

import math
from typing import Any, Optional, Sequence

import discord

from cogs.market.bond_embeds import describe_done, due_text
from cogs.shared import ui
from domain import bonds, market, market_events
from domain import options as opt
from domain.options import name_of

TICK_MINUTES = 1
HOLDER_ROWS = 10  # チャートに載せる保有者の数
LIVE_MINUTES = 60  # 出したチャートを自動で更新する時間（最後に使ってから）
CORRELATION_MINUTES = 24 * 60  # 銘柄どうしの連動を調べる、直近の長さ（分）
EVENT_TEXT = {1: "📈 急騰！", -1: "📉 急落！"}


def tax_line(res: Any) -> str:
    """利益にかかった税（キャピタルゲイン税）の一行。税がなければ空。"""
    if not getattr(res, "tax", 0):
        return ""
    return f"🕴️ 帝愛の税（利益の累進課税）: **−{res.tax:,}ペリカ**（預金者の利回りになります）\n"


def margin_position_text(side: str, units: int, entry_value: float, margin: int, price: float) -> str:
    """信用取引の建玉の状態（建値・いまの価格・損益・担保）。強制決済は口座ぜんたいの維持率で決まる。"""
    avg = entry_value * market.UNIT / units
    pnl = market.margin_pnl(side, units, entry_value, price)
    pct = (avg - price) / avg * 100 if side == "short" else (price - avg) / avg * 100
    hint = "下がると利益" if side == "short" else "上がると利益"
    return (
        f"建てている: **{market.lots(units)}**\n建値 {market.fmt(avg)} → いま {market.fmt(price)}　{'🔺' if pct >= 0 else '🔻'}**{pct:+.1f}%**（{hint}）\n"
        f"含み損益: **{pnl:+,}ペリカ**　担保: {ui.pelica(margin)}"
    )


def margin_ratio_text(status: dict) -> str:
    """口座の維持率の表示（🟢 150%以上 / 🟡 強制決済ラインの手前 / 🔴 強制決済の対象）。建玉がなければ空文字。"""
    ratio = status["ratio"]
    if ratio is None:
        return ""
    mark = "🟢" if ratio >= 1.5 else "🟡" if ratio >= market.OPEN_RATIO else "🔴"
    return (
        f"{mark} **口座の維持率 {ratio:.0%}**　有効証拠金 {ui.pelica(int(status['equity']))} ÷ 必要証拠金 {ui.pelica(math.ceil(status['required']))}\n"
        f"（{market.LOSSCUT_RATIO:.0%} を下回ると、損の大きい建玉から強制決済。現物・債券は担保に数えます）"
    )


def spot_position_text(units: int, cost: int, price: float) -> str:
    """持ち分の状態（保有・取得価格・いまの価格と％・評価損益）。"""
    pct = market.pnl_percent(price, units, cost)
    value, _fee = market.sell_quote(units, price)
    diff = value - cost
    return (
        f"保有: **{market.lots(units)}**\n取得 {market.fmt(market.avg_price(units, cost))} → いま {market.fmt(price)}　"
        f"{'🔺' if pct >= 0 else '🔻'}**{pct:+.1f}%**\n評価損益: **{diff:+,}ペリカ**（売ったときの手取り {ui.pelica(value)}）"
    )


def describe_option_done(row: dict) -> str:
    label = name_of(row)
    icon = "⚖️" if row["kind"] == opt.PAIR else opt.KINDS[row["kind"]][1]
    target = (
        f"{market.BY_KEY[row['symbol']].name} vs {market.BY_KEY[row['symbol2']].name}"
        if row["kind"] == opt.PAIR
        else market.BY_KEY[row["symbol"]].name
    )
    if row["side"] == "short":
        net = row["premium"] + row["paid"] - row["margin"]
        result = {
            "sold": "買い戻し",
            "expired": "🎯 **満期・無事**（プレミアムが利益）",
            "exercised": "💥 **満期・損失**",
        }[row["status"]]
        return f"{icon} {label} {target} プレミアム {ui.pelica(row['premium'])} → {result}（損益 {net:+,}）"
    head = f"{icon} {label} {target} 掛け金 {ui.pelica(row['premium'])}"
    if row["status"] == "sold":
        return f"{head} → 売却 {ui.pelica(row['paid'])}（{row['paid'] - row['premium']:+,}）"
    if row["status"] == "exercised":
        return f"{head} → 🎯 **当たり** {ui.pelica(row['paid'])}（{row['paid'] - row['premium']:+,}）"
    return f"{head} → 外れ（満期の価格 {market.fmt(row['final'])}）"


def option_line(row: dict, prices: dict) -> str:
    """保有のオプションの1行（種類・銘柄・条件・満期）。"""
    label, sym = name_of(row), market.BY_KEY[row["symbol"]]
    icon = "🎰" if row["side"] == "short" else opt.KINDS[row["kind"]][1]
    if row["kind"] == opt.PAIR:
        other = market.BY_KEY[row["symbol2"]]
        a, b = prices.get(row["symbol"]), prices.get(row["symbol2"])
        rel = (
            f"（いま {sym.name} {(a / row['spot'] - 1) * 100:+.1f}%・{other.name} {(b / row['spot2'] - 1) * 100:+.1f}%）"
            if a and b
            else ""
        )
        return (
            f"`#{row['id']}` {icon} {label} {sym.name} vs {other.name}　満期 {due_text(row['expiry'])}\n"
            f"　{sym.name}が{other.name}を上回ると当たり{rel}　掛け金 {ui.pelica(row['premium'])}"
        )
    now_price = prices.get(row["symbol"])
    cond = "超える" if row["kind"] == opt.CALL else "下回る"
    if row["side"] == "short":
        return (
            f"`#{row['id']}` {icon} {label} {sym.name}　満期 {due_text(row['expiry'])}\n"
            f"　{market.fmt(row['strike'])} を{cond}と**損失**（いま {market.fmt(now_price) if now_price else '-'}）　"
            f"もらったプレミアム {ui.pelica(row['premium'])}・証拠金 {ui.pelica(row['margin'])}"
        )
    return (
        f"`#{row['id']}` {icon} {label} {sym.name}　満期 {due_text(row['expiry'])}\n"
        f"　{market.fmt(row['strike'])} を{cond}と当たり（いま {market.fmt(now_price) if now_price else '-'}）　掛け金 {ui.pelica(row['premium'])}"
    )


def correlation_lines(symbol: str, hist_by_symbol: dict[str, list]) -> str:
    """その銘柄と、ほかの銘柄の連動（直近24時間の1分ごとの値動きの相関）。"""
    hist = hist_by_symbol[symbol]
    lines = []
    for other in market.SYMBOLS:
        if other.key != symbol:
            value = market.correlation_from_history(hist, hist_by_symbol[other.key])
            lines.append(f"{other.emoji} {other.name}: {market.describe_correlation(value)}")
    return "\n".join(lines)


def pin_lines(pin: int) -> str:
    """ストップ高・ストップ安に張り付き中なら、その表示と、約定しない売買。"""
    if not pin:
        return ""
    locked = "・".join(market.PIN_ACTION_TEXT[a] for a in market.PIN_LOCKS[pin])
    return f"\n**{market.PIN_MARK[pin]}に張り付き中**　約定しない売買: {locked}"


def correlation_table(hist_by_symbol: dict[str, list]) -> str:
    """4銘柄どうしの連動を、行列の表にする（直近24時間。まだデータが少ないところは —）。"""
    head = " " * 8 + " ".join(f"{s.emoji:^5}" for s in market.SYMBOLS)
    rows = [head]
    for a in market.SYMBOLS:
        cells = []
        for b in market.SYMBOLS:
            value = (
                1.0 if a.key == b.key else market.correlation_from_history(hist_by_symbol[a.key], hist_by_symbol[b.key])
            )
            cells.append(f"{value:+.2f}" if value is not None else "  — ")
        rows.append(f"{a.emoji} {a.key:<5} " + " ".join(f"{c:^5}" for c in cells))
    return "```\n" + "\n".join(rows) + "\n```"


def event_lines(events_rows: list, now: float) -> str:
    """市場イベント（ボラティリティ予告）。なければ空文字。"""
    ev = market_events.current(events_rows, now)
    return market_events.event_text(ev, now) if ev else ""


def earnings_lines(ev: Optional[dict]) -> str:
    """今週の決算発表の予告（月曜9:00から、発表まで）。なければ空文字。"""
    if ev is None:
        return ""
    sym = market.BY_KEY[ev["symbol"]]
    return f"📢 {sym.emoji} **{sym.name}** の決算発表: <t:{int(ev['release_at'])}:F>（<t:{int(ev['release_at'])}:R>）\n好決算なら急騰、悪決算なら急落。サプライズなら、値幅制限を突き抜ける。"


def alert_lines(price_histories: dict[str, list[float]]) -> str:
    """相関崩壊の警報（連動する2銘柄の値動きの差が開いたもの）。なければ空文字。"""
    return "\n".join(f"⚠️ {market_events.describe_alert(a)}" for a in market_events.spread_alerts(price_histories)[:3])


def cut_lines(prices: dict[str, float], rows: list) -> str:
    """ロスカット水準の密集（サーバー内の信用建玉が、ほぼ同じ価格帯で強制決済されるところ）。なければ空文字。"""
    lines = []
    for sym in market.SYMBOLS:
        for side, label, sign in (("long", "信用買い", -1), ("short", "空売り", 1)):
            near = [
                (price, notional)
                for s, sd, price, notional in rows
                if s == sym.key and sd == side and abs(price / prices[sym.key] - 1) <= 0.10
            ]
            if not near:
                continue
            total = sum(n for _, n in near)
            nearest = min(near, key=lambda x: abs(x[0] - prices[sym.key]))[0]
            move = (nearest / prices[sym.key] - 1) * 100
            lines.append(
                f"{sym.emoji}{sym.name}: {market.fmt(nearest)}（{move:+.1f}%）の{'下' if sign < 0 else '上'}に、{label} 計 {ui.pelica(int(total))}分が強制決済のライン"
            )
    return "\n".join(lines[:6])


def chart_embed(
    symbol: str,
    period: str,
    hist: list,
    pin: int,
    hist_24h: dict[str, list],
    holders: Sequence = (),
    shorts: Sequence = (),
    title_prefix: str = "",
    live_until: Optional[float] = None,
    ended: bool = False,
    now: float = 0.0,
) -> discord.Embed:
    sym = market.BY_KEY[symbol]
    label, _seconds = market.PERIODS[period]
    prices = [p for _, p, _ in hist]
    if not prices:
        return ui.embed(f"📈 {sym.name}", "まだ価格のデータがありません。", ui.INFO)
    s = market.summary(prices)
    if s is None:
        return ui.embed(f"📈 {sym.name}", "まだ価格のデータがありません。", ui.INFO)
    last_event = next((e for _, _, e in reversed(hist[-5:]) if e), 0)
    color = ui.OK if s["change"] >= 0 else ui.ERROR
    arrow = "▲" if s["change"] >= 0 else "▼"
    body = (
        f"```\n{market.render_chart(prices)}\n```\n"
        f"現在 **{market.pel(s['last'])}**　{arrow} **{s['change']:+.1f}%**（{label}）\n"
        f"高値 {market.fmt(s['high'])}　安値 {market.fmt(s['low'])}"
        + (f"\n**{EVENT_TEXT[last_event]}**" if last_event else "")
        + pin_lines(pin)
    )
    e = ui.embed(f"{sym.emoji} {title_prefix}{sym.name}（{label}）", body, color)
    e.add_field(name="🔗 ほかの銘柄との連動（直近24時間）", value=correlation_lines(symbol, hist_24h), inline=False)
    if holders:
        lines = []
        for uid, units, cost in holders:
            pct = market.pnl_percent(s["last"], units, cost)
            lines.append(
                f"<@{uid}> {market.lots(units)}　取得 {market.fmt(market.avg_price(units, cost))} → {'🔺' if pct >= 0 else '🔻'}**{pct:+.1f}%**"
            )
        e.add_field(name=f"👥 保有者（このサーバー・上位{HOLDER_ROWS}人）", value="\n".join(lines), inline=False)
    if shorts:
        lines = []
        for uid, units, entry_value in shorts:
            avg = entry_value * market.UNIT / units
            pct = (avg - s["last"]) / avg * 100  # 下がると利益
            lines.append(
                f"<@{uid}> {market.lots(units)}　建値 {market.fmt(avg)} → {'🔺' if pct >= 0 else '🔻'}**{pct:+.1f}%**"
            )
        e.add_field(name=f"📉 空売り中（このサーバー・上位{HOLDER_ROWS}人）", value="\n".join(lines), inline=False)
    if ended:
        note = "更新は終了しました。最新は /証券 市況 で"
    elif live_until is not None:
        note = f"{TICK_MINUTES}分ごとに自動で更新（あと{max(1, int((live_until - now) // 60))}分）"
    else:
        note = f"{TICK_MINUTES}分ごとに値が動きます"
    e.set_footer(text=f"{note} ｜ 売買の手数料はありません ｜ ゲーム内の通貨です")
    return e


def summary_embed(
    hist_24h: dict[str, list],
    latest: dict[str, float],
    upcoming_earnings: Optional[dict],
    event_rows: list,
    alert_histories: dict[str, list[float]],
    cut_rows: list,
    now: float,
) -> discord.Embed:
    """全銘柄のいまの価格と、銘柄どうしの連動、市場イベント・警報・ロスカット水準。"""
    lines = []
    for sym in market.SYMBOLS:
        prices = [p for _, p, _ in hist_24h[sym.key]] or [latest[sym.key]]
        s = market.summary(prices)
        if s is None:
            continue
        arrow = "▲" if s["change"] >= 0 else "▼"
        lines.append(
            f"{sym.emoji} **{sym.name}**　**{market.pel(s['last'])}**（1枚）　{arrow} {s['change']:+.1f}%（24時間）　高値 {market.fmt(s['high'])} 安値 {market.fmt(s['low'])}"
        )
    body = "\n".join(lines) + "\n\n**銘柄どうしの連動（直近24時間の1分ごとの値動き）**\n" + correlation_table(hist_24h)
    e = ui.embed("📈 市況（全銘柄）", body + "\n下のメニューで、銘柄と期間のチャートを見られます。", ui.GOLD)
    for name, text in (
        ("📢 決算発表", earnings_lines(upcoming_earnings)),
        ("⚡ 市場イベント", event_lines(event_rows, now)),
        ("🔀 相関崩壊の警報", alert_lines(alert_histories)),
        ("🎯 ロスカットが密集しているところ", cut_lines(latest, cut_rows) if cut_rows else ""),
    ):
        if text:
            e.add_field(name=name, value=text[:1024], inline=False)
    return e


def holdings_embed(
    prices: dict[str, float],
    spots: dict[str, tuple[int, int]],
    margins: dict[tuple[str, str], tuple[int, float, int]],
    options: dict[str, list[dict]],
    bond_data: dict[str, list[dict]],
    option_quotes: dict[int, int],
    bond_quotes: dict[int, int],
    margin_status: Optional[dict],
) -> discord.Embed:
    if (
        not spots
        and not margins
        and not bond_data["active"]
        and not bond_data["done"]
        and not options["active"]
        and not options["done"]
    ):
        return ui.embed("💼 証券", "まだ持っていません。`/証券 注文` で買えます。", ui.INFO)
    parts, total = [], 0
    for sym in market.SYMBOLS:
        if sym.key not in spots and (("long", sym.key) not in margins) and (("short", sym.key) not in margins):
            continue
        price = prices[sym.key]
        lines = [f"{sym.emoji} **{sym.name}**　価格 {market.pel(price)}"]
        if sym.key in spots:
            units, cost = spots[sym.key]
            value, _fee = market.sell_quote(units, price)  # 売ったら受け取れる額
            diff = value - cost
            total += diff
            lines.append(
                f"保有: **{market.lots(units)}**　評価額 **{ui.pelica(value)}**（売ったときの手取り）\n"
                f"買った額 {ui.pelica(cost)}　損益 **{'+' if diff >= 0 else ''}{diff:,}ペリカ**"
            )
        for side, icon in (("long", "📈"), ("short", "📉")):
            if (side, sym.key) in margins:
                units, entry, margin = margins[(side, sym.key)]
                lines.append(
                    f"{icon} **{market.SIDE_NAME[side]}**\n{margin_position_text(side, units, entry, margin, price)}"
                )
                total += market.margin_pnl(side, units, entry, price)
        parts.append("\n".join(lines))
    if options["active"]:
        lines = [
            f"{option_line(r, prices)}\n　"
            + (
                f"いま買い戻すと **{ui.pelica(option_quotes[r['id']])}**"
                if r["side"] == "short"
                else f"いま売ると **{ui.pelica(option_quotes[r['id']])}**"
            )
            for r in options["active"]
        ]
        parts.append(
            "🎫 **オプション**（満期の価格で自動決済。買いの損失は掛け金だけ・🎰裸売りの損失は借金になりうる）\n"
            + "\n".join(lines)
        )
    if bond_data["active"]:
        lines = []
        for r in bond_data["active"]:
            k = bonds.KINDS[r["kind"]]
            lines.append(
                f"`#{r['id']}` {k.emoji} {k.name} 元本 {ui.pelica(r['principal'])} → 満期 {due_text(r['due'])}（受取 {ui.pelica(bonds.maturity_payout(k, r['principal']))}）"
                + (f"　いま売ると {ui.pelica(bond_quotes[r['id']])}" if k.sellable else "　⚠️ 倒産しうる・売れない")
            )
        parts.append("🏦 **債券**\n" + "\n".join(lines))
    ratio_text = margin_ratio_text(margin_status) if margins and margin_status is not None else ""
    if ratio_text:
        parts.append(ratio_text)
    e = ui.embed("💼 証券", "\n\n".join(parts) or "いま持っている銘柄はありません。", ui.OK if total >= 0 else ui.ERROR)
    if options["done"]:
        e.add_field(
            name="オプションの最近の決着",
            value="\n".join(describe_option_done(r) for r in options["done"]),
            inline=False,
        )
    if bond_data["done"]:
        e.add_field(name="債券の最近の決着", value="\n".join(describe_done(r) for r in bond_data["done"]), inline=False)
    e.set_footer(
        text="下のボタンで、その持ち物を全部決済します（一部だけなら /証券 決済）。ランキングの総資産には債券の元本も含まれます"
    )
    return e


def earnings_announce_embed(row: dict) -> discord.Embed:
    """今週の決算発表の予告埋め込み。"""
    sym = market.BY_KEY[row["symbol"]]
    return ui.embed(
        "📢 今週の決算発表",
        f"{sym.emoji} **{sym.name}** の決算発表は、<t:{int(row['release_at'])}:F>（<t:{int(row['release_at'])}:R>）。\n"
        "結果は、発表の瞬間まで、だれにも分かりません。好決算なら急騰、悪決算なら急落、サプライズなら値幅制限を突き抜けます。",
        ui.INFO,
    )


def earnings_result_embed(row: dict, pin: int) -> discord.Embed:
    """決算発表の結果埋め込み。"""
    sym = market.BY_KEY[row["symbol"]]
    good = row["direction"] > 0
    return ui.embed(
        f"📊 {sym.name} 決算発表: {'好決算！' if good else '悪決算…'}",
        f"{sym.emoji} **{sym.name}** の決算は、**{'市場予想を大きく上回りました' if good else '市場予想を大きく下回りました'}**"
        f"（理論価格 {row['direction'] * row['size']:+.0%}）。"
        + (f"\n**{market.PIN_MARK[pin]}に張り付いています！**" if pin else ""),
        ui.OK if good else ui.ERROR,
    )


def option_target_text(
    kind: str,
    sym,
    other,
    strike_pct: float,
    price: float,
    strike: float,
    units: int,
    cost: int,
) -> str:
    """オプション買いの勝利条件と想定リターン。"""
    if kind == opt.PAIR:
        lines = [
            f"勝利条件: **{opt.OPTION_HOURS}時間後**に、{sym.name}の上昇率が{other.name}の上昇率を上回ること（基準はなし）"
        ]
        for a_move, b_move in ((0.05, 0.0), (0.10, 0.0), (0.0, -0.05)):
            pay = min(opt.pair_intrinsic(units, 1.0, 1.0, 1 + a_move, 1 + b_move), cost * opt.PAYOUT_CAP_MULT)
            lines.append(
                f"想定リターン: {sym.name} {a_move * 100:+.0f}%・{other.name} {b_move * 100:+.0f}% で **約 {pay:,}ペリカ（{pay / cost:.1f}倍）**"
            )
        return "\n".join(lines)
    sign = 1 if kind == opt.CALL else -1
    lines = [
        f"勝利条件: **{opt.OPTION_HOURS}時間後**の価格が **{market.fmt(strike)}**（現在 {market.fmt(price)} の "
        f"{sign * strike_pct * 100:+.1f}%）を{'超える' if kind == opt.CALL else '下回る'}こと"
    ]
    for move in (0.10, 0.20):
        final = price * (1 + sign * move)
        pay = min(opt.intrinsic(kind, units, final, strike), cost * opt.PAYOUT_CAP_MULT)
        lines.append(f"想定リターン: 価格が {sign * move * 100:+.0f}% で **約 {pay:,}ペリカ（{pay / cost:.1f}倍）**")
    return "\n".join(lines)


def bond_buy_embed(kind_key: str, amount: int, now: float) -> discord.Embed:
    """債券購入の完了埋め込み。"""
    k = bonds.KINDS[kind_key]
    lines = [
        f"元本: **{ui.pelica(amount)}**　満期: {k.hours // 24}日後（{due_text(now + k.hours * 3600)}）",
        f"満期に受け取る額: **{ui.pelica(bonds.maturity_payout(k, amount))}**（利息 +{k.rate}%）",
    ]
    if k.default_pct:
        lines.append(
            f"⚠️ 満期までに **{k.default_pct}%の確率で倒産**します（満期を待たず、**途中のどの瞬間にも**起こります）。倒産すると元本は戻りません。売ることもできません。"
        )
    else:
        sold = bonds.sale_value(k, amount, now, now + k.hours * 3600)
        lines.append(
            f"元本保証です。満期の前でも、残り時間で割り引いた値段で売れます（`/証券 決済`。いま売ると {ui.pelica(sold)}、待つほど満期の額に近づきます）。"
        )
    return ui.ok_embed(f"{k.emoji} {k.name}を買いました", "\n".join(lines))

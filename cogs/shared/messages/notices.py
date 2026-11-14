"""通知イベント（Notice）の Discord UI 向けフォーマッタ定義。"""

from typing import Any, Callable

NoticeFormatter = str | Callable[[dict[str, Any]], str]


def _p2p_won_failed(p: dict[str, Any]) -> str:
    from cogs.shared.messages import format_failure

    return (
        f"⚠️ **{p['label']}** は落札しましたが、{format_failure(p['reason'])}"
        f"そのため取引は成立せず、{p['refund']:,}ペリカ は返りました。"
    )


NOTICE_FORMATTERS: dict[str, NoticeFormatter] = {
    "grace_entered": lambda p: (
        f"⚠️ 借金が {p['grace_debt']:,}ペリカ に達し、**破産猶予**に入りました。"
        f"<t:{int(p['until'])}:R> までに借金を {p['grace_debt']:,}ペリカ 未満へ戻せなければ、強制破産です。"
    ),
    "grace_exited": "😮‍💨 借金が減り、破産猶予から抜けました。",
    "cds_self_covered": lambda p: f"🛡️ 自分への CDS が、借金 {p['pay']:,}ペリカ を肩代わりしました（帝愛が払います）。",
    "cds_default_payout": lambda p: (
        f"💀 CDS が的中しました。<@{p['target_id']}> の強制破産で、{p['pay']:,}ペリカ を受け取りました。"
    ),
    "debt_interest": lambda p: f"🕴️ 帝愛の利息が付きました（+{p['add']:,}ペリカ。日利{p['rate']:.0%}）。",
    "seize_deposit": lambda p: f"💥 帝愛への預金が差し押さえられました（{p['taken']:,}ペリカ）。",
    "seize_margin": lambda p: (
        f"💥 {p['symbol_name']}の{p['side_label']}が強制決済されました（戻り {p['money']:,}ペリカ）。"
    ),
    "seize_spot": lambda p: f"💥 保有していた{p['symbol_name']}が差し押さえられました（{p['money']:,}ペリカ）。",
    "seize_option": lambda p: f"💥 オプションが差し押さえられました（{p['got']:,}ペリカ）。",
    "seize_bond": lambda p: f"💥 通常債が強制売却されました（{p['refund']:,}ペリカ）。",
    "seed_loan": lambda p: (
        f"🕴️ 帝愛ファイナンスから種銭 {p['amount']:,}ペリカ が貸し付けられました。「これで取り返してこい…！」"
    ),
    "margin_losscut": lambda p: (
        f"💥 口座の維持率が {p['losscut_ratio']:.0%} を下回り、"
        f"{p['symbol_name']}の{p['side_label']}が強制決済されました（損益 {p['profit']:+,}ペリカ）。"
    ),
    "p2p_sold": lambda p: (
        f"🤝 **{p['label']}** が **{p['price']:,}ペリカ** で売れました"
        f"（手数料 {p['fee']:,}ペリカ・受け取り {p['net']:,}ペリカ）。"
    ),
    "p2p_outbid": lambda p: (
        f"📉 **{p['label']}** の入札を、**{p['amount']:,}ペリカ** で抜かれました"
        f"（預かっていた {p['refund']:,}ペリカ は返りました）。"
    ),
    "p2p_seller_pulled_back": lambda p: (
        f"⚠️ **{p['label']}** の出品者が取引できなくなったため、競りは中止され、"
        f"入札 {p['refund']:,}ペリカ は返りました。"
    ),
    "p2p_won": lambda p: f"🎉 **{p['label']}** を **{p['bid']:,}ペリカ** で落札しました。",
    "p2p_won_failed": _p2p_won_failed,
    "p2p_buyer_cannot_receive": lambda p: (
        f"**{p['label']}** は、買い手が受け取れず、取引が成立しませんでした（品は戻りました）。"
    ),
    "p2p_unsold": lambda p: f"**{p['label']}** は、売れ残りました（品は戻りました）。",
}

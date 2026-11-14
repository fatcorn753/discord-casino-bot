"""共通・経済・ショップ・帝愛・貸付・P2P 領域の失敗メッセージ定義。"""

from typing import Any, Callable

from infra import catalogs

FailureFormatter = str | Callable[[dict[str, Any]], str]


def _grace_lock(_p: dict[str, Any]) -> str:
    return catalogs.economy().wallet.grace_lock_text


def _shop_out_of_stock(p: dict[str, Any]) -> str:
    when = f"（次の補充: <t:{int(p['next_at'])}:R>）" if p.get("next_at") else ""
    return f"在庫が足りません（いまの在庫: {p['stock']}個）{when}。在庫は1人ずつ別々で、時間がたつと補充されます。"


def _loan_borrower_blocked(p: dict[str, Any]) -> str:
    from cogs.shared.messages import format_failure

    return "借りる人が、いまは借りられない状態です。" + format_failure(p["inner"])


ECONOMY_FAILURES: dict[str, FailureFormatter] = {
    # 共通・所持金・借金
    "already_owned": "すでに持っています。",
    "invalid_spec": "指定が正しくありません。",
    "amount_must_be_positive": "金額は1以上にしてください。",
    "insufficient_balance": lambda p: f"残高が足りません（いまの残高: {p['balance']:,}ペリカ）。",
    "insufficient_balance_need": lambda p: (
        f"ペリカが足りません（{p['need']:,}ペリカ必要・残高 {p['balance']:,}ペリカ）。"
    ),
    "debt_blocks_buy": lambda p: f"借金が {p['debt']:,}ペリカ 残っているので、買えません。",
    "debt_blocks_sell": lambda p: f"借金が {p['debt']:,}ペリカ 残っているので、売れません。",
    "debt_blocks_shopping": lambda p: f"借金が {p['debt']:,}ペリカ 残っているので、買い物はできません。",
    "debt_blocks_transfer": lambda p: f"借金が {p['debt']:,}ペリカ 残っているので、送金はできません。",
    "debt_blocks_quiz": lambda p: f"借金が {p['debt']:,}ペリカ 残っているので、出題できません。",
    "debt_blocks_raffle": lambda p: f"借金が {p['debt']:,}ペリカ 残っているので、参加できません。",
    "grace_lock": _grace_lock,
    "user_not_registered": "その人は、まだこのサーバーで遊んでいません。",
    # 送金
    "transfer_self": "自分には送金できません。",
    "transfer_min_games": lambda p: (
        f"送金は、{p['min_games']}回以上ゲームで遊んでからできます（複数アカウントでの稼ぎ対策です）。"
    ),
    "transfer_daily_limit": lambda p: (
        f"1日の送金は合計 {p['daily_limit']:,} ペリカまでです（今日は {p['sent_today']:,} ペリカ送金済み）。"
    ),
    "transfer_insufficient_balance": lambda p: (
        f"残高が足りません（送金額 {p['amount']:,} + 手数料 {p['fee']:,} が必要です）。"
    ),
    # ショップ・競売・裏情報
    "shop_invalid_qty_or_price": "個数と値段は1以上にしてください。",
    "shop_out_of_stock": _shop_out_of_stock,
    "auction_item_unavailable": "その品は競売にありません。",
    "auction_katagi_only": "競売で買えるのは、借金も延滞もないカタギだけです。",
    "auction_insufficient_qty": "その数は残っていません。",
    "auction_horse_sold": "その馬は、もう売れてしまいました。",
    "insider_no_event": "いま、売れる情報はありません（次の決算が決まるまで、お待ちください）。",
    "insider_already_bought": "この決算の裏情報メモは、もう買っています。",
    "insider_sold_out": "この決算の裏情報メモは、売り切れです（サーバーで5枚まで）。",
    # 帝愛銀行・借金・CDS・逃亡・破産
    "bank_katagi_only": "帝愛に預けられるのは、🟢 カタギだけです（借金か延滞がある人、破産者は預けられません）。",
    "bank_min_deposit": lambda p: f"預金は {p['min_deposit']:,}ペリカ 以上からです。",
    "bank_no_deposit": "預金がありません。",
    "bank_withdraw_range": lambda p: f"引き出せるのは 1〜{p['max_amount']:,}ペリカ です（いまの預金）。",
    "bank_frozen": "🚨 帝愛は支払停止中です（預金封鎖）。手元の現金が、引当金のロックで尽きています。",
    "bank_available_limit": lambda p: (
        f"いま引き出せるのは {p['available']:,}ペリカ までです（帝愛の利用可能現金）。残りは、現金が戻ってからです。"
    ),
    "cds_min_stake": lambda p: f"掛け金は {p['min_stake']:,}ペリカ 以上からです。",
    "cds_target_in_grace": "その人は、すでに破産猶予に入っているので、掛けられません。",
    "cds_target_bankrupt": "その人は、破産者なので、掛けられません。",
    "cds_self_cap": lambda p: (
        f"自分への CDS は、配当の見込みの合計が {p['cap']:,}ペリカ までです"
        f"（いま {p['have']:,}ペリカ分。あと掛けられるのは {p['room']:,}ペリカ）。"
    ),
    "seed_bankrupt_blocked": "破産者は、完済するまで、帝愛から借りられません。",
    "seed_grace_blocked": "破産猶予の間は、新しく借りられません。",
    "seed_not_needed": lambda p: (
        f"手元が {p['seed']:,}ペリカ 以上あるので、借りられません（借りられるのは、枠の {p['seed']:,}ペリカ に足りない分まで）。"
    ),
    "credit_bankrupt_blocked": "破産者は、帝愛の信用を使えません。",
    "credit_grace_blocked": "破産猶予の間は、帝愛の信用を使えません。",
    "credit_limit_exceeded": lambda p: (
        f"帝愛の信用で借りられるのは、1回 {p['seed']:,}ペリカ までです（足りない額: {p['need']:,}ペリカ）。"
    ),
    "repay_no_debt": "帝愛への借金はありません。",
    "repay_no_cash": lambda p: f"手元にペリカがありません（いまの残高: {p['balance']:,}ペリカ）。",
    "repay_exceeds_debt": lambda p: f"返す額が借金を超えています（借金: {p['debt']:,}ペリカ）。",
    "escape_bankrupt_blocked": "破産者は、逃亡できません（すでに全財産を差し押さえられています）。",
    "escape_min_debt": lambda p: f"逃亡を試せるのは、帝愛の借金が {p['min_debt']:,}ペリカ 以上のときだけです。",
    "escape_max_tries": lambda p: f"逃亡のチャンスは、通算 {p['max_tries']} 回までです。もう使い切りました。",
    "escape_cooldown": lambda p: f"警戒が厳しく、次に試せるのは <t:{int(p['ready'])}:R> です。",
    "bankrupt_already": "すでに破産者です。",
    "bankrupt_no_debt": "借金も貸付の借り入れもないので、破産できません。",
    # 貸付
    "loan_blacklisted": "破産のブラックリストの間は、貸付を借りられません（完済して7日たつと解除されます）。",
    "loan_has_overdue": "延滞中の貸付があるので、借りられません。先に返してください。",
    "loan_min_principal": lambda p: f"金額は {p['min_principal']:,}ペリカ 以上にしてください。",
    "loan_min_rate": "日利は 0 以上にしてください。",
    "loan_term_range": lambda p: f"期間は {p['min_days']}〜{p['max_days']}日 にしてください。",
    "loan_max_borrower_open": lambda p: (
        f"出せる募集は、同時に {p['max_open']} 件までです。先の募集を取り下げてください。"
    ),
    "loan_request_ended": "この募集はもう終わっています（ほかの人が貸したか、取り下げられました）。",
    "loan_self_lend": "自分の募集には、貸せません。",
    "loan_request_expired": "募集の期限が過ぎました。",
    "loan_borrower_blocked": _loan_borrower_blocked,
    "loan_max_lender_active": lambda p: f"同時に貸せるのは {p['max_active']} 件までです。",
    "loan_self_offer": "自分には貸せません。",
    "loan_max_lender_open": lambda p: f"承諾待ちの提案は {p['max_open']} 件までです。",
    "loan_offer_ended": "この提案はもう終わっています。",
    "loan_accept_borrower_only": "この提案を承諾できるのは、借りる本人だけです。",
    "loan_offer_expired": "承諾の期限が過ぎました。",
    "loan_lender_insufficient_balance": "貸す側の残高が足りなくなったので、成立しませんでした。",
    "loan_decline_borrower_only": "断れるのは、借りる本人だけです。",
    "loan_cancel_requester_only": "取り下げられるのは、募集を出した人だけです。",
    "loan_cancel_lender_only": "取り下げられるのは、貸す側だけです。",
    "loan_repay_not_found": "返す貸付が見つかりません。",
    "loan_repay_borrower_only": "返せるのは、借りた本人だけです。",
    "loan_repay_exceeds_remaining": lambda p: f"返す額が残りを超えています（残り {p['remaining']:,}ペリカ）。",
    # P2P
    "p2p_horse_limit": lambda p: f"持てる馬は {p['max_horses']} 頭までです（出品中の馬も数えます）。",
    "p2p_self_buy": "自分の出品は、買えません。",
    "p2p_katagi_only": "取引で買えるのは、借金も延滞もないカタギだけです。",
    "p2p_pair_limit": lambda p: (
        f"同じ相手との取引は、24時間で合計 {p['limit']:,}ペリカ までです"
        f"（いま {p['used']:,}ペリカ・今回 {p['amount']:,}ペリカ）。"
    ),
    "p2p_pair_limit_short": "同じ相手との取引の上限を超えます。",
    "p2p_invalid_spec": "出品の指定が正しくありません。",
    "p2p_invalid_auction_hours": lambda p: f"競りの長さは、{'・'.join(map(str, p['hours']))} 時間から選んでください。",
    "p2p_max_active": lambda p: f"同時に出せるのは {p['max_active']} 件までです。",
    "p2p_item_not_tradable": "その品は、取引できません（称号・コレクション・限定品・帝愛市の品は不可）。",
    "p2p_insufficient_qty": "その数は、持っていません。",
    "p2p_rental_not_tradable": "レンタル馬は出品できません。",
    "p2p_horse_in_race": "出走予定のレースが終わるまで、出品できません。",
    "p2p_price_range": lambda p: f"値段は {p['lo']:,}〜{p['hi']:,}ペリカ にしてください（定価の幅に収めます）。",
    "p2p_not_found": "その出品は、もうありません。",
    "p2p_is_auction": "これは競りです。入札してください。",
    "p2p_is_fixed": "これは定価の出品です。「買う」を押してください。",
    "p2p_already_highest_bidder": "いま、あなたが最高入札者です。",
    "p2p_bid_min": lambda p: f"入札は {p['need']:,}ペリカ 以上にしてください。",
    "p2p_cancel_not_found": "取り消せる出品が見つかりません。",
    "p2p_cancel_has_bid": "入札が入っているので、取り消せません。",
}

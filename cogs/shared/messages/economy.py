"""共通・経済・ショップ・帝愛・貸付・P2P 領域の失敗メッセージ定義。"""

from typing import Any

from cogs.shared.messages.base import MessageEntry
from cogs.shared.messages.base import MessageFormatter as BaseMessageFormatter

FailureFormatter = MessageEntry


def _shop_out_of_stock(p: dict[str, Any]) -> str:
    when = f"（次の補充: <t:{int(p['next_at'])}:R>）" if p.get("next_at") else ""
    return f"在庫が足りません（いまの在庫: {p['stock']}個）{when}。在庫は1人ずつ別々で、時間がたつと補充されます。"


ECONOMY_FAILURES: dict[str, FailureFormatter] = {
    # 共通・所持金・借金
    "already_owned": "すでに持っています。",
    "invalid_spec": "指定が正しくありません。",
    "amount_must_be_positive": "金額は1以上にしてください。",
    "insufficient_balance": lambda p: f"残高が足りません（いまの残高: {p['balance']:,}ペリカ）。",
    # 会社
    "company_name_empty": "社名を入れてください。",
    "company_name_too_long": lambda p: f"社名は {p['max']} 文字までです。",
    "company_name_invalid_chars": "社名に使えない文字（@ < > ` * _ ~ | # : / など）か、リンクが入っています。",
    "company_kind_unknown": "事業所の種類を、選び直してください。",
    "company_exists": "会社は、1人1社までです。",
    "company_none": "会社を持っていません。`/会社 設立` で作れます。",
    "company_katagi_only": "帝愛への借金や、延滞中の貸付がある間は、会社の設立と出資はできません。",
    "company_found_short": lambda p: (
        f"設立には {p['cost']:,}ペリカ（設立費と建設費）が必要です（いまの残高: {p['balance']:,}ペリカ）。"
    ),
    "company_amount_invalid": "金額は、1ペリカ以上で入れてください。",
    "company_no_funds": "会社資金がありません。",
    "company_funds_short": lambda p: f"会社資金が足りません（{p['need']:,}ペリカ 必要・いま {p['funds']:,}ペリカ）。",
    "company_nothing_to_ship": "出荷できる在庫が、まだありません。",
    "company_no_budget": "帝愛物産の買取予算（買取プールと、今日の買取枠）が尽きていて、いまは買い取ってもらえません。日付が変わると、買取枠が戻ります。",
    "company_max_level": lambda p: f"事業所は、もう最大（Lv{p['max']}）です。",
    "company_same_kind": "いまと同じ種類です。",
    "company_funds_locked": lambda p: (
        f"会社資金は、出資者から預かっている {p['locked']:,}ペリカ を超える分しか引き出せません"
        "（出資者のお金は、増築や維持費には使えます）。"
    ),
    "company_withdraw_over": lambda p: (
        f"引き出せるのは {p['free']:,}ペリカ までです（出資者から預かっている {p['locked']:,}ペリカ は引き出せません）。"
    ),
    "stake_self": "自分の会社には、`/会社 状況` の「出資」で会社資金を入れてください（持分は付きません）。",
    "stake_min": lambda p: f"出資は、1回 {p['min']:,}ペリカ からです。",
    "stake_no_company": "その人は、会社を持っていません。",
    "stake_cap": lambda p: (
        f"この会社が受けられる出資は、あと {p['room']:,}ペリカ までです（上限 {p['cap']:,}ペリカ・事業所の建設費の累計の2倍）。"
    ),
    "dividend_rate_range": lambda p: f"配当率は 0〜{p['max']:.0%} で決めてください。",
    "insufficient_balance_need": lambda p: (
        f"ペリカが足りません（{p['need']:,}ペリカ必要・残高 {p['balance']:,}ペリカ）。"
    ),
    "kyosai_bad_units": lambda p: "口数は1以上を指定してください。",
    "kyosai_bad_price": lambda p: "値段は0以上を指定してください。",
    "kyosai_bad_buyer": lambda p: "自分や帝愛には譲れません。",
    "kyosai_no_auction": lambda p: "いま競りに出ている口はありません。",
    "kyosai_bid_low": lambda p: f"入札は {p['need']:,}ペリカ 以上にしてください。",
    "kyosai_units_short": lambda p: f"譲れる口は {p['have']}口 です（ほかの申し出に出している口は数えません）。",
    "kyosai_gate_locked": lambda p: (
        "いまは口の売買が止まっています（帝愛銀行の準備率が低いか、破産猶予中の人がいます）。"
    ),
    "kyosai_offer_not_found": lambda p: (
        "その申し出は見つかりません（期限切れか、取り消されたか、自分あてではありません）。"
    ),
    "kyosai_bad_premium": lambda p: f"上乗せ金利は 0%/日 より大きく、{p['max_pct']:g}%/日 以下で指定してください。",
    "kyosai_no_units": lambda p: "共済の口を持っている人だけができます。",
    "kyosai_motion_units": lambda p: (
        f"動議を出せるのは、共済の口を {p['need']:,}口 以上持っている人だけです（いま {p['have']:,}口）。"
    ),
    "kyosai_motion_open": lambda p: f"投票中の動議は、1人 {p['max']}件 までです。締め切られてから出してください。",
    "kyosai_motion_cooldown": lambda p: f"次の動議は <t:{int(p['until'])}:R> から出せます。",
    "kyosai_motion_ambiguous": lambda p: f"投票中の動議が複数あります。番号（{p['ids']}）を指定してください。",
    "kyosai_no_motion": lambda p: "その動議は投票中ではありません（締め切られたか、番号が違います）。",
    "poll_bad_title": lambda p: f"議題は 1〜{p['max']}文字 で書いてください。",
    "poll_bad_duration": lambda p: f"期限は 1分〜{p['max_minutes'] // 1440}日 で指定してください。",
    "poll_too_many": lambda p: f"受付中の投票は、1人 {p['max']}個 までです。締め切られてから作ってください。",
    "poll_closed": lambda p: "この投票はもう締め切られています。",
    "poll_not_creator": lambda p: "票数を書き換えられるのは、この投票を作った人だけです。",
    "poll_bad_count": lambda p: f"票数は 0〜{p['max']:,} で指定してください。",
    # 配信
    "stream_bad_title": lambda p: f"配信のタイトルは 1〜{p['max']}文字 で書いてください。",
    "stream_bad_url": lambda p: "URL は https:// か http:// で始めてください。",
    "stream_already_live": lambda p: "配信中のパネルがあります。先に「⏹️ 配信を終える」で閉じてください。",
    "stream_ended": lambda p: "この配信は終わりました。",
    "stream_not_streamer": lambda p: "これは配信者だけが使えます（または、配信が終わっています）。",
    "stream_self_tip": lambda p: "自分の配信には、スパチャを送れません。",
    "stream_tip_min": lambda p: f"スパチャは {p['min']:,}ペリカ から送れます。",
    "stream_bad_label": lambda p: f"勝負の名前は {p['max']}文字 までにしてください。",
    "stream_round_active": lambda p: "決まっていない勝負があります。勝敗を決めるか、無効にしてから始めてください。",
    "stream_no_open_round": lambda p: "いまは賭けを受け付けていません。",
    "stream_no_round": lambda p: "決まっていない勝負がありません。",
    "stream_bad_side": lambda p: "賭ける側が正しくありません。",
    "stream_self_bet": lambda p: "配信者は、自分の勝負に賭けられません。",
    "stream_other_side": lambda p: "この勝負には、もう反対の側に賭けています（側は変えられません）。",
    "stream_bet_min": lambda p: f"賭けは {p['min']:,}ペリカ からです。",
    "stream_bet_max": lambda p: (
        f"1回の勝負に賭けられるのは、1人 {p['max']:,}ペリカ までです（いま {p['already']:,}ペリカ 賭けています）。"
    ),
    "debt_blocks": lambda p: f"借金が {p['debt']:,}ペリカ 残っているので、{p['verb']}。",
    "grace_lock": "破産猶予中は、取引（出品・買う・入札）ができません（破産の前に、資産を移して、借金を踏み倒すのを防ぐためです）。",
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
    "bank_katagi_only": "帝愛信託に入れられるのは、🟢 カタギだけです（借金か延滞がある人、破産者は入れられません）。",
    "bank_min_deposit": lambda p: f"帝愛信託は {p['min_deposit']:,}ペリカ 以上からです。",
    "bank_no_deposit": "帝愛信託の残高がありません。",
    "bank_withdraw_range": lambda p: f"引き出せるのは 1〜{p['max_amount']:,}ペリカ です（いまの信託の残高）。",
    "bank_frozen": "🚨 帝愛は支払停止中です（信託の解約停止）。手元の現金が、引当金のロックで尽きています。",
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
    "seed_blacklisted": "破産・海外逃亡のブラックリスト期間中（完済から7日）は、種銭を借りられません。",
    "seed_grace_blocked": "破産猶予の間は、新しく借りられません。",
    "seed_not_needed": lambda p: (
        f"手元が {p['seed']:,}ペリカ 以上あるので、借りられません（借りられるのは、枠の {p['seed']:,}ペリカ に足りない分まで）。"
    ),
    "credit_bankrupt_blocked": "破産者は、帝愛の信用を使えません。",
    "credit_blacklisted": "破産・海外逃亡のブラックリスト期間中（完済から7日）は、帝愛の信用を使えません。",
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


class MessageFormatter(BaseMessageFormatter):
    """経済・ショップ・帝愛・貸付・P2P 領域の失敗フォーマッタを重ねた実装。"""

    def _register_entries(self) -> None:
        super()._register_entries()
        self.failures.update(ECONOMY_FAILURES)
        self.failures["loan_borrower_blocked"] = lambda p: (
            "借りる人が、いまは借りられない状態です。" + self.format_failure(p["inner"])
        )

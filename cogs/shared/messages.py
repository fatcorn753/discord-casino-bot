"""ドメイン・永続化層が返す構造化エラー・通知イベント・表示データの Discord 向け整形。"""

from typing import Any, Optional

from domain import horse as horse_mod
from domain import market as market_mod
from domain.earnings import MemoInfo
from domain.errors import Failure, Notice
from domain.history import summarize_history
from infra import catalogs


def _grace_lock_text() -> str:
    return catalogs.economy().wallet.grace_lock_text


def format_failure(reason: Any) -> str:
    """Failure（または互換の str）を日本語 UI 文言へ整形する。"""
    if not isinstance(reason, Failure):
        return str(reason)
    p = reason.params
    c = reason.code

    # ---- 共通・所持金・借金 ----
    if c == "already_owned":
        return "すでに持っています。"
    if c == "invalid_spec":
        return "指定が正しくありません。"
    if c == "amount_must_be_positive":
        return "金額は1以上にしてください。"
    if c == "insufficient_balance":
        return f"残高が足りません（いまの残高: {p['balance']:,}ペリカ）。"
    if c == "insufficient_balance_need":
        return f"ペリカが足りません（{p['need']:,}ペリカ必要・残高 {p['balance']:,}ペリカ）。"
    if c == "debt_blocks_buy":
        return f"借金が {p['debt']:,}ペリカ 残っているので、買えません。"
    if c == "debt_blocks_sell":
        return f"借金が {p['debt']:,}ペリカ 残っているので、売れません。"
    if c == "debt_blocks_shopping":
        return f"借金が {p['debt']:,}ペリカ 残っているので、買い物はできません。"
    if c == "debt_blocks_transfer":
        return f"借金が {p['debt']:,}ペリカ 残っているので、送金はできません。"
    if c == "debt_blocks_quiz":
        return f"借金が {p['debt']:,}ペリカ 残っているので、出題できません。"
    if c == "debt_blocks_raffle":
        return f"借金が {p['debt']:,}ペリカ 残っているので、参加できません。"
    if c == "grace_lock":
        return _grace_lock_text()
    if c == "user_not_registered":
        return "その人は、まだこのサーバーで遊んでいません。"

    # ---- 送金 ----
    if c == "transfer_self":
        return "自分には送金できません。"
    if c == "transfer_min_games":
        return f"送金は、{p['min_games']}回以上ゲームで遊んでからできます（複数アカウントでの稼ぎ対策です）。"
    if c == "transfer_daily_limit":
        return f"1日の送金は合計 {p['daily_limit']:,} ペリカまでです（今日は {p['sent_today']:,} ペリカ送金済み）。"
    if c == "transfer_insufficient_balance":
        return f"残高が足りません（送金額 {p['amount']:,} + 手数料 {p['fee']:,} が必要です）。"

    # ---- ショップ・競売・裏情報 ----
    if c == "shop_invalid_qty_or_price":
        return "個数と値段は1以上にしてください。"
    if c == "shop_out_of_stock":
        when = f"（次の補充: <t:{int(p['next_at'])}:R>）" if p.get("next_at") else ""
        return f"在庫が足りません（いまの在庫: {p['stock']}個）{when}。在庫は1人ずつ別々で、時間がたつと補充されます。"
    if c == "auction_item_unavailable":
        return "その品は競売にありません。"
    if c == "auction_katagi_only":
        return "競売で買えるのは、借金も延滞もないカタギだけです。"
    if c == "auction_insufficient_qty":
        return "その数は残っていません。"
    if c == "auction_horse_sold":
        return "その馬は、もう売れてしまいました。"
    if c == "insider_no_event":
        return "いま、売れる情報はありません（次の決算が決まるまで、お待ちください）。"
    if c == "insider_already_bought":
        return "この決算の裏情報メモは、もう買っています。"
    if c == "insider_sold_out":
        return "この決算の裏情報メモは、売り切れです（サーバーで5枚まで）。"

    # ---- 証券・信用取引・オプション・債券 ----
    if c == "market_pin_limit_down":
        name = market_mod.BY_KEY[p["symbol"]].name
        act = market_mod.PIN_ACTION_TEXT[p["action"]]
        return f"🚨 {name} は**ストップ安に張り付いています**。{act}は、買い手がいなくて約定しません（下げが止まるまで、待つしかありません）。"
    if c == "market_pin_limit_up":
        name = market_mod.BY_KEY[p["symbol"]].name
        act = market_mod.PIN_ACTION_TEXT[p["action"]]
        return f"🚀 {name} は**ストップ高に張り付いています**。{act}は、売り手がいなくて約定しません（上げが止まるまで、待つしかありません）。"
    if c == "crypto_invalid_amount":
        return "金額が正しくありません。"
    if c == "crypto_insufficient_balance":
        return f"ペリカが足りません（残高 {p['balance']:,}ペリカ）。"
    if c == "crypto_amount_too_small":
        return "その金額では 0.001枚も買えません。金額を増やしてください。"
    if c == "crypto_collateral_locked":
        return (
            f"信用取引の担保になっているので、売れません（売ると維持率が {p['ratio']:.0%}・{p['open_ratio']:.0%} 以上が必要）。"
            "先に建玉を決済してください。"
        )
    if c == "crypto_not_held":
        return f"{p['name']}を持っていません。"
    if c == "crypto_sell_range":
        return f"売れるのは 0.001〜{p['max_lots']} です（持っている分）。"
    if c == "crypto_proceeds_too_small":
        return "その数量では受け取れる額が 1ペリカ未満です。もう少し多く売ってください。"
    if c == "margin_debt_blocks":
        return f"借金が {p['debt']:,}ペリカ 残っているので、{p['label']}はできません。"
    if c == "margin_opposite_open":
        return f"{p['name']}の{p['other_label']}を建てています。先に決済してください。"
    if c == "margin_min_collateral":
        return f"担保が {p['min_margin']}ペリカ 未満になる量です。枚数を増やしてください。"
    if c == "margin_insufficient_collateral":
        return f"担保が足りません（{p['need']:,}ペリカ必要・残高 {p['balance']:,}ペリカ）。"
    if c == "margin_ratio_too_low":
        return (
            f"口座の維持率が足りなくなります（建てると {p['ratio']:.0%}・{p['open_ratio']:.0%} 以上が必要）。"
            "枚数を減らすか、レバレッジを下げるか、現物や債券を担保に持ってください。"
        )
    if c == "margin_not_open":
        return f"{p['label']}を建てていません。"
    if c == "margin_close_range":
        return f"決済できるのは 0.001〜{p['max_lots']} です（建てている分）。"
    if c == "market_no_price":
        return "まだ価格がありません。"
    if c == "option_pair_same_symbol":
        return "ペア・ワラントは、ちがう2つの銘柄を選んでください。"
    if c == "option_target_pct_range":
        return f"目標割合は、1〜{p['max_pct']}% にしてください。"
    if c == "option_min_premium":
        return f"掛け金は {p['min_premium']:,}ペリカ 以上からです。"
    if c == "option_min_margin":
        return f"証拠金は {p['min_premium']:,}ペリカ 以上からです。"
    if c == "option_buy_too_small":
        return "その掛け金では 1口も買えません。掛け金を増やしてください。"
    if c == "option_write_too_small":
        return "その証拠金では 1口も売れません。証拠金を増やしてください。"
    if c == "option_insufficient_balance":
        return f"残高が足りません（{p['cost']:,}ペリカ必要・残高 {p['balance']:,}ペリカ）。"
    if c == "option_max_open_premium":
        return f"持てるオプションの掛け金は、合計 {p['max_open']:,}ペリカ までです（あと {p['left']:,}ペリカ）。"
    if c == "option_max_short_margin":
        return f"預けられる裸売りの証拠金は、合計 {p['max_margin']:,}ペリカ までです（あと {p['left']:,}ペリカ）。"
    if c == "option_bank_no_cash":
        return "いま帝愛銀行の手元の現金が足りず、プレミアムを払えません（準備率が低いとき）。"
    if c == "option_not_found":
        return "その番号のオプションは持っていません。"
    if c == "bond_invalid_kind":
        return "債券の種類が正しくありません。"
    if c == "bond_min_amount":
        return f"{p['min_amount']:,}ペリカ以上で買ってください。"
    if c == "bond_max_total":
        return (
            f"{p['name']}は元本の合計が {p['max_total']:,}ペリカ まで（いま {p['held']:,}ペリカ 保有）。"
            f"あと {p['room']:,}ペリカ まで買えます。"
        )
    if c == "bond_not_found":
        return "その番号の債券は持っていません。"
    if c == "bond_not_sellable":
        return f"{p['name']}は売れません（満期まで持つだけです）。"

    # ---- 帝愛銀行・借金・CDS・逃亡・破産 ----
    if c == "bank_katagi_only":
        return "帝愛に預けられるのは、🟢 カタギだけです（借金か延滞がある人、破産者は預けられません）。"
    if c == "bank_min_deposit":
        return f"預金は {p['min_deposit']:,}ペリカ 以上からです。"
    if c == "bank_no_deposit":
        return "預金がありません。"
    if c == "bank_withdraw_range":
        return f"引き出せるのは 1〜{p['max_amount']:,}ペリカ です（いまの預金）。"
    if c == "bank_frozen":
        return "🚨 帝愛は支払停止中です（預金封鎖）。手元の現金が、引当金のロックで尽きています。"
    if c == "bank_available_limit":
        return f"いま引き出せるのは {p['available']:,}ペリカ までです（帝愛の利用可能現金）。残りは、現金が戻ってからです。"
    if c == "cds_min_stake":
        return f"掛け金は {p['min_stake']:,}ペリカ 以上からです。"
    if c == "cds_target_in_grace":
        return "その人は、すでに破産猶予に入っているので、掛けられません。"
    if c == "cds_target_bankrupt":
        return "その人は、破産者なので、掛けられません。"
    if c == "cds_self_cap":
        return (
            f"自分への CDS は、配当の見込みの合計が {p['cap']:,}ペリカ までです"
            f"（いま {p['have']:,}ペリカ分。あと掛けられるのは {p['room']:,}ペリカ）。"
        )
    if c == "seed_bankrupt_blocked":
        return "破産者は、完済するまで、帝愛から借りられません。"
    if c == "seed_grace_blocked":
        return "破産猶予の間は、新しく借りられません。"
    if c == "seed_not_needed":
        return f"手元が {p['seed']:,}ペリカ 以上あるので、借りられません（借りられるのは、枠の {p['seed']:,}ペリカ に足りない分まで）。"
    if c == "credit_bankrupt_blocked":
        return "破産者は、帝愛の信用を使えません。"
    if c == "credit_grace_blocked":
        return "破産猶予の間は、帝愛の信用を使えません。"
    if c == "credit_limit_exceeded":
        return f"帝愛の信用で借りられるのは、1回 {p['seed']:,}ペリカ までです（足りない額: {p['need']:,}ペリカ）。"
    if c == "repay_no_debt":
        return "帝愛への借金はありません。"
    if c == "repay_no_cash":
        return f"手元にペリカがありません（いまの残高: {p['balance']:,}ペリカ）。"
    if c == "repay_exceeds_debt":
        return f"返す額が借金を超えています（借金: {p['debt']:,}ペリカ）。"
    if c == "escape_bankrupt_blocked":
        return "破産者は、逃亡できません（すでに全財産を差し押さえられています）。"
    if c == "escape_min_debt":
        return f"逃亡を試せるのは、帝愛の借金が {p['min_debt']:,}ペリカ 以上のときだけです。"
    if c == "escape_max_tries":
        return f"逃亡のチャンスは、通算 {p['max_tries']} 回までです。もう使い切りました。"
    if c == "escape_cooldown":
        return f"警戒が厳しく、次に試せるのは <t:{int(p['ready'])}:R> です。"
    if c == "bankrupt_already":
        return "すでに破産者です。"
    if c == "bankrupt_no_debt":
        return "借金も貸付の借り入れもないので、破産できません。"

    # ---- 貸付 ----
    if c == "loan_blacklisted":
        return "破産のブラックリストの間は、貸付を借りられません（完済して7日たつと解除されます）。"
    if c == "loan_has_overdue":
        return "延滞中の貸付があるので、借りられません。先に返してください。"
    if c == "loan_min_principal":
        return f"金額は {p['min_principal']:,}ペリカ 以上にしてください。"
    if c == "loan_min_rate":
        return "日利は 0 以上にしてください。"
    if c == "loan_term_range":
        return f"期間は {p['min_days']}〜{p['max_days']}日 にしてください。"
    if c == "loan_max_borrower_open":
        return f"出せる募集は、同時に {p['max_open']} 件までです。先の募集を取り下げてください。"
    if c == "loan_request_ended":
        return "この募集はもう終わっています（ほかの人が貸したか、取り下げられました）。"
    if c == "loan_self_lend":
        return "自分の募集には、貸せません。"
    if c == "loan_request_expired":
        return "募集の期限が過ぎました。"
    if c == "loan_borrower_blocked":
        return "借りる人が、いまは借りられない状態です。" + format_failure(p["inner"])
    if c == "loan_max_lender_active":
        return f"同時に貸せるのは {p['max_active']} 件までです。"
    if c == "loan_self_offer":
        return "自分には貸せません。"
    if c == "loan_max_lender_open":
        return f"承諾待ちの提案は {p['max_open']} 件までです。"
    if c == "loan_offer_ended":
        return "この提案はもう終わっています。"
    if c == "loan_accept_borrower_only":
        return "この提案を承諾できるのは、借りる本人だけです。"
    if c == "loan_offer_expired":
        return "承諾の期限が過ぎました。"
    if c == "loan_lender_insufficient_balance":
        return "貸す側の残高が足りなくなったので、成立しませんでした。"
    if c == "loan_decline_borrower_only":
        return "断れるのは、借りる本人だけです。"
    if c == "loan_cancel_requester_only":
        return "取り下げられるのは、募集を出した人だけです。"
    if c == "loan_cancel_lender_only":
        return "取り下げられるのは、貸す側だけです。"
    if c == "loan_repay_not_found":
        return "返す貸付が見つかりません。"
    if c == "loan_repay_borrower_only":
        return "返せるのは、借りた本人だけです。"
    if c == "loan_repay_exceeds_remaining":
        return f"返す額が残りを超えています（残り {p['remaining']:,}ペリカ）。"

    # ---- P2P ----
    if c == "p2p_horse_limit":
        return f"持てる馬は {p['max_horses']} 頭までです（出品中の馬も数えます）。"
    if c == "p2p_self_buy":
        return "自分の出品は、買えません。"
    if c == "p2p_katagi_only":
        return "取引で買えるのは、借金も延滞もないカタギだけです。"
    if c == "p2p_pair_limit":
        return (
            f"同じ相手との取引は、24時間で合計 {p['limit']:,}ペリカ までです"
            f"（いま {p['used']:,}ペリカ・今回 {p['amount']:,}ペリカ）。"
        )
    if c == "p2p_pair_limit_short":
        return "同じ相手との取引の上限を超えます。"
    if c == "p2p_invalid_spec":
        return "出品の指定が正しくありません。"
    if c == "p2p_invalid_auction_hours":
        return f"競りの長さは、{'・'.join(map(str, p['hours']))} 時間から選んでください。"
    if c == "p2p_max_active":
        return f"同時に出せるのは {p['max_active']} 件までです。"
    if c == "p2p_item_not_tradable":
        return "その品は、取引できません（称号・コレクション・限定品・帝愛市の品は不可）。"
    if c == "p2p_insufficient_qty":
        return "その数は、持っていません。"
    if c == "p2p_rental_not_tradable":
        return "レンタル馬は出品できません。"
    if c == "p2p_horse_in_race":
        return "出走予定のレースが終わるまで、出品できません。"
    if c == "p2p_price_range":
        return f"値段は {p['lo']:,}〜{p['hi']:,}ペリカ にしてください（定価の幅に収めます）。"
    if c == "p2p_not_found":
        return "その出品は、もうありません。"
    if c == "p2p_is_auction":
        return "これは競りです。入札してください。"
    if c == "p2p_is_fixed":
        return "これは定価の出品です。「買う」を押してください。"
    if c == "p2p_already_highest_bidder":
        return "いま、あなたが最高入札者です。"
    if c == "p2p_bid_min":
        return f"入札は {p['need']:,}ペリカ 以上にしてください。"
    if c == "p2p_cancel_not_found":
        return "取り消せる出品が見つかりません。"
    if c == "p2p_cancel_has_bid":
        return "入札が入っているので、取り消せません。"

    # ---- 競馬 ----
    if c == "horse_select_stat_to_add":
        return "増やすステータスを選んでください。"
    if c == "horse_select_distinct_stats":
        return "動かす元と先は、別のステータスを選んでください。"
    if c == "horse_stat_at_min":
        return f"{horse_mod.STAT_NAMES[p['stat']]} は、これ以上は減らせません（最低 {p['min']}）。"
    if c == "horse_stat_at_max":
        return f"{horse_mod.STAT_NAMES[p['stat']]} は、これ以上は増やせません（最高 {p['max']}）。"
    if c == "horse_name_empty":
        return "名前を入れてください。"
    if c == "horse_name_too_long":
        return f"名前は {p['max']} 文字までです。"
    if c == "horse_name_invalid_chars":
        return "名前に使えない文字（@ < > ` * _ ~ | # : / など）か、リンクが入っています。"
    if c == "horse_invalid_look":
        return "毛色と勝負服の色を、選び直してください。"
    if c == "horse_max_owned":
        return f"持てる馬は {p['max_horses']} 頭までです。"
    if c == "horse_no_voucher":
        return "「競走馬（購入券）」を持っていません。ショップで買えます。"
    if c == "horse_no_rental":
        return "いまレンタルしている馬がいません。"
    if c == "horse_select_own":
        return "自分の馬を選んでください。"
    if c == "horse_rental_no_stats":
        return "レンタル馬は、ステータスを調整できません（買うと調整できます）。"
    if c == "horse_no_carrot":
        return "「にんじん」を持っていません。ショップで買えます。"
    if c == "horse_rental_no_retire":
        return "レンタル馬は引退できません（買わなければ、日が替わると自然に消えます）。"
    if c == "horse_busy_no_retire":
        return "出走予定のレースが終わるまで、引退できません。"
    if c == "horse_retire_fee_short":
        return f"引退費用 {p['fee']:,}ペリカ が足りません（残高 {p['balance']:,}ペリカ）。"
    if c == "horse_gold_carrot_max":
        return f"この馬には、金にんじんをもう使えません（1頭に {p['max_count']} 本まで）。"
    if c == "horse_no_gold_carrot":
        return "「金にんじん」を持っていません。ショップで買えます。"
    if c == "race_entry_closed":
        return "このレースの申し込みは、もう締め切りました。"
    if c == "horse_retired_by_age":
        return f"この馬は定年（{p['max_races']}戦）に達したので、出走できません（引退済みのはずです）。"
    if c == "race_one_horse_per_owner":
        return "1人が1つのレースに出せる馬は、1頭までです。"
    if c == "race_field_full":
        return "出走枠が埋まりました。"
    if c == "race_duplicate_name":
        return "同じ名前の馬がすでに出走します。"
    if c == "race_horse_busy":
        return "この馬は、ほかのレースに出走予定です。"
    if c == "race_entry_fee_short":
        return f"出走料 {p['fee']:,}ペリカ が足りません（残高 {p['balance']:,}ペリカ）。"
    if c == "race_not_entered":
        return "この馬は、申し込んでいません。"
    if c == "race_select_bet_kind":
        return "馬券の種類を選んでください。"
    if c == "race_select_distinct_horses":
        return f"{horse_mod.BET_KINDS[p['kind']]}は、違う馬を {p['need']} 頭選んでください。"
    if c == "race_stake_range":
        return f"賭け金は {p['min_stake']:,}〜{p['max_stake']:,}ペリカ にしてください。"
    if c == "race_bet_unavailable":
        return "このレースの馬券は、まだ（またはもう）買えません。"
    if c == "race_bet_closed":
        return "馬券の発売は、締め切りました。"
    if c == "race_lane_not_running":
        return "その馬は出走しません。"
    if c == "race_odds_pending":
        return "オッズを計算中です。少し待ってから、もう一度買ってください。"
    if c == "race_max_stake":
        return f"1つのレースに賭けられるのは、合計 {p['max_race_stake']:,}ペリカ までです。"
    if c == "race_multi_only":
        return "まとめて買えるのは、単勝と複勝だけです。"
    if c == "race_select_horses":
        return "馬を選んでください。"
    if c == "race_max_stake_many":
        return (
            f"1つのレースに賭けられるのは、合計 {p['max_race_stake']:,}ペリカ までです"
            f"（今回は {p['total']:,}、すでに {p['spent']:,}）。"
        )
    if c == "race_insufficient_balance_many":
        return f"残高が足りません（合計 {p['total']:,}ペリカ 必要。いまの残高: {p['balance']:,}ペリカ）。"
    if c == "race_partial_bought":
        return f"{format_failure(p['inner'])}（先の {p['bought']} 頭ぶんは買えています）"

    # ---- 地下作業・発掘品 ----
    if c == "relic_not_relic":
        return "発掘品ではありません。"
    if c == "relic_max_star":
        return f"★{p['star_max']} は最上位で、これ以上は合体できません。"
    if c == "relic_need_more":
        return f"合体には、同じ発掘品が {p['count']}個 要ります（出品中の品は数えません）。"
    if c == "labor_not_tired":
        return "疲れていません。"
    if c == "labor_no_drink":
        return "栄養ドリンコを持っていません（ショップの消耗品で買えます）。"
    if c == "labor_unknown_tool":
        return "そんな工具はありません。"
    if c == "labor_tool_max":
        return f"{p['tool_name']}は、もう最大です。"
    if c == "labor_insufficient_balance":
        return f"ペリカが足りません（{p['cost']:,} 必要）。"
    if c == "labor_unknown_worker":
        return "そんな作業員はいません。"
    if c == "labor_slots_full":
        return "人数がいっぱいです（寮を強化するか、誰かを解雇してください）。"
    if c == "labor_hire_insufficient_balance":
        return f"ペリカが足りません（契約料 {p['cost']:,} 必要）。"
    if c == "labor_worker_absent":
        return f"{p['worker_name']}は、いません。"
    if c == "labor_dorm_max":
        return "寮は、もう最大です。"

    # ---- クイズ・抽選 ----
    if c == "quiz_ended":
        return "この問題は終了しました。"
    if c == "quiz_wrong_guild":
        return "この問題は、出題されたサーバーでだけ答えられます。"
    if c == "quiz_self_answer":
        return "自分が出した問題には答えられません。"
    if c == "quiz_already_answered":
        return "この問題にはもう答えています。"
    if c == "quiz_open_limit":
        return f"同時に出せる問題は {p['limit']} つまでです。`/クイズ 取り下げ` で下げてから出してください。"
    if c == "quiz_daily_limit":
        return f"出題は、24時間に {p['limit']} 問までです。"
    if c == "quiz_insufficient_balance":
        return f"残高が報酬（{p['reward']:,}ペリカ）に足りません。出題するには、正解1人ぶんの報酬が残高に必要です。"
    if c == "raffle_min_fee":
        return "参加費は1ペリカ以上にしてください。"
    if c == "raffle_already_open":
        return "すでに募集中の抽選があります。"
    if c == "raffle_closed":
        return "募集は終わりました。"
    if c == "raffle_already_joined":
        return "すでに参加しています。"
    if c == "raffle_insufficient_balance":
        return f"残高が足りません（参加費 {p['fee']:,}ペリカ、いまの残高 {p['balance']:,}ペリカ）。"

    return str(reason)


def format_notice(item: Any) -> str:
    """Notice（または互換の str）を Discord UI 向け文言へ整形する。"""
    if not isinstance(item, Notice):
        return str(item)
    p = item.params
    c = item.code

    if c == "grace_entered":
        return (
            f"⚠️ 借金が {p['grace_debt']:,}ペリカ に達し、**破産猶予**に入りました。"
            f"<t:{int(p['until'])}:R> までに借金を {p['grace_debt']:,}ペリカ 未満へ戻せなければ、強制破産です。"
        )
    if c == "grace_exited":
        return "😮‍💨 借金が減り、破産猶予から抜けました。"
    if c == "cds_self_covered":
        return f"🛡️ 自分への CDS が、借金 {p['pay']:,}ペリカ を肩代わりしました（帝愛が払います）。"
    if c == "cds_default_payout":
        return f"💀 CDS が的中しました。<@{p['target_id']}> の強制破産で、{p['pay']:,}ペリカ を受け取りました。"
    if c == "debt_interest":
        return f"🕴️ 帝愛の利息が付きました（+{p['add']:,}ペリカ。日利{p['rate']:.0%}）。"
    if c == "seize_deposit":
        return f"💥 帝愛への預金が差し押さえられました（{p['taken']:,}ペリカ）。"
    if c == "seize_margin":
        return f"💥 {p['symbol_name']}の{p['side_label']}が強制決済されました（戻り {p['money']:,}ペリカ）。"
    if c == "seize_spot":
        return f"💥 保有していた{p['symbol_name']}が差し押さえられました（{p['money']:,}ペリカ）。"
    if c == "seize_option":
        return f"💥 オプションが差し押さえられました（{p['got']:,}ペリカ）。"
    if c == "seize_bond":
        return f"💥 通常債が強制売却されました（{p['refund']:,}ペリカ）。"
    if c == "seed_loan":
        return f"🕴️ 帝愛ファイナンスから種銭 {p['amount']:,}ペリカ が貸し付けられました。「これで取り返してこい…！」"
    if c == "margin_losscut":
        return (
            f"💥 口座の維持率が {p['losscut_ratio']:.0%} を下回り、"
            f"{p['symbol_name']}の{p['side_label']}が強制決済されました（損益 {p['profit']:+,}ペリカ）。"
        )
    if c == "p2p_sold":
        return f"🤝 **{p['label']}** が **{p['price']:,}ペリカ** で売れました（手数料 {p['fee']:,}ペリカ・受け取り {p['net']:,}ペリカ）。"
    if c == "p2p_outbid":
        return f"📉 **{p['label']}** の入札を、**{p['amount']:,}ペリカ** で抜かれました（預かっていた {p['refund']:,}ペリカ は返りました）。"
    if c == "p2p_seller_pulled_back":
        return f"⚠️ **{p['label']}** の出品者が取引できなくなったため、競りは中止され、入札 {p['refund']:,}ペリカ は返りました。"
    if c == "p2p_won":
        return f"🎉 **{p['label']}** を **{p['bid']:,}ペリカ** で落札しました。"
    if c == "p2p_won_failed":
        return f"⚠️ **{p['label']}** は落札しましたが、{format_failure(p['reason'])}そのため取引は成立せず、{p['refund']:,}ペリカ は返りました。"
    if c == "p2p_buyer_cannot_receive":
        return f"**{p['label']}** は、買い手が受け取れず、取引が成立しませんでした（品は戻りました）。"
    if c == "p2p_unsold":
        return f"**{p['label']}** は、売れ残りました（品は戻りました）。"
    return str(item)


def format_notices(items: list[Any]) -> list[str]:
    return [format_notice(n) for n in items]


def format_memo(info: MemoInfo) -> str:
    word = "大幅な上方修正の噂" if info.hint > 0 else "大幅な下方修正の噂"
    return (
        f"【極秘メモ】<t:{int(info.release_at)}:F> の {info.symbol_name} の決算、**{word}**あり…？\n"
        f"（確度 {info.truth:.0%} ／ ガセの確率 {1 - info.truth:.0%}。信じるかは、あなた次第。周りには黙っておくか、煽るか…）"
    )


def format_history(rows_newest_first: list, limit: int = 12) -> list[str]:
    entries = summarize_history(rows_newest_first, limit)
    lines: list[str] = []
    for e in entries:
        if e.kind == "game":
            text = f"{e.game} 賭{e.bet:,}"
        elif e.kind == "transfer":
            arrow = "→" if e.direction == "送" else "←"
            text = f"送金 {arrow} <@{e.target_user_id}>"
        else:
            text = e.reason
        lines.append(f"<t:{int(e.ts)}:t> {text}　**{e.delta:+,}**　→ {e.balance_after:,}ペリカ")
    return lines


def format_standings_lines(rows: list, medals: dict[int, str], viewer_id: Optional[int] = None) -> list[str]:
    lines: list[str] = []
    for rank, (uid, net, plays) in enumerate(rows, 1):
        mark = medals.get(rank, f"`{rank:>2}`")
        me = "　◀ あなた" if viewer_id == uid else ""
        lines.append(f"{mark} <@{uid}>　**{net:+,}ペリカ**（{plays}戦）{me}")
    return lines

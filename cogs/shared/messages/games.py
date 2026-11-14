"""競馬・地下作業・発掘品・クイズ・抽選領域の失敗メッセージ定義。"""

from typing import Any, Callable

from domain import horse as horse_mod

FailureFormatter = str | Callable[[dict[str, Any]], str]


def _race_partial_bought(p: dict[str, Any]) -> str:
    from cogs.shared.messages import format_failure

    return f"{format_failure(p['inner'])}（先の {p['bought']} 頭ぶんは買えています）"


GAMES_FAILURES: dict[str, FailureFormatter] = {
    # 競馬
    "horse_select_stat_to_add": "増やすステータスを選んでください。",
    "horse_select_distinct_stats": "動かす元と先は、別のステータスを選んでください。",
    "horse_stat_at_min": lambda p: f"{horse_mod.STAT_NAMES[p['stat']]} は、これ以上は減らせません（最低 {p['min']}）。",
    "horse_stat_at_max": lambda p: f"{horse_mod.STAT_NAMES[p['stat']]} は、これ以上は増やせません（最高 {p['max']}）。",
    "horse_name_empty": "名前を入れてください。",
    "horse_name_too_long": lambda p: f"名前は {p['max']} 文字までです。",
    "horse_name_invalid_chars": "名前に使えない文字（@ < > ` * _ ~ | # : / など）か、リンクが入っています。",
    "horse_invalid_look": "毛色と勝負服の色を、選び直してください。",
    "horse_max_owned": lambda p: f"持てる馬は {p['max_horses']} 頭までです。",
    "horse_no_voucher": "「競走馬（購入券）」を持っていません。ショップで買えます。",
    "horse_no_rental": "いまレンタルしている馬がいません。",
    "horse_select_own": "自分の馬を選んでください。",
    "horse_rental_no_stats": "レンタル馬は、ステータスを調整できません（買うと調整できます）。",
    "horse_no_carrot": "「にんじん」を持っていません。ショップで買えます。",
    "horse_rental_no_retire": "レンタル馬は引退できません（買わなければ、日が替わると自然に消えます）。",
    "horse_busy_no_retire": "出走予定のレースが終わるまで、引退できません。",
    "horse_retire_fee_short": lambda p: f"引退費用 {p['fee']:,}ペリカ が足りません（残高 {p['balance']:,}ペリカ）。",
    "horse_gold_carrot_max": lambda p: f"この馬には、金にんじんをもう使えません（1頭に {p['max_count']} 本まで）。",
    "horse_no_gold_carrot": "「金にんじん」を持っていません。ショップで買えます。",
    "race_entry_closed": "このレースの申し込みは、もう締め切りました。",
    "horse_retired_by_age": lambda p: (
        f"この馬は定年（{p['max_races']}戦）に達したので、出走できません（引退済みのはずです）。"
    ),
    "race_one_horse_per_owner": "1人が1つのレースに出せる馬は、1頭までです。",
    "race_field_full": "出走枠が埋まりました。",
    "race_duplicate_name": "同じ名前の馬がすでに出走します。",
    "race_horse_busy": "この馬は、ほかのレースに出走予定です。",
    "race_entry_fee_short": lambda p: f"出走料 {p['fee']:,}ペリカ が足りません（残高 {p['balance']:,}ペリカ）。",
    "race_not_entered": "この馬は、申し込んでいません。",
    "race_select_bet_kind": "馬券の種類を選んでください。",
    "race_select_distinct_horses": lambda p: (
        f"{horse_mod.BET_KINDS[p['kind']]}は、違う馬を {p['need']} 頭選んでください。"
    ),
    "race_stake_range": lambda p: f"賭け金は {p['min_stake']:,}〜{p['max_stake']:,}ペリカ にしてください。",
    "race_bet_unavailable": "このレースの馬券は、まだ（またはもう）買えません。",
    "race_bet_closed": "馬券の発売は、締め切りました。",
    "race_lane_not_running": "その馬は出走しません。",
    "race_odds_pending": "オッズを計算中です。少し待ってから、もう一度買ってください。",
    "race_max_stake": lambda p: f"1つのレースに賭けられるのは、合計 {p['max_race_stake']:,}ペリカ までです。",
    "race_multi_only": "まとめて買えるのは、単勝と複勝だけです。",
    "race_select_horses": "馬を選んでください。",
    "race_max_stake_many": lambda p: (
        f"1つのレースに賭けられるのは、合計 {p['max_race_stake']:,}ペリカ までです"
        f"（今回は {p['total']:,}、すでに {p['spent']:,}）。"
    ),
    "race_insufficient_balance_many": lambda p: (
        f"残高が足りません（合計 {p['total']:,}ペリカ 必要。いまの残高: {p['balance']:,}ペリカ）。"
    ),
    "race_partial_bought": _race_partial_bought,
    # 地下作業・発掘品
    "relic_not_relic": "発掘品ではありません。",
    "relic_max_star": lambda p: f"★{p['star_max']} は最上位で、これ以上は合体できません。",
    "relic_need_more": lambda p: f"合体には、同じ発掘品が {p['count']}個 要ります（出品中の品は数えません）。",
    "labor_not_tired": "疲れていません。",
    "labor_no_drink": "栄養ドリンコを持っていません（ショップの消耗品で買えます）。",
    "labor_unknown_tool": "そんな工具はありません。",
    "labor_tool_max": lambda p: f"{p['tool_name']}は、もう最大です。",
    "labor_insufficient_balance": lambda p: f"ペリカが足りません（{p['cost']:,} 必要）。",
    "labor_unknown_worker": "そんな作業員はいません。",
    "labor_slots_full": "人数がいっぱいです（寮を強化するか、誰かを解雇してください）。",
    "labor_hire_insufficient_balance": lambda p: f"ペリカが足りません（契約料 {p['cost']:,} 必要）。",
    "labor_worker_absent": lambda p: f"{p['worker_name']}は、いません。",
    "labor_dorm_max": "寮は、もう最大です。",
    # クイズ・抽選
    "quiz_ended": "この問題は終了しました。",
    "quiz_wrong_guild": "この問題は、出題されたサーバーでだけ答えられます。",
    "quiz_self_answer": "自分が出した問題には答えられません。",
    "quiz_already_answered": "この問題にはもう答えています。",
    "quiz_open_limit": lambda p: (
        f"同時に出せる問題は {p['limit']} つまでです。`/クイズ 取り下げ` で下げてから出してください。"
    ),
    "quiz_daily_limit": lambda p: f"出題は、24時間に {p['limit']} 問までです。",
    "quiz_insufficient_balance": lambda p: (
        f"残高が報酬（{p['reward']:,}ペリカ）に足りません。出題するには、正解1人ぶんの報酬が残高に必要です。"
    ),
    "raffle_min_fee": "参加費は1ペリカ以上にしてください。",
    "raffle_already_open": "すでに募集中の抽選があります。",
    "raffle_closed": "募集は終わりました。",
    "raffle_already_joined": "すでに参加しています。",
    "raffle_insufficient_balance": lambda p: (
        f"残高が足りません（参加費 {p['fee']:,}ペリカ、いまの残高 {p['balance']:,}ペリカ）。"
    ),
}

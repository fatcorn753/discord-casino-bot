"""帝愛ファイナンス（借金）と身分の数字。調整はここの定数を書き換える。

借金（users.debt）は、帝愛からの借金（シニア債権）。プレイヤー間の貸付（/貸付）は、そのあとに回収されるジュニア債権。

- 借金に落ちたとき、現金 → 空売り → CSトークン → 通常債の順に強制執行（差し押さえ）してから、残りを借金にする
- 借金がある人の手元が空なら、帝愛が種銭 SEED を貸し付ける（借金に加算）。デイリーでも、手元が SEED 未満なら補充する
- 入ってきた収入（ゲームの純利益・デイリー・ミッション報酬など）の REPAY_RATE を、借金の返済へ天引きする。残りは手元に残る
- デイリーは、先に SKIM_RATE を帝愛が没収する（借金の元本は減らない）
- 身分: カタギ（借金・延滞なし）／地下労働者（借金か延滞がある）／破産者（自己破産のあと、完済して BLACKLIST_DAYS 日たつまで）
- 自己破産: 全財産とショップの全品を差し押さえ、換金して債権者へ配当する。貸付の残りは免責、帝愛の借金は BANKRUPT_FLOOR まで圧縮
"""

SEED = 3_000  # 帝愛が貸し付ける種銭の枠（手元がこの額になるまで補充する）
SKIM_RATE = 0.20  # 借金中のデイリーから、帝愛が没収する割合
REPAY_RATE = 0.30  # 収入から、借金の返済へ天引きする割合
BANKRUPT_FLOOR = 10_000  # 自己破産のあとに残る、帝愛の借金の上限（これより多い分は圧縮）
BLACKLIST_DAYS = 7  # 破産者の身分が続く、完済後の日数
SEIZE_RATE = 0.50  # ショップの品と、解約できない債券を差し押さえるときの換金率（購入額に対して）

KATAGI, UNDERGROUND, BANKRUPT = "katagi", "underground", "bankrupt"
STATUS_LABEL = {KATAGI: "🟢 カタギ", UNDERGROUND: "🟡 地下労働者", BANKRUPT: "🔴 破産者"}
STATUS_NAME = {UNDERGROUND: "地下労働者", BANKRUPT: "破産者"}  # ニックネームの末尾に【…】で付ける名前


def withhold(income: int) -> int:
    """収入から天引きする額。"""
    return int(income * REPAY_RATE)


def skim(amount: int) -> int:
    """デイリーから没収する額。"""
    return int(amount * SKIM_RATE)

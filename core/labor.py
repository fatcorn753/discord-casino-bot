"""地下作業（クリッカー）の数字と計算。DB にも Discord にも触らない（処理は core/eco_labor.py、画面は cogs/labor_cog.py）。

ペリカが尽きた人の救済。元手は 0。「働く」ボタンを押すとペリカが貯まる。
- 疲労: 押すたびに溜まり、溜まるほど1回の稼ぎが減る。時間で自然に回復する。「休憩」で一気に回復できるが、しばらく押せない
- 事故: 押すたびに小さな確率で起き、その回の稼ぎが消えて、疲労が増える
- 工具: ツルハシ（稼ぎ）・手袋（疲労の溜まりにくさ）・ヘルメット（事故の少なさ）を、稼いだペリカで強化する
インフレを防ぐため、1日の稼ぎに上限があり、手元が多い人は働けない。
"""

TOOLS = ("pick", "glove", "helmet")
TOOL_NAME = {"pick": "ツルハシ", "glove": "手袋", "helmet": "ヘルメット"}
TOOL_MARK = {"pick": "⛏", "glove": "🧤", "helmet": "⛑"}
TOOL_EFFECT = {"pick": "稼ぎが増える", "glove": "疲れにくくなる", "helmet": "事故が減る"}
MAX_LEVEL = 5
UPGRADE_COST = (150, 400, 900, 1_800, 3_500)  # 次のレベルへ上げる値段（レベル0→1, 1→2, …）

BASE_PAY = 10  # 工具なしの、1回の稼ぎ
PAY_PER_LEVEL = 4  # ツルハシ1段ごとの加算（最大 30）
FATIGUE_MAX = 100.0
FATIGUE_GAIN = 3.0  # 工具なしの、1回で溜まる疲労
GAIN_CUT_PER_LEVEL = 0.4  # 手袋1段ごとの軽減（最大で 1.0）
MIN_PAY_RATE = 0.4  # 疲労が満タンのときの、稼ぎの割合（空のときは 1.0）
RECOVER_PER_SEC = 0.2  # 疲労の自然回復（100 回復するのに約8分）
ACCIDENT_BASE = 0.04  # 工具なしの、事故の確率
ACCIDENT_CUT_PER_LEVEL = 0.007  # ヘルメット1段ごとの軽減（最大で 0.005）
ACCIDENT_FATIGUE = 10.0  # 事故で増える疲労

CLICK_INTERVAL = 1.0  # 連打の最短間隔（秒）
REST_SECONDS = 20.0  # 休憩の間、押せない時間
REST_RECOVER = 30.0  # 休憩で回復する疲労

DAILY_CAP = (
    1_000  # 1日に稼げる上限（工具なし）。工具を1段強化するごとに CAP_PER_LEVEL ずつ増える（デイリーを超えない程度）
)
CAP_PER_LEVEL = 100
BALANCE_LIMIT = 5_000  # 手元がこれを超える人は、働けない（救済が目的）
AUTO_COLLECT = 100  # 稼ぎがこの額たまったら、自動で残高に移す（台帳の行を増やしすぎない）


def clamp_level(level: int) -> int:
    return max(0, min(MAX_LEVEL, int(level)))


def daily_cap(pick: int, glove: int, helmet: int) -> int:
    """1日に稼げる上限。工具を強化するほど増える（強化が、稼ぎの速さだけでなく、1日の天井も上げる）。"""
    return DAILY_CAP + CAP_PER_LEVEL * sum(clamp_level(x) for x in (pick, glove, helmet))


def pay_per_click(pick: int) -> int:
    return BASE_PAY + PAY_PER_LEVEL * clamp_level(pick)


def fatigue_gain(glove: int) -> float:
    return FATIGUE_GAIN - GAIN_CUT_PER_LEVEL * clamp_level(glove)


def accident_chance(helmet: int) -> float:
    return ACCIDENT_BASE - ACCIDENT_CUT_PER_LEVEL * clamp_level(helmet)


def fatigue_now(fatigue: float, since: float, now: float) -> float:
    """最後に疲労を更新してから時間がたった、いまの疲労。"""
    return max(0.0, min(FATIGUE_MAX, fatigue - max(0.0, now - since) * RECOVER_PER_SEC))


def pay_rate(fatigue: float) -> float:
    """疲労に応じた、稼ぎの割合（1.0 〜 MIN_PAY_RATE）。"""
    return 1.0 - (1.0 - MIN_PAY_RATE) * max(0.0, min(FATIGUE_MAX, fatigue)) / FATIGUE_MAX


def click_pay(pick: int, fatigue: float) -> int:
    """いまの疲労での、1回の稼ぎ（事故がなければ）。少なくとも 1。"""
    return max(1, round(pay_per_click(pick) * pay_rate(fatigue)))


def upgrade_cost(level: int):
    """いまのレベルから次へ上げる値段。最大なら None。"""
    level = clamp_level(level)
    return None if level >= MAX_LEVEL else UPGRADE_COST[level]


def bar(fatigue: float, width: int = 10) -> str:
    n = round(width * max(0.0, min(FATIGUE_MAX, fatigue)) / FATIGUE_MAX)
    return "🟥" * n + "⬜" * (width - n)


def click_outcome(pick: int, glove: int, helmet: int, fatigue: float, rng) -> dict:
    """1回押した結果（上限や残高は見ない）。{accident, pay, fatigue}。fatigue は押したあとの疲労。"""
    if rng.random() < accident_chance(helmet):
        return {"accident": True, "pay": 0, "fatigue": min(FATIGUE_MAX, fatigue + ACCIDENT_FATIGUE)}
    return {
        "accident": False,
        "pay": click_pay(pick, fatigue),
        "fatigue": min(FATIGUE_MAX, fatigue + fatigue_gain(glove)),
    }

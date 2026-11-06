"""「90分」「2時間」「1時間30分」「3日」のような期間の書き方を、分に直す（クイズの有効期間などで使う）。"""

import re
import unicodedata

UNITS = {"日": 1440, "d": 1440, "時間": 60, "時": 60, "h": 60, "分": 1, "m": 1}
TOKEN = re.compile(r"(\d+)\s*(日|時間|時|分|d|h|m)", re.IGNORECASE)


def parse_duration(text: str, default_unit: str = "時間") -> int:
    """期間を分で返す。単位なしの数字だけなら default_unit（時間・分・日）として読む。読めなければ ValueError。"""
    t = unicodedata.normalize("NFKC", text).strip().replace(",", "")
    if not t:
        raise ValueError("期間を入力してください。")
    if t.isdigit():
        return int(t) * UNITS[default_unit]
    rest = TOKEN.sub("", t).strip()
    tokens = TOKEN.findall(t)
    if rest or not tokens:
        raise ValueError("期間は「90分」「2時間」「1時間30分」「3日」のように書いてください。")
    units = [u.lower() for _, u in tokens]
    if len(set(units)) != len(units):
        raise ValueError("同じ単位が2回あります。「1時間30分」のように、単位ごとに1回で書いてください。")
    return sum(int(n) * UNITS[u.lower()] for n, u in tokens)


def format_minutes(minutes: int) -> str:
    """分を「1日2時間30分」のように読みやすくする。"""
    minutes = int(minutes)
    days, rest = divmod(minutes, 1440)
    hours, mins = divmod(rest, 60)
    parts = [f"{days}日" if days else "", f"{hours}時間" if hours else "", f"{mins}分" if mins else ""]
    return "".join(parts) or "0分"

"""タイムゾーン（config.json の utc_offset_hours）に基づく現在時刻・日付の取得。"""

import datetime


def local_now(bot) -> datetime.datetime:
    tz = datetime.timezone(datetime.timedelta(hours=float(bot.config.get("utc_offset_hours", 9))))
    return datetime.datetime.now(tz)


def local_today(bot) -> datetime.date:
    return local_now(bot).date()

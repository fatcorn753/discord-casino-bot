"""タイムゾーン（config.json の utc_offset_hours）に基づく現在時刻・日付の取得。"""

import datetime
from typing import Any

from domain.ports import IClock


class ConfigClock(IClock):
    """config 辞書の utc_offset_hours を使う時刻プロバイダ。"""

    def __init__(self, config: dict[str, Any]) -> None:
        self._offset = float(config.get("utc_offset_hours", 9))

    def now(self) -> datetime.datetime:
        tz = datetime.timezone(datetime.timedelta(hours=self._offset))
        return datetime.datetime.now(tz)

    def today(self) -> datetime.date:
        return self.now().date()


def local_now(bot: Any) -> datetime.datetime:
    return ConfigClock(bot.config).now()


def local_today(bot: Any) -> datetime.date:
    return ConfigClock(bot.config).today()

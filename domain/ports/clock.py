"""時刻取得の抽象契約（DIP: アプリ/プレゼンテーション層が時刻実装に直接依存しないため）。"""

import datetime


class IClock:
    """ローカルタイムゾーン基準の現在時刻・日付を返す契約。"""

    def now(self) -> datetime.datetime:
        raise NotImplementedError

    def today(self) -> datetime.date:
        raise NotImplementedError

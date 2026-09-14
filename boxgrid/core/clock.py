"""시계 추상화. 백테스트는 FixedClock 으로 시간을 주입한다."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))


class Clock:
    """실시간 시계 (UTC)."""

    def now(self) -> datetime:
        """현재 UTC 시각."""
        return datetime.now(timezone.utc)

    def today(self) -> str:
        """KST 기준 날짜 문자열(YYYY-MM-DD). 일손실·일거래수 집계 단위."""
        return self.now().astimezone(KST).strftime("%Y-%m-%d")


class FixedClock(Clock):
    """테스트·백테스트용 고정 시계."""

    def __init__(self, ts: datetime) -> None:
        self._ts = ts

    def now(self) -> datetime:
        return self._ts

    def set(self, ts: datetime) -> None:
        """현재 시각을 옮긴다."""
        self._ts = ts

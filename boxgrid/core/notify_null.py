"""알림 없는 기본 구현.

엔진은 `Notifier` Protocol(= `emit(event)` 하나)만 의존한다.
실제 채널 구현(텔레그램·카카오)은 P2 의 `boxgrid/notify/` 가 담당한다.
"""
from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

from .events import Event

log = logging.getLogger(__name__)


@runtime_checkable
class Notifier(Protocol):
    """엔진이 요구하는 알림자 인터페이스."""

    async def emit(self, event: Event) -> None:  # pragma: no cover - 프로토콜 정의
        ...


class NullNotifier:
    """아무 곳에도 보내지 않고 로그만 남기는 알림자. 페이퍼·테스트 기본값."""

    name = "null"

    def __init__(self) -> None:
        self.events: list[Event] = []

    async def emit(self, event: Event) -> None:
        """이벤트를 기록하고 로그로만 남긴다."""
        self.events.append(event)
        log.info("[%s] %s | %s", event.kind.value, event.title, event.body)

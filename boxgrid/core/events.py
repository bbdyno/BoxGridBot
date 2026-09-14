"""알림 이벤트 모델. 엔진과 notify 패키지가 함께 쓴다."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class EventKind(str, Enum):
    ENTRY = "ENTRY"                  # 그리드 한 단 체결
    EXIT = "EXIT"                    # 청산(부분 포함)
    GRID = "GRID"                    # 그리드 게시·회수
    DAILY_CHECK = "DAILY_CHECK"      # 09:00 일봉 판정 결과
    LEVEL_TOUCH = "LEVEL_TOUCH"      # signal 모드: 레벨 도달 알림
    ARM_CONFIRM = "ARM_CONFIRM"      # confirm 모드: 게시 승인 요청(버튼)
    REFUSAL = "REFUSAL"
    ERROR = "ERROR"
    HEARTBEAT_LOST = "HEARTBEAT_LOST"
    DAILY_REPORT = "DAILY_REPORT"
    KILL = "KILL"
    LIVE_CONFIRM = "LIVE_CONFIRM"
    KAKAO_TOKEN_EXPIRING = "KAKAO_TOKEN_EXPIRING"
    INFO = "INFO"


@dataclass
class Event:
    kind: EventKind
    severity: str = "info"  # "info" | "warn" | "error"
    title: str = ""
    body: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    ts: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

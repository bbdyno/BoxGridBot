"""알림 라우터. 이벤트 종류별로 채널을 선택해 보낸다.

`EventKind`/`Event` 는 엔진과 공유하는 `boxgrid/core/events.py` 정의를 그대로 쓴다
(재정의하면 엔진 쪽 isinstance 검사가 깨진다).
"""
from __future__ import annotations

import logging
import time
from typing import Any, Callable, Protocol

from ..core.events import Event, EventKind
from .formats import format_full, format_short

log = logging.getLogger(__name__)

MERGE_WINDOW_SEC = 60.0
RATE_LIMIT_PER_MIN = 5
RATE_WINDOW_SEC = 60.0


class Channel(Protocol):
    """알림 채널 인터페이스."""

    name: str

    async def send(self, event: Event, text: str) -> bool: ...


def _formatter_for(channel_name: str) -> Callable[[Event], str]:
    """채널 이름에 맞는 포맷터. 모르는 채널은 전체 포맷을 쓴다."""
    if channel_name == "kakao":
        return format_short
    return format_full


class Notifier:
    """채널 목록과 라우팅 표로 이벤트를 분배한다."""

    def __init__(
        self,
        channels: list[Any],
        routing: dict[EventKind, list[str]],
        now_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        self.channels: dict[str, Any] = {c.name: c for c in channels}
        self.routing: dict[EventKind, list[str]] = {}
        for kind, names in (routing or {}).items():
            key = kind if isinstance(kind, EventKind) else EventKind(kind)
            self.routing[key] = list(names)
        self._now = now_fn
        # kind -> {"ts": float, "count": int} : 60초 내 반복 병합용
        self._merge_state: dict[EventKind, dict[str, Any]] = {}
        # channel name -> 최근 발송 시각 리스트 : 분당 상한용
        self._rate_state: dict[str, list[float]] = {}

    def bind_engine(self, engine: Any) -> None:
        """엔진 훅(status/pause/close_all/confirm_live)이 필요한 채널에 엔진을 연결한다."""
        for channel in self.channels.values():
            if hasattr(channel, "engine"):
                channel.engine = engine

    async def start(self) -> None:
        """start() 를 가진 채널(텔레그램 폴링 등)을 같은 이벤트 루프에서 시작한다."""
        for channel in self.channels.values():
            starter = getattr(channel, "start", None)
            if callable(starter):
                await starter()

    async def stop(self) -> None:
        for channel in self.channels.values():
            stopper = getattr(channel, "stop", None)
            if callable(stopper):
                try:
                    await stopper()
                except Exception as exc:  # noqa: BLE001
                    log.warning("채널 %s 정지 실패: %s", channel.name, exc)

    def _apply_merge(self, event: Event) -> Event:
        """같은 kind 가 60초 내 반복되면 이번 건에 반복 횟수를 표시한다."""
        kind = event.kind
        now = self._now()
        state = self._merge_state.get(kind)
        if state is None or now - state["ts"] >= MERGE_WINDOW_SEC:
            self._merge_state[kind] = {"ts": now, "count": 1}
            return event
        state["count"] += 1
        state["ts"] = now
        count = state["count"]
        merged_body = f"{event.body}\n(최근 1분 내 {count}회째 반복)" if event.body else f"(최근 1분 내 {count}회째 반복)"
        return Event(kind=event.kind, severity=event.severity, title=event.title,
                     body=merged_body, data=event.data, ts=event.ts)

    def _allow_rate(self, channel_name: str) -> bool:
        """채널별 분당 5회 상한. 초과하면 False."""
        now = self._now()
        history = self._rate_state.setdefault(channel_name, [])
        history[:] = [ts for ts in history if now - ts < RATE_WINDOW_SEC]
        if len(history) >= RATE_LIMIT_PER_MIN:
            return False
        history.append(now)
        return True

    async def _report_failure(self, failed_channel: str, kind: EventKind, error: Exception) -> None:
        """채널 실패를 텔레그램에 INFO 로 한 줄 보고한다. INFO 는 카카오로 보내지 않아 무한루프를 막는다."""
        if failed_channel == "telegram":
            return
        telegram = self.channels.get("telegram")
        if telegram is None:
            return
        info_event = Event(kind=EventKind.INFO, severity="warn", title="알림 채널 오류",
                            body=f"{failed_channel} 채널 발송 실패({kind.value}): {error}")
        try:
            text = _formatter_for("telegram")(info_event)
            await telegram.send(info_event, text)
        except Exception as exc:  # noqa: BLE001
            log.warning("실패 보고마저 실패했습니다: %s", exc)

    async def emit(self, event: Event) -> None:
        """이벤트를 라우팅 표에 따라 채널로 보낸다. 한 채널 실패가 다른 채널을 막지 않는다."""
        merged = self._apply_merge(event)
        channel_names = self.routing.get(event.kind, [])
        for channel_name in channel_names:
            channel = self.channels.get(channel_name)
            if channel is None:
                continue
            if not self._allow_rate(channel_name):
                log.warning("채널 %s 분당 발송 한도 초과, 건너뜁니다: %s", channel_name, event.kind.value)
                continue
            text = _formatter_for(channel_name)(merged)
            try:
                await channel.send(merged, text)
            except Exception as exc:  # noqa: BLE001
                log.warning("채널 %s 발송 실패(%s): %s", channel_name, event.kind.value, exc)
                await self._report_failure(channel_name, event.kind, exc)


def build_notifier(cfg: Any) -> "Notifier | None":
    """`AppConfig` 로 Notifier 를 조립한다. 켜진 채널이 없으면 None."""
    import os

    notify_cfg = dict(getattr(cfg, "notify", None) or {})
    if not notify_cfg:
        return None

    channels: list[Any] = []

    telegram_cfg = dict(notify_cfg.get("telegram") or {})
    if telegram_cfg.get("enabled"):
        token = os.getenv("TELEGRAM_BOT_TOKEN")
        raw_ids = os.getenv("TELEGRAM_CHAT_IDS", "")
        chat_ids = [int(x) for x in raw_ids.split(",") if x.strip()]
        if token and chat_ids:
            from .telegram_bot import TelegramChannel

            channels.append(TelegramChannel(token, chat_ids))
        else:
            log.warning("텔레그램이 활성화되어 있지만 TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_IDS 가 없습니다.")

    kakao_cfg = dict(notify_cfg.get("kakao") or {})
    if kakao_cfg.get("enabled"):
        app_key = os.getenv("KAKAO_REST_API_KEY")
        if app_key:
            from .kakao_memo import KakaoChannel

            channels.append(KakaoChannel(app_key, link_url=kakao_cfg.get("link_url", "https://t.me/")))
        else:
            log.warning("카카오가 활성화되어 있지만 KAKAO_REST_API_KEY 가 없습니다.")

    if not channels:
        return None

    routing_raw = dict(notify_cfg.get("routing") or {})
    routing: dict[EventKind, list[str]] = {}
    for kind in EventKind:
        names = routing_raw.get(kind.value)
        if names is None:
            names = routing_raw.get(kind, [])
        routing[kind] = list(names or [])

    notifier = Notifier(channels, routing)

    kakao_channel = next((c for c in channels if c.name == "kakao"), None)
    if kakao_channel is not None:
        async def _notify_expiring(_notifier: Notifier = notifier) -> None:
            await _notifier.emit(Event(
                kind=EventKind.KAKAO_TOKEN_EXPIRING, severity="warn",
                title="카카오 토큰 만료 임박",
                body="카카오 리프레시 토큰이 곧 만료됩니다. scripts/kakao_auth.py 로 재인증하세요.",
            ))

        kakao_channel.on_token_expiring = _notify_expiring

    return notifier

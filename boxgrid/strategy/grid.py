"""박스권 하단 그리드 롱 전략의 상태 머신(순수 로직).

거래소·네트워크·저장소를 만지지 않는다. 입력(상태, 시세)을 받아 결정(Decision) 목록을 돌려주고
엔진이 그 결정을 실행한다. 그래서 시계를 고정한 단위 테스트로 모든 분기를 검증할 수 있다.

상태
----
IDLE         추세 필터 미통과. 주문 없음.
ARMED        추세 통과. P1..Pn 지정가 매수 게시.
IN_POSITION  한 단 이상 체결. 일봉 종가 손절 / 틱 단위 익절·재난 손절 감시.
EXITED       손절·전량 익절 후. 쿨다운 뒤 추세 재확인 시 IDLE 로 복귀.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any

import pandas as pd

from ..config import AppConfig
from ..core.models import Position, StopMode
from .levels import Levels, TrendInfo, compute_levels, trend_filter


class State(str, Enum):
    IDLE = "IDLE"
    ARMED = "ARMED"
    IN_POSITION = "IN_POSITION"
    EXITED = "EXITED"


# ---------- 결정 ----------
@dataclass
class PlaceGrid:
    levels: Levels
    reason: str = ""


@dataclass
class CancelGrid:
    reason: str = ""


@dataclass
class SellAll:
    reason: str
    exit_reason: str


@dataclass
class SellPct:
    pct: float
    reason: str
    exit_reason: str


@dataclass
class Transition:
    to: State
    reason: str = ""


@dataclass
class Notify:
    title: str
    body: str
    severity: str = "info"


Decision = PlaceGrid | CancelGrid | SellAll | SellPct | Transition | Notify


# ---------- 상태 ----------
@dataclass
class GridState:
    """영속화되는 전략 상태."""

    state: State = State.IDLE
    levels: Levels | None = None
    orders: dict[int, str] = field(default_factory=dict)      # level -> order_id (열린 주문)
    filled_levels: list[int] = field(default_factory=list)
    position: Position | None = None
    tp1_done: bool = False
    trail_high: float | None = None
    armed_at: str | None = None
    exited_at: str | None = None
    last_daily_ts: str | None = None
    trend: dict[str, Any] = field(default_factory=dict)
    last_price: float | None = None
    pending_confirm: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "levels": self.levels.to_dict() if self.levels else None,
            "orders": {str(k): v for k, v in self.orders.items()},
            "filled_levels": list(self.filled_levels),
            "position": self.position.to_dict() if self.position else None,
            "tp1_done": self.tp1_done, "trail_high": self.trail_high,
            "armed_at": self.armed_at, "exited_at": self.exited_at, "last_daily_ts": self.last_daily_ts,
            "trend": dict(self.trend), "last_price": self.last_price, "pending_confirm": self.pending_confirm,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "GridState":
        if not d:
            return cls()
        gs = cls(state=State(d.get("state", "IDLE")))
        gs.levels = Levels.from_dict(d["levels"]) if d.get("levels") else None
        gs.orders = {int(k): v for k, v in (d.get("orders") or {}).items()}
        gs.filled_levels = [int(x) for x in d.get("filled_levels") or []]
        gs.position = Position.from_dict(d["position"]) if d.get("position") else None
        gs.tp1_done = bool(d.get("tp1_done", False))
        gs.trail_high = d.get("trail_high")
        gs.armed_at = d.get("armed_at")
        gs.exited_at = d.get("exited_at")
        gs.last_daily_ts = d.get("last_daily_ts")
        gs.trend = dict(d.get("trend") or {})
        gs.last_price = d.get("last_price")
        gs.pending_confirm = d.get("pending_confirm")
        return gs

    @property
    def has_position(self) -> bool:
        return self.position is not None and self.position.is_open


# ---------- 판단 ----------
def _trend_dict(t: TrendInfo) -> dict[str, Any]:
    return {"ok": t.ok, "close": t.close, "sma": t.sma, "candle_ts": t.candle_ts, "reason": t.reason}


def evaluate_daily(gs: GridState, daily: pd.DataFrame, cfg: AppConfig, now: datetime) -> list[Decision]:
    """일봉 확정 직후 한 번 호출. 추세 판정, 손절 판정, 게시/회수 결정."""
    out: list[Decision] = []
    trend = trend_filter(daily, cfg.levels.sma_len)
    gs.trend = _trend_dict(trend)
    gs.last_daily_ts = trend.candle_ts or (daily.index[-1].isoformat() if daily is not None and not daily.empty else None)
    close = trend.close

    # 1) 보유 중: 일봉 종가 손절만 본다. 추세 이탈 자체로는 팔지 않는다.
    if gs.state is State.IN_POSITION:
        sl = gs.levels.sl if gs.levels else None
        if sl is not None and close < sl:
            out.append(CancelGrid("일봉 종가 손절"))
            out.append(SellAll(f"일봉 종가 {close:,.0f} < 손절선 {sl:,.0f}", "stop_daily"))
            out.append(Transition(State.EXITED, "일봉 종가 손절"))
        else:
            out.append(Notify("일봉 판정: 보유 유지",
                              f"종가 {close:,.0f} / 손절선 {sl:,.0f} / SMA{cfg.levels.sma_len} {trend.sma:,.0f}"))
        return out

    # 2) 게시 중: 추세 이탈이면 회수. 미체결이면 레벨 갱신.
    if gs.state is State.ARMED:
        if not trend.ok:
            out.append(CancelGrid("추세 필터 이탈"))
            out.append(Transition(State.IDLE, f"종가 {close:,.0f} < SMA {trend.sma:,.0f}"))
            return out
        if cfg.rearm_daily_if_unfilled and not gs.filled_levels and gs.levels is not None and cfg.levels.mode == "dynamic":
            try:
                fresh = compute_levels(daily, cfg.levels, now)
            except ValueError as exc:
                out.append(Notify("레벨 재계산 실패", str(exc), "warn"))
                return out
            if fresh.max_change_pct(gs.levels) >= cfg.levels.rearm_threshold_pct:
                out.append(CancelGrid("레벨 갱신"))
                out.append(PlaceGrid(fresh, "박스 이동으로 레벨 재게시"))
            else:
                out.append(Notify("일봉 판정: 게시 유지", f"종가 {close:,.0f} > SMA {trend.sma:,.0f}, 레벨 변화 미미"))
        else:
            out.append(Notify("일봉 판정: 게시 유지", f"종가 {close:,.0f} > SMA {trend.sma:,.0f}"))
        return out

    # 3) 대기 중(IDLE/EXITED): 쿨다운과 추세를 보고 게시.
    if gs.state is State.EXITED:
        if gs.exited_at:
            cooldown_end = datetime.fromisoformat(gs.exited_at) + timedelta(days=cfg.reentry_cooldown_days)
            if now < cooldown_end:
                out.append(Notify("일봉 판정: 재진입 쿨다운", f"{cooldown_end.isoformat()} 까지 대기"))
                return out
        out.append(Transition(State.IDLE, "쿨다운 종료"))

    if not trend.ok:
        out.append(Notify("일봉 판정: 관망", f"{trend.reason} (종가 {close:,.0f}, SMA {trend.sma:,.0f})"))
        return out
    try:
        lv = compute_levels(daily, cfg.levels, now)
    except ValueError as exc:
        out.append(Notify("레벨 계산 실패", str(exc), "warn"))
        return out
    out.append(PlaceGrid(lv, f"추세 통과: 종가 {close:,.0f} > SMA{cfg.levels.sma_len} {trend.sma:,.0f}"))
    return out


def evaluate_tick(gs: GridState, price: float, cfg: AppConfig, now: datetime) -> list[Decision]:
    """틱마다 호출. 보유 중일 때만 재난 손절·익절·트레일링을 본다."""
    out: list[Decision] = []
    gs.last_price = price
    if gs.state is not State.IN_POSITION or gs.levels is None or not gs.has_position:
        return out
    lv = gs.levels
    disaster = lv.sl * (1.0 - cfg.levels.disaster_pct / 100.0)
    if price <= disaster:
        out.append(CancelGrid("재난 손절"))
        out.append(SellAll(f"현재가 {price:,.0f} <= 재난 손절선 {disaster:,.0f}", "stop_disaster"))
        out.append(Transition(State.EXITED, "재난 손절"))
        return out

    if gs.tp1_done:
        if gs.trail_high is None or price > gs.trail_high:
            gs.trail_high = price
        trail_stop = gs.trail_high * (1.0 - cfg.levels.trail_pct / 100.0)
        if price <= trail_stop:
            out.append(SellAll(f"고점 {gs.trail_high:,.0f} 대비 {cfg.levels.trail_pct}% 되돌림 (현재 {price:,.0f})", "trail"))
            out.append(Transition(State.EXITED, "트레일링 청산"))
        return out

    if price >= lv.tp:
        out.append(CancelGrid("1차 익절"))
        out.append(SellPct(cfg.levels.tp1_pct, f"현재가 {price:,.0f} >= 익절선 {lv.tp:,.0f}", "tp1"))
        gs.tp1_done = True
        gs.trail_high = price
    return out


def on_level_filled(gs: GridState, level: int) -> list[Decision]:
    """지정가 한 단이 체결됐을 때. 첫 체결이면 IN_POSITION 으로."""
    out: list[Decision] = []
    if level not in gs.filled_levels:
        gs.filled_levels.append(level)
    gs.orders.pop(level, None)
    if gs.state is State.ARMED:
        out.append(Transition(State.IN_POSITION, f"P{level} 체결"))
    return out


def apply_transition(gs: GridState, to: State, now: datetime) -> None:
    """상태 전이와 부수 필드 정리."""
    gs.state = to
    if to is State.ARMED:
        gs.armed_at = now.isoformat()
        gs.filled_levels = []
        gs.tp1_done = False
        gs.trail_high = None
        gs.exited_at = None
    elif to is State.EXITED:
        gs.exited_at = now.isoformat()
        gs.orders = {}
    elif to is State.IDLE:
        gs.levels = None
        gs.orders = {}
        gs.filled_levels = []
        gs.tp1_done = False
        gs.trail_high = None
        gs.pending_confirm = None

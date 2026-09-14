"""리스크 가드. 그리드 게시 전 한도를 검사하고 킬 스위치를 관리한다."""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta

import pandas as pd

from ..core.clock import Clock
from ..core.models import Trade

log = logging.getLogger(__name__)

_TF_MINUTES = {
    "1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "2h": 120,
    "4h": 240, "6h": 360, "12h": 720, "1d": 1440,
}


def timeframe_minutes(tf: str) -> int:
    return _TF_MINUTES.get(tf, 60)


@dataclass
class RiskConfig:
    """리스크 한도."""

    max_position_pct: float = 100.0      # 시드 대비 그리드 총 비중 상한
    daily_loss_limit_pct: float = 8.0    # 오늘 실현 손실이 이만큼이면 신규 게시 금지
    max_stops_per_week: int = 2          # 주간 손절 횟수 상한(초과 시 게시 금지)
    min_order_cost: float = 5000.0
    stale_minutes: int = 30              # 일봉 데이터 허용 지연(분)
    kill_file: str = "data/KILL"
    live_first_day_cap_pct: float = 10.0


@dataclass
class Refusal:
    rule: str
    detail: str


class Guard:
    """한도를 강제한다."""

    def __init__(self, cfg: RiskConfig, clock: Clock, live_started_at: datetime | None = None) -> None:
        self.cfg = cfg
        self.clock = clock
        self.live_started_at = live_started_at
        self._killed = False
        self._kill_reason = ""
        self._day = clock.today()
        self._daily_pnl = 0.0
        self._stop_times: list[datetime] = []

    def _roll_day(self) -> None:
        today = self.clock.today()
        if today != self._day:
            self._day = today
            self._daily_pnl = 0.0

    # ---------- 킬 ----------
    def killed(self) -> bool:
        if self._killed:
            return True
        kf = self.cfg.kill_file
        return bool(kf) and os.path.exists(kf)

    def kill(self, reason: str) -> None:
        self._killed = True
        self._kill_reason = reason
        kf = self.cfg.kill_file
        if kf:
            os.makedirs(os.path.dirname(kf) or ".", exist_ok=True)
            with open(kf, "w", encoding="utf-8") as f:
                f.write(f"{self.clock.now().isoformat()} {reason}\n")
        log.error("킬 스위치 작동: %s", reason)

    def unkill(self) -> None:
        self._killed = False
        self._kill_reason = ""
        kf = self.cfg.kill_file
        if kf and os.path.exists(kf):
            os.remove(kf)

    # ---------- 집계 ----------
    def on_trade_closed(self, trade: Trade) -> None:
        self._roll_day()
        self._daily_pnl += trade.pnl
        if trade.exit_reason.startswith("stop"):
            self._stop_times.append(trade.exit_time)

    @property
    def state(self) -> dict:
        week_ago = self.clock.now() - timedelta(days=7)
        return {
            "day": self._day,
            "daily_pnl": self._daily_pnl,
            "stops_this_week": sum(1 for t in self._stop_times if t >= week_ago),
            "killed": self.killed(),
            "kill_reason": self._kill_reason,
        }

    # ---------- 검사 ----------
    def allowed_pct(self) -> float:
        """지금 허용되는 최대 비중(%)."""
        pct = self.cfg.max_position_pct
        now = self.clock.now()
        if self.live_started_at is not None and now < self.live_started_at + timedelta(days=1):
            pct = min(pct, self.cfg.live_first_day_cap_pct)
        return pct

    def check_arm(self, equity: float, total_cost: float, min_level_cost: float) -> Refusal | None:
        """그리드 게시 가능 여부. 통과하면 None."""
        self._roll_day()
        if self.killed():
            return Refusal("kill", f"킬 스위치가 켜져 있습니다. ({self._kill_reason or self.cfg.kill_file})")
        if equity > 0 and self._daily_pnl < 0 and abs(self._daily_pnl) / equity * 100.0 >= self.cfg.daily_loss_limit_pct:
            return Refusal("daily_loss", f"오늘 실현 손실이 한도({self.cfg.daily_loss_limit_pct}%)에 닿았습니다.")
        week_ago = self.clock.now() - timedelta(days=7)
        stops = sum(1 for t in self._stop_times if t >= week_ago)
        if stops >= self.cfg.max_stops_per_week:
            return Refusal("weekly_stops", f"최근 7일 손절 {stops}회로 신규 게시를 막았습니다.")
        if equity > 0 and total_cost / equity * 100.0 > self.allowed_pct() + 1e-6:
            return Refusal("max_position", f"그리드 총액이 허용 비중({self.allowed_pct():.0f}%)을 넘습니다.")
        if min_level_cost < self.cfg.min_order_cost:
            return Refusal("min_order", f"단별 주문 금액 {min_level_cost:,.0f}이 최소 주문 금액({self.cfg.min_order_cost:,.0f}) 미만입니다.")
        return None

    def check_stale(self, candles: pd.DataFrame, timeframe: str, now: datetime) -> Refusal | None:
        """마지막 닫힌 봉이 너무 오래됐는지."""
        if candles is None or candles.empty:
            return Refusal("stale", "캔들 데이터가 없습니다.")
        closed_at = candles.index[-1].to_pydatetime() + timedelta(minutes=timeframe_minutes(timeframe))
        allowance = timeframe_minutes(timeframe) + self.cfg.stale_minutes
        age_min = (now - closed_at).total_seconds() / 60.0
        if age_min > allowance:
            return Refusal("stale", f"마지막 봉이 마감 후 {age_min:.0f}분 지난 데이터입니다. (허용 {allowance}분)")
        return None

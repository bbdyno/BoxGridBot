"""백테스트. 1시간봉을 재생하면서 실제 GridEngine 을 그대로 돌린다.

- 일봉은 1시간봉을 UTC 기준으로 합쳐 만든다(업비트·바이낸스 일봉 = UTC 0시 = 09:00 KST 마감).
- 매 시간봉이 닫힌 시점으로 시계를 옮기고, UTC 0시 봉이 닫히면 일봉 판정을 돌린다.
- 지정가 체결은 봉 저가 <= 지정가, 익절·재난 손절은 봉 종가 기준(보수적).
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import pandas as pd

from ..config import AppConfig
from ..core.clock import FixedClock
from ..core.engine import GridEngine
from ..core.notify_null import NullNotifier
from ..core.store import Store
from ..exchange.paper import PaperExchange
from ..risk.guard import Guard

log = logging.getLogger(__name__)


@dataclass
class BacktestResult:
    start: str
    end: str
    initial_cash: float
    final_equity: float
    return_pct: float
    buy_hold_pct: float
    trades: list[dict[str, Any]] = field(default_factory=list)
    n_fills: int = 0
    max_drawdown_pct: float = 0.0
    days_in_position: int = 0
    days_armed: int = 0
    equity_curve: list[tuple[str, float]] = field(default_factory=list)

    def summary(self) -> str:
        wins = [t for t in self.trades if t["pnl"] > 0]
        by_reason: dict[str, int] = {}
        for t in self.trades:
            by_reason[t["exit_reason"]] = by_reason.get(t["exit_reason"], 0) + 1
        lines = [
            f"구간 {self.start[:10]} ~ {self.end[:10]}",
            f"수익률 {self.return_pct:+.2f}%  (보유 시 {self.buy_hold_pct:+.2f}%)",
            f"최대 낙폭 {self.max_drawdown_pct:.2f}%",
            f"청산 {len(self.trades)}건 (승 {len(wins)}), 매수 체결 {self.n_fills}건",
            f"청산 사유 {by_reason}",
            f"보유 일수 {self.days_in_position} / 게시 일수 {self.days_armed}",
        ]
        return "\n".join(lines)


async def _run(hourly: pd.DataFrame, cfg: AppConfig, warmup_days: int) -> BacktestResult:
    cfg.mode = "paper"
    cfg.op_mode = "auto"
    ex = PaperExchange(hourly, cash=cfg.initial_cash, fee=cfg.fee, slippage=cfg.slippage,
                       quote=cfg.symbol.partition("/")[2] or "KRW")
    clock = FixedClock(hourly.index[0].to_pydatetime())
    guard = Guard(cfg.risk, clock)
    engine = GridEngine(cfg, ex, guard, NullNotifier(), Store(":memory:"), clock)

    start_i = min(len(hourly) - 1, warmup_days * 24)
    ex.seek(start_i)
    curve: list[tuple[str, float]] = []
    peak = cfg.initial_cash
    mdd = 0.0
    days_pos = days_armed = 0
    last_day = None
    for i in range(start_i, len(hourly)):
        ex.seek(i)
        ts = hourly.index[i]
        clock.set((ts + timedelta(hours=1)).to_pydatetime())
        if ts.hour == 0:
            await engine.daily_check()
        await engine.tick()
        eq = ex.equity(cfg.symbol)
        peak = max(peak, eq)
        mdd = max(mdd, (peak - eq) / peak * 100.0)
        day = ts.strftime("%Y-%m-%d")
        if day != last_day:
            last_day = day
            curve.append((day, eq))
            if engine.gs.state.value == "IN_POSITION":
                days_pos += 1
            elif engine.gs.state.value == "ARMED":
                days_armed += 1
    # 마감: 남은 포지션은 평가액으로만 계산(청산하지 않음)
    final_eq = ex.equity(cfg.symbol)
    p0 = float(hourly["close"].iloc[start_i])
    p1 = float(hourly["close"].iloc[-1])
    trades = engine.store.recent_trades(10_000)[::-1]
    return BacktestResult(
        start=hourly.index[start_i].isoformat(), end=hourly.index[-1].isoformat(),
        initial_cash=cfg.initial_cash, final_equity=final_eq,
        return_pct=(final_eq / cfg.initial_cash - 1.0) * 100.0, buy_hold_pct=(p1 / p0 - 1.0) * 100.0,
        trades=trades, n_fills=sum(1 for f in ex.fills if f.side == "buy"), max_drawdown_pct=mdd,
        days_in_position=days_pos, days_armed=days_armed, equity_curve=curve,
    )


def run_backtest(hourly: pd.DataFrame, cfg: AppConfig, warmup_days: int | None = None) -> BacktestResult:
    """동기 진입점."""
    warmup = warmup_days if warmup_days is not None else cfg.levels.sma_len + 5
    return asyncio.run(_run(hourly, cfg, warmup))

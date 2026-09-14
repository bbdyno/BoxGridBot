"""테스트 공용 도우미: 스텁 시세 소스, 일봉 생성기, 엔진 조립."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd

from boxgrid.config import AppConfig, from_dict
from boxgrid.core.clock import FixedClock
from boxgrid.core.engine import GridEngine
from boxgrid.core.notify_null import NullNotifier
from boxgrid.core.store import Store
from boxgrid.exchange.base import Exchange
from boxgrid.exchange.paper import PaperExchange
from boxgrid.risk.guard import Guard

UTC = timezone.utc


def make_daily(n: int = 260, end: datetime | None = None, box_low: float = 100_000.0,
               box_high: float = 110_000.0, box_days: int = 20, rng: float = 2_000.0,
               start_price: float = 50_000.0) -> pd.DataFrame:
    """앞쪽은 완만한 상승, 마지막 box_days 는 박스권. 일봉 index = UTC 00:00."""
    end = end or datetime(2026, 9, 1, tzinfo=UTC)
    idx = pd.date_range(end=end, periods=n, freq="1D", tz="UTC")
    closes = []
    for i in range(n):
        if i < n - box_days:
            closes.append(start_price + (box_low - start_price) * i / max(n - box_days - 1, 1))
        else:
            k = i - (n - box_days)
            closes.append(box_low + (box_high - box_low) * (0.5 + 0.5 * ((-1) ** k) * 0.6))
    close = pd.Series(closes, index=idx, dtype="float64")
    df = pd.DataFrame({
        "open": close - rng * 0.2, "high": close + rng * 0.5, "low": close - rng * 0.5,
        "close": close, "volume": 10.0,
    }, index=idx)
    # 박스 극값을 정확히 맞춘다
    tail = df.index[-box_days:]
    df.loc[tail, "low"] = df.loc[tail, "low"].clip(lower=box_low)
    df.loc[tail, "high"] = df.loc[tail, "high"].clip(upper=box_high)
    df.loc[tail[0], "low"] = box_low
    df.loc[tail[1], "high"] = box_high
    df.index.name = "ts"
    return df


class StubSource(Exchange):
    """가격과 일봉을 테스트가 직접 넣는 시세 소스."""

    id = "stub"

    def __init__(self, daily: pd.DataFrame, price: float) -> None:
        self.daily = daily
        self.price = price

    def fetch_ohlcv(self, symbol, timeframe, limit=200, since=None):
        return self.daily.tail(limit).copy()

    def fetch_price(self, symbol):
        return self.price

    def fetch_balance(self):
        return {}

    def market_info(self, symbol):
        return {"min_cost": 5000.0, "fee": 0.0005, "precision": 8}

    def create_market_buy(self, *a, **k):
        raise NotImplementedError

    def create_market_sell(self, *a, **k):
        raise NotImplementedError

    def create_limit_buy(self, *a, **k):
        raise NotImplementedError

    def cancel_order(self, *a, **k):
        raise NotImplementedError

    def fetch_order(self, *a, **k):
        raise NotImplementedError

    def fetch_open_orders(self, symbol):
        return []

    def append_day(self, close: float, low: float | None = None, high: float | None = None) -> None:
        """다음 일봉을 추가한다."""
        ts = self.daily.index[-1] + pd.Timedelta(days=1)
        low = close - 500 if low is None else low
        high = close + 500 if high is None else high
        row = pd.DataFrame({"open": [close], "high": [high], "low": [low], "close": [close], "volume": [10.0]},
                           index=pd.DatetimeIndex([ts], tz="UTC", name="ts"))
        self.daily = pd.concat([self.daily, row])


def base_cfg(**over) -> AppConfig:
    raw = {
        "mode": "paper", "op_mode": "auto", "symbol": "BTC/KRW", "initial_cash": 10_000_000, "fee": 0.0005,
        "slippage": 0.0, "tick_seconds": 60,
        "levels": {"mode": "dynamic", "box_lookback": 20, "atr_len": 14, "sma_len": 200},
        "risk": {"max_position_pct": 100, "daily_loss_limit_pct": 8, "max_stops_per_week": 2,
                 "min_order_cost": 5000, "stale_minutes": 30, "kill_file": ""},
        "notify": {},
        "dca": {"enabled": True, "base_amount": 20000},
    }
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(raw.get(k), dict):
            raw[k].update(v)
        else:
            raw[k] = v
    return from_dict(raw)


def after_close(daily: pd.DataFrame, seconds: int = 30) -> datetime:
    """마지막 일봉이 확정된 직후 시각."""
    return (daily.index[-1] + pd.Timedelta(days=1) + pd.Timedelta(seconds=seconds)).to_pydatetime()


def build(cfg: AppConfig | None = None, daily: pd.DataFrame | None = None, price: float = 105_000.0,
          store: Store | None = None, exchange: PaperExchange | None = None):
    cfg = cfg or base_cfg()
    daily = daily if daily is not None else make_daily()
    src = StubSource(daily, price)
    ex = exchange or PaperExchange(src, cash=cfg.initial_cash, fee=cfg.fee, slippage=cfg.slippage)
    if exchange is not None:
        ex.price_source = src
    clock = FixedClock(after_close(daily))
    guard = Guard(cfg.risk, clock)
    store = store or Store(":memory:")
    eng = GridEngine(cfg, ex, guard, NullNotifier(), store, clock)
    return eng, src, ex, clock

import numpy as np
import pandas as pd

from boxgrid.backtest.runner import run_backtest
from tests.helpers import base_cfg


def _synthetic_hourly(days=320, seed=7):
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2025-01-01", periods=days * 24, freq="h", tz="UTC")
    # 상승 추세 + 사인 박스 + 잡음
    t = np.arange(len(idx))
    base = 50_000 + t * 8 + 4_000 * np.sin(t / 240.0)
    noise = rng.normal(0, 300, len(idx)).cumsum() * 0.05
    close = base + noise
    high = close + rng.uniform(50, 600, len(idx))
    low = close - rng.uniform(50, 600, len(idx))
    df = pd.DataFrame({"open": close, "high": high, "low": low, "close": close, "volume": 1.0}, index=idx)
    df.index.name = "ts"
    return df


def test_backtest_runs_and_produces_trades():
    cfg = base_cfg(initial_cash=10_000_000)
    res = run_backtest(_synthetic_hourly(), cfg)
    assert res.final_equity > 0
    assert res.n_fills > 0
    assert res.days_armed + res.days_in_position > 0
    assert "수익률" in res.summary()
    assert all(t["exit_reason"] in ("stop_daily", "stop_disaster", "tp1", "trail", "manual", "kill") for t in res.trades)

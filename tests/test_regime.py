import numpy as np
import pandas as pd

from boxgrid.config import LevelConfig
from boxgrid.strategy.levels import compute_levels, pullback_reference
from boxgrid.strategy.regime import detect_regime
from tests.helpers import after_close, base_cfg, build


def _series(tail):
    """넓게 흔들리는 160일 + 좁은 수렴 25일 + tail."""
    rng = np.random.default_rng(3)
    wide = 100_000 + np.cumsum(rng.normal(0, 1500, 160))
    base = wide[-1]
    tight = base + rng.normal(0, 120, 25)
    closes = np.concatenate([wide, tight, tail(base)])
    idx = pd.date_range(end="2026-09-01", periods=len(closes), freq="1D", tz="UTC")
    c = pd.Series(closes, index=idx)
    return pd.DataFrame({"open": c.shift(1).fillna(c), "high": c + 300, "low": c - 300, "close": c, "volume": 1.0}, index=idx)


def test_squeeze_then_breakout_is_expansion():
    up = _series(lambda b: b + np.array([800, 1800, 2600, 3500, 4200]))
    r = detect_regime(up, LevelConfig())
    assert r.name == "expansion" and r.above_mid and r.since


def test_breakdown_or_flat_is_box():
    down = _series(lambda b: b - np.array([800, 1800, 2600, 3500, 4200]))
    assert detect_regime(down, LevelConfig()).name == "box"
    flat = _series(lambda b: b + np.array([10, -20, 15, -5, 0]))
    assert detect_regime(flat, LevelConfig()).name == "box"
    assert detect_regime(flat.tail(30), LevelConfig()).name == "box"  # 데이터 부족


def test_adaptive_levels_anchor_to_recent_high():
    up = _series(lambda b: b + np.array([800, 1800, 2600, 3500, 4200]))
    now = after_close(up)
    lv = compute_levels(up, LevelConfig(mode="adaptive", sma_len=50), now)
    assert lv.regime == "pullback" and lv.anchor_high == float(up["high"].tail(10).max())
    assert lv.anchor_high > lv.prices[0] > lv.prices[-1] > lv.sl and lv.tp > lv.anchor_high
    box = compute_levels(up, LevelConfig(mode="dynamic", sma_len=50), now)
    assert box.regime == "box" and box.prices[0] < lv.prices[0]
    ref = pullback_reference(up, LevelConfig(), now)
    assert ref is not None and ref.prices == lv.prices


async def test_daily_message_shows_regime_line():
    eng, src, ex, clock = build()
    await eng.daily_check()
    body = eng.store.recent_events(kind="DAILY_CHECK")[0]["body"]
    assert "장세:" in body
    assert "regime" in eng.gs.trend

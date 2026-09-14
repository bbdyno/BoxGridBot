import os
from datetime import datetime, timedelta, timezone

import pytest
import yaml

from boxgrid.config import from_dict, load_config
from boxgrid.core.clock import FixedClock
from boxgrid.core.models import Trade
from boxgrid.risk.guard import Guard, RiskConfig


def _trade(pnl, reason, ts):
    return Trade(symbol="BTC/KRW", strategy="g", entry_time=ts, exit_time=ts, entry_price=1, exit_price=1,
                 qty=1, pnl=pnl, pnl_pct=0, fee=0, exit_reason=reason)


def test_guard_limits(tmp_path):
    now = datetime(2026, 9, 1, tzinfo=timezone.utc)
    clock = FixedClock(now)
    g = Guard(RiskConfig(kill_file=str(tmp_path / "KILL"), max_position_pct=50, daily_loss_limit_pct=5), clock)
    assert g.check_arm(1_000_000, 400_000, 100_000) is None
    assert g.check_arm(1_000_000, 600_000, 100_000).rule == "max_position"
    assert g.check_arm(1_000_000, 400_000, 1000).rule == "min_order"
    g.on_trade_closed(_trade(-60_000, "stop_daily", now))
    assert g.check_arm(1_000_000, 400_000, 100_000).rule == "daily_loss"
    g.on_trade_closed(_trade(-60_000, "stop_daily", now))
    clock.set(now + timedelta(days=1))
    assert g.check_arm(1_000_000, 400_000, 100_000).rule == "weekly_stops"
    clock.set(now + timedelta(days=8))
    assert g.check_arm(1_000_000, 400_000, 100_000) is None
    g.kill("test")
    assert g.killed() and os.path.exists(tmp_path / "KILL")
    assert g.check_arm(1_000_000, 400_000, 100_000).rule == "kill"
    g.unkill()
    assert not g.killed()


def test_first_day_cap_live():
    now = datetime(2026, 9, 1, tzinfo=timezone.utc)
    g = Guard(RiskConfig(kill_file="", live_first_day_cap_pct=10), FixedClock(now), live_started_at=now)
    assert g.allowed_pct() == 10
    assert g.check_arm(1_000_000, 200_000, 50_000).rule == "max_position"


def test_config_defaults_and_validation(tmp_path):
    cfg = from_dict({})
    assert cfg.mode == "paper" and cfg.live_lock and cfg.op_mode == "auto" and cfg.n_levels == 4
    with pytest.raises(ValueError):
        from_dict({"op_mode": "yolo"})
    with pytest.raises(ValueError):
        from_dict({"levels": {"mode": "magic"}})
    p = tmp_path / "g.yaml"
    p.write_text(yaml.safe_dump({"mode": "live", "live_lock": False, "levels": {"sma_len": 100}, "unknown": 1}))
    cfg = load_config(str(p))
    assert cfg.is_live and not cfg.live_lock and cfg.levels.sma_len == 100
    assert load_config(str(tmp_path / "missing.yaml")).mode == "paper"


def test_repo_config_loads():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cfg = load_config(os.path.join(root, "config", "grid.yaml"))
    assert cfg.live_lock and cfg.mode == "paper" and cfg.symbol == "BTC/KRW"
    assert cfg.levels.weights_pct == [20, 20, 20, 40]

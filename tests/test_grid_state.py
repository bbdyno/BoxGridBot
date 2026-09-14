"""상태 머신 순수 로직."""
from datetime import timedelta

from boxgrid.strategy import grid as G
from boxgrid.strategy.levels import compute_levels
from boxgrid.core.models import Fill, Position
from tests.helpers import after_close, base_cfg, make_daily


def _armed_state(cfg, daily):
    gs = G.GridState()
    now = after_close(daily)
    lv = compute_levels(daily, cfg.levels, now)
    gs.levels = lv
    G.apply_transition(gs, G.State.ARMED, now)
    gs.orders = {1: "o1", 2: "o2", 3: "o3", 4: "o4"}
    return gs, lv, now


def _with_position(gs, lv, now):
    gs.position = Position(symbol="BTC/KRW", stop=lv.sl, take=lv.tp)
    gs.position.add_fill(Fill(symbol="BTC/KRW", side="buy", qty=1.0, price=lv.prices[0], cost=lv.prices[0],
                              fee=0.0, ts=now, mode="paper", level=1))
    for d in G.on_level_filled(gs, 1):
        G.apply_transition(gs, d.to, now)
    assert gs.state is G.State.IN_POSITION


def test_idle_to_armed_when_trend_ok():
    cfg = base_cfg()
    daily = make_daily()
    gs = G.GridState()
    out = G.evaluate_daily(gs, daily, cfg, after_close(daily))
    assert any(isinstance(d, G.PlaceGrid) for d in out)
    assert gs.trend["ok"]


def test_idle_stays_when_trend_broken():
    cfg = base_cfg()
    daily = make_daily(start_price=200_000.0)  # 하락 추세: 종가 < SMA
    gs = G.GridState()
    out = G.evaluate_daily(gs, daily, cfg, after_close(daily))
    assert not any(isinstance(d, G.PlaceGrid) for d in out)
    assert not gs.trend["ok"]


def test_daily_close_stop_only_on_close_below_sl():
    cfg = base_cfg()
    daily = make_daily()
    gs, lv, now = _armed_state(cfg, daily)
    _with_position(gs, lv, now)
    # 장중에 SL 아래로 찍어도(틱) 팔지 않는다
    intraday = lv.sl - 1.0
    assert G.evaluate_tick(gs, intraday, cfg, now) == []
    assert gs.state is G.State.IN_POSITION
    # 종가가 SL 위로 마감 → 유지
    import pandas as pd
    ts = daily.index[-1] + pd.Timedelta(days=1)
    row = pd.DataFrame({"open": [lv.sl + 100], "high": [lv.sl + 500], "low": [lv.sl - 3000], "close": [lv.sl + 100],
                        "volume": [1.0]}, index=pd.DatetimeIndex([ts], tz="UTC"))
    d2 = pd.concat([daily, row])
    out = G.evaluate_daily(gs, d2, cfg, after_close(d2))
    assert not any(isinstance(d, G.SellAll) for d in out)
    # 종가가 SL 아래로 확정 → 전량 손절 + EXITED
    row2 = row.copy()
    row2.index = pd.DatetimeIndex([ts + pd.Timedelta(days=1)], tz="UTC")
    row2["close"] = lv.sl - 1.0
    d3 = pd.concat([d2, row2])
    out = G.evaluate_daily(gs, d3, cfg, after_close(d3))
    kinds = [type(d) for d in out]
    assert G.CancelGrid in kinds and G.SellAll in kinds and G.Transition in kinds
    sell = next(d for d in out if isinstance(d, G.SellAll))
    assert sell.exit_reason == "stop_daily"


def test_disaster_stop_intraday():
    cfg = base_cfg()
    daily = make_daily()
    gs, lv, now = _armed_state(cfg, daily)
    _with_position(gs, lv, now)
    disaster = lv.sl * (1 - cfg.levels.disaster_pct / 100)
    out = G.evaluate_tick(gs, disaster - 1, cfg, now)
    sell = next(d for d in out if isinstance(d, G.SellAll))
    assert sell.exit_reason == "stop_disaster"


def test_tp1_then_trailing():
    cfg = base_cfg()
    daily = make_daily()
    gs, lv, now = _armed_state(cfg, daily)
    _with_position(gs, lv, now)
    out = G.evaluate_tick(gs, lv.tp + 1, cfg, now)
    part = next(d for d in out if isinstance(d, G.SellPct))
    assert part.pct == 50.0 and gs.tp1_done and gs.trail_high == lv.tp + 1
    assert any(isinstance(d, G.CancelGrid) for d in out)
    # 고점 갱신
    G.evaluate_tick(gs, lv.tp * 1.10, cfg, now)
    assert gs.trail_high == lv.tp * 1.10
    # 3% 되돌림 전에는 유지
    assert G.evaluate_tick(gs, lv.tp * 1.10 * 0.98, cfg, now) == []
    out = G.evaluate_tick(gs, lv.tp * 1.10 * 0.969, cfg, now)
    sell = next(d for d in out if isinstance(d, G.SellAll))
    assert sell.exit_reason == "trail"


def test_armed_cancels_on_trend_loss_and_rearms_on_box_move():
    cfg = base_cfg()
    daily = make_daily()
    gs, lv, now = _armed_state(cfg, daily)
    # 박스가 크게 이동한 새 일봉들 → 미체결이면 재게시
    import pandas as pd
    rows = []
    for k in range(5):
        ts = daily.index[-1] + pd.Timedelta(days=k + 1)
        c = 120_000 + k * 500
        rows.append(pd.DataFrame({"open": [c], "high": [c + 1000], "low": [c - 1000], "close": [c], "volume": [1.0]},
                                 index=pd.DatetimeIndex([ts], tz="UTC")))
    d2 = pd.concat([daily, *rows])
    out = G.evaluate_daily(gs, d2, cfg, after_close(d2))
    assert any(isinstance(d, G.CancelGrid) for d in out) and any(isinstance(d, G.PlaceGrid) for d in out)
    # 추세 이탈 → 회수 + IDLE
    crash = pd.DataFrame({"open": [30_000.0], "high": [31_000.0], "low": [29_000.0], "close": [30_000.0], "volume": [1.0]},
                         index=pd.DatetimeIndex([d2.index[-1] + pd.Timedelta(days=1)], tz="UTC"))
    d3 = pd.concat([d2, crash])
    out = G.evaluate_daily(gs, d3, cfg, after_close(d3))
    assert any(isinstance(d, G.CancelGrid) for d in out)
    tr = next(d for d in out if isinstance(d, G.Transition))
    assert tr.to is G.State.IDLE


def test_exited_respects_cooldown_then_rearms():
    cfg = base_cfg(reentry_cooldown_days=2)
    daily = make_daily()
    gs = G.GridState()
    now = after_close(daily)
    G.apply_transition(gs, G.State.EXITED, now)
    out = G.evaluate_daily(gs, daily, cfg, now + timedelta(days=1))
    assert not any(isinstance(d, G.PlaceGrid) for d in out)
    out = G.evaluate_daily(gs, daily, cfg, now + timedelta(days=2, seconds=1))
    assert any(isinstance(d, G.PlaceGrid) for d in out)


def test_state_roundtrip():
    cfg = base_cfg()
    daily = make_daily()
    gs, lv, now = _armed_state(cfg, daily)
    _with_position(gs, lv, now)
    gs.tp1_done = True
    gs.trail_high = 123.0
    again = G.GridState.from_dict(gs.to_dict())
    assert again.state is G.State.IN_POSITION
    assert again.levels.sl == lv.sl and again.position.qty == 1.0 and again.trail_high == 123.0
    assert again.filled_levels == [1] and 1 not in again.orders

import pytest

from boxgrid.config import LevelConfig
from boxgrid.strategy.levels import closed_daily, compute_levels, plan_orders, trend_filter
from tests.helpers import after_close, make_daily


def test_trend_filter_uses_sma200():
    daily = make_daily()
    t = trend_filter(daily, 200)
    assert t.ok and t.close > t.sma
    short = daily.tail(100)
    assert not trend_filter(short, 200).ok


def test_dynamic_levels_follow_box_and_atr():
    daily = make_daily()
    cfg = LevelConfig()
    lv = compute_levels(daily, cfg, after_close(daily))
    assert lv.box_low == 100_000.0 and lv.box_high == 110_000.0
    assert lv.prices[0] > lv.prices[1] > lv.prices[2] > lv.prices[3]
    assert lv.prices[1] == pytest.approx(lv.box_low)
    assert lv.sl == pytest.approx(lv.box_low - 2.0 * lv.atr)
    assert lv.tp == pytest.approx(lv.box_high + 0.25 * lv.atr)
    assert lv.mode == "dynamic"


def test_fixed_levels_and_validation():
    daily = make_daily()
    cfg = LevelConfig(mode="fixed", fixed={"p1": 79, "p2": 77.5, "p3": 76.5, "p4": 75.7, "sl": 73.6, "tp": 82.5})
    lv = compute_levels(daily, cfg, after_close(daily))
    assert lv.prices == [79, 77.5, 76.5, 75.7] and lv.sl == 73.6 and lv.mode == "fixed"
    bad = LevelConfig(mode="fixed", fixed={"p1": 79, "p2": 77.5, "p3": 76.5, "p4": 75.7, "sl": 76, "tp": 82.5})
    with pytest.raises(ValueError):
        compute_levels(daily, bad, after_close(daily))


def test_plan_orders_splits_seed():
    daily = make_daily()
    lv = compute_levels(daily, LevelConfig(), after_close(daily))
    plan = plan_orders(lv, 1_000_000, 5000)
    assert [p["cost"] for p in plan] == [200_000, 200_000, 200_000, 400_000]
    assert all(p["qty"] * p["price"] == pytest.approx(p["cost"]) for p in plan)
    with pytest.raises(ValueError):
        plan_orders(lv, 10_000, 5000)


def test_closed_daily_excludes_forming_candle():
    daily = make_daily()
    now_mid = (daily.index[-1] + __import__("pandas").Timedelta(hours=12)).to_pydatetime()
    assert len(closed_daily(daily, now_mid)) == len(daily) - 1
    assert len(closed_daily(daily, after_close(daily))) == len(daily)


def test_weights_must_sum_to_100():
    with pytest.raises(ValueError):
        LevelConfig(weights_pct=[50, 50, 50, 50])


def test_clamp_below_price_keeps_order():
    from boxgrid.strategy.levels import clamp_below_price
    daily = make_daily()
    lv = compute_levels(daily, LevelConfig(), after_close(daily))
    # 현재가가 P1, P2 보다 낮으면 둘 다 현재가 아래로 내려가고 순서가 유지된다
    price = lv.prices[1] - 10
    out, adj = clamp_below_price(lv, price, 0.3)
    assert adj == [1, 2]
    assert out.prices[0] < price and out.prices[0] > out.prices[1] > out.prices[2] == lv.prices[2]
    assert out.sl == lv.sl and out.tp == lv.tp
    # 현재가가 모든 레벨 위면 그대로
    same, adj = clamp_below_price(lv, lv.prices[0] + 1000, 0.3)
    assert adj == [] and same is lv
    # 현재가가 손절선 근처면 순서가 깨져 ValueError
    with pytest.raises(ValueError):
        clamp_below_price(lv, lv.sl + 10, 0.3)

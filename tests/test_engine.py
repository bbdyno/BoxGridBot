"""엔진 통합: 페이퍼 거래소 + 스텁 시세 + 고정 시계."""
import pandas as pd
import pytest

from boxgrid.core.store import Store
from boxgrid.exchange.paper import PaperExchange
from boxgrid.strategy import grid as G
from tests.helpers import after_close, base_cfg, build, make_daily


async def _arm(eng, src, clock):
    await eng.daily_check()
    assert eng.gs.state is G.State.ARMED
    assert len(eng.gs.orders) == 4
    return eng.gs.levels


async def test_daily_check_arms_and_fills_flow():
    eng, src, ex, clock = build()
    lv = await _arm(eng, src, clock)
    assert len(ex.fetch_open_orders("BTC/KRW")) == 4
    kinds = [e["kind"] for e in eng.store.recent_events()]
    assert "GRID" in kinds and "DAILY_CHECK" in kinds
    # 같은 일봉은 두 번 판정하지 않는다
    before = len(eng.store.recent_events())
    await eng.daily_check()
    assert len(eng.store.recent_events()) == before
    # P1 도달 → 체결 → IN_POSITION
    src.price = lv.prices[0] - 10
    await eng.tick()
    assert eng.gs.state is G.State.IN_POSITION
    assert eng.gs.filled_levels == [1] and len(eng.gs.orders) == 3
    assert eng.gs.position.qty > 0
    assert eng.store.recent_fills()[0]["level"] == 1
    # P2 도달 → 평단 하락
    ep1 = eng.gs.position.entry_price
    src.price = lv.prices[1] - 10
    await eng.tick()
    assert eng.gs.filled_levels == [1, 2] and eng.gs.position.entry_price < ep1


async def test_intraday_below_sl_does_not_sell_but_daily_close_does():
    eng, src, ex, clock = build()
    lv = await _arm(eng, src, clock)
    src.price = lv.prices[0] - 10
    await eng.tick()
    assert eng.gs.state is G.State.IN_POSITION
    src.price = lv.sl - 100  # 장중 SL 이탈(재난 손절선 위)
    assert src.price > lv.sl * 0.97
    await eng.tick()
    assert eng.gs.state is G.State.IN_POSITION and eng.gs.position is not None
    assert eng.store.recent_trades() == []
    # 종가 SL 위로 마감 → 유지
    src.append_day(close=lv.sl + 50, low=lv.sl - 2000)
    clock.set(after_close(src.daily))
    await eng.daily_check()
    assert eng.gs.state is G.State.IN_POSITION
    # 종가 SL 아래로 확정 → 전량 청산, 남은 주문 취소, EXITED
    src.append_day(close=lv.sl - 50)
    src.price = lv.sl - 50
    clock.set(after_close(src.daily))
    await eng.daily_check()
    assert eng.gs.state is G.State.EXITED
    assert eng.gs.position is None and eng.gs.orders == {}
    assert ex.fetch_open_orders("BTC/KRW") == []
    trades = eng.store.recent_trades()
    assert len(trades) == 1 and trades[0]["exit_reason"] == "stop_daily" and trades[0]["pnl"] < 0
    # 쿨다운 후 추세 재확인 시 다시 게시된다(다음 날 종가가 SMA 위)
    src.append_day(close=105_000)
    clock.set(after_close(src.daily))
    await eng.daily_check()
    assert eng.gs.state is G.State.ARMED


async def test_disaster_stop_and_tp_trailing():
    eng, src, ex, clock = build()
    lv = await _arm(eng, src, clock)
    src.price = lv.prices[0] - 10
    await eng.tick()
    src.price = lv.sl * 0.97 - 1
    await eng.tick()
    assert eng.gs.state is G.State.EXITED
    assert eng.store.recent_trades()[0]["exit_reason"] == "stop_disaster"

    eng, src, ex, clock = build()
    lv = await _arm(eng, src, clock)
    src.price = lv.prices[0] - 10
    await eng.tick()
    qty = eng.gs.position.qty
    src.price = lv.tp + 1
    await eng.tick()
    assert eng.gs.position.qty == pytest.approx(qty / 2)
    assert eng.gs.orders == {}  # 1차 익절 시 남은 그리드 회수
    assert eng.store.recent_trades()[0]["exit_reason"] == "tp1"
    src.price = lv.tp * 1.1
    await eng.tick()
    src.price = lv.tp * 1.1 * 0.96
    await eng.tick()
    assert eng.gs.state is G.State.EXITED and eng.gs.position is None
    assert eng.store.recent_trades()[0]["exit_reason"] == "trail"


async def test_reconcile_picks_up_fills_while_down_and_restores_paper():
    store = Store(":memory:")
    eng, src, ex, clock = build(store=store)
    lv = await _arm(eng, src, clock)
    # 봇이 죽은 사이 체결
    src.price = lv.prices[1] - 10
    ex.sync("BTC/KRW")
    # 같은 저장소·같은 거래소 객체로 재시작
    eng2, src2, ex2, clock2 = build(store=store, exchange=ex, price=src.price)
    assert eng2.gs.state is G.State.ARMED
    await eng2.reconcile()
    assert eng2.gs.state is G.State.IN_POSITION
    assert sorted(eng2.gs.filled_levels) == [1, 2] and len(eng2.gs.orders) == 2
    # 페이퍼 거래소가 새로 만들어져도 저장본에서 잔고·주문장이 복원된다
    eng3, src3, ex3, clock3 = build(store=store, price=src.price)
    assert ex3.balances["BTC"] == pytest.approx(ex.balances["BTC"])
    assert len(ex3.fetch_open_orders("BTC/KRW")) == 2


async def test_signal_mode_alerts_without_orders():
    eng, src, ex, clock = build(cfg=base_cfg(op_mode="signal"))
    await eng.daily_check()
    assert eng.gs.state is G.State.ARMED and eng.gs.orders == {}
    assert ex.fetch_open_orders("BTC/KRW") == []
    grid_ev = eng.store.recent_events(kind="GRID")[0]
    assert "[판단]" in grid_ev["title"]
    src.price = eng.gs.levels.prices[0] - 1
    await eng.tick()
    touch = eng.store.recent_events(kind="LEVEL_TOUCH")
    assert len(touch) == 1 and "P1" in touch[0]["title"]
    await eng.tick()
    assert len(eng.store.recent_events(kind="LEVEL_TOUCH")) == 1  # 중복 알림 없음


async def test_live_lock_blocks_trading():
    eng, src, ex, clock = build(cfg=base_cfg(mode="live", live_lock=True))
    assert not eng.trading_enabled
    await eng.daily_check()
    assert eng.gs.state is G.State.ARMED and eng.gs.orders == {}
    eng2, *_ = build(cfg=base_cfg(mode="live", live_lock=False))
    assert not eng2.trading_enabled
    eng2.confirm_live()
    assert eng2.trading_enabled


async def test_confirm_mode_waits_for_button_and_times_out():
    from datetime import timedelta

    eng, src, ex, clock = build(cfg=base_cfg(op_mode="confirm", confirm_timeout_min=30))
    await eng.daily_check()
    assert eng.gs.state is G.State.IDLE and eng.gs.pending_confirm
    assert eng.store.recent_events(kind="ARM_CONFIRM")
    msg = await eng.confirm_arm()
    assert "ARMED" in msg and len(eng.gs.orders) == 4
    # 타임아웃
    eng, src, ex, clock = build(cfg=base_cfg(op_mode="confirm", confirm_timeout_min=30))
    await eng.daily_check()
    clock.set(clock.now() + timedelta(minutes=31))
    await eng.tick()
    assert not eng.gs.pending_confirm


async def test_manual_commands():
    eng, src, ex, clock = build()
    msg = await eng.arm()
    assert eng.gs.state is G.State.ARMED and "ARMED" in msg
    assert "이미" in await eng.arm()
    msg = await eng.disarm()
    assert eng.gs.state is G.State.IDLE and ex.fetch_open_orders("BTC/KRW") == []
    assert eng.set_mode("confirm").startswith("운용 모드")
    assert "하나" in eng.set_mode("weird")
    assert "p1 = 79,000" in eng.set_fixed_level("p1", 79000, 20)
    assert eng.cfg.levels.mode == "fixed"
    assert "키는" in eng.set_fixed_level("p9", 1)
    assert "레벨 계산 실패" in await eng.arm()  # p2.. 미입력
    for k, v in {"p2": 100000, "p3": 99000, "p4": 98000, "sl": 96000, "tp": 111000}.items():
        eng.set_fixed_level(k, v)
    eng.set_fixed_level("p1", 101000)
    await eng.arm()
    assert eng.gs.levels.mode == "fixed" and eng.gs.levels.sl == 96000
    src.price = eng.gs.levels.prices[0] - 1
    await eng.tick()
    await eng.close_all("테스트")
    assert eng.gs.state is G.State.EXITED and eng.gs.position is None
    st = eng.status()
    assert st["state"] == "EXITED" and st["trading_enabled"]
    assert "SL" in eng.levels_text()


async def test_guard_refuses_after_weekly_stops():
    eng, src, ex, clock = build(cfg=base_cfg(risk={"max_stops_per_week": 1}))
    lv = await _arm(eng, src, clock)
    src.price = lv.prices[0] - 10
    await eng.tick()
    src.price = lv.sl * 0.9
    await eng.tick()
    assert eng.gs.state is G.State.EXITED
    src.append_day(close=105_000)
    src.append_day(close=105_000)
    src.price = 105_000.0
    clock.set(after_close(src.daily))
    await eng.daily_check()
    assert eng.gs.state is G.State.IDLE
    assert eng.store.recent_events(kind="REFUSAL")[0]["title"].endswith("[weekly_stops]")


async def test_stale_daily_blocks_check():
    from datetime import timedelta

    eng, src, ex, clock = build()
    clock.set(clock.now() + timedelta(days=3))
    await eng.daily_check()
    assert eng.gs.state is G.State.IDLE
    assert eng.store.recent_events(kind="ERROR")[0]["title"] == "일봉 데이터 지연"


async def test_place_grid_never_above_current_price():
    eng, src, ex, clock = build(price=99_500.0)  # P1(≈103k), P2(100k) 보다 낮은 현재가
    await eng.daily_check()
    assert eng.gs.state is G.State.ARMED and len(eng.gs.orders) == 4
    for o in ex.fetch_open_orders("BTC/KRW"):
        assert o.price < 99_500.0
    assert "조정한 단: P1, P2" in eng.store.recent_events(kind="GRID")[0]["body"]
    # 즉시 체결되지 않았다
    await eng.tick()
    assert eng.gs.state is G.State.ARMED and eng.gs.filled_levels == []

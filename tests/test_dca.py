import pandas as pd

from boxgrid.config import DcaConfig
from boxgrid.strategy.dca import dca_verdict
from tests.helpers import base_cfg, build, make_daily


def test_discount_tiers():
    daily = make_daily()
    sma = float(daily["close"].tail(200).mean())
    cfg = DcaConfig(base_amount=20_000)
    above = dca_verdict(daily, sma * 1.05, cfg)
    assert above.multiplier == 0 and above.grade == "적립만" and above.discount_pct > 0
    small = dca_verdict(daily, sma * 0.95, cfg)
    assert small.multiplier == 0.5 and small.grade == "조금 더" and small.extra_amount == 10_000
    mid = dca_verdict(daily, sma * 0.85, cfg)
    assert mid.multiplier == 1.0 and mid.grade == "더 담기"
    deep = dca_verdict(daily, sma * 0.75, cfg)
    assert deep.multiplier >= 2.0 and deep.grade == "많이 담기" and any("여유 자금" in r for r in deep.reasons)
    assert "싸다" in deep.headline(20_000, lambda x: f"{x:,.0f}원")


def test_rsi_bonus_and_cap():
    daily = make_daily()
    # 마지막 30일을 매일 1.5% 씩 떨어뜨려 RSI 를 과매도로 만든다
    idx = daily.index[-30:]
    c = float(daily["close"].iloc[-31])
    for ts in idx:
        c *= 0.985
        daily.loc[ts, ["open", "high", "low", "close"]] = [c * 1.005, c * 1.01, c * 0.99, c]
    sma = float(daily["close"].tail(200).mean())
    v = dca_verdict(daily, sma * 0.75, DcaConfig(max_multiplier=2.2))
    assert v.rsi < 35 and v.multiplier == 2.2  # 2.0 + 0.5 → 상한 2.2
    v2 = dca_verdict(daily, sma * 0.75, DcaConfig(rsi_bonus=0.0))
    assert v2.multiplier == 2.0


def test_insufficient_data():
    v = dca_verdict(make_daily(n=50), 100.0, DcaConfig())
    assert v.grade == "데이터 부족" and v.multiplier == 0


async def test_daily_message_contains_dca_section_and_command():
    eng, src, ex, clock = build(cfg=base_cfg(dca={"enabled": True, "base_amount": 20000}))
    await eng.daily_check()
    body = eng.store.recent_events(kind="DAILY_CHECK")[0]["body"]
    assert "적립 추가 매수 지표" in body and "200일 평균보다" in body
    txt = eng.dca_now()
    assert "현재가" in txt and "RSI" in txt

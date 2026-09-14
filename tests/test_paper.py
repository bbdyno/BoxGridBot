import pandas as pd
import pytest

from boxgrid.exchange.paper import PaperExchange, resample_daily
from tests.helpers import StubSource, make_daily


def test_limit_buy_reserves_and_fills_on_price():
    src = StubSource(make_daily(), price=105_000.0)
    ex = PaperExchange(src, cash=1_000_000, fee=0.0, slippage=0.0)
    o = ex.create_limit_buy("BTC/KRW", 100_000.0, 2.0, {"level": 1})
    assert o.is_open and ex.fetch_balance()["KRW"] == 800_000
    assert ex.sync("BTC/KRW") == []
    src.price = 99_000.0
    fills = ex.sync("BTC/KRW")
    assert len(fills) == 1 and fills[0].price == 100_000.0 and fills[0].level == 1
    assert ex.fetch_order(o.id, "BTC/KRW").status == "closed"
    assert ex.balances["BTC"] == pytest.approx(2.0) and ex.balances["KRW"] == 800_000
    assert ex.fetch_open_orders("BTC/KRW") == []


def test_cancel_releases_reserve_and_insufficient_cash():
    src = StubSource(make_daily(), price=105_000.0)
    ex = PaperExchange(src, cash=300_000, fee=0.0, slippage=0.0)
    o = ex.create_limit_buy("BTC/KRW", 100_000.0, 2.0, {})
    with pytest.raises(ValueError):
        ex.create_limit_buy("BTC/KRW", 100_000.0, 2.0, {})
    ex.cancel_order(o.id, "BTC/KRW")
    assert ex.fetch_balance()["KRW"] == 300_000
    assert ex.fetch_order(o.id, "BTC/KRW").status == "canceled"


def test_replay_fills_on_bar_low_and_daily_resample():
    idx = pd.date_range("2026-01-01", periods=72, freq="h", tz="UTC")
    close = pd.Series([100.0] * 72, index=idx)
    df = pd.DataFrame({"open": close, "high": close + 1, "low": close - 1, "close": close, "volume": 1.0}, index=idx)
    df.loc[idx[40], "low"] = 90.0
    df.loc[idx[40], "open"] = 95.0
    ex = PaperExchange(df, cash=10_000, fee=0.0, slippage=0.0)
    ex.seek(10)
    o = ex.create_limit_buy("BTC/KRW", 92.0, 10.0, {})
    ex.seek(39)
    assert ex.sync("BTC/KRW") == []
    ex.seek(40)
    fills = ex.sync("BTC/KRW")
    assert len(fills) == 1 and fills[0].price == 92.0  # 시가 95 > 지정가 92 → 지정가 체결
    daily = ex.fetch_ohlcv("BTC/KRW", "1d", limit=10)
    assert len(daily) == 2  # 커서(40시간째)까지 → 2일치(두 번째는 미완성)
    assert daily["low"].iloc[1] == 90.0
    assert len(resample_daily(df)) == 3

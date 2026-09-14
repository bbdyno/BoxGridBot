from datetime import datetime, timezone

from boxgrid.core.models import Fill, Position, StopMode


def _fill(qty, price, level, fee=0.0):
    return Fill(symbol="BTC/KRW", side="buy", qty=qty, price=price, cost=qty * price, fee=fee,
                ts=datetime(2026, 1, 1, tzinfo=timezone.utc), mode="paper", level=level)


def test_position_accumulates_and_averages():
    pos = Position(symbol="BTC/KRW")
    assert not pos.is_open
    pos.add_fill(_fill(1.0, 100.0, 1))
    pos.add_fill(_fill(1.0, 90.0, 2))
    assert pos.qty == 2.0
    assert pos.entry_price == 95.0
    assert [f["level"] for f in pos.fills] == [1, 2]
    assert pos.stop_mode is StopMode.DAILY_CLOSE


def test_partial_remove_scales_cost():
    pos = Position(symbol="BTC/KRW")
    pos.add_fill(_fill(2.0, 100.0, 1, fee=2.0))
    cost, fee = pos.remove_qty(1.0)
    assert cost == 100.0 and fee == 1.0
    assert pos.qty == 1.0 and pos.cost == 100.0 and pos.entry_price == 100.0
    pos.remove_qty(5.0)
    assert not pos.is_open and pos.cost == 0.0


def test_roundtrip_dict():
    pos = Position(symbol="BTC/KRW", stop=90.0, take=120.0)
    pos.add_fill(_fill(1.0, 100.0, 1))
    again = Position.from_dict(pos.to_dict())
    assert again.qty == 1.0 and again.stop == 90.0 and again.entry_time == pos.entry_time

"""모의(페이퍼) 거래소. 잔고와 지정가 주문장을 스스로 관리한다.

가격 출처는 두 가지다.
- Exchange: 공개 API 로 현재가·캔들을 읽어오는 실시간 모드
- DataFrame: CSV 를 재생하는 오프라인 모드 (`advance()` 로 커서를 옮긴다)

지정가 매수는 `sync()` 가 호출될 때 체결 여부를 판정한다.
- 실시간 모드: 현재가 <= 지정가
- 재생 모드: 현재 봉의 저가 <= 지정가 (체결가는 지정가, 시가가 더 낮으면 시가)
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

import pandas as pd

from ..core.models import Fill, Order
from .base import Exchange, split_symbol

log = logging.getLogger(__name__)


_AGG = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}


def resample_daily(df: pd.DataFrame) -> pd.DataFrame:
    """봉 데이터를 UTC 일봉으로 합친다(라벨 = 일 시작). 마지막 미완성 일봉도 포함한다(호출자가 잘라낸다)."""
    return df.resample("1D", label="left", closed="left").agg(_AGG).dropna(subset=["open"])


class PaperExchange(Exchange):
    """실제 주문 없이 체결을 흉내 내는 거래소."""

    id = "paper"

    def __init__(
        self,
        price_source: Exchange | pd.DataFrame,
        cash: float = 1_000_000.0,
        fee: float = 0.0005,
        slippage: float = 0.001,
        quote: str = "KRW",
    ) -> None:
        self.price_source = price_source
        self.fee = float(fee)
        self.slippage = float(slippage)
        self.balances: dict[str, float] = {quote: float(cash)}
        self.reserved: dict[str, float] = {}   # 지정가에 묶인 견적통화
        self.fills: list[Fill] = []
        self.orders: dict[str, Order] = {}
        self._cursor = 0
        if isinstance(price_source, pd.DataFrame):
            self._df = price_source
            self._cursor = 0
        else:
            self._df = None

    # ---------- 오프라인 재생 ----------
    @property
    def replaying(self) -> bool:
        return self._df is not None

    def advance(self, steps: int = 1) -> bool:
        """재생 커서를 옮긴다. 끝에 닿으면 False."""
        if self._df is None:
            return False
        if self._cursor + steps >= len(self._df):
            self._cursor = len(self._df) - 1
            return False
        self._cursor += steps
        return True

    def seek(self, idx: int) -> None:
        if self._df is not None:
            self._cursor = max(0, min(idx, len(self._df) - 1))

    # ---------- 조회 ----------
    def fetch_ohlcv(
        self, symbol: str, timeframe: str, limit: int = 200, since: datetime | None = None
    ) -> pd.DataFrame:
        if self._df is not None:
            df = self._df.iloc[: self._cursor + 1]
            if timeframe == "1d" and self._step() < pd.Timedelta(days=1):
                df = resample_daily(df)
            return df.tail(limit).copy()
        return self.price_source.fetch_ohlcv(symbol, timeframe, limit=limit, since=since)

    def _step(self) -> pd.Timedelta:
        if self._df is None or len(self._df) < 2:
            return pd.Timedelta(days=1)
        return pd.Timedelta(self._df.index[1] - self._df.index[0])

    def fetch_ohlcv_range(self, symbol, timeframe, start, end=None) -> pd.DataFrame:
        if self._df is not None:
            df = self._df.iloc[: self._cursor + 1]
            return df.loc[df.index >= start].copy()
        return self.price_source.fetch_ohlcv_range(symbol, timeframe, start, end)

    def fetch_price(self, symbol: str) -> float:
        if self._df is not None:
            return float(self._df["close"].iloc[self._cursor])
        return float(self.price_source.fetch_price(symbol))

    def current_bar(self) -> pd.Series | None:
        if self._df is None:
            return None
        return self._df.iloc[self._cursor]

    def fetch_balance(self) -> dict[str, float]:
        """가용 잔고(지정가에 묶인 금액 제외)."""
        out = dict(self.balances)
        for k, v in self.reserved.items():
            out[k] = out.get(k, 0.0) - v
        return out

    def market_info(self, symbol: str) -> dict:
        return {"min_cost": 5000.0, "fee": self.fee, "precision": 8}

    def _now(self) -> datetime:
        if self._df is not None:
            return self._df.index[self._cursor].to_pydatetime()
        return datetime.now(timezone.utc)

    # ---------- 시장가 ----------
    def create_market_buy(self, symbol: str, cost: float, meta: dict) -> Fill:
        base, quote = split_symbol(symbol)
        cost = float(cost)
        if self.fetch_balance().get(quote, 0.0) + 1e-6 < cost:
            raise ValueError(f"{quote} 잔고 부족: 가용 {self.fetch_balance().get(quote, 0.0):,.0f}, 필요 {cost:,.0f}")
        price = self.fetch_price(symbol) * (1.0 + self.slippage)
        return self._settle_buy(symbol, price, cost, meta, order_id=f"paper-m-{uuid.uuid4().hex[:8]}")

    def _settle_buy(self, symbol: str, price: float, cost: float, meta: dict, order_id: str) -> Fill:
        base, quote = split_symbol(symbol)
        fee_amt = cost * self.fee
        qty = (cost - fee_amt) / price
        self.balances[quote] = self.balances.get(quote, 0.0) - cost
        self.balances[base] = self.balances.get(base, 0.0) + qty
        fill = Fill(
            symbol=symbol, side="buy", qty=qty, price=price, cost=cost, fee=fee_amt,
            ts=self._now(), mode="paper", strategy=str(meta.get("strategy", "")),
            reason=str(meta.get("reason", "")), order_id=order_id, level=meta.get("level"),
        )
        self.fills.append(fill)
        log.info("[페이퍼] 매수 %s %.8f @ %.0f (%.0f)", symbol, qty, price, cost)
        return fill

    def create_market_sell(self, symbol: str, qty: float, meta: dict) -> Fill:
        base, quote = split_symbol(symbol)
        qty = float(qty)
        held = self.balances.get(base, 0.0)
        if qty > held + 1e-12:
            qty = held
        if qty <= 0:
            raise ValueError(f"{base} 보유 수량이 없습니다.")
        price = self.fetch_price(symbol) * (1.0 - self.slippage)
        gross = qty * price
        fee_amt = gross * self.fee
        self.balances[base] = held - qty
        self.balances[quote] = self.balances.get(quote, 0.0) + gross - fee_amt
        fill = Fill(
            symbol=symbol, side="sell", qty=qty, price=price, cost=gross, fee=fee_amt,
            ts=self._now(), mode="paper", strategy=str(meta.get("strategy", "")),
            reason=str(meta.get("reason", "")), order_id=f"paper-m-{uuid.uuid4().hex[:8]}",
        )
        self.fills.append(fill)
        log.info("[페이퍼] 매도 %s %.8f @ %.0f", symbol, qty, price)
        return fill

    # ---------- 지정가 ----------
    def create_limit_buy(self, symbol: str, price: float, qty: float, meta: dict) -> Order:
        base, quote = split_symbol(symbol)
        cost = float(price) * float(qty)
        if self.fetch_balance().get(quote, 0.0) + 1e-6 < cost:
            raise ValueError(f"{quote} 잔고 부족: 가용 {self.fetch_balance().get(quote, 0.0):,.0f}, 필요 {cost:,.0f}")
        oid = f"paper-l-{uuid.uuid4().hex[:8]}"
        order = Order(id=oid, symbol=symbol, side="buy", type="limit", price=float(price),
                      qty=float(qty), status="open", ts=self._now())
        self.orders[oid] = order
        self.reserved[quote] = self.reserved.get(quote, 0.0) + cost
        self._meta = getattr(self, "_meta", {})
        self._meta[oid] = dict(meta)
        log.info("[페이퍼] 지정가 매수 게시 %s %.8f @ %.0f", symbol, qty, price)
        return order

    def cancel_order(self, order_id: str, symbol: str) -> Order:
        order = self.orders.get(order_id)
        if order is None:
            raise KeyError(f"주문 없음: {order_id}")
        if order.status == "open":
            _, quote = split_symbol(symbol)
            self.reserved[quote] = max(self.reserved.get(quote, 0.0) - (order.price or 0.0) * order.remaining, 0.0)
            order.status = "canceled"
        return order

    def fetch_order(self, order_id: str, symbol: str) -> Order:
        order = self.orders.get(order_id)
        if order is None:
            raise KeyError(f"주문 없음: {order_id}")
        return order

    def fetch_open_orders(self, symbol: str) -> list[Order]:
        return [o for o in self.orders.values() if o.symbol == symbol and o.status == "open"]

    def fetch_my_fills(self, symbol: str, since: datetime | None = None) -> list[Fill]:
        return [f for f in self.fills if f.symbol == symbol and (since is None or f.ts >= since)]

    def sync(self, symbol: str) -> list[Fill]:
        """열린 지정가 매수의 체결을 판정한다. 새로 체결된 Fill 목록을 돌려준다."""
        base, quote = split_symbol(symbol)
        new_fills: list[Fill] = []
        bar = self.current_bar()
        for order in list(self.orders.values()):
            if order.symbol != symbol or order.status != "open" or order.side != "buy":
                continue
            limit = float(order.price or 0.0)
            if bar is not None:
                low = float(bar["low"])
                if low > limit:
                    continue
                fill_price = min(limit, float(bar["open"]))
            else:
                px = self.fetch_price(symbol)
                if px > limit:
                    continue
                fill_price = limit
            cost = limit * order.qty
            self.reserved[quote] = max(self.reserved.get(quote, 0.0) - cost, 0.0)
            actual_cost = fill_price * order.qty
            fill = self._settle_buy(symbol, fill_price, actual_cost, self._meta.get(order.id, {}), order.id)
            # 지정가 체결은 수량 기준: 수수료만큼 수량이 줄지 않게 원가를 수수료 포함으로 보정
            order.filled = fill.qty
            order.avg_price = fill_price
            order.fee = fill.fee
            order.cost = fill.cost
            order.status = "closed"
            new_fills.append(fill)
        return new_fills

    def equity(self, symbol: str) -> float:
        base, quote = split_symbol(symbol)
        return self.balances.get(quote, 0.0) + self.balances.get(base, 0.0) * self.fetch_price(symbol)

"""도메인 모델. 그리드 분할 매수·부분 청산을 위해 포지션이 체결 목록을 갖는다."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class StopMode(str, Enum):
    """손절 판정 방식."""

    INTRADAY = "intraday"       # 실시간 가격이 손절가에 닿으면 즉시
    DAILY_CLOSE = "daily_close"  # 일봉 확정 종가가 손절가 미만일 때만


@dataclass
class Fill:
    """체결 한 건."""

    symbol: str
    side: str            # "buy" | "sell"
    qty: float
    price: float
    cost: float          # 견적통화 금액(수수료 제외 총액)
    fee: float
    ts: datetime
    mode: str            # "paper" | "live"
    strategy: str = ""
    reason: str = ""
    order_id: str | None = None
    level: int | None = None  # 그리드 단(1~4). 매도는 None


@dataclass
class Order:
    """거래소 주문 상태 스냅샷."""

    id: str
    symbol: str
    side: str
    type: str            # "limit" | "market"
    price: float | None
    qty: float
    filled: float = 0.0
    status: str = "open"  # "open" | "closed" | "canceled"
    avg_price: float | None = None
    fee: float = 0.0
    ts: datetime | None = None
    cost: float = 0.0

    @property
    def remaining(self) -> float:
        return max(self.qty - self.filled, 0.0)

    @property
    def is_open(self) -> bool:
        return self.status == "open"


@dataclass
class Position:
    """롱 포지션. 여러 단의 체결을 누적하고 부분 매도를 지원한다."""

    symbol: str
    qty: float = 0.0
    cost: float = 0.0          # 남은 수량의 매입 금액(수수료 제외)
    fee: float = 0.0
    entry_time: datetime | None = None
    fills: list[dict[str, Any]] = field(default_factory=list)
    stop: float | None = None
    take: float | None = None
    stop_mode: StopMode = StopMode.DAILY_CLOSE

    @property
    def is_open(self) -> bool:
        return self.qty > 1e-12

    @property
    def entry_price(self) -> float:
        """평균 매입 단가(수수료 제외)."""
        if self.qty <= 0:
            return 0.0
        return self.cost / self.qty

    def add_fill(self, fill: Fill) -> None:
        """매수 체결을 누적한다."""
        if fill.side != "buy":
            raise ValueError("add_fill 은 매수 체결만 받습니다.")
        if not self.is_open:
            self.entry_time = fill.ts
        self.qty += fill.qty
        self.cost += fill.cost
        self.fee += fill.fee
        self.fills.append({
            "level": fill.level, "qty": fill.qty, "price": fill.price,
            "cost": fill.cost, "fee": fill.fee, "ts": fill.ts.isoformat(), "order_id": fill.order_id,
        })

    def remove_qty(self, qty: float) -> tuple[float, float]:
        """부분 매도 후 남은 원가를 비례 차감한다. (차감된 원가, 차감된 수수료)를 돌려준다."""
        if self.qty <= 0:
            return 0.0, 0.0
        qty = min(qty, self.qty)
        ratio = qty / self.qty
        cost_part = self.cost * ratio
        fee_part = self.fee * ratio
        self.qty -= qty
        self.cost -= cost_part
        self.fee -= fee_part
        if self.qty <= 1e-12:
            self.qty = 0.0
            self.cost = 0.0
            self.fee = 0.0
        return cost_part, fee_part

    def pnl_pct(self, price: float) -> float:
        """현재가 기준 평가 손익률(%)."""
        ep = self.entry_price
        if ep <= 0:
            return 0.0
        return (price / ep - 1.0) * 100.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol, "qty": self.qty, "cost": self.cost, "fee": self.fee,
            "entry_time": self.entry_time.isoformat() if self.entry_time else None,
            "fills": list(self.fills), "stop": self.stop, "take": self.take,
            "stop_mode": self.stop_mode.value,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Position":
        et = d.get("entry_time")
        return cls(
            symbol=d["symbol"], qty=float(d.get("qty", 0.0)), cost=float(d.get("cost", 0.0)),
            fee=float(d.get("fee", 0.0)),
            entry_time=datetime.fromisoformat(et) if et else None,
            fills=list(d.get("fills") or []), stop=d.get("stop"), take=d.get("take"),
            stop_mode=StopMode(d.get("stop_mode", StopMode.DAILY_CLOSE.value)),
        )


@dataclass
class Trade:
    """청산 한 건(부분 청산도 한 건으로 기록)."""

    symbol: str
    strategy: str
    entry_time: datetime
    exit_time: datetime
    entry_price: float
    exit_price: float
    qty: float
    pnl: float
    pnl_pct: float
    fee: float
    exit_reason: str  # "stop_daily" | "stop_disaster" | "tp1" | "trail" | "manual" | "kill"

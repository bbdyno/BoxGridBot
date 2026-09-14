"""거래소 인터페이스. 심볼 표기는 ccxt 통일 규격("BTC/KRW"). 지정가 주문을 지원한다."""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

import pandas as pd

from ..core.models import Fill, Order


def split_symbol(symbol: str) -> tuple[str, str]:
    """'BTC/KRW' -> ('BTC', 'KRW')."""
    base, _, quote = symbol.partition("/")
    return base, (quote or "KRW")


class Exchange(ABC):
    """거래소 공통 인터페이스."""

    id: str = "base"

    # ---------- 시세 ----------
    @abstractmethod
    def fetch_ohlcv(
        self, symbol: str, timeframe: str, limit: int = 200, since: datetime | None = None
    ) -> pd.DataFrame:
        """OHLCV 를 DataFrame(index=UTC tz-aware)으로 돌려준다."""

    def fetch_ohlcv_range(
        self, symbol: str, timeframe: str, start: datetime, end: datetime | None = None
    ) -> pd.DataFrame:
        """구간 전체(페이지네이션). 기본 구현은 limit 한 번."""
        return self.fetch_ohlcv(symbol, timeframe, limit=1000, since=start)

    @abstractmethod
    def fetch_price(self, symbol: str) -> float:
        """현재가."""

    @abstractmethod
    def fetch_balance(self) -> dict[str, float]:
        """자산별 가용 잔고. 예: {"KRW": 1000000.0, "BTC": 0.01}."""

    @abstractmethod
    def market_info(self, symbol: str) -> dict:
        """최소 주문 금액·수수료·정밀도."""

    # ---------- 시장가 ----------
    @abstractmethod
    def create_market_buy(self, symbol: str, cost: float, meta: dict) -> Fill:
        """시장가 매수. cost 는 지불할 견적통화 금액."""

    @abstractmethod
    def create_market_sell(self, symbol: str, qty: float, meta: dict) -> Fill:
        """시장가 매도. qty 는 기초자산 수량."""

    # ---------- 지정가 ----------
    @abstractmethod
    def create_limit_buy(self, symbol: str, price: float, qty: float, meta: dict) -> Order:
        """지정가 매수를 게시한다. 즉시 체결되지 않으면 open 상태 Order 를 돌려준다."""

    @abstractmethod
    def cancel_order(self, order_id: str, symbol: str) -> Order:
        """주문을 취소하고 최종 상태를 돌려준다."""

    @abstractmethod
    def fetch_order(self, order_id: str, symbol: str) -> Order:
        """주문 상태 조회."""

    @abstractmethod
    def fetch_open_orders(self, symbol: str) -> list[Order]:
        """미체결 주문 목록."""

    def fetch_my_fills(self, symbol: str, since: datetime | None = None) -> list[Fill]:
        """체결 내역(재시작 대조용). 지원하지 않으면 빈 목록."""
        return []

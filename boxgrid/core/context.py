"""전략에 넘기는 순수 데이터 컨텍스트.

거래소·네트워크·파일에 접근하지 않는다. 백테스트와 라이브가 같은 객체를 쓴다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import pandas as pd

from .models import Position

OHLCV_COLS = ["open", "high", "low", "close", "volume"]

_AGG = {
    "open": "first",
    "high": "max",
    "low": "min",
    "close": "last",
    "volume": "sum",
}


@dataclass
class Context:
    """전략 판단에 필요한 모든 정보. candles 는 '닫힌 봉만' 담는다."""

    symbol: str
    timeframe: str
    candles: pd.DataFrame
    price: float
    position: Position | None = None
    cash: float = 0.0
    equity: float = 0.0
    params: dict[str, Any] = field(default_factory=dict)
    regime: str | None = None
    now: datetime | None = None

    def resample(self, rule: str) -> pd.DataFrame:
        """상위 타임프레임으로 리샘플링. 마지막 미완성 구간은 제외한다.

        pandas 3.0 오프셋 별칭을 쓴다 ("1h", "4h", "1D").
        """
        df = self.candles
        if df.empty:
            return df.copy()
        out = (
            df.resample(rule, label="left", closed="left")
            .agg(_AGG)
            .dropna(subset=["open"])
        )
        if out.empty:
            return out
        offset = pd.tseries.frequencies.to_offset(rule)
        step = self._bar_step()
        last_start = out.index[-1]
        if step is not None and df.index[-1] + step < last_start + offset:
            out = out.iloc[:-1]
        return out

    def last(self, n: int = 1) -> pd.DataFrame:
        """마지막 n개 닫힌 봉."""
        return self.candles.tail(n)

    def _bar_step(self) -> pd.Timedelta | None:
        """캔들 간격 추정(중앙값)."""
        idx = self.candles.index
        if len(idx) < 2:
            return None
        diffs = pd.Series(idx).diff().dropna()
        if diffs.empty:
            return None
        return pd.Timedelta(diffs.median())

"""보조지표. pandas 만 사용하며 모든 함수는 입력과 같은 인덱스의 Series 를 돌려준다."""
from __future__ import annotations

import pandas as pd


def sma(s: pd.Series, n: int) -> pd.Series:
    """단순이동평균."""
    return s.rolling(n, min_periods=n).mean()


def ema(s: pd.Series, n: int) -> pd.Series:
    """지수이동평균(adjust=False)."""
    return s.ewm(span=n, adjust=False, min_periods=n).mean()


def rsi(s: pd.Series, n: int = 14) -> pd.Series:
    """Wilder RSI. 첫 n봉은 단순평균, 이후 Wilder 평활(alpha=1/n)."""
    delta = s.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = gain.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()
    avg_loss = loss.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()
    rs = avg_gain / avg_loss.replace(0.0, pd.NA)
    out = 100.0 - (100.0 / (1.0 + rs))
    out = out.where(avg_loss != 0.0, 100.0)
    out = out.where(~(avg_gain.isna() | avg_loss.isna()), other=float("nan"))
    return out.astype("float64")


def true_range(df: pd.DataFrame) -> pd.Series:
    """True Range."""
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    """Wilder ATR."""
    return true_range(df).ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()


def bollinger(s: pd.Series, n: int = 20, k: float = 2.0) -> tuple[pd.Series, pd.Series, pd.Series]:
    """볼린저 밴드 (중심선, 상단, 하단). 표준편차는 표본(ddof=1)."""
    mid = sma(s, n)
    sd = s.rolling(n, min_periods=n).std(ddof=1)
    return mid, mid + k * sd, mid - k * sd


def highest(s: pd.Series, n: int) -> pd.Series:
    """최근 n봉 최고값."""
    return s.rolling(n, min_periods=n).max()


def lowest(s: pd.Series, n: int) -> pd.Series:
    """최근 n봉 최저값."""
    return s.rolling(n, min_periods=n).min()


def volume_sma(df: pd.DataFrame, n: int = 20) -> pd.Series:
    """거래량 단순이동평균."""
    return sma(df["volume"], n)


def crosses_above(a: pd.Series, b: pd.Series) -> pd.Series:
    """a 가 b 를 상향 돌파한 봉에서 True."""
    a = a.astype("float64")
    b = b.astype("float64")
    cur = a > b
    prev = a.shift(1) <= b.shift(1)
    valid = a.notna() & b.notna() & a.shift(1).notna() & b.shift(1).notna()
    return (cur & prev & valid).fillna(False).astype(bool)


def crosses_below(a: pd.Series, b: pd.Series) -> pd.Series:
    """a 가 b 를 하향 돌파한 봉에서 True."""
    a = a.astype("float64")
    b = b.astype("float64")
    cur = a < b
    prev = a.shift(1) >= b.shift(1)
    valid = a.notna() & b.notna() & a.shift(1).notna() & b.shift(1).notna()
    return (cur & prev & valid).fillna(False).astype(bool)

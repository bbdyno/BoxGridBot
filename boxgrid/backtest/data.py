"""OHLCV 데이터 로더. CSV 캐시를 먼저 보고 없으면 ccxt 공개 API 로 받는다."""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone

import pandas as pd

log = logging.getLogger(__name__)

COLS = ["open", "high", "low", "close", "volume"]


def load_csv(path: str) -> pd.DataFrame:
    """CSV 를 OHLCV DataFrame 으로 읽는다.

    - 시간 컬럼 이름은 `ts` 또는 `timestamp` 를 모두 허용한다.
    - 첫 줄이 `#` 로 시작하는 주석이어도 된다.
    - 시간 값은 ISO 문자열과 epoch 밀리초 정수를 모두 허용한다.
    """
    df = pd.read_csv(path, comment="#")
    df.columns = [str(c).strip().lower() for c in df.columns]
    ts_col = next((c for c in ("ts", "timestamp", "time", "date", "datetime") if c in df.columns), None)
    if ts_col is None:
        raise ValueError(f"시간 컬럼(ts 또는 timestamp)을 찾지 못했습니다: {list(df.columns)}")
    missing = [c for c in COLS if c not in df.columns]
    if missing:
        raise ValueError(f"필수 컬럼이 없습니다: {missing}")

    ts = df[ts_col]
    if pd.api.types.is_numeric_dtype(ts):
        idx = pd.to_datetime(ts.astype("int64"), unit="ms", utc=True)
    else:
        idx = pd.to_datetime(ts, utc=True, format="mixed")
    out = df[COLS].astype("float64")
    out.index = pd.DatetimeIndex(idx, name="ts")
    out = out.sort_index()
    out = out[~out.index.duplicated(keep="last")]
    return out


def save_csv(df: pd.DataFrame, path: str, comment: str | None = None) -> None:
    """OHLCV 를 캐시 CSV 로 저장한다(ISO UTC, 첫 줄 주석 가능)."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    body = df.copy()
    body.index.name = "ts"
    with open(path, "w", encoding="utf-8", newline="") as f:
        if comment:
            f.write(f"# {comment}\n")
        body.to_csv(f, date_format="%Y-%m-%dT%H:%M:%S%z")


def load_ohlcv(
    symbol: str,
    tf: str = "1h",
    days: int = 730,
    cache_dir: str = "data/ohlcv",
    exchange_id: str = "upbit",
) -> pd.DataFrame:
    """캐시 CSV 우선, 없으면 ccxt 공개 API 로 내려받아 캐시한다."""
    safe = symbol.replace("/", "_")
    path = os.path.join(cache_dir, f"{safe}_{tf}_{days}d.csv")
    if os.path.exists(path):
        log.info("캐시에서 읽습니다: %s", path)
        return load_csv(path)

    from ..exchange.ccxt_adapter import CcxtExchange

    start = datetime.now(timezone.utc) - timedelta(days=days)
    ex = CcxtExchange(exchange_id)
    df = ex.fetch_ohlcv_range(symbol, tf, start)
    if not df.empty:
        save_csv(df, path, comment=f"{exchange_id} {symbol} {tf} {days}일")
    return df

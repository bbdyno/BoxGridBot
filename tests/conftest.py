"""공통 픽스처. 합성 OHLCV(결정적, 2,000봉 1h)를 만들어 둔다."""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

FIXTURE_DIR = os.path.join(ROOT, "tests", "fixtures")
SYNTHETIC_CSV = os.path.join(FIXTURE_DIR, "synthetic_1h.csv")


@pytest.fixture(scope="session")
def synthetic_csv() -> str:
    """합성 CSV 경로. 없으면 생성한다(결정적)."""
    if not os.path.exists(SYNTHETIC_CSV):
        sys.path.insert(0, FIXTURE_DIR)
        import make_synthetic

        make_synthetic.write(SYNTHETIC_CSV)
    return SYNTHETIC_CSV


@pytest.fixture(scope="session")
def ohlcv(synthetic_csv: str) -> pd.DataFrame:
    """합성 OHLCV DataFrame (UTC 인덱스)."""
    from boxgrid.backtest.data import load_csv

    return load_csv(synthetic_csv)


@pytest.fixture
def small_ohlcv() -> pd.DataFrame:
    """테스트용 작은 OHLCV (48봉 1h, 값이 예측 가능한 직선)."""
    idx = pd.date_range("2024-01-01", periods=48, freq="h", tz="UTC")
    close = pd.Series(range(100, 148), dtype="float64").to_numpy()
    df = pd.DataFrame(
        {
            "open": close - 0.5,
            "high": close + 1.0,
            "low": close - 1.0,
            "close": close,
            "volume": [10.0] * 48,
        },
        index=idx,
    )
    df.index.name = "ts"
    return df

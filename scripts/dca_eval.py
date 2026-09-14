"""적립 추가 매수 지표 평가: 매일 base_amount 적립 vs 지표대로 추가 매수했을 때 평균 단가·수익률 비교.

    .venv/bin/python scripts/dca_eval.py --days 730
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from boxgrid.backtest.data import load_ohlcv  # noqa: E402
from boxgrid.config import load_config  # noqa: E402
from boxgrid.notify.humanize import won  # noqa: E402
from boxgrid.strategy.dca import dca_verdict  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--config", default="config/grid.yaml")
    p.add_argument("--days", type=int, default=2200)
    args = p.parse_args()
    cfg = load_config(args.config)
    daily = load_ohlcv(cfg.symbol, "1d", days=args.days, exchange_id=cfg.exchange).iloc[:-1]
    base = cfg.dca.base_amount
    warm = cfg.dca.sma_len + 1
    print(f"일봉 {len(daily)}개 ({daily.index[0].date()} ~ {daily.index[-1].date()}), 마지막 가격 {won(float(daily['close'].iloc[-1]))}")
    for label, days in (("최근 2년", 730), ("최근 4년", 1460), ("전체", len(daily) - warm)):
        start = max(warm, len(daily) - days)
        pq = pc = bq = bc = 0.0
        grades: dict[str, int] = {}
        for i in range(start, len(daily)):
            price = float(daily["open"].iloc[i])  # 판정 직후 시가에 매수
            v = dca_verdict(daily.iloc[:i], price, cfg.dca)
            grades[v.grade] = grades.get(v.grade, 0) + 1
            pq += base / price
            pc += base
            bq += (base + v.extra_amount) / price
            bc += base + v.extra_amount
        last = float(daily["close"].iloc[-1])
        print(f"\n== {label} ({daily.index[start].date()} ~) ==")
        for name, q, c in (("그냥 적립", pq, pc), ("지표대로 추가", bq, bc)):
            print(f"  {name:8s} 투입 {won(c):>12s}  평균단가 {won(c / q):>12s}  수익률 {(q * last / c - 1) * 100:+.1f}%")
        print(f"  평균단가 차이 {((bc / bq) / (pc / pq) - 1) * 100:+.1f}%, 추가 투입 {(bc / pc - 1) * 100:+.0f}%, 등급별 일수 {grades}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

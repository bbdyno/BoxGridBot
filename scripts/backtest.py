"""백테스트 실행.

    .venv/bin/python scripts/backtest.py --days 730 [--exchange upbit --symbol BTC/KRW --config config/grid.yaml]
"""
from __future__ import annotations

import argparse
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from boxgrid.backtest.data import load_ohlcv  # noqa: E402
from boxgrid.backtest.runner import run_backtest  # noqa: E402
from boxgrid.config import load_config  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--config", default="config/grid.yaml")
    p.add_argument("--days", type=int, default=730)
    p.add_argument("--exchange", default=None)
    p.add_argument("--symbol", default=None)
    p.add_argument("--csv", default=None, help="캐시 대신 쓸 1h CSV")
    p.add_argument("--cash", type=float, default=None)
    args = p.parse_args()
    logging.basicConfig(level=logging.WARNING)
    cfg = load_config(args.config)
    if args.exchange:
        cfg.exchange = args.exchange
    if args.symbol:
        cfg.symbol = args.symbol
    if args.cash:
        cfg.initial_cash = args.cash
    if args.csv:
        from boxgrid.backtest.data import load_csv
        hourly = load_csv(args.csv)
    else:
        hourly = load_ohlcv(cfg.symbol, "1h", days=args.days, exchange_id=cfg.exchange)
    print(f"1h 봉 {len(hourly)}개 로드")
    res = run_backtest(hourly, cfg)
    print(res.summary())
    print("\n거래 내역")
    for t in res.trades:
        print(f"  {t['entry_time'][:10]} → {t['exit_time'][:10]} {t['exit_reason']:<13} "
              f"진입 {t['entry_price']:>14,.0f} 청산 {t['exit_price']:>14,.0f} 손익 {t['pnl']:>12,.0f} ({t['pnl_pct']:+.2f}%)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

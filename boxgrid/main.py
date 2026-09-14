"""CLI 진입점.

    python -m boxgrid.main --config config/grid.yaml
    python -m boxgrid.main --mode paper --max-cycles 3      # 스모크
    python -m boxgrid.main --mode live --unlock --yes-live   # 실거래(잠금 해제 + 승인)

`live_lock: true`(기본)이면 --mode live 여도 페이퍼로 강등되어 판단·알림만 한다.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import logging.handlers
import os
import sys

from .config import AppConfig, load_config

log = logging.getLogger("boxgrid")
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"


def load_dotenv(path: str = ".env") -> None:
    """의존성 없이 .env 를 읽는다. 이미 있는 환경변수는 덮어쓰지 않는다."""
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def setup_logging(log_path: str, level: int = logging.INFO) -> None:
    root = logging.getLogger()
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)
    fmt = logging.Formatter(LOG_FORMAT)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.addHandler(sh)
    try:
        os.makedirs(os.path.dirname(log_path) or ".", exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(log_path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)
    except OSError as exc:  # pragma: no cover
        print(f"파일 로그를 열지 못했습니다: {exc}", file=sys.stderr)
    for noisy in ("apscheduler", "httpx", "telegram", "ccxt"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m boxgrid.main", description="박스권 하단 그리드 롱 봇")
    p.add_argument("--mode", choices=["paper", "live"], default=None)
    p.add_argument("--config", default="config/grid.yaml")
    p.add_argument("--tick", type=int, default=None)
    p.add_argument("--unlock", action="store_true", help="live_lock 을 해제한다(실주문 허용)")
    p.add_argument("--yes-live", action="store_true", help="텔레그램 승인 없이 라이브 시작")
    p.add_argument("--max-cycles", type=int, default=None)
    p.add_argument("--log-level", default="INFO")
    return p


def build_engine(cfg: AppConfig):
    from .core.clock import Clock
    from .core.engine import GridEngine
    from .core.notify_null import NullNotifier
    from .core.store import Store
    from .exchange.ccxt_adapter import CcxtExchange
    from .exchange.paper import PaperExchange
    from .risk.guard import Guard

    clock = Clock()
    _, quote = cfg.symbol.partition("/")[0::2]
    if cfg.is_live:
        key_prefix = cfg.exchange.upper()
        exchange = CcxtExchange(cfg.exchange, api_key=os.getenv(f"{key_prefix}_API_KEY"),
                                secret=os.getenv(f"{key_prefix}_SECRET"))
        guard = Guard(cfg.risk, clock, live_started_at=clock.now())
    else:
        exchange = PaperExchange(CcxtExchange(cfg.exchange), cash=cfg.initial_cash, fee=cfg.fee,
                                 slippage=cfg.slippage, quote=quote or "KRW")
        guard = Guard(cfg.risk, clock)

    notifier = NullNotifier()
    try:
        from .notify.router import build_notifier

        notifier = build_notifier(cfg) or notifier
    except Exception as exc:  # noqa: BLE001
        log.warning("알림 채널 조립 실패, 로그만 남깁니다: %s", exc)
    return GridEngine(cfg, exchange, guard, notifier, Store(cfg.db_path), clock)


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    args = build_parser().parse_args(argv)
    cfg = load_config(args.config)
    if args.mode:
        cfg.mode = args.mode
    if args.tick:
        cfg.tick_seconds = args.tick
    if args.unlock:
        cfg.live_lock = False
    setup_logging(cfg.log_path, getattr(logging, args.log_level.upper(), logging.INFO))

    if cfg.is_live and cfg.live_lock:
        log.warning("live_lock 이 켜져 있어 페이퍼 모드로 강등합니다. 실주문은 나가지 않습니다. (--unlock 으로 해제)")
        cfg.mode = "paper"

    if cfg.is_live:
        key_prefix = cfg.exchange.upper()
        if not (os.getenv(f"{key_prefix}_API_KEY") and os.getenv(f"{key_prefix}_SECRET")):
            print(f"라이브 모드에는 {key_prefix}_API_KEY / {key_prefix}_SECRET 가 필요합니다.", file=sys.stderr)
            return 2
        has_telegram = bool(os.getenv("TELEGRAM_BOT_TOKEN")) and bool(os.getenv("TELEGRAM_CHAT_IDS"))
        if not has_telegram and not args.yes_live:
            print("라이브 모드는 텔레그램 설정 또는 --yes-live 가 필요합니다.", file=sys.stderr)
            return 2

    engine = build_engine(cfg)
    if cfg.is_live and args.yes_live:
        engine.confirm_live()
    try:
        asyncio.run(engine.run(max_cycles=args.max_cycles))
    except KeyboardInterrupt:
        log.info("사용자 중단으로 종료합니다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

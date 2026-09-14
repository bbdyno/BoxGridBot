"""ccxt 어댑터. 공개 시세는 키 없이도 읽는다. 지정가 주문·취소·조회를 지원한다."""
from __future__ import annotations

import logging
import math
import time
from datetime import datetime, timedelta, timezone

import pandas as pd

from ..core.models import Fill, Order
from .base import Exchange, split_symbol

log = logging.getLogger(__name__)

_TF_MS = {
    "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
    "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000, "6h": 21_600_000,
    "12h": 43_200_000, "1d": 86_400_000,
}

# 업비트 KRW 마켓 호가 단위(2024-10 개정). (하한가, 틱)
_UPBIT_KRW_TICKS = [
    (2_000_000, 1000.0), (1_000_000, 500.0), (500_000, 100.0), (100_000, 50.0),
    (10_000, 10.0), (1_000, 1.0), (100, 0.1), (10, 0.01), (1, 0.001), (0.1, 0.0001), (0, 0.00001),
]


def upbit_krw_tick(price: float) -> float:
    """업비트 원화 마켓 호가 단위."""
    for floor, tick in _UPBIT_KRW_TICKS:
        if price >= floor:
            return tick
    return 0.00001


def round_price_down(price: float, tick: float) -> float:
    """호가 단위로 내림(매수 지정가는 내림이 안전)."""
    if tick <= 0:
        return price
    return math.floor(price / tick + 1e-9) * tick


def ohlcv_to_df(rows: list[list]) -> pd.DataFrame:
    """ccxt OHLCV 배열을 UTC 인덱스 DataFrame 으로."""
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
    if df.empty:
        df = df.astype({c: "float64" for c in ["open", "high", "low", "close", "volume"]})
        df.index = pd.DatetimeIndex([], tz="UTC", name="ts")
        return df[["open", "high", "low", "close", "volume"]]
    df["ts"] = pd.to_datetime(df["ts"].astype("int64"), unit="ms", utc=True)
    df = df.set_index("ts").sort_index()
    df = df[~df.index.duplicated(keep="last")]
    return df.astype("float64")


def _ts_from_ms(ts_ms) -> datetime:
    if ts_ms:
        return pd.to_datetime(int(ts_ms), unit="ms", utc=True).to_pydatetime()
    return datetime.now(timezone.utc)


class CcxtExchange(Exchange):
    """ccxt 로 거래소에 붙는다. 키가 없으면 공개 API 만 쓴다."""

    def __init__(
        self,
        exchange_id: str = "upbit",
        api_key: str | None = None,
        secret: str | None = None,
        max_retries: int = 3,
    ) -> None:
        import ccxt  # 지연 임포트: 테스트에서 mock 하기 쉽게

        self.id = exchange_id
        self.max_retries = max_retries
        opts: dict = {"enableRateLimit": True}
        if api_key and secret:
            opts.update({"apiKey": api_key, "secret": secret})
        self.client = getattr(ccxt, exchange_id)(opts)
        self._has_keys = bool(api_key and secret)
        self._markets_loaded = False

    # ---------- 공통 ----------
    def _retry(self, fn, *args, **kwargs):
        """429/네트워크 오류에 지수 백오프 3회."""
        import ccxt

        delay = 1.0
        last: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                return fn(*args, **kwargs)
            except (ccxt.RateLimitExceeded, ccxt.NetworkError, ccxt.ExchangeNotAvailable) as exc:
                last = exc
                log.warning("거래소 호출 실패(%d/%d): %s", attempt + 1, self.max_retries, exc)
                if attempt < self.max_retries - 1:
                    time.sleep(delay)
                    delay *= 2
        raise last  # type: ignore[misc]

    def _ensure_markets(self) -> None:
        if not self._markets_loaded:
            self._retry(self.client.load_markets)
            self._markets_loaded = True

    def _require_keys(self) -> None:
        if not self._has_keys:
            raise PermissionError("API 키가 없어 주문·잔고 API 를 쓸 수 없습니다.")

    # ---------- 시세 ----------
    def fetch_ohlcv(
        self, symbol: str, timeframe: str, limit: int = 200, since: datetime | None = None
    ) -> pd.DataFrame:
        """최근 캔들. 거래소 1회 한도를 넘는 limit 은 페이지네이션으로 채운다."""
        if limit > 200 and since is None:
            step = _TF_MS.get(timeframe, 3_600_000)
            start = datetime.now(timezone.utc) - timedelta(milliseconds=step * (limit + 5))
            return self.fetch_ohlcv_range(symbol, timeframe, start).tail(limit)
        since_ms = int(since.timestamp() * 1000) if since else None
        rows = self._retry(self.client.fetch_ohlcv, symbol, timeframe, since_ms, limit)
        return ohlcv_to_df(rows)

    def fetch_ohlcv_range(
        self, symbol: str, timeframe: str, start: datetime, end: datetime | None = None
    ) -> pd.DataFrame:
        """since 페이지네이션으로 구간 전체를 모은다 (한 번에 200)."""
        end = end or datetime.now(timezone.utc)
        step = _TF_MS.get(timeframe, 3_600_000)
        cursor = int(start.timestamp() * 1000)
        end_ms = int(end.timestamp() * 1000)
        frames: list[pd.DataFrame] = []
        guard = 0
        while cursor < end_ms and guard < 10_000:
            guard += 1
            rows = self._retry(self.client.fetch_ohlcv, symbol, timeframe, cursor, 200)
            if not rows:
                break
            frames.append(ohlcv_to_df(rows))
            last_ms = int(rows[-1][0])
            if last_ms <= cursor:
                break
            cursor = last_ms + step
        if not frames:
            return ohlcv_to_df([])
        df = pd.concat(frames).sort_index()
        df = df[~df.index.duplicated(keep="last")]
        return df.loc[(df.index >= start) & (df.index <= end)]

    def fetch_price(self, symbol: str) -> float:
        """현재가(ticker last)."""
        t = self._retry(self.client.fetch_ticker, symbol)
        return float(t.get("last") or t.get("close"))

    def fetch_balance(self) -> dict[str, float]:
        """가용 잔고. 키가 없으면 빈 dict."""
        if not self._has_keys:
            return {}
        bal = self._retry(self.client.fetch_balance)
        free = bal.get("free", {}) or {}
        return {k: float(v) for k, v in free.items() if v}

    def market_info(self, symbol: str) -> dict:
        """마켓 메타(최소 주문 금액·수수료·정밀도)."""
        try:
            self._ensure_markets()
            m = self.client.market(symbol)
            return {
                "min_cost": float((m.get("limits", {}).get("cost", {}) or {}).get("min") or 5000.0),
                "fee": float(m.get("taker") or 0.0005),
                "precision": m.get("precision", {}),
            }
        except Exception as exc:  # noqa: BLE001
            log.warning("마켓 정보를 읽지 못했습니다: %s", exc)
            return {"min_cost": 5000.0, "fee": 0.0005, "precision": {}}

    # ---------- 정밀도 ----------
    def normalize_price(self, symbol: str, price: float) -> float:
        """거래소 호가 단위로 가격을 내림 정규화한다."""
        _, quote = split_symbol(symbol)
        if self.id == "upbit" and quote == "KRW":
            return round_price_down(price, upbit_krw_tick(price))
        try:
            self._ensure_markets()
            return float(self.client.price_to_precision(symbol, price))
        except Exception:  # noqa: BLE001
            return price

    def normalize_qty(self, symbol: str, qty: float) -> float:
        """거래소 수량 정밀도로 내림."""
        try:
            self._ensure_markets()
            return float(self.client.amount_to_precision(symbol, qty))
        except Exception:  # noqa: BLE001
            return math.floor(qty * 1e8) / 1e8

    # ---------- 주문 ----------
    def create_market_buy(self, symbol: str, cost: float, meta: dict) -> Fill:
        """시장가 매수(업비트는 KRW 금액 주문)."""
        self._require_keys()
        params = {}
        if self.id == "upbit":
            order = self._retry(self.client.create_order, symbol, "market", "buy", cost, None, params)
        else:
            params = {"quoteOrderQty": cost} if self.id == "binance" else {}
            price = self.fetch_price(symbol)
            order = self._retry(self.client.create_order, symbol, "market", "buy", cost / price, None, params)
        order = self._refresh_if_needed(order, symbol)
        return self._to_fill(symbol, "buy", order, meta, fallback_cost=cost)

    def create_market_sell(self, symbol: str, qty: float, meta: dict) -> Fill:
        """시장가 매도."""
        self._require_keys()
        qty = self.normalize_qty(symbol, qty)
        order = self._retry(self.client.create_order, symbol, "market", "sell", qty, None, {})
        order = self._refresh_if_needed(order, symbol)
        return self._to_fill(symbol, "sell", order, meta, fallback_qty=qty)

    def create_limit_buy(self, symbol: str, price: float, qty: float, meta: dict) -> Order:
        """지정가 매수 게시."""
        self._require_keys()
        price = self.normalize_price(symbol, price)
        qty = self.normalize_qty(symbol, qty)
        raw = self._retry(self.client.create_order, symbol, "limit", "buy", qty, price, {})
        return self._to_order(raw, symbol, fallback_price=price, fallback_qty=qty)

    def cancel_order(self, order_id: str, symbol: str) -> Order:
        """주문 취소. 이미 체결·취소된 주문이면 현재 상태를 돌려준다."""
        self._require_keys()
        import ccxt

        try:
            self._retry(self.client.cancel_order, order_id, symbol)
        except (ccxt.OrderNotFound, ccxt.InvalidOrder) as exc:
            log.info("취소 불가(이미 종료된 주문일 수 있음) %s: %s", order_id, exc)
        return self.fetch_order(order_id, symbol)

    def fetch_order(self, order_id: str, symbol: str) -> Order:
        self._require_keys()
        raw = self._retry(self.client.fetch_order, order_id, symbol)
        return self._to_order(raw, symbol)

    def fetch_open_orders(self, symbol: str) -> list[Order]:
        self._require_keys()
        rows = self._retry(self.client.fetch_open_orders, symbol)
        return [self._to_order(r, symbol) for r in rows]

    def fetch_my_fills(self, symbol: str, since: datetime | None = None) -> list[Fill]:
        """체결 내역. 거래소가 지원하지 않으면 빈 목록."""
        if not self._has_keys or not self.client.has.get("fetchMyTrades"):
            return []
        since_ms = int(since.timestamp() * 1000) if since else None
        try:
            rows = self._retry(self.client.fetch_my_trades, symbol, since_ms)
        except Exception as exc:  # noqa: BLE001
            log.warning("체결 내역 조회 실패: %s", exc)
            return []
        out: list[Fill] = []
        for r in rows:
            out.append(Fill(
                symbol=symbol, side=str(r.get("side")), qty=float(r.get("amount") or 0.0),
                price=float(r.get("price") or 0.0), cost=float(r.get("cost") or 0.0),
                fee=float((r.get("fee") or {}).get("cost") or 0.0), ts=_ts_from_ms(r.get("timestamp")),
                mode="live", order_id=r.get("order"),
            ))
        return out

    # ---------- 변환 ----------
    def _refresh_if_needed(self, order: dict, symbol: str) -> dict:
        """시장가 응답에 체결 정보가 없으면 한 번 더 조회한다(업비트는 응답이 비어 있다)."""
        if order.get("filled") or order.get("average"):
            return order
        oid = order.get("id")
        if not oid:
            return order
        for _ in range(5):
            time.sleep(0.4)
            try:
                fresh = self._retry(self.client.fetch_order, oid, symbol)
            except Exception as exc:  # noqa: BLE001
                log.warning("주문 재조회 실패: %s", exc)
                break
            if fresh.get("filled") or fresh.get("status") in ("closed", "canceled"):
                return fresh
        return order

    def _to_order(self, raw: dict, symbol: str, fallback_price: float | None = None,
                  fallback_qty: float = 0.0) -> Order:
        status = str(raw.get("status") or "open")
        if status in ("open", "closed", "canceled"):
            pass
        elif status in ("expired", "rejected", "cancelled"):
            status = "canceled"
        else:
            status = "open"
        price = raw.get("price")
        return Order(
            id=str(raw.get("id")), symbol=symbol, side=str(raw.get("side") or "buy"),
            type=str(raw.get("type") or "limit"),
            price=float(price) if price is not None else fallback_price,
            qty=float(raw.get("amount") or fallback_qty or 0.0),
            filled=float(raw.get("filled") or 0.0), status=status,
            avg_price=float(raw["average"]) if raw.get("average") else None,
            fee=float((raw.get("fee") or {}).get("cost") or 0.0),
            ts=_ts_from_ms(raw.get("timestamp")), cost=float(raw.get("cost") or 0.0),
        )

    def _to_fill(
        self, symbol: str, side: str, order: dict, meta: dict,
        fallback_cost: float = 0.0, fallback_qty: float = 0.0,
    ) -> Fill:
        price = float(order.get("average") or order.get("price") or 0.0)
        qty = float(order.get("filled") or fallback_qty or 0.0)
        cost = float(order.get("cost") or fallback_cost or qty * price)
        fee = float((order.get("fee") or {}).get("cost") or 0.0)
        return Fill(
            symbol=symbol, side=side, qty=qty, price=price, cost=cost, fee=fee,
            ts=_ts_from_ms(order.get("timestamp")),
            mode="live", strategy=str(meta.get("strategy", "")), reason=str(meta.get("reason", "")),
            order_id=order.get("id"), level=meta.get("level"),
        )

    def order_to_fill(self, order: Order, meta: dict) -> Fill:
        """체결 완료된 Order 를 Fill 로 바꾼다."""
        price = float(order.avg_price or order.price or 0.0)
        cost = order.cost or order.filled * price
        return Fill(
            symbol=order.symbol, side=order.side, qty=order.filled, price=price, cost=cost,
            fee=order.fee, ts=order.ts or datetime.now(timezone.utc), mode="live",
            strategy=str(meta.get("strategy", "")), reason=str(meta.get("reason", "")),
            order_id=order.id, level=meta.get("level"),
        )

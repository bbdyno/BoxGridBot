"""그리드 엔진. 상태 머신(strategy.grid)의 결정을 거래소에 실행하고 상태를 영속화한다.

asyncio 단일 루프. AsyncIOScheduler 로 틱·일봉 판정·리포트를 돌린다.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any

import pandas as pd

from ..config import AppConfig
from ..exchange.base import Exchange, split_symbol
from ..risk.guard import Guard
from ..strategy import grid as G
from ..strategy.dca import dca_verdict
from ..strategy.levels import Levels, clamp_below_price, closed_daily, compute_levels, plan_orders, trend_filter
from .clock import KST, Clock
from .events import Event, EventKind
from .models import Fill, Order, Position, Trade
from .notify_null import NullNotifier
from .store import Store
from ..notify import humanize as H

log = logging.getLogger(__name__)

STATE_KEY = "grid"
PAPER_KEY = "paper"
STRATEGY_NAME = "box_bottom_grid_long"


class GridEngine:
    """결정 실행기 + 스케줄러."""

    def __init__(self, cfg: AppConfig, exchange: Exchange, guard: Guard, notifier=None,
                 store: Store | None = None, clock: Clock | None = None) -> None:
        self.cfg = cfg
        self.exchange = exchange
        self.guard = guard
        self.notifier = notifier or NullNotifier()
        self.store = store or Store(cfg.db_path)
        self.clock = clock or getattr(guard, "clock", None) or Clock()
        self.symbol = cfg.symbol
        self.op_mode = cfg.op_mode
        self.cycles = 0
        self.started_at = self.clock.now()
        self.last_tick_at: datetime | None = None
        self._stop = asyncio.Event()
        self._lock = asyncio.Lock()
        self.gs = G.GridState.from_dict(self.store.get_state(STATE_KEY))
        self.live_confirmed: bool = not cfg.is_live
        self._touched_levels: set[int] = set()
        self._in_daily = False
        self._daily_grid_placed = False
        self._last_daily: pd.DataFrame | None = None
        self._restore_paper()
        bind = getattr(self.notifier, "bind_engine", None)
        if callable(bind):
            bind(self)

    # ---------- 영속화 ----------
    def _save(self) -> None:
        self.store.set_state(STATE_KEY, self.gs.to_dict())
        if getattr(self.exchange, "id", "") == "paper" and not getattr(self.exchange, "replaying", False):
            ex = self.exchange
            self.store.set_state(PAPER_KEY, {
                "balances": dict(ex.balances), "reserved": dict(ex.reserved),
                "orders": [o.__dict__ | {"ts": o.ts.isoformat() if o.ts else None} for o in ex.orders.values()],
                "meta": dict(getattr(ex, "_meta", {})),
            })

    def _restore_paper(self) -> None:
        """페이퍼 거래소는 프로세스 재시작 시 잔고·주문장이 사라지므로 저장본에서 복원한다."""
        ex = self.exchange
        if getattr(ex, "id", "") != "paper" or getattr(ex, "replaying", False):
            return
        snap = self.store.get_state(PAPER_KEY)
        if not snap or ex.orders or ex.fills:
            return
        ex.balances = {k: float(v) for k, v in snap.get("balances", {}).items()}
        ex.reserved = {k: float(v) for k, v in snap.get("reserved", {}).items()}
        ex._meta = {k: dict(v) for k, v in (snap.get("meta") or {}).items()}
        for od in snap.get("orders", []):
            od = dict(od)
            ts = od.pop("ts", None)
            order = Order(**od)
            order.ts = datetime.fromisoformat(ts) if ts else None
            ex.orders[order.id] = order
        log.info("페이퍼 잔고·주문장을 복원했습니다: %s", ex.balances)

    # ---------- 알림 ----------
    async def emit(self, kind: EventKind, title: str, body: str, severity: str = "info",
                   data: dict[str, Any] | None = None) -> None:
        event = Event(kind=kind, severity=severity, title=title, body=body, data=data or {}, ts=self.clock.now())
        try:
            self.store.record_event(event)
        except Exception as exc:  # noqa: BLE001
            log.warning("이벤트 저장 실패: %s", exc)
        try:
            await self.notifier.emit(event)
        except Exception as exc:  # noqa: BLE001
            log.warning("알림 전송 실패: %s", exc)

    # ---------- 잔고 ----------
    @property
    def quote(self) -> str:
        return split_symbol(self.symbol)[1]

    @property
    def base(self) -> str:
        return split_symbol(self.symbol)[0]

    def _balances(self) -> dict[str, float]:
        try:
            return self.exchange.fetch_balance()
        except Exception as exc:  # noqa: BLE001
            log.warning("잔고 조회 실패: %s", exc)
            return {}

    def _seed(self, balances: dict[str, float]) -> float:
        _, quote = split_symbol(self.symbol)
        cash = float(balances.get(quote, 0.0))
        slack = 0.998 if self.cfg.is_live else 1.0
        return cash * self.cfg.seed_pct / 100.0 * slack

    def _equity(self, price: float, balances: dict[str, float]) -> float:
        base, quote = split_symbol(self.symbol)
        held = float(balances.get(base, 0.0))
        pos_qty = self.gs.position.qty if self.gs.has_position else 0.0
        reserved = 0.0
        if getattr(self.exchange, "id", "") == "paper":
            reserved = float(getattr(self.exchange, "reserved", {}).get(quote, 0.0))
        return float(balances.get(quote, 0.0)) + reserved + max(held, pos_qty) * price

    @property
    def order_mode_text(self) -> str:
        """알림용 주문 상태 문구."""
        if not self.trading_enabled:
            return "주문 잠금(판단·알림만)"
        return "페이퍼 주문(모의)" if not self.cfg.is_live else "실주문"

    @property
    def trading_enabled(self) -> bool:
        """실제(또는 페이퍼) 주문을 낼 수 있는지."""
        if self.op_mode == "signal":
            return False
        if self.cfg.is_live and (self.cfg.live_lock or not self.live_confirmed):
            return False
        return True

    def confirm_live(self) -> None:
        self.live_confirmed = True
        log.warning("라이브 매매가 승인되었습니다.")

    # ---------- 결정 실행 ----------
    async def execute(self, decisions: list[G.Decision], now: datetime | None = None) -> None:
        now = now or self.clock.now()
        for d in decisions:
            try:
                if isinstance(d, G.PlaceGrid):
                    await self._place_grid(d.levels, d.reason, now)
                elif isinstance(d, G.CancelGrid):
                    await self._cancel_grid(d.reason)
                elif isinstance(d, G.SellAll):
                    await self._sell_all(d.reason, d.exit_reason)
                elif isinstance(d, G.SellPct):
                    await self._sell_pct(d.pct, d.reason, d.exit_reason)
                elif isinstance(d, G.Transition):
                    G.apply_transition(self.gs, d.to, now)
                    log.info("상태 전이 -> %s (%s)", d.to.value, d.reason)
                elif isinstance(d, G.Notify):
                    if d.severity == "warn":
                        await self.emit(EventKind.DAILY_CHECK, f"⚠️ {d.title}", d.body, d.severity)
                    else:
                        log.info("%s: %s", d.title, d.body)
            except Exception as exc:  # noqa: BLE001
                log.exception("결정 실행 실패: %s", d)
                await self.emit(EventKind.ERROR, "결정 실행 실패", f"{type(d).__name__}: {exc}", "error")
        self._save()

    def _levels_text(self, lv: Levels, price: float | None = None) -> str:
        price = price or self.gs.last_price or lv.close
        seed = self._planned_seed()
        lines = [f"현재가 {H.won(price, self.quote)}", "매수 대기 가격(현재가 대비):"]
        lines += H.level_lines(lv.to_dict(), price, self.gs.filled_levels, list(self.gs.orders), self.quote, self.gs.seed or seed)
        lines += H.exit_lines(lv.to_dict(), price, self.quote, self.cfg.levels.tp1_pct)
        if lv.mode == "dynamic":
            lines.append(f"  근거: 최근 {self.cfg.levels.box_lookback}일 박스 {H.won(lv.box_low, self.quote)}~{H.won(lv.box_high, self.quote)}, 하루 변동폭(ATR) {H.won(lv.atr, self.quote)}")
        return "\n".join(lines)

    def _planned_seed(self) -> float | None:
        try:
            return self._seed(self._balances()) or None
        except Exception:  # noqa: BLE001
            return None

    async def _place_grid(self, lv: Levels, reason: str, now: datetime, force: bool = False) -> None:
        """그리드 게시. op_mode 와 잠금 상태에 따라 알림만 내거나 실제 게시한다."""
        if self.op_mode == "signal" or (self.cfg.is_live and not self.trading_enabled):
            self.gs.levels = lv
            self.gs.pending_confirm = None
            G.apply_transition(self.gs, G.State.ARMED, now)
            self._touched_levels = set()
            self._daily_grid_placed = True
            self.gs.seed = self._planned_seed()
            if not self._in_daily:
                await self.emit(EventKind.GRID, "🧭 [판단] 지금이라면 매수 대기 주문을 깔 자리입니다 (주문은 내지 않음)",
                                f"{reason}\n{self._levels_text(lv)}", data={"levels": lv.to_dict()})
            return
        if self.op_mode == "confirm" and not force:
            self.gs.pending_confirm = {"levels": lv.to_dict(), "requested_at": now.isoformat(), "reason": reason}
            await self.emit(EventKind.ARM_CONFIRM, "🙋 매수 대기 주문을 깔까요?",
                            f"{reason}\n{self._levels_text(lv)}\n\n아래 버튼으로 답해 주세요. {self.cfg.confirm_timeout_min}분 안에 답이 없으면 이번엔 깔지 않습니다.",
                            severity="warn", data={"levels": lv.to_dict()})
            return

        balances = self._balances()
        price = self._price(fallback=lv.close)
        try:
            lv, adjusted = clamp_below_price(lv, price, self.cfg.levels.min_gap_pct)
        except ValueError as exc:
            await self.emit(EventKind.REFUSAL, "그리드 게시 거절 [near_sl]",
                            f"현재가 {price:,.0f}가 손절선에 너무 가까워 레벨을 만들 수 없습니다: {exc}", "warn")
            return
        if adjusted:
            reason += f"\n(현재가 {price:,.0f} 아래로 조정한 단: {', '.join(f'P{i}' for i in adjusted)})"
        seed = self._seed(balances)
        info = self.exchange.market_info(self.symbol)
        min_cost = max(float(info.get("min_cost", 5000.0)), self.cfg.risk.min_order_cost)
        try:
            plan = plan_orders(lv, seed, min_cost)
        except ValueError as exc:
            await self.emit(EventKind.REFUSAL, "⛔ 매수 대기 주문을 깔지 않았습니다", str(exc), "warn")
            return
        refusal = self.guard.check_arm(self._equity(price, balances), sum(p["cost"] for p in plan),
                                       min(p["cost"] for p in plan))
        if refusal is not None:
            await self.emit(EventKind.REFUSAL, f"⛔ 매수 대기 주문을 깔지 않았습니다 [{refusal.rule}]",
                            f"이유: {refusal.detail}", "warn")
            return

        self.gs.levels = lv
        self.gs.pending_confirm = None
        G.apply_transition(self.gs, G.State.ARMED, now)
        self.gs.seed = seed
        placed: list[str] = []
        immediate: list[tuple[int, Order]] = []
        for p in plan:
            meta = {"strategy": STRATEGY_NAME, "reason": f"P{p['level']} 지정가", "level": p["level"]}
            try:
                order = self.exchange.create_limit_buy(self.symbol, p["price"], p["qty"], meta)
            except Exception as exc:  # noqa: BLE001
                log.exception("P%d 게시 실패", p["level"])
                await self.emit(EventKind.ERROR, f"P{p['level']} 게시 실패", str(exc), "error")
                continue
            if order.status == "closed":
                immediate.append((p["level"], order))
            else:
                self.gs.orders[p["level"]] = order.id
            placed.append(f"P{p['level']} {p['price']:,.0f} x {p['qty']:.6f} ({p['cost']:,.0f})")
        self._save()
        self._daily_grid_placed = True
        if not self._in_daily:
            await self.emit(EventKind.GRID, "📌 매수 대기 주문을 깔았습니다" + ("" if self.cfg.is_live else " (모의)"),
                            f"{reason}\n{self._levels_text(lv, price)}", data={"levels": lv.to_dict(), "seed": seed})
        for level, order in immediate:
            await self._on_fill(level, self._order_to_fill(order, level))

    async def _cancel_grid(self, reason: str) -> None:
        """열린 지정가를 모두 취소한다. 취소 전에 체결된 건은 체결로 처리한다."""
        for level, oid in list(self.gs.orders.items()):
            try:
                order = self.exchange.cancel_order(oid, self.symbol)
            except Exception as exc:  # noqa: BLE001
                log.warning("P%d 취소 실패(%s): %s", level, oid, exc)
                await self.emit(EventKind.ERROR, f"P{level} 취소 실패", str(exc), "error")
                continue
            if order.filled > 0:
                await self._on_fill(level, self._order_to_fill(order, level), transition=False)
            self.gs.orders.pop(level, None)
        if reason and not self._in_daily:
            await self.emit(EventKind.GRID, "↩️ 매수 대기 주문을 거둬들였습니다", f"이유: {reason}")

    def _order_to_fill(self, order: Order, level: int) -> Fill:
        conv = getattr(self.exchange, "order_to_fill", None)
        meta = {"strategy": STRATEGY_NAME, "reason": f"P{level} 체결", "level": level}
        if callable(conv):
            return conv(order, meta)
        # 페이퍼: 거래소 체결 목록에서 같은 주문 id 를 찾는다
        for f in getattr(self.exchange, "fills", []):
            if f.order_id == order.id:
                f.level = level
                f.strategy = STRATEGY_NAME
                return f
        price = float(order.avg_price or order.price or 0.0)
        return Fill(symbol=self.symbol, side="buy", qty=order.filled, price=price, cost=order.cost or order.filled * price,
                    fee=order.fee, ts=order.ts or self.clock.now(), mode=self.cfg.mode, strategy=STRATEGY_NAME,
                    reason=f"P{level} 체결", order_id=order.id, level=level)

    async def _on_fill(self, level: int, fill: Fill, transition: bool = True) -> None:
        """한 단 체결 반영."""
        if fill.qty <= 0:
            self.gs.orders.pop(level, None)
            return
        if self.gs.position is None or not self.gs.position.is_open:
            lv = self.gs.levels
            self.gs.position = Position(symbol=self.symbol, stop=lv.sl if lv else None, take=lv.tp if lv else None)
        self.gs.position.add_fill(fill)
        self.store.record_fill(fill)
        decisions = G.on_level_filled(self.gs, level) if transition else []
        if not transition:
            if level not in self.gs.filled_levels:
                self.gs.filled_levels.append(level)
            self.gs.orders.pop(level, None)
            if self.gs.state is G.State.ARMED:
                decisions = [G.Transition(G.State.IN_POSITION, f"P{level} 체결")]
        for d in decisions:
            if isinstance(d, G.Transition):
                G.apply_transition(self.gs, d.to, self.clock.now())
        self._save()
        pos = self.gs.position
        q = self.quote
        lv = self.gs.levels
        n_total = len(lv.prices) if lv else 0
        remaining = [i for i in range(1, n_total + 1) if i not in self.gs.filled_levels]
        lines = [
            f"{H.won(fill.price, q)}에 {H.qty_text(fill.qty, self.base)}, {H.won(fill.cost, q)}어치 샀습니다.",
            f"지금까지 {len(self.gs.filled_levels)}/{n_total}단 매수, 평균 단가 {H.won(pos.entry_price, q)}, 총 {H.won(pos.cost, q)}.",
        ]
        if remaining and lv:
            nxt = remaining[0]
            lines.append(f"다음: {nxt}단 {H.won(lv.prices[nxt - 1], q)} 까지 더 떨어지면 추가 매수 대기 중.")
        if lv:
            lines.append(f"손절선 {H.won(lv.sl, q)} (일봉 종가 기준) / 익절선 {H.won(lv.tp, q)}.")
        await self.emit(EventKind.ENTRY, f"🛒 {level}단 매수 체결" + ("" if self.cfg.is_live else " (모의)"),
                        "\n".join(lines),
                        data={"symbol": self.symbol, "price": fill.price, "qty": fill.qty, "level": level,
                              "stop": pos.stop, "take": pos.take})

    async def _sell_qty(self, qty: float, reason: str, exit_reason: str) -> Trade | None:
        pos = self.gs.position
        if pos is None or not pos.is_open or qty <= 0:
            return None
        qty = min(qty, pos.qty)
        meta = {"strategy": STRATEGY_NAME, "reason": reason}
        fill = self.exchange.create_market_sell(self.symbol, qty, meta)
        self.store.record_fill(fill)
        cost_part, fee_part = pos.remove_qty(fill.qty)
        proceeds = fill.cost - fill.fee
        pnl = proceeds - (cost_part + fee_part)
        trade = Trade(
            symbol=self.symbol, strategy=STRATEGY_NAME, entry_time=pos.entry_time or fill.ts, exit_time=fill.ts,
            entry_price=cost_part / fill.qty if fill.qty else 0.0, exit_price=fill.price, qty=fill.qty,
            pnl=pnl, pnl_pct=(pnl / cost_part * 100.0) if cost_part else 0.0, fee=fee_part + fill.fee,
            exit_reason=exit_reason,
        )
        self.store.record_trade(trade)
        self.guard.on_trade_closed(trade)
        if not pos.is_open:
            self.gs.position = None
        self._save()
        q = self.quote
        titles = {
            "stop_daily": "🛑 손절했습니다", "stop_disaster": "🚨 급락 방어로 전부 팔았습니다",
            "tp1": "💰 절반 익절했습니다", "trail": "💰 나머지도 익절했습니다",
            "manual": "↩️ 요청대로 전부 팔았습니다", "kill": "🔴 킬 스위치로 전부 팔았습니다",
        }
        nexts = {
            "stop_daily": f"다음: {self.cfg.reentry_cooldown_days}일 쉬고, 추세가 살아 있으면 다시 매수 대기 주문을 깝니다.",
            "stop_disaster": f"다음: {self.cfg.reentry_cooldown_days}일 쉬고, 추세가 살아 있으면 다시 매수 대기 주문을 깝니다.",
            "tp1": f"다음: 남은 절반은 고점 대비 {self.cfg.levels.trail_pct:.0f}% 빠지면 팝니다. 매수 대기 주문은 거뒀습니다.",
            "trail": f"다음: {self.cfg.reentry_cooldown_days}일 쉬고 새 박스에서 다시 시작합니다.",
        }
        sign = "이익" if pnl >= 0 else "손실"
        lines = [
            f"이유: {reason}",
            f"{H.won(fill.price, q)}에 {H.qty_text(fill.qty, self.base)} 팔았습니다. {sign} {H.won(abs(pnl), q)} ({trade.pnl_pct:+.1f}%).",
        ]
        if self.gs.position is not None and self.gs.position.is_open:
            lines.append(f"남은 보유: {H.qty_text(self.gs.position.qty, self.base)} (평단 {H.won(self.gs.position.entry_price, q)}).")
        if exit_reason in nexts:
            lines.append(nexts[exit_reason])
        await self.emit(EventKind.EXIT, titles.get(exit_reason, "청산") + ("" if self.cfg.is_live else " (모의)"),
                        "\n".join(lines),
                        data={"symbol": self.symbol, "price": fill.price, "qty": fill.qty, "pnl": pnl,
                              "pnl_pct": trade.pnl_pct, "exit_reason": exit_reason})
        return trade

    async def _sell_all(self, reason: str, exit_reason: str) -> None:
        if self.gs.position is None:
            return
        await self._sell_qty(self.gs.position.qty, reason, exit_reason)

    async def _sell_pct(self, pct: float, reason: str, exit_reason: str) -> None:
        if self.gs.position is None:
            return
        await self._sell_qty(self.gs.position.qty * pct / 100.0, reason, exit_reason)

    # ---------- 시세 ----------
    def _price(self, fallback: float | None = None) -> float:
        try:
            return float(self.exchange.fetch_price(self.symbol))
        except Exception as exc:  # noqa: BLE001
            log.warning("현재가 조회 실패: %s", exc)
            if fallback:
                return float(fallback)
            if self.gs.last_price:
                return float(self.gs.last_price)
            raise

    def _fetch_daily(self, now: datetime) -> pd.DataFrame:
        need = self.cfg.levels.sma_len + 40
        df = self.exchange.fetch_ohlcv(self.symbol, "1d", limit=need)
        return closed_daily(df, now)

    # ---------- 주기 작업 ----------
    async def tick(self) -> None:
        """한 사이클: 체결 확인 → 틱 판단 → 승인 타임아웃 → signal 모드 레벨 알림."""
        async with self._lock:
            self.cycles += 1
            now = self.clock.now()
            self.last_tick_at = now
            try:
                price = self._price()
            except Exception:  # noqa: BLE001
                return
            sync = getattr(self.exchange, "sync", None)
            if callable(sync):
                sync(self.symbol)
            await self._check_fills()
            decisions = G.evaluate_tick(self.gs, price, self.cfg, now)
            if decisions:
                await self.execute(decisions, now)
            await self._check_confirm_timeout(now)
            await self._signal_level_touch(price)
            self.store.set_state("heartbeat", now.isoformat())
            self._save()
            if self.cycles % 30 == 1:
                log.info("cycle %d 상태 %s 가격 %.0f", self.cycles, self.gs.state.value, price)

    async def _check_fills(self) -> None:
        for level, oid in list(self.gs.orders.items()):
            try:
                order = self.exchange.fetch_order(oid, self.symbol)
            except Exception as exc:  # noqa: BLE001
                log.warning("P%d 주문 조회 실패(%s): %s", level, oid, exc)
                continue
            if order.status == "closed" or (order.status == "canceled" and order.filled > 0):
                await self._on_fill(level, self._order_to_fill(order, level))
            elif order.status == "canceled":
                self.gs.orders.pop(level, None)
                await self.emit(EventKind.GRID, f"⚠️ {level}단 매수 대기 주문이 거래소에서 취소됐습니다",
                                f"봇이 취소한 게 아닙니다(주문 {oid}). 직접 취소한 게 아니면 거래소 앱을 확인하세요.", "warn")

    async def _check_confirm_timeout(self, now: datetime) -> None:
        pc = self.gs.pending_confirm
        if not pc:
            return
        requested = datetime.fromisoformat(pc["requested_at"])
        if now >= requested + timedelta(minutes=self.cfg.confirm_timeout_min):
            self.gs.pending_confirm = None
            await self.emit(EventKind.GRID, "⏱ 답이 없어 이번엔 매수 대기 주문을 깔지 않았습니다",
                            "다음 일봉 판정 때 다시 물어봅니다. 지금 깔려면 /arm.", "warn")

    async def _signal_level_touch(self, price: float) -> None:
        """주문 없는 모드(signal/live_lock)에서 레벨 도달을 알린다."""
        if self.trading_enabled or self.gs.state is not G.State.ARMED or self.gs.levels is None:
            return
        for i, p in enumerate(self.gs.levels.prices, start=1):
            if price <= p and i not in self._touched_levels:
                self._touched_levels.add(i)
                w = self.gs.levels.weights_pct[i - 1]
                await self.emit(EventKind.LEVEL_TOUCH, f"🧭 [판단] {i}단 매수 가격에 왔습니다 (주문은 내지 않음)",
                                f"현재가 {H.won(price, self.quote)} 가 {i}단 {H.won(p, self.quote)} 아래로 내려왔습니다.\n"
                                f"실제 매매였다면 시드의 {w:.0f}%를 여기서 샀을 자리입니다.",
                                data={"symbol": self.symbol, "price": price, "level": i})

    async def daily_check(self, force: bool = False) -> None:
        """일봉 확정 직후 판정. 같은 봉을 두 번 처리하지 않는다."""
        async with self._lock:
            now = self.clock.now()
            try:
                daily = self._fetch_daily(now)
            except Exception as exc:  # noqa: BLE001
                log.exception("일봉 조회 실패")
                await self.emit(EventKind.ERROR, "일봉 조회 실패", str(exc), "error")
                return
            if daily is None or daily.empty:
                await self.emit(EventKind.ERROR, "일봉 판정 불가", "확정 일봉이 없습니다.", "error")
                return
            last_ts = daily.index[-1].isoformat()
            self._last_daily = daily
            if not force and self.gs.last_daily_ts == last_ts:
                log.info("일봉 %s 은 이미 판정했습니다.", last_ts)
                return
            stale = self.guard.check_stale(daily, "1d", now)
            if stale is not None:
                await self.emit(EventKind.ERROR, "일봉 데이터 지연", f"{stale.detail}\n오늘 판단은 건너뜁니다. 거래소 API 상태를 확인하세요.", "warn")
                return
            await self._check_fills()
            try:
                self.gs.last_price = self._price()
            except Exception:  # noqa: BLE001
                pass
            state_before = self.gs.state
            self._in_daily = True
            self._daily_grid_placed = False
            try:
                decisions = G.evaluate_daily(self.gs, daily, self.cfg, now)
                await self.execute(decisions, now)
            finally:
                self._in_daily = False
            await self.emit(EventKind.DAILY_CHECK, f"📊 오늘의 판단 ({now.astimezone(KST):%-m/%-d %H:%M} 일봉 마감 기준)",
                            self._daily_summary(state_before, now),
                            data={"state": self.gs.state.value, **self.gs.trend})

    def _daily_summary(self, state_before: G.State, now: datetime) -> str:
        """09:00 한 통으로 끝나는 사람용 요약."""
        t = self.gs.trend
        q = self.quote
        price = self.gs.last_price or t.get("close") or 0.0
        close, sma = float(t.get("close") or 0.0), float(t.get("sma") or 0.0)
        lines: list[str] = []
        if t.get("ok"):
            lines.append(f"추세: 상승 ✅  어제 종가 {H.won(close, q)} > 200일 평균 {H.won(sma, q)} ({H.pct(close, sma)})")
        else:
            lines.append(f"추세: 하락 ❌  어제 종가 {H.won(close, q)} < 200일 평균 {H.won(sma, q)} ({H.pct(close, sma)})")
        lines.append("")
        st = self.gs.state
        lv = self.gs.levels
        pos = self.gs.position
        mock = "" if self.cfg.is_live else " (모의)"
        if st is G.State.IN_POSITION and pos is not None and lv is not None:
            lines.append(f"보유 중{mock}: {len(self.gs.filled_levels)}/{len(lv.prices)}단 매수, 평균 단가 {H.won(pos.entry_price, q)}, 현재 {H.pct(price, pos.entry_price)}")
            lines.append(f"결정: 계속 보유합니다. 어제 종가가 손절선 {H.won(lv.sl, q)} 위에서 마감했습니다.")
            lines.append(f"익절선 {H.won(lv.tp, q)}까지 {H.pct(lv.tp, price)}, 손절선까지 {H.pct(lv.sl, price)}.")
            if self.gs.tp1_done and self.gs.trail_high:
                lines.append(f"절반은 이미 익절했고, 나머지는 고점 {H.won(self.gs.trail_high, q)} 대비 {self.cfg.levels.trail_pct:.0f}% 빠지면 팝니다.")
            if self.gs.orders:
                nxt = min(self.gs.orders)
                lines.append(f"추가 매수 대기: {nxt}단 {H.won(lv.prices[nxt - 1], q)} ({H.pct(lv.prices[nxt - 1], price)})")
        elif st is G.State.ARMED and lv is not None:
            if self.trading_enabled:
                head = "결정: 매수 대기 주문을 " + ("깔았습니다" if self._daily_grid_placed else "그대로 둡니다") + mock + "."
            else:
                head = "결정: 지금이 매수 대기 자리입니다. (판단만, 주문 없음)"
            lines.append(head)
            lines.append(f"현재가 {H.won(price, q)}. 아래 가격까지 내려오면 나눠서 삽니다.")
            lines += H.level_lines(lv.to_dict(), price, self.gs.filled_levels, list(self.gs.orders), q, self.gs.seed)
            lines += H.exit_lines(lv.to_dict(), price, q, self.cfg.levels.tp1_pct)
            lines.append("")
            lines.append("한 줄: 상승장이라 박스 아래쪽 눌림을 기다립니다. 급락이 없으면 오늘은 아무 일도 없습니다.")
        elif st is G.State.EXITED:
            lines.append("결정: 최근 청산 직후라 하루 쉽니다. 내일 추세가 살아 있으면 다시 매수 대기 주문을 깝니다.")
        else:
            if state_before is G.State.ARMED:
                lines.append("결정: 추세가 꺾여 매수 대기 주문을 거뒀습니다. 200일 평균 위로 다시 올라올 때까지 사지 않습니다.")
            elif t.get("ok"):
                lines.append("결정: 조건은 맞지만 매수 대기 주문을 깔지 못했습니다. 바로 위 경고 메시지를 확인하세요.")
            else:
                lines.append("결정: 아무것도 하지 않습니다. 하락 추세에서는 사지 않습니다. 종가가 200일 평균 위로 올라오면 알려드립니다.")
        if self.cfg.dca.enabled and self._last_daily is not None:
            lines.append("")
            lines.append(self.dca_text(self._last_daily, price))
        return "\n".join(lines)

    def dca_text(self, daily: pd.DataFrame, price: float) -> str:
        """적립 추가 매수 지표 섹션."""
        q = self.quote
        v = dca_verdict(daily, price, self.cfg.dca)
        head = v.headline(self.cfg.dca.base_amount, lambda x: H.won(x, q))
        lines = [f"💰 적립 추가 매수 지표: {head}"]
        lines += [f"  · {r}" for r in v.reasons]
        lines.append("  (참고용 규칙 점수입니다. 최종 판단은 본인이 합니다)")
        return "\n".join(lines)

    def dca_now(self) -> str:
        """/dca 명령: 지금 가격으로 계산."""
        try:
            now = self.clock.now()
            daily = self._fetch_daily(now)
            price = self._price()
        except Exception as exc:  # noqa: BLE001
            return f"계산 실패: {exc}"
        self._last_daily = daily
        return f"현재가 {H.won(price, self.quote)}\n" + self.dca_text(daily, price)

    async def daily_report(self) -> None:
        date = self.clock.now().astimezone(KST).strftime("%Y-%m-%d")
        pnl = self.store.get_daily_pnl(date)
        if pnl == 0:
            log.info("%s 실현 손익 없음", date)
            return
        await self.emit(EventKind.DAILY_REPORT, f"🧾 {date} 실현 손익",
                        f"{'이익' if pnl > 0 else '손실'} {H.won(abs(pnl), self.quote)}", data={"pnl": pnl})

    async def heartbeat(self) -> None:
        self.store.set_state("heartbeat", self.clock.now().isoformat())

    # ---------- 재시작 대조 ----------
    async def reconcile(self) -> None:
        """저장된 상태와 거래소 실제 상태를 맞춘다."""
        await self._check_fills()
        base, _ = split_symbol(self.symbol)
        notes: list[str] = []
        try:
            open_orders = self.exchange.fetch_open_orders(self.symbol)
        except Exception as exc:  # noqa: BLE001
            open_orders = []
            notes.append(f"미체결 조회 실패: {exc}")
        known = set(self.gs.orders.values())
        foreign = [o for o in open_orders if o.id not in known and o.side == "buy"]
        if foreign:
            notes.append(f"봇이 모르는 미체결 매수 {len(foreign)}건이 있습니다(건드리지 않음).")
        missing = [lvl for lvl, oid in self.gs.orders.items() if oid not in {o.id for o in open_orders}]
        for lvl in missing:
            try:
                order = self.exchange.fetch_order(self.gs.orders[lvl], self.symbol)
            except Exception:  # noqa: BLE001
                self.gs.orders.pop(lvl, None)
                notes.append(f"P{lvl} 주문을 거래소에서 찾지 못해 목록에서 뺐습니다.")
        if self.gs.has_position and self.cfg.is_live:
            held = float(self._balances().get(base, 0.0))
            if held < self.gs.position.qty * 0.98:
                notes.append(f"보유 수량 불일치: 기록 {self.gs.position.qty:.6f} vs 거래소 {held:.6f}. 수동 매도 여부를 확인하세요.")
        if self.gs.state is G.State.ARMED and not self.gs.orders and self.trading_enabled and not self.gs.filled_levels:
            notes.append("ARMED 인데 열린 주문이 없어 IDLE 로 되돌립니다. 다음 일봉 판정에서 재게시합니다.")
            G.apply_transition(self.gs, G.State.IDLE, self.clock.now())
        self._save()
        if notes:
            await self.emit(EventKind.INFO, "⚠️ 재시작 후 확인이 필요합니다", "\n".join(notes), "warn")
        else:
            log.info("재시작 대조: 저장 상태와 거래소 상태가 일치합니다 (%s)", self.gs.state.value)

    # ---------- 명령 ----------
    async def arm(self, force: bool = True) -> str:
        """지금 추세·레벨을 계산해 게시한다(confirm 모드도 force 면 바로)."""
        async with self._lock:
            if self.gs.state is G.State.ARMED and self.gs.orders:
                return "이미 매수 대기 주문이 깔려 있습니다. /levels 로 확인하세요."
            if self.gs.state is G.State.IN_POSITION:
                return "보유 중에는 새로 깔지 않습니다."
            now = self.clock.now()
            daily = self._fetch_daily(now)
            t = trend_filter(daily, self.cfg.levels.sma_len)
            self.gs.trend = {"ok": t.ok, "close": t.close, "sma": t.sma, "candle_ts": t.candle_ts, "reason": t.reason}
            if not t.ok:
                self._save()
                return f"하락 추세라 깔지 않습니다. 어제 종가 {H.won(t.close, self.quote)} < 200일 평균 {H.won(t.sma, self.quote)}"
            try:
                lv = compute_levels(daily, self.cfg.levels, now)
            except ValueError as exc:
                return f"레벨 계산 실패: {exc}"
            if self.gs.state is G.State.EXITED:
                G.apply_transition(self.gs, G.State.IDLE, now)
            await self._place_grid(lv, "수동 /arm", now, force=force)
            self._save()
            return f"처리했습니다. 상태: {self.gs.state.value}"

    async def confirm_arm(self) -> str:
        async with self._lock:
            pc = self.gs.pending_confirm
            if not pc:
                return "승인 대기 중인 게시가 없습니다."
            lv = Levels.from_dict(pc["levels"])
            await self._place_grid(lv, pc.get("reason", "승인"), self.clock.now(), force=True)
            return f"깔았습니다. 상태: {self.gs.state.value}"

    async def disarm(self) -> str:
        async with self._lock:
            await self._cancel_grid("수동 /disarm")
            if self.gs.state is G.State.ARMED:
                G.apply_transition(self.gs, G.State.IDLE, self.clock.now())
            self._save()
            return f"거뒀습니다. 상태: {self.gs.state.value}"

    def set_mode(self, mode: str) -> str:
        if mode not in ("signal", "confirm", "auto"):
            return "모드는 signal | confirm | auto 중 하나입니다."
        self.op_mode = mode
        self.store.set_state("op_mode", mode)
        return f"운용 모드를 {mode} 로 바꿨습니다."

    def set_fixed_level(self, key: str, price: float, weight: float | None = None) -> str:
        """고정 레벨 값을 바꾼다. 적용은 /arm."""
        key = key.lower()
        lc = self.cfg.levels
        n = len(lc.offsets_atr)
        valid = {f"p{i + 1}" for i in range(n)} | {"sl", "tp"}
        if key not in valid:
            return f"키는 {', '.join(sorted(valid))} 중 하나입니다."
        lc.fixed[key] = float(price)
        if weight is not None and key.startswith("p"):
            idx = int(key[1:]) - 1
            lc.weights_pct[idx] = float(weight)
        lc.mode = "fixed"
        self.store.set_state("fixed_levels", {"fixed": lc.fixed, "weights_pct": lc.weights_pct})
        missing = sorted(valid - set(lc.fixed))
        tail = f" (아직 없음: {', '.join(missing)})" if missing else " 모든 값이 채워졌습니다. /arm 으로 적용하세요."
        return f"{key} = {price:,.0f}{tail}"

    async def close_all(self, reason: str) -> None:
        async with self._lock:
            await self._cancel_grid("")
            if self.gs.has_position:
                await self._sell_all(reason, "manual")
            G.apply_transition(self.gs, G.State.EXITED, self.clock.now())
            self._save()

    def pause(self) -> None:
        self.set_mode("signal")

    def resume(self) -> None:
        self.set_mode(self.cfg.op_mode if self.cfg.op_mode != "signal" else "auto")

    def stop(self) -> None:
        self._stop.set()

    def status(self) -> dict:
        pos = self.gs.position
        return {
            "mode": self.cfg.mode, "op_mode": self.op_mode, "live_lock": self.cfg.live_lock, "quote": self.quote,
            "trading_enabled": self.trading_enabled, "order_mode": self.order_mode_text, "state": self.gs.state.value,
            "cycles": self.cycles, "last_tick_at": self.last_tick_at.isoformat() if self.last_tick_at else None,
            "price": self.gs.last_price, "trend": dict(self.gs.trend), "guard": self.guard.state,
            "orders": dict(self.gs.orders), "filled_levels": list(self.gs.filled_levels),
            "position": None if pos is None else {
                "qty": pos.qty, "entry_price": pos.entry_price, "stop": pos.stop, "take": pos.take,
                "pnl_pct": pos.pnl_pct(self.gs.last_price) if self.gs.last_price else None,
            },
            "levels": self.gs.levels.to_dict() if self.gs.levels else None,
            "pending_confirm": bool(self.gs.pending_confirm),
        }

    def levels_text(self) -> str:
        if self.gs.levels is None:
            return "계산된 레벨이 없습니다. 추세 필터 미통과이거나 아직 일봉 판정 전입니다."
        return self._levels_text(self.gs.levels)

    # ---------- 실행 루프 ----------
    async def run(self, max_cycles: int | None = None) -> None:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler

        saved_mode = self.store.get_state("op_mode")
        if saved_mode in ("signal", "confirm", "auto"):
            self.op_mode = saved_mode
        saved_fixed = self.store.get_state("fixed_levels")
        if saved_fixed and self.cfg.levels.mode == "fixed":
            self.cfg.levels.fixed.update(saved_fixed.get("fixed", {}))

        starter = getattr(self.notifier, "start", None)
        if callable(starter):
            await starter()
        state_names = {"IDLE": "관망", "ARMED": "매수 대기", "IN_POSITION": "보유 중", "EXITED": "청산 직후"}
        await self.emit(EventKind.INFO, "🟢 봇을 켰습니다",
                        f"{H.mode_line(self.cfg.is_live, self.trading_enabled)}\n"
                        f"종목 {self.symbol} ({self.cfg.exchange}), 현재 상태: {state_names.get(self.gs.state.value, self.gs.state.value)}.\n"
                        f"매일 {self.cfg.daily_close_hour_kst:02d}:00 일봉이 마감되면 그날의 판단을 한 통으로 보내드립니다. 궁금하면 /status.")
        await self.reconcile()
        await self.daily_check()

        sched = AsyncIOScheduler(timezone="Asia/Seoul")
        sched.add_job(self.tick, "interval", seconds=self.cfg.tick_seconds, max_instances=1, coalesce=True)
        sched.add_job(self.daily_check, "cron", hour=self.cfg.daily_close_hour_kst, minute=0,
                      second=self.cfg.daily_close_delay_sec, misfire_grace_time=3600)
        sched.add_job(self.daily_report, "cron", hour=self.cfg.daily_close_hour_kst, minute=5, misfire_grace_time=3600)
        sched.add_job(self.heartbeat, "interval", minutes=5)
        sched.start()
        if self.cfg.is_live and not self.cfg.live_lock and not self.live_confirmed:
            await self.emit(EventKind.LIVE_CONFIRM, "⚠️ 실제 주문을 시작할까요?",
                            "이 버튼을 누르면 진짜 돈으로 주문이 나갑니다. 첫날은 시드의 10%까지만 씁니다.", "warn")
        try:
            if max_cycles:
                while self.cycles < max_cycles and not self._stop.is_set():
                    await asyncio.sleep(0.5)
            else:
                await self._stop.wait()
        finally:
            sched.shutdown(wait=False)
            stopper = getattr(self.notifier, "stop", None)
            if callable(stopper):
                try:
                    await stopper()
                except Exception as exc:  # noqa: BLE001
                    log.warning("알림 채널 정지 실패: %s", exc)
            log.info("엔진 종료: 총 %d cycle", self.cycles)

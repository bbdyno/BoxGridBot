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
from ..strategy.levels import Levels, clamp_below_price, closed_daily, compute_levels, plan_orders, trend_filter
from .clock import KST, Clock
from .events import Event, EventKind
from .models import Fill, Order, Position, Trade
from .notify_null import NullNotifier
from .store import Store

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
                    await self.emit(EventKind.DAILY_CHECK, d.title, d.body, d.severity)
            except Exception as exc:  # noqa: BLE001
                log.exception("결정 실행 실패: %s", d)
                await self.emit(EventKind.ERROR, "결정 실행 실패", f"{type(d).__name__}: {exc}", "error")
        self._save()

    def _levels_text(self, lv: Levels) -> str:
        lines = []
        for i, (p, w) in enumerate(zip(lv.prices, lv.weights_pct), start=1):
            mark = "✅" if i in self.gs.filled_levels else ("📌" if i in self.gs.orders else "·")
            lines.append(f"{mark} P{i} {p:,.0f} ({w:.0f}%)")
        lines.append(f"SL {lv.sl:,.0f} (일봉 종가) / TP {lv.tp:,.0f} ({self.cfg.levels.tp1_pct:.0f}% 익절)")
        if lv.mode == "dynamic":
            lines.append(f"박스 {lv.box_low:,.0f}~{lv.box_high:,.0f} / ATR {lv.atr:,.0f} / SMA {lv.sma:,.0f}")
        return "\n".join(lines)

    async def _place_grid(self, lv: Levels, reason: str, now: datetime, force: bool = False) -> None:
        """그리드 게시. op_mode 와 잠금 상태에 따라 알림만 내거나 실제 게시한다."""
        if self.op_mode == "signal" or (self.cfg.is_live and not self.trading_enabled):
            self.gs.levels = lv
            self.gs.pending_confirm = None
            G.apply_transition(self.gs, G.State.ARMED, now)
            self._touched_levels = set()
            why = "signal 모드" if self.op_mode == "signal" else "실거래 잠금(live_lock)"
            await self.emit(EventKind.GRID, f"[판단] 매수 그리드 신호 ({why}, 주문 없음)",
                            f"{reason}\n{self._levels_text(lv)}", data={"levels": lv.to_dict()})
            return
        if self.op_mode == "confirm" and not force:
            self.gs.pending_confirm = {"levels": lv.to_dict(), "requested_at": now.isoformat(), "reason": reason}
            await self.emit(EventKind.ARM_CONFIRM, "그리드 게시 승인 요청",
                            f"{reason}\n{self._levels_text(lv)}\n{self.cfg.confirm_timeout_min}분 내 미응답 시 보류합니다.",
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
            await self.emit(EventKind.REFUSAL, "그리드 게시 거절", str(exc), "warn")
            return
        refusal = self.guard.check_arm(self._equity(price, balances), sum(p["cost"] for p in plan),
                                       min(p["cost"] for p in plan))
        if refusal is not None:
            await self.emit(EventKind.REFUSAL, f"그리드 게시 거절 [{refusal.rule}]", refusal.detail, "warn")
            return

        self.gs.levels = lv
        self.gs.pending_confirm = None
        G.apply_transition(self.gs, G.State.ARMED, now)
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
        await self.emit(EventKind.GRID, "그리드 게시", f"{reason}\n시드 {seed:,.0f}\n" + "\n".join(placed),
                        data={"levels": lv.to_dict(), "seed": seed})
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
        if reason:
            await self.emit(EventKind.GRID, "그리드 회수", reason)

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
        await self.emit(EventKind.ENTRY, f"P{level} 체결 {self.symbol}",
                        f"체결가 {fill.price:,.0f} / 수량 {fill.qty:.6f} / 금액 {fill.cost:,.0f}\n"
                        f"누적 {len(self.gs.filled_levels)}단, 평단 {pos.entry_price:,.0f}, 수량 {pos.qty:.6f}",
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
        await self.emit(EventKind.EXIT, f"청산({exit_reason}) {self.symbol}",
                        f"{reason}\n체결가 {fill.price:,.0f} / 수량 {fill.qty:.6f} / 손익 {pnl:,.0f} ({trade.pnl_pct:+.2f}%)",
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
                await self.emit(EventKind.GRID, f"P{level} 주문이 외부에서 취소됨", f"주문 {oid}", "warn")

    async def _check_confirm_timeout(self, now: datetime) -> None:
        pc = self.gs.pending_confirm
        if not pc:
            return
        requested = datetime.fromisoformat(pc["requested_at"])
        if now >= requested + timedelta(minutes=self.cfg.confirm_timeout_min):
            self.gs.pending_confirm = None
            await self.emit(EventKind.GRID, "게시 승인 시간 초과", "응답이 없어 이번 게시는 보류합니다.", "warn")

    async def _signal_level_touch(self, price: float) -> None:
        """주문 없는 모드(signal/live_lock)에서 레벨 도달을 알린다."""
        if self.trading_enabled or self.gs.state is not G.State.ARMED or self.gs.levels is None:
            return
        for i, p in enumerate(self.gs.levels.prices, start=1):
            if price <= p and i not in self._touched_levels:
                self._touched_levels.add(i)
                await self.emit(EventKind.LEVEL_TOUCH, f"[판단] P{i} 도달",
                                f"현재가 {price:,.0f} <= P{i} {p:,.0f}. 실거래라면 {self.gs.levels.weights_pct[i-1]:.0f}% 매수.",
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
            if not force and self.gs.last_daily_ts == last_ts:
                log.info("일봉 %s 은 이미 판정했습니다.", last_ts)
                return
            stale = self.guard.check_stale(daily, "1d", now)
            if stale is not None:
                await self.emit(EventKind.ERROR, "일봉 데이터 지연", stale.detail, "warn")
                return
            await self._check_fills()
            decisions = G.evaluate_daily(self.gs, daily, self.cfg, now)
            await self.execute(decisions, now)
            t = self.gs.trend
            await self.emit(EventKind.DAILY_CHECK, f"일봉 판정 완료 ({now.astimezone(KST):%m-%d %H:%M})",
                            f"상태 {self.gs.state.value} / 종가 {t.get('close', 0):,.0f} / SMA {t.get('sma', 0):,.0f} / "
                            f"추세 {'상승' if t.get('ok') else '이탈'}",
                            data={"state": self.gs.state.value, **t})

    async def daily_report(self) -> None:
        date = self.clock.now().astimezone(KST).strftime("%Y-%m-%d")
        pnl = self.store.get_daily_pnl(date)
        await self.emit(EventKind.DAILY_REPORT, f"{date} 일일 리포트",
                        f"실현 손익 {pnl:,.0f} / 상태 {self.gs.state.value} / 사이클 {self.cycles}", data={"pnl": pnl})

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
        body = "\n".join(notes) if notes else "저장 상태와 거래소 상태가 일치합니다."
        await self.emit(EventKind.INFO, f"재시작 대조: {self.gs.state.value}", body, "warn" if notes else "info")

    # ---------- 명령 ----------
    async def arm(self, force: bool = True) -> str:
        """지금 추세·레벨을 계산해 게시한다(confirm 모드도 force 면 바로)."""
        async with self._lock:
            if self.gs.state is G.State.ARMED and self.gs.orders:
                return "이미 게시 중입니다. /levels 로 확인하세요."
            if self.gs.state is G.State.IN_POSITION:
                return "보유 중에는 재게시하지 않습니다."
            now = self.clock.now()
            daily = self._fetch_daily(now)
            t = trend_filter(daily, self.cfg.levels.sma_len)
            self.gs.trend = {"ok": t.ok, "close": t.close, "sma": t.sma, "candle_ts": t.candle_ts, "reason": t.reason}
            if not t.ok:
                self._save()
                return f"추세 필터 미통과: {t.reason} (종가 {t.close:,.0f}, SMA {t.sma:,.0f})"
            try:
                lv = compute_levels(daily, self.cfg.levels, now)
            except ValueError as exc:
                return f"레벨 계산 실패: {exc}"
            if self.gs.state is G.State.EXITED:
                G.apply_transition(self.gs, G.State.IDLE, now)
            await self._place_grid(lv, "수동 /arm", now, force=force)
            self._save()
            return f"처리했습니다. 상태 {self.gs.state.value}"

    async def confirm_arm(self) -> str:
        async with self._lock:
            pc = self.gs.pending_confirm
            if not pc:
                return "승인 대기 중인 게시가 없습니다."
            lv = Levels.from_dict(pc["levels"])
            await self._place_grid(lv, pc.get("reason", "승인"), self.clock.now(), force=True)
            return f"게시했습니다. 상태 {self.gs.state.value}"

    async def disarm(self) -> str:
        async with self._lock:
            await self._cancel_grid("수동 /disarm")
            if self.gs.state is G.State.ARMED:
                G.apply_transition(self.gs, G.State.IDLE, self.clock.now())
            self._save()
            return f"회수했습니다. 상태 {self.gs.state.value}"

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
            "mode": self.cfg.mode, "op_mode": self.op_mode, "live_lock": self.cfg.live_lock,
            "trading_enabled": self.trading_enabled, "state": self.gs.state.value,
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
        await self.emit(EventKind.INFO, "엔진 시작",
                        f"거래소 {self.cfg.exchange} ({self.cfg.mode}), 운용 {self.op_mode}, "
                        f"실거래 {'가능' if self.trading_enabled else '잠금'}, tick {self.cfg.tick_seconds}초")
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
            await self.emit(EventKind.LIVE_CONFIRM, "라이브 매매 승인 요청",
                            "실제 주문을 시작하려면 아래 버튼을 눌러 승인해 주세요.", "warn")
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

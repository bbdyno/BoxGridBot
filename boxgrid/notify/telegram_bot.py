"""텔레그램 채널. 알림 발신 + 명령 수신(chat_id 화이트리스트) + 인라인 확인 버튼."""
from __future__ import annotations

import logging
from typing import Any

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

from ..core.events import Event, EventKind
from . import humanize as H

STATE_NAMES = {"IDLE": "관망 (하락 추세, 안 삼)", "ARMED": "매수 대기 (주문 깔아 둠)",
               "IN_POSITION": "보유 중", "EXITED": "청산 직후 (하루 쉼)"}

log = logging.getLogger(__name__)

HELP = """명령
/status 상태 요약
/trend 추세 필터·상태 머신
/levels 그리드 레벨과 체결 현황
/balance 포지션·손익
/report [YYYY-MM-DD] 일일 손익
/dca 적립 추가 매수 지표(지금 가격 기준)
/arm 지금 추세·레벨 계산해 게시
/disarm 게시 회수
/mode signal|confirm|auto 운용 모드
/set p1 79000000 20 고정 레벨(가격, 비중%)
/close 전량 청산(확인 버튼)
/kill 킬 스위치(2단계 확인)
/help 이 도움말"""


class TelegramChannel:
    name = "telegram"

    def __init__(self, token: str, chat_ids: list[int], engine_ref: Any = None) -> None:
        self.token = token
        self.chat_ids = set(chat_ids)
        self.engine = engine_ref
        self.app: Application | None = None
        self.bot: Bot | None = None

    async def start(self, app_loop: Any = None) -> None:
        self.app = Application.builder().token(self.token).build()
        self.bot = self.app.bot
        self._register_handlers()
        await self.app.initialize()
        await self.app.start()
        await self.app.updater.start_polling(drop_pending_updates=True)

    async def stop(self) -> None:
        if self.app is None:
            return
        if self.app.updater is not None:
            await self.app.updater.stop()
        await self.app.stop()
        await self.app.shutdown()

    def _register_handlers(self) -> None:
        app = self.app
        assert app is not None
        for name, fn in [
            ("start", self._cmd_help), ("help", self._cmd_help), ("status", self._cmd_status),
            ("trend", self._cmd_trend), ("levels", self._cmd_levels), ("balance", self._cmd_balance),
            ("report", self._cmd_report), ("dca", self._cmd_dca), ("arm", self._cmd_arm), ("disarm", self._cmd_disarm),
            ("mode", self._cmd_mode), ("set", self._cmd_set), ("close", self._cmd_close), ("kill", self._cmd_kill),
            ("pause", self._cmd_pause), ("resume", self._cmd_resume),
        ]:
            app.add_handler(CommandHandler(name, fn))
        app.add_handler(CallbackQueryHandler(self._on_callback))

    def _is_allowed(self, update: Update) -> bool:
        chat = update.effective_chat
        if chat is None or chat.id not in self.chat_ids:
            log.warning("화이트리스트 밖 chat_id 요청을 무시합니다: %s", getattr(chat, "id", None))
            return False
        return True

    async def _reply(self, update: Update, text: str, keyboard=None) -> None:
        await update.effective_message.reply_text(text, reply_markup=keyboard)

    def _need_engine(self) -> str | None:
        return None if self.engine is not None else "엔진이 연결되어 있지 않습니다."

    # ---------- 조회 ----------
    async def _cmd_help(self, update: Update, context: "ContextTypes.DEFAULT_TYPE") -> None:
        if self._is_allowed(update):
            await self._reply(update, HELP)

    async def _cmd_status(self, update: Update, context: "ContextTypes.DEFAULT_TYPE") -> None:
        if not self._is_allowed(update):
            return
        if (err := self._need_engine()):
            return await self._reply(update, err)
        s = self.engine.status()
        t = s.get("trend") or {}
        q = s.get("quote", "KRW")
        lines = [f"상태: {STATE_NAMES.get(s['state'], s['state'])}", f"{s.get('order_mode', '')}"]
        if s.get("price"):
            lines.append(f"현재가: {H.won(s['price'], q)}")
        if t:
            ok = t.get("ok")
            lines.append(f"추세: {'상승 ✅' if ok else '하락 ❌'}  (어제 종가 {H.won(t.get('close'), q)} vs 200일 평균 {H.won(t.get('sma'), q)})")
        pos = s.get("position")
        if pos:
            pnl = f" ({pos['pnl_pct']:+.1f}%)" if pos.get("pnl_pct") is not None else ""
            lines.append(f"보유: {len(s['filled_levels'])}단, 평단 {H.won(pos['entry_price'], q)}{pnl}")
        if s["orders"]:
            lines.append(f"매수 대기 주문: {len(s['orders'])}건 (/levels 로 가격 확인)")
        g = s.get("guard") or {}
        if g.get("killed"):
            lines.append("🔴 킬 스위치 켜짐: 새 주문 안 냄")
        if s.get("pending_confirm"):
            lines.append("🙋 매수 대기 주문 승인을 기다리는 중 (위 메시지의 버튼)")
        await self._reply(update, "\n".join(lines))

    async def _cmd_trend(self, update: Update, context: "ContextTypes.DEFAULT_TYPE") -> None:
        if not self._is_allowed(update):
            return
        if (err := self._need_engine()):
            return await self._reply(update, err)
        s = self.engine.status()
        t = s.get("trend") or {}
        q = s.get("quote", "KRW")
        if not t:
            return await self._reply(update, "아직 일봉 판정 전입니다. 09:00 이후 다시 보세요.")
        ok = t.get("ok")
        body = (f"{'상승 추세 ✅' if ok else '하락 추세 ❌'}\n"
                f"어제({t.get('candle_ts', '')[:10]}) 종가 {H.won(t.get('close'), q)}\n"
                f"200일 평균 {H.won(t.get('sma'), q)} ({H.pct(float(t.get('close') or 0), float(t.get('sma') or 1))})\n"
                f"{'평균선 위라 매수 후보를 봅니다.' if ok else '평균선 아래라 사지 않습니다.'}\n"
                f"봇 상태: {STATE_NAMES.get(s['state'], s['state'])}")
        await self._reply(update, body)

    async def _cmd_levels(self, update: Update, context: "ContextTypes.DEFAULT_TYPE") -> None:
        if not self._is_allowed(update):
            return
        if (err := self._need_engine()):
            return await self._reply(update, err)
        await self._reply(update, self.engine.levels_text())

    async def _cmd_balance(self, update: Update, context: "ContextTypes.DEFAULT_TYPE") -> None:
        if not self._is_allowed(update):
            return
        if (err := self._need_engine()):
            return await self._reply(update, err)
        s = self.engine.status()
        q = s.get("quote", "KRW")
        pos = s.get("position")
        if not pos:
            return await self._reply(update, "보유 포지션 없음")
        pnl = f"{pos['pnl_pct']:+.1f}%" if pos.get("pnl_pct") is not None else "-"
        await self._reply(update, f"보유 {pos['qty']:.4f}, 평균 단가 {H.won(pos['entry_price'], q)}\n평가 손익 {pnl}\n"
                                  f"손절선 {H.won(pos['stop'], q)} (일봉 종가 기준) / 익절선 {H.won(pos['take'], q)}")

    async def _cmd_report(self, update: Update, context: "ContextTypes.DEFAULT_TYPE") -> None:
        if not self._is_allowed(update):
            return
        if (err := self._need_engine()):
            return await self._reply(update, err)
        date = context.args[0] if context.args else None
        pnl = self.engine.store.get_daily_pnl(date)
        trades = self.engine.store.recent_trades(5)
        q = self.engine.status().get("quote", "KRW")
        names = {"stop_daily": "손절", "stop_disaster": "급락 방어", "tp1": "절반 익절", "trail": "익절", "manual": "수동", "kill": "킬"}
        lines = [f"오늘 실현 손익: {'+' if pnl >= 0 else '-'}{H.won(abs(pnl), q)}", f"최근 거래 {len(trades)}건"]
        for t in trades:
            lines.append(f"- {t['exit_time'][:10]} {names.get(t['exit_reason'], t['exit_reason'])} {'+' if t['pnl'] >= 0 else '-'}{H.won(abs(t['pnl']), q)} ({t['pnl_pct']:+.1f}%)")
        await self._reply(update, "\n".join(lines))

    async def _cmd_dca(self, update: Update, context: "ContextTypes.DEFAULT_TYPE") -> None:
        if not self._is_allowed(update):
            return
        if (err := self._need_engine()):
            return await self._reply(update, err)
        await self._reply(update, self.engine.dca_now())

    # ---------- 제어 ----------
    async def _cmd_arm(self, update: Update, context: "ContextTypes.DEFAULT_TYPE") -> None:
        if not self._is_allowed(update):
            return
        if (err := self._need_engine()):
            return await self._reply(update, err)
        await self._reply(update, await self.engine.arm(force=True))

    async def _cmd_disarm(self, update: Update, context: "ContextTypes.DEFAULT_TYPE") -> None:
        if not self._is_allowed(update):
            return
        if (err := self._need_engine()):
            return await self._reply(update, err)
        await self._reply(update, await self.engine.disarm())

    async def _cmd_mode(self, update: Update, context: "ContextTypes.DEFAULT_TYPE") -> None:
        if not self._is_allowed(update):
            return
        if (err := self._need_engine()):
            return await self._reply(update, err)
        if not context.args:
            return await self._reply(update, f"현재 운용 모드: {self.engine.op_mode}\n사용법: /mode signal|confirm|auto")
        await self._reply(update, self.engine.set_mode(context.args[0].lower()))

    async def _cmd_set(self, update: Update, context: "ContextTypes.DEFAULT_TYPE") -> None:
        if not self._is_allowed(update):
            return
        if (err := self._need_engine()):
            return await self._reply(update, err)
        args = context.args or []
        if len(args) < 2:
            return await self._reply(update, "사용법: /set p1 79000000 [비중%]  또는  /set sl 73600000")
        try:
            price = float(args[1].replace(",", ""))
            weight = float(args[2]) if len(args) > 2 else None
        except ValueError:
            return await self._reply(update, "가격·비중은 숫자여야 합니다.")
        await self._reply(update, self.engine.set_fixed_level(args[0], price, weight))

    async def _cmd_pause(self, update: Update, context: "ContextTypes.DEFAULT_TYPE") -> None:
        if not self._is_allowed(update):
            return
        if self.engine is not None:
            self.engine.pause()
        await self._reply(update, "주문을 멈췄습니다. 판단과 알림만 계속합니다. 되돌리려면 /resume.")

    async def _cmd_resume(self, update: Update, context: "ContextTypes.DEFAULT_TYPE") -> None:
        if not self._is_allowed(update):
            return
        if self.engine is not None:
            self.engine.resume()
        await self._reply(update, f"운용 모드: {self.engine.op_mode if self.engine else '-'}")

    async def _cmd_close(self, update: Update, context: "ContextTypes.DEFAULT_TYPE") -> None:
        if not self._is_allowed(update):
            return
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("전량 청산 확인", callback_data="close:confirm")]])
        await self._reply(update, "그리드를 회수하고 모든 포지션을 청산할까요?", kb)

    async def _cmd_kill(self, update: Update, context: "ContextTypes.DEFAULT_TYPE") -> None:
        if not self._is_allowed(update):
            return
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("정말요?", callback_data="kill:step1")]])
        await self._reply(update, "킬 스위치를 누르면 즉시 전량 청산 후 신규 게시를 막습니다.", kb)

    async def _on_callback(self, update: Update, context: "ContextTypes.DEFAULT_TYPE") -> None:
        query = update.callback_query
        chat = update.effective_chat
        if query is None:
            return
        if chat is None or chat.id not in self.chat_ids:
            await query.answer()
            return
        data = query.data or ""
        await query.answer()
        eng = self.engine
        if data == "close:confirm":
            if eng is not None:
                await eng.close_all("텔레그램 /close")
            await query.edit_message_text("전량 청산을 실행했습니다.")
        elif data == "kill:step1":
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("네, 전량 청산", callback_data="kill:step2")]])
            await query.edit_message_text("정말요? 되돌릴 수 없습니다.", reply_markup=kb)
        elif data == "kill:step2":
            if eng is not None:
                eng.guard.kill("텔레그램 /kill")
                await eng.close_all("킬 스위치")
            await query.edit_message_text("킬 스위치를 실행했습니다.")
        elif data == "arm:confirm":
            msg = await eng.confirm_arm() if eng is not None else "엔진 없음"
            await query.edit_message_text(f"승인: {msg}")
        elif data == "arm:skip":
            if eng is not None:
                eng.gs.pending_confirm = None
            await query.edit_message_text("이번 게시는 보류했습니다.")
        elif data == "live:confirm":
            if eng is not None:
                eng.confirm_live()
            await query.edit_message_text("라이브 시작을 승인했습니다.")

    async def send(self, event: Event, text: str) -> bool:
        if self.bot is None:
            log.warning("텔레그램 봇이 시작되지 않아 발송을 건너뜁니다.")
            return False
        keyboard = None
        if event.kind == EventKind.LIVE_CONFIRM:
            keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("라이브 승인", callback_data="live:confirm")]])
        elif event.kind == EventKind.ARM_CONFIRM:
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("게시", callback_data="arm:confirm"),
                InlineKeyboardButton("보류", callback_data="arm:skip"),
            ]])
        ok = False
        for chat_id in self.chat_ids:
            try:
                await self.bot.send_message(chat_id=chat_id, text=text, reply_markup=keyboard)
                ok = True
            except Exception as exc:  # noqa: BLE001
                log.warning("텔레그램 발송 실패(chat_id=%s): %s", chat_id, exc)
        return ok

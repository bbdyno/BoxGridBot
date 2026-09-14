"""텔레그램 채널: 화이트리스트, 명령 라우팅, 버튼."""
from types import SimpleNamespace

import pytest

from boxgrid.core.events import Event, EventKind
from boxgrid.notify.telegram_bot import TelegramChannel


class FakeMessage:
    def __init__(self):
        self.sent = []

    async def reply_text(self, text, reply_markup=None):
        self.sent.append((text, reply_markup))


class FakeEngine:
    def __init__(self):
        self.op_mode = "auto"
        self.calls = []
        self.store = SimpleNamespace(get_daily_pnl=lambda d=None: 1234.0, recent_trades=lambda n: [])
        self.gs = SimpleNamespace(pending_confirm={"x": 1})
        self.guard = SimpleNamespace(kill=lambda r: self.calls.append(("kill", r)))

    def status(self):
        return {"state": "ARMED", "mode": "paper", "quote": "KRW", "op_mode": "auto", "trading_enabled": True, "order_mode": "페이퍼 주문(모의)", "price": 100.0,
                "trend": {"ok": True, "close": 100.0, "sma": 90.0, "candle_ts": "2026-09-01", "reason": "위"},
                "filled_levels": [], "orders": {1: "a"}, "guard": {}, "cycles": 3, "pending_confirm": False,
                "position": {"qty": 1.0, "entry_price": 100.0, "stop": 90.0, "take": 120.0, "pnl_pct": 1.0}}

    def levels_text(self):
        return "P1 100"

    def dca_now(self):
        return "현재가 100원\n💰 적립 추가 매수 지표: 50점"

    async def arm(self, force=True):
        self.calls.append(("arm", force)); return "armed"

    async def disarm(self):
        self.calls.append(("disarm",)); return "disarmed"

    def set_mode(self, m):
        self.op_mode = m; return f"mode {m}"

    def set_fixed_level(self, k, p, w=None):
        return f"{k}={p}/{w}"

    async def close_all(self, reason):
        self.calls.append(("close", reason))

    async def confirm_arm(self):
        self.calls.append(("confirm",)); return "ok"

    def confirm_live(self):
        self.calls.append(("live",))

    def pause(self):
        self.op_mode = "signal"

    def resume(self):
        self.op_mode = "auto"


def _update(chat_id=1):
    msg = FakeMessage()
    return SimpleNamespace(effective_chat=SimpleNamespace(id=chat_id), effective_message=msg), msg


def _ctx(args=None):
    return SimpleNamespace(args=args or [])


@pytest.fixture
def ch():
    eng = FakeEngine()
    return TelegramChannel("token", [1], eng), eng


async def test_whitelist_rejects_other_chat(ch):
    c, _ = ch
    upd, msg = _update(chat_id=999)
    await c._cmd_status(upd, _ctx())
    assert msg.sent == []


async def test_status_levels_balance(ch):
    c, _ = ch
    upd, msg = _update()
    await c._cmd_status(upd, _ctx())
    assert "매수 대기" in msg.sent[0][0] and "페이퍼 주문" in msg.sent[0][0]
    await c._cmd_levels(upd, _ctx())
    assert msg.sent[1][0] == "P1 100"
    await c._cmd_balance(upd, _ctx())
    assert "평균 단가 100원" in msg.sent[2][0]
    await c._cmd_trend(upd, _ctx())
    assert "200일 평균" in msg.sent[3][0]
    await c._cmd_report(upd, _ctx())
    assert "1,234원" in msg.sent[4][0]
    await c._cmd_dca(upd, _ctx())
    assert "적립 추가 매수 지표" in msg.sent[5][0]


async def test_control_commands(ch):
    c, eng = ch
    upd, msg = _update()
    await c._cmd_arm(upd, _ctx()); assert ("arm", True) in eng.calls
    await c._cmd_disarm(upd, _ctx()); assert ("disarm",) in eng.calls
    await c._cmd_mode(upd, _ctx(["confirm"])); assert eng.op_mode == "confirm"
    await c._cmd_mode(upd, _ctx()); assert "현재 운용 모드" in msg.sent[-1][0]
    await c._cmd_set(upd, _ctx(["p1", "79,000,000", "20"])); assert msg.sent[-1][0] == "p1=79000000.0/20.0"
    await c._cmd_set(upd, _ctx(["p1"])); assert "사용법" in msg.sent[-1][0]
    await c._cmd_set(upd, _ctx(["p1", "abc"])); assert "숫자" in msg.sent[-1][0]
    await c._cmd_pause(upd, _ctx()); assert eng.op_mode == "signal"
    await c._cmd_close(upd, _ctx()); assert msg.sent[-1][1] is not None
    await c._cmd_help(upd, _ctx()); assert "/levels" in msg.sent[-1][0]


async def test_callbacks(ch):
    c, eng = ch
    edits = []

    class Q:
        def __init__(self, data):
            self.data = data

        async def answer(self):
            pass

        async def edit_message_text(self, text, reply_markup=None):
            edits.append(text)

    for data in ("close:confirm", "kill:step1", "kill:step2", "arm:confirm", "arm:skip", "live:confirm"):
        upd = SimpleNamespace(callback_query=Q(data), effective_chat=SimpleNamespace(id=1))
        await c._on_callback(upd, _ctx())
    assert ("close", "텔레그램 /close") in eng.calls and ("kill", "텔레그램 /kill") in eng.calls
    assert ("confirm",) in eng.calls and ("live",) in eng.calls and eng.gs.pending_confirm is None
    assert len(edits) == 6
    upd = SimpleNamespace(callback_query=Q("close:confirm"), effective_chat=SimpleNamespace(id=999))
    await c._on_callback(upd, _ctx())
    assert len(edits) == 6


async def test_send_attaches_buttons(ch):
    c, _ = ch
    sent = []

    class Bot:
        async def send_message(self, chat_id, text, reply_markup=None):
            sent.append((chat_id, text, reply_markup))

    assert not await c.send(Event(kind=EventKind.INFO, title="x"), "x")  # 봇 미시작
    c.bot = Bot()
    assert await c.send(Event(kind=EventKind.ARM_CONFIRM, title="a"), "a")
    assert sent[0][2] is not None and len(sent[0][2].inline_keyboard[0]) == 2
    await c.send(Event(kind=EventKind.INFO, title="i"), "i")
    assert sent[1][2] is None

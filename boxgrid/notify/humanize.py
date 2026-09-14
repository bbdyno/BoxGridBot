"""사람이 읽는 알림 문장. 숫자 나열 대신 '무슨 일 · 왜 · 다음에 볼 것' 순서로 쓴다."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from ..core.clock import KST


def won(x: float | None, quote: str = "KRW") -> str:
    """1억 452만원 / 9,935만원 / 12,340원. 다른 통화는 콤마 숫자."""
    if x is None:
        return "-"
    x = float(x)
    if quote != "KRW":
        return f"{x:,.2f} {quote}"
    if x >= 1e8:
        eok = int(x // 1e8)
        man = round((x - eok * 1e8) / 1e4)
        if man >= 10000:
            eok += 1
            man = 0
        return f"{eok}억 {man:,}만원" if man else f"{eok}억원"
    if x >= 1e6:
        return f"{round(x / 1e4):,}만원"
    return f"{x:,.0f}원"


def qty_text(q: float, base: str = "BTC") -> str:
    return f"{q:.4f} {base}"


def pct(a: float, b: float) -> str:
    """a 가 b 대비 몇 % 인지. +1.7% / -3.6%"""
    if not b:
        return "-"
    return f"{(a / b - 1.0) * 100:+.1f}%"


def kst_date(ts: datetime | str | None) -> str:
    if ts is None:
        return ""
    if isinstance(ts, str):
        ts = datetime.fromisoformat(ts)
    return ts.astimezone(KST).strftime("%-m/%-d")


def mode_line(is_live: bool, trading_enabled: bool) -> str:
    if not trading_enabled:
        return "판단만 하고 주문은 내지 않는 모드입니다."
    if is_live:
        return "⚠️ 실제 주문이 나가는 모드입니다."
    return "모의 매매(페이퍼) 모드라 실제 주문은 나가지 않습니다."


def level_lines(levels: dict[str, Any], price: float, filled: list[int], open_levels: list[int],
                quote: str, seed: float | None = None) -> list[str]:
    out = []
    for i, (p, w) in enumerate(zip(levels["prices"], levels["weights_pct"]), start=1):
        mark = "✅ 체결" if i in filled else ("⏳ 대기" if i in open_levels else "·")
        amt = f" · {won(seed * w / 100, quote)}" if seed else f" · {w:.0f}%"
        out.append(f"  {i}단 {won(p, quote)} ({pct(p, price)}){amt} {mark}")
    return out


def exit_lines(levels: dict[str, Any], price: float, quote: str, tp1_pct: float) -> list[str]:
    return [
        f"  손절: 일봉 종가가 {won(levels['sl'], quote)} 아래로 마감하면 전부 팝니다 ({pct(levels['sl'], price)})",
        f"  익절: {won(levels['tp'], quote)} 에 닿으면 {tp1_pct:.0f}% 팔고 나머지는 고점 추적 ({pct(levels['tp'], price)})",
    ]

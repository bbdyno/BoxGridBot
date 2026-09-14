"""알림 텍스트 포맷터.

`format_full` 은 텔레그램용(제목·심볼가격·전략사유 3줄 헤더 + 상세),
`format_short` 는 카카오용(120자 이내, 개행 2개 이내)이다.
"""
from __future__ import annotations

from typing import Any

from ..core.events import Event

SHORT_MAX_LEN = 120


def _fmt_number(value: Any) -> str:
    """천 단위 콤마. 정수면 소수점 없이, 아니면 소수 둘째자리까지."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return str(value)
    if f == int(f):
        return f"{int(f):,}"
    return f"{f:,.2f}"


def _fmt_signed_number(value: Any) -> str:
    """부호 있는 숫자(손익 금액용). 음수는 -, 나머지는 + ."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return str(value)
    body = _fmt_number(abs(f))
    return f"-{body}" if f < 0 else f"+{body}"


def _fmt_pct(value: Any) -> str:
    """부호 있는 퍼센트(손익률용)."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return str(value)
    sign = "+" if f >= 0 else ""
    return f"{sign}{f:.2f}%"


def format_full(event: Event) -> str:
    """텔레그램용. 제목 한 줄 + 본문. 본문이 사람이 읽는 문장을 이미 담고 있다."""
    title = event.title or event.kind.value
    return f"{title}\n{event.body}" if event.body else title


def format_short(event: Event) -> str:
    """카카오용 짧은 포맷. 120자 이내, 줄바꿈 2개 이내."""
    data = event.data or {}
    title = event.title or event.kind.value

    head_parts = [title]
    symbol = data.get("symbol")
    price = data.get("price")
    if symbol:
        head_parts.append(str(symbol))
    if price is not None:
        head_parts.append(f"{_fmt_number(price)}원")
    line1 = " ".join(head_parts)

    reason = data.get("reason") or event.body
    lines = [line1]
    if reason:
        lines.append(str(reason))

    text = "\n".join(lines[:2])
    if len(text) > SHORT_MAX_LEN:
        text = text[: SHORT_MAX_LEN - 1] + "…"
    return text

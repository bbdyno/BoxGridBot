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
    """텔레그램용 전체 포맷. 헤더 3줄 + 상세."""
    data = event.data or {}
    title = event.title or event.kind.value

    symbol = data.get("symbol")
    price = data.get("price")
    line2_parts = []
    if symbol:
        line2_parts.append(str(symbol))
    if price is not None:
        line2_parts.append(f"{_fmt_number(price)}원")
    line2 = " · ".join(line2_parts) if line2_parts else "-"

    strategy = data.get("strategy")
    reason = data.get("reason") or event.body
    line3_parts = []
    if strategy:
        line3_parts.append(str(strategy))
    if reason:
        line3_parts.append(str(reason))
    line3 = " · ".join(line3_parts) if line3_parts else "-"

    lines = [title, line2, line3]

    detail_lines: list[str] = []
    if event.body and event.body != reason:
        detail_lines.append(event.body)
    if "qty" in data and data["qty"] is not None:
        detail_lines.append(f"수량: {_fmt_number(data['qty'])}")
    if "pnl" in data and data["pnl"] is not None:
        detail_lines.append(f"손익: {_fmt_signed_number(data['pnl'])}원")
    if "pnl_pct" in data and data["pnl_pct"] is not None:
        detail_lines.append(f"손익률: {_fmt_pct(data['pnl_pct'])}")
    if "stop" in data and data["stop"] is not None:
        detail_lines.append(f"손절가: {_fmt_number(data['stop'])}원")
    if "take" in data and data["take"] is not None:
        detail_lines.append(f"익절가: {_fmt_number(data['take'])}원")

    text = "\n".join(lines)
    if detail_lines:
        text += "\n" + "\n".join(detail_lines)
    return text


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

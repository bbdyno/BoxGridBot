"""적립식(DCA) 추가 매수 지표 — "평소보다 싸면 더 담기".

매일 정액 적립하는 사람이 "오늘 더 살 자리인가"를 판단하도록 추천 배수를 낸다.
핵심 규칙은 하나다: 현재가가 200일 평균보다 얼마나 싼가(할인폭). 싸질수록 더 담고, 평균 위면 적립분만.
RSI 가 과매도(기본 35 미만)면 0.5배를 더한다.

업비트 BTC/KRW 2020-09~2026-09 일봉으로 검증: 2년·4년·6년 구간 모두 그냥 적립보다 평균 단가가 5~11% 낮았다
(scripts/dca_eval.py). 반대로 "상승 추세에서 눌림 때만 추가"하는 방식은 세 구간 모두 평균 단가가 더 높았다.

판단의 최종 책임은 사용자에게 있다. 순수 함수라 테스트로 모든 분기를 검증한다.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from .. import indicators as ind
from ..config import DcaConfig


@dataclass
class DcaVerdict:
    multiplier: float     # 추가 매수 배수(기본 적립액 기준). 0 이면 적립분만
    extra_amount: float
    grade: str            # "적립만" | "조금 더" | "더 담기" | "많이 담기" | "데이터 부족"
    discount_pct: float   # 200일 평균 대비 (음수 = 싸다)
    rsi: float
    reasons: list[str] = field(default_factory=list)
    price: float = 0.0

    def headline(self, base_amount: float, won) -> str:
        if self.grade == "데이터 부족":
            return "데이터 부족으로 계산 불가"
        where = (f"200일 평균보다 {abs(self.discount_pct):.0f}% 싸다" if self.discount_pct < 0
                 else f"200일 평균보다 {self.discount_pct:.0f}% 비싸다")
        if self.multiplier > 0:
            return f"{where} → {self.grade}: 적립 {won(base_amount)} + 추가 {won(self.extra_amount)} 검토"
        return f"{where} → {self.grade}: 오늘은 평소 적립분만"


def dca_verdict(daily: pd.DataFrame, price: float, cfg: DcaConfig) -> DcaVerdict:
    """확정 일봉 + 현재가로 지표를 계산한다."""
    need = max(cfg.sma_len, cfg.high_lookback, cfg.rsi_len + 1)
    if daily is None or len(daily) < need:
        n = 0 if daily is None else len(daily)
        return DcaVerdict(0.0, 0.0, "데이터 부족", 0.0, 0.0, [f"일봉 {n}개뿐이라 계산 불가(필요 {need}개)"], price)

    close = daily["close"]
    sma = float(ind.sma(close, cfg.sma_len).iloc[-1])
    rsi = float(ind.rsi(close, cfg.rsi_len).iloc[-1])
    high = float(daily["high"].tail(cfg.high_lookback).max())
    gap = (price / sma - 1.0) * 100.0
    dd = (price / high - 1.0) * 100.0

    mult = 0.0
    for threshold, m in sorted(zip(cfg.discount_tiers_pct, cfg.tier_multipliers), reverse=True):
        # threshold 는 "이만큼 이상 싸면" (양수 %). gap 이 -threshold 이하일 때 적용
        if gap <= -threshold:
            mult = m
            break
    reasons: list[str] = []
    if gap < 0:
        reasons.append(f"현재가가 200일 평균 {_fmt(sma)} 보다 {abs(gap):.1f}% 낮음 → 기본 배수 {mult:g}")
    else:
        reasons.append(f"현재가가 200일 평균 {_fmt(sma)} 보다 {gap:.1f}% 높음 → 추가 없음 (평균 아래로 오면 다시 봅니다)")

    if mult > 0 and rsi < cfg.rsi_oversold:
        mult += cfg.rsi_bonus
        reasons.append(f"RSI {rsi:.0f} 과매도 → +{cfg.rsi_bonus:g}배")
    else:
        reasons.append(f"RSI {rsi:.0f} ({'과매도' if rsi < cfg.rsi_oversold else '과매수' if rsi > 70 else '중립'})")
    mult = min(mult, cfg.max_multiplier)
    reasons.append(f"{cfg.high_lookback}일 고점 대비 {dd:+.1f}% (참고)")
    if mult >= 2:
        reasons.append("하락이 길어질 수 있으니 여유 자금 범위에서만 추가하세요.")

    if mult <= 0:
        grade = "적립만"
    elif mult < 1:
        grade = "조금 더"
    elif mult < 2:
        grade = "더 담기"
    else:
        grade = "많이 담기"
    return DcaVerdict(mult, cfg.base_amount * mult, grade, gap, rsi, reasons, price)


def _fmt(x: float) -> str:
    from ..notify.humanize import won

    return won(x)

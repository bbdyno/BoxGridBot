"""그리드 레벨 계산. 일봉 데이터만 받아 순수 함수로 계산한다."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import pandas as pd

from .. import indicators as ind
from ..config import LevelConfig


@dataclass
class Levels:
    """한 번 확정된 그리드 레벨 묶음."""

    prices: list[float]           # P1..Pn (내림차순)
    weights_pct: list[float]      # 각 단 비중(%)
    sl: float                     # 일봉 종가 손절선
    tp: float                     # 1차 익절선
    box_low: float = 0.0
    box_high: float = 0.0
    atr: float = 0.0
    sma: float = 0.0
    close: float = 0.0
    mode: str = "dynamic"
    regime: str = "box"           # "box"(박스 하단 매수) | "pullback"(고점 기준 눌림 매수)
    anchor_high: float = 0.0
    computed_at: str = ""
    candle_ts: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "prices": list(self.prices), "weights_pct": list(self.weights_pct), "sl": self.sl, "tp": self.tp,
            "box_low": self.box_low, "box_high": self.box_high, "atr": self.atr, "sma": self.sma,
            "close": self.close, "mode": self.mode, "regime": self.regime, "anchor_high": self.anchor_high,
            "computed_at": self.computed_at, "candle_ts": self.candle_ts,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Levels":
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})

    def max_change_pct(self, other: "Levels") -> float:
        """두 레벨 묶음의 최대 가격 차이(%)."""
        pairs = list(zip(self.prices, other.prices)) + [(self.sl, other.sl), (self.tp, other.tp)]
        return max(abs(a - b) / b * 100.0 for a, b in pairs if b)


@dataclass
class TrendInfo:
    ok: bool
    close: float
    sma: float
    candle_ts: str
    reason: str = ""


def closed_daily(df: pd.DataFrame, now: datetime) -> pd.DataFrame:
    """진행 중인 오늘 봉을 제외한 확정 일봉만 남긴다."""
    if df is None or df.empty:
        return df
    step = pd.Timedelta(days=1)
    return df[df.index + step <= pd.Timestamp(now)]


def trend_filter(daily: pd.DataFrame, sma_len: int) -> TrendInfo:
    """마지막 확정 일봉 종가 > SMA(sma_len) 이면 상승 추세."""
    if daily is None or len(daily) < sma_len:
        n = 0 if daily is None else len(daily)
        return TrendInfo(False, 0.0, 0.0, "", f"일봉이 {n}개뿐이라 SMA{sma_len}을 계산할 수 없습니다.")
    sma_val = float(ind.sma(daily["close"], sma_len).iloc[-1])
    close = float(daily["close"].iloc[-1])
    ok = close > sma_val
    return TrendInfo(ok, close, sma_val, daily.index[-1].isoformat(),
                     "종가가 SMA 위" if ok else "종가가 SMA 아래")


def compute_levels(daily: pd.DataFrame, cfg: LevelConfig, now: datetime) -> Levels:
    """확정 일봉으로 레벨을 계산한다. fixed 모드는 설정값을 그대로 쓴다."""
    ts = daily.index[-1].isoformat() if daily is not None and not daily.empty else ""
    close = float(daily["close"].iloc[-1]) if daily is not None and not daily.empty else 0.0
    sma_val = float(ind.sma(daily["close"], cfg.sma_len).iloc[-1]) if daily is not None and len(daily) >= cfg.sma_len else 0.0
    if cfg.mode == "fixed":
        f = cfg.fixed
        n = len(cfg.offsets_atr)
        need = [f"p{i + 1}" for i in range(n)] + ["sl", "tp"]
        missing = [k for k in need if k not in f]
        if missing:
            raise ValueError(f"고정 레벨 값이 비어 있습니다: {', '.join(missing)} (/set 으로 채우세요)")
        prices = [float(f[f"p{i + 1}"]) for i in range(n)]
        lv = Levels(prices=prices, weights_pct=list(cfg.weights_pct), sl=float(f["sl"]), tp=float(f["tp"]),
                    close=close, sma=sma_val, mode="fixed", computed_at=now.isoformat(), candle_ts=ts)
        _validate(lv)
        return lv

    if cfg.mode == "adaptive":
        from .regime import detect_regime

        if detect_regime(daily, cfg).name == "expansion":
            return _pullback_levels(daily, cfg, now, close, sma_val, ts)

    need = max(cfg.box_lookback, cfg.atr_len + 1)
    if daily is None or len(daily) < need:
        raise ValueError(f"레벨 계산에 일봉 {need}개가 필요합니다 (현재 {0 if daily is None else len(daily)}개).")
    box = daily.tail(cfg.box_lookback)
    box_low = float(box["low"].min())
    box_high = float(box["high"].max())
    atr_val = float(ind.atr(daily, cfg.atr_len).iloc[-1])
    if not atr_val or atr_val != atr_val:
        raise ValueError("ATR 을 계산할 수 없습니다.")
    prices = [box_low + off * atr_val for off in cfg.offsets_atr]
    lv = Levels(
        prices=prices, weights_pct=list(cfg.weights_pct),
        sl=box_low - cfg.sl_atr * atr_val, tp=box_high + cfg.tp_atr * atr_val,
        box_low=box_low, box_high=box_high, atr=atr_val, sma=sma_val, close=close,
        mode=cfg.mode, regime="box", computed_at=now.isoformat(), candle_ts=ts,
    )
    _validate(lv)
    return lv


def _pullback_levels(daily: pd.DataFrame, cfg: LevelConfig, now: datetime, close: float, sma_val: float, ts: str) -> Levels:
    """발산 장세: 최근 고점에서 ATR 배수만큼 눌린 자리에 분할 매수. 손절은 최근 스윙 저점 아래(일봉 종가)."""
    atr_val = float(ind.atr(daily, cfg.atr_len).iloc[-1])
    if not atr_val or atr_val != atr_val:
        raise ValueError("ATR 을 계산할 수 없습니다.")
    anchor = float(daily["high"].tail(cfg.pullback_high_lookback).max())
    prices = [anchor - k * atr_val for k in cfg.pullback_offsets_atr]
    swing_low = float(daily["low"].tail(cfg.pullback_swing_lookback).min())
    sl = min(swing_low - cfg.pullback_sl_atr * atr_val, prices[-1] - 1.0 * atr_val)
    box = daily.tail(cfg.box_lookback)
    lv = Levels(
        prices=prices, weights_pct=list(cfg.weights_pct), sl=sl, tp=anchor + cfg.pullback_tp_atr * atr_val,
        box_low=float(box["low"].min()), box_high=float(box["high"].max()), atr=atr_val, sma=sma_val, close=close,
        mode="adaptive", regime="pullback", anchor_high=anchor, computed_at=now.isoformat(), candle_ts=ts,
    )
    _validate(lv)
    return lv


def pullback_reference(daily: pd.DataFrame, cfg: LevelConfig, now: datetime) -> Levels | None:
    """발산 장세에서 참고로 보여줄 '고점 기준 눌림 가격'. 계산 불가면 None."""
    try:
        close = float(daily["close"].iloc[-1])
        return _pullback_levels(daily, cfg, now, close, 0.0, daily.index[-1].isoformat())
    except Exception:  # noqa: BLE001
        return None


def _validate(lv: Levels) -> None:
    if any(p <= 0 for p in lv.prices) or lv.sl <= 0 or lv.tp <= 0:
        raise ValueError("레벨 가격은 양수여야 합니다.")
    if list(lv.prices) != sorted(lv.prices, reverse=True):
        raise ValueError("레벨은 P1 > P2 > ... 순이어야 합니다.")
    if lv.sl >= lv.prices[-1]:
        raise ValueError("손절선은 마지막 매수 레벨보다 낮아야 합니다.")
    if lv.tp <= lv.prices[0]:
        raise ValueError("익절선은 첫 매수 레벨보다 높아야 합니다.")


def clamp_below_price(lv: Levels, price: float, gap_pct: float) -> tuple[Levels, list[int]]:
    """현재가 이상인 레벨을 현재가 - gap 아래로 내린다(지정가 매수가 즉시 시장가 체결되는 것을 막는다).

    내려간 단이 여러 개면 순서를 지키도록 단마다 gap 만큼 더 낮춘다. 조정된 단 번호 목록을 함께 돌려준다.
    """
    cap = price * (1.0 - gap_pct / 100.0)
    prices = list(lv.prices)
    adjusted: list[int] = []
    k = 0
    for i, p in enumerate(prices):
        if p >= cap:
            prices[i] = cap * (1.0 - gap_pct / 100.0) ** k
            adjusted.append(i + 1)
            k += 1
        else:
            k = 0
    if not adjusted:
        return lv, []
    out = Levels(**{**lv.to_dict(), "prices": prices})
    _validate(out)
    return out, adjusted


def plan_orders(lv: Levels, seed: float, min_cost: float) -> list[dict[str, float]]:
    """시드를 비중대로 나눠 단별 주문(가격·수량·금액)을 만든다."""
    plan = []
    for i, (price, w) in enumerate(zip(lv.prices, lv.weights_pct), start=1):
        cost = seed * w / 100.0
        plan.append({"level": i, "price": price, "cost": cost, "qty": cost / price if price else 0.0})
    if plan and min(p["cost"] for p in plan) < min_cost:
        raise ValueError(f"시드 {seed:,.0f}로는 단별 최소 주문 금액({min_cost:,.0f})을 맞출 수 없습니다.")
    return plan

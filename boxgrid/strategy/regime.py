"""장세 판별: 수렴(박스) vs 발산(상승 확장).

일봉 볼린저 밴드(20, 2)로 본다. 예측하지 않고 확인한다.
- 발산 시작: 최근에 밴드가 좁아졌다가(수렴) 종가가 중심선 위에 있고 밴드가 다시 벌어지거나 종가가 상단에 닿음
- 발산 유지: 시작 이후 종가가 중심선 위에 머무는 동안
- 그 외: 박스(수렴)

발산 장세에서는 저점이 높아져 박스 하단까지 눌림이 오지 않는다. 그래서 레벨을 최근 고점 기준 눌림으로 바꾼다.
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from .. import indicators as ind
from ..config import LevelConfig


@dataclass
class Regime:
    name: str             # "box" | "expansion"
    above_mid: bool
    mid: float
    bandwidth_pct: float  # (상단-하단)/중심선 %
    squeeze_recent: bool
    since: str = ""       # 발산 시작 일봉
    reason: str = ""

    def to_dict(self) -> dict:
        return dict(self.__dict__)


def detect_regime(daily: pd.DataFrame, cfg: LevelConfig) -> Regime:
    n = cfg.bb_len
    need = n + cfg.squeeze_window + 5
    if daily is None or len(daily) < need:
        return Regime("box", False, 0.0, 0.0, False, reason="일봉 부족으로 박스로 간주")
    close = daily["close"]
    mid, upper, lower = ind.bollinger(close, n, cfg.bb_k)
    bw = (upper - lower) / mid
    hist = bw.tail(cfg.squeeze_window)
    threshold = hist.quantile(cfg.squeeze_quantile)
    squeezed = bw <= bw.rolling(cfg.squeeze_window, min_periods=n).quantile(cfg.squeeze_quantile)
    recent_squeeze = squeezed.rolling(cfg.squeeze_recent_days, min_periods=1).max().astype(bool)
    above = close > mid
    opening = (bw > bw.shift(1)) & (bw.shift(1) > bw.shift(2))
    trigger = recent_squeeze & above & (opening | (close >= upper))

    # 마지막 trigger 이후 종가가 계속 중심선 위였는지
    name, since = "box", ""
    idx = list(daily.index)
    last_trigger = None
    for i in range(len(idx) - 1, max(len(idx) - cfg.expansion_max_days - 1, n) - 1, -1):
        if not bool(above.iloc[i]):
            break
        if bool(trigger.iloc[i]):
            last_trigger = i
    if last_trigger is not None:
        name, since = "expansion", idx[last_trigger].isoformat()

    a = bool(above.iloc[-1])
    reason = ("밴드가 좁아졌다가 종가가 중심선 위에서 벌어지는 중" if name == "expansion"
              else ("종가가 볼린저 중심선 위지만 발산 신호는 없음" if a else "종가가 볼린저 중심선 아래"))
    return Regime(name, a, float(mid.iloc[-1]), float(bw.iloc[-1] * 100), bool(bw.iloc[-1] <= threshold), since, reason)

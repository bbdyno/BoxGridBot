"""설정 로더. dataclass + yaml. 알 수 없는 키는 무시한다."""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field, fields
from typing import Any

import yaml

from .risk.guard import RiskConfig

log = logging.getLogger(__name__)


@dataclass
class LevelConfig:
    """그리드 레벨 계산 설정."""

    mode: str = "dynamic"                  # dynamic(박스 하단, 기본) | adaptive(발산 장세엔 고점 눌림, 실험용) | fixed
    box_lookback: int = 20                 # 일봉 박스 구간(일)
    atr_len: int = 14
    sma_len: int = 200
    offsets_atr: list[float] = field(default_factory=lambda: [0.5, 0.0, -0.5, -1.0])
    weights_pct: list[float] = field(default_factory=lambda: [20.0, 20.0, 20.0, 40.0])
    sl_atr: float = 2.0                    # SL = 박스 저점 - sl_atr * ATR
    tp_atr: float = 0.25                   # TP = 박스 고점 + tp_atr * ATR
    tp1_pct: float = 50.0                  # TP 도달 시 익절 비율
    trail_pct: float = 3.0                 # 이후 고점 대비 되돌림 청산
    disaster_pct: float = 3.0              # SL 대비 추가 하락 시 즉시 청산
    rearm_threshold_pct: float = 0.5       # 미체결 상태에서 레벨이 이만큼 바뀌면 재게시
    min_gap_pct: float = 0.3               # 현재가보다 높은 레벨은 현재가 - 이 비율 아래로 내려 게시
    fixed: dict[str, float] = field(default_factory=dict)  # p1..p4, sl, tp
    # --- 장세 판별(일봉 볼린저) ---
    bb_len: int = 20
    bb_k: float = 2.0
    squeeze_window: int = 120              # 밴드폭 분위수를 보는 구간(일)
    squeeze_quantile: float = 0.25         # 이 분위수 이하면 '수렴'
    squeeze_recent_days: int = 10          # 최근 며칠 안에 수렴이 있었어야 발산으로 인정
    expansion_max_days: int = 60           # 발산 유지 최대 일수
    # --- 발산 장세용 눌림 레벨 ---
    pullback_high_lookback: int = 10       # 기준 고점 구간(일)
    pullback_offsets_atr: list[float] = field(default_factory=lambda: [0.5, 1.0, 1.5, 2.0])  # 고점 - k*ATR
    pullback_swing_lookback: int = 15      # 손절 기준 스윙 저점 구간(일)
    pullback_sl_atr: float = 0.5           # SL = 스윙 저점 - k*ATR
    pullback_tp_atr: float = 1.0           # TP = 기준 고점 + k*ATR

    def __post_init__(self) -> None:
        if len(self.offsets_atr) != len(self.weights_pct):
            raise ValueError("offsets_atr 와 weights_pct 길이가 다릅니다.")
        if len(self.pullback_offsets_atr) != len(self.weights_pct):
            raise ValueError("pullback_offsets_atr 와 weights_pct 길이가 다릅니다.")
        total = sum(self.weights_pct)
        if abs(total - 100.0) > 1e-6:
            raise ValueError(f"weights_pct 합이 100 이어야 합니다 (현재 {total}).")


@dataclass
class DcaConfig:
    """적립식 추가 매수 지표 설정. 핵심은 200일 평균 대비 할인폭."""

    enabled: bool = True
    base_amount: float = 20_000.0                 # 매일 자동 적립 금액
    sma_len: int = 200
    rsi_len: int = 14
    high_lookback: int = 90                       # 참고 표시용 고점 구간
    discount_tiers_pct: list[float] = field(default_factory=lambda: [0.0, 10.0, 20.0])  # 이만큼 이상 싸면
    tier_multipliers: list[float] = field(default_factory=lambda: [0.5, 1.0, 2.0])       # 추가 배수
    rsi_oversold: float = 35.0
    rsi_bonus: float = 0.5
    max_multiplier: float = 2.5

    def __post_init__(self) -> None:
        if len(self.discount_tiers_pct) != len(self.tier_multipliers):
            raise ValueError("discount_tiers_pct 와 tier_multipliers 길이가 다릅니다.")


@dataclass
class AppConfig:
    """애플리케이션 전체 설정."""

    mode: str = "paper"          # paper | live
    live_lock: bool = True       # True 면 live 로 켜도 실주문 없이 페이퍼로 강등한다
    op_mode: str = "auto"        # signal | confirm | auto
    exchange: str = "upbit"
    symbol: str = "BTC/KRW"
    tick_seconds: int = 60
    initial_cash: float = 10_000_000.0
    fee: float = 0.0005
    slippage: float = 0.001
    seed_pct: float = 100.0      # 견적통화 잔고 중 이 전략에 쓰는 비율
    daily_close_hour_kst: int = 9
    daily_close_delay_sec: int = 30
    confirm_timeout_min: int = 30
    reentry_cooldown_days: int = 1
    rearm_daily_if_unfilled: bool = True
    levels: LevelConfig = field(default_factory=LevelConfig)
    dca: DcaConfig = field(default_factory=DcaConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    notify: dict[str, Any] = field(default_factory=dict)
    db_path: str = "data/boxgrid.db"
    log_path: str = "logs/boxgrid.log"

    @property
    def is_live(self) -> bool:
        return self.mode == "live"

    @property
    def n_levels(self) -> int:
        return len(self.levels.offsets_atr)


def _pick(cls, raw: dict[str, Any]) -> dict[str, Any]:
    known = {f.name for f in fields(cls)}
    return {k: v for k, v in raw.items() if k in known}


def from_dict(raw: dict[str, Any]) -> AppConfig:
    """dict 를 AppConfig 로 변환한다."""
    cfg = AppConfig(**_pick(AppConfig, {k: v for k, v in raw.items() if k not in ("levels", "dca", "risk", "notify")}))
    cfg.levels = LevelConfig(**_pick(LevelConfig, dict(raw.get("levels") or {})))
    cfg.dca = DcaConfig(**_pick(DcaConfig, dict(raw.get("dca") or {})))
    cfg.risk = RiskConfig(**_pick(RiskConfig, dict(raw.get("risk") or {})))
    cfg.notify = dict(raw.get("notify") or {})
    if cfg.mode not in ("paper", "live"):
        raise ValueError(f"mode 는 paper|live 여야 합니다: {cfg.mode}")
    if cfg.op_mode not in ("signal", "confirm", "auto"):
        raise ValueError(f"op_mode 는 signal|confirm|auto 여야 합니다: {cfg.op_mode}")
    if cfg.levels.mode not in ("adaptive", "dynamic", "fixed"):
        raise ValueError(f"levels.mode 는 adaptive|dynamic|fixed 여야 합니다: {cfg.levels.mode}")
    return cfg


def load_config(path: str = "config/grid.yaml") -> AppConfig:
    """YAML 설정을 읽는다. 파일이 없으면 경고 후 기본값."""
    if not path or not os.path.exists(path):
        log.warning("설정 파일이 없어 기본값으로 실행합니다: %s", path)
        return AppConfig()
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    return from_dict(raw)

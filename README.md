# BoxGridBot

박스권 하단 그리드 롱 봇. 일봉 200 SMA 위에서만 박스 저점 부근에 4단 지정가 매수를 깔고,
손절은 **일봉 확정 종가**로만 판정한다(장중 꼬리에 털리지 않기 위해). 텔레그램으로 제어·알림한다.

## 전략 요약

| 항목 | 규칙 |
|---|---|
| 추세 필터 | 확정 일봉 종가 > SMA(200) 일 때만 게시 |
| 레벨(동적) | P1..P4 = 박스(20일) 저점 + [0.5, 0, -0.5, -1.0] × ATR(14), 비중 20/20/20/40 % |
| 손절 | 박스 저점 − 2 ATR. 09:00 KST 일봉 확정 종가가 이 아래일 때만 전량 청산 |
| 재난 손절 | 손절선 −3% 를 장중 이탈하면 즉시 전량 청산(회로차단기) |
| 익절 | 박스 고점 + 0.25 ATR 도달 시 50% 익절, 나머지는 고점 대비 3% 트레일링 |
| 게시 규칙 | 현재가보다 높은 레벨은 현재가 −0.3% 아래로 내려 게시(즉시 시장가 체결 방지) |
| 재진입 | 손절 후 1일 쿨다운, 추세 재확인 뒤 재게시 |

상태 머신: `IDLE → ARMED → IN_POSITION → EXITED → IDLE`. 상태는 SQLite 에 저장되며 재시작 시 거래소와 대조한다.

## 실거래 잠금

`config/grid.yaml` 의 `live_lock: true`(기본) 이면 `--mode live` 로 켜도 **페이퍼로 강등**되어 판단·알림·모의 손익만 기록한다.
판단이 검증되면 `live_lock: false` + 거래소 키 + 텔레그램 "라이브 승인" 버튼(또는 `--yes-live`)으로 실주문을 연다.

운용 모드(`op_mode`, 텔레그램 `/mode` 로 변경): `signal`(주문 없이 레벨 도달 알림만) · `confirm`(게시 전 승인 버튼) · `auto`.

## 설치·실행

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env   # TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_IDS 채우기
.venv/bin/python -m pytest -q
.venv/bin/python -m boxgrid.main                       # 페이퍼(업비트 실시세)
.venv/bin/python -m boxgrid.main --max-cycles 3 --tick 5   # 스모크
.venv/bin/python scripts/backtest.py --days 730        # 백테스트(1h 캐시는 data/ohlcv/)
```

상시 가동(macOS): `scripts/install_launchagent.sh` (제거는 `remove` 인자). 다른 맥으로 옮길 때는 `MIGRATION.md`.

## 장세 표시 (수렴/발산)

일봉 볼린저 밴드(20, 2)로 박스(수렴)와 발산(상승 확장)을 구분해 09:00 메시지와 `/trend` 에 보여준다.
발산 장세에서는 저점이 높아져 박스 하단까지 안 내려올 수 있으므로, 최근 고점 기준 **참고 눌림 가격**을 함께 표시한다(주문 아님).
발산 장세에 실제로 그 가격에 주문하는 `levels.mode: adaptive` 도 있지만, 2년 백테스트에서 박스 방식보다 나빠(+4.8%/MDD 13.7% vs +15.3%/9.4%) 기본은 꺼져 있다.
근거와 기각 사유는 `docs/VIDEO_NOTES.md`.

## 적립 추가 매수 지표 (/dca)

매일 정액 적립(예: 업비트 코인 모으기 2만원)에 얹어 "오늘 더 살 자리인가"를 09:00 판단 메시지에 함께 알려준다.
규칙은 하나다. **현재가가 200일 평균보다 싸면 할인폭만큼 더 담고, 평균 위면 적립분만.** RSI 35 미만이면 0.5배 추가.

| 200일 평균 대비 | 추가 배수 | 2만원 적립 기준 |
|---|---|---|
| 위 | 0 | 적립분만 |
| 0~10% 싸다 | 0.5 | +1만원 |
| 10~20% 싸다 | 1.0 | +2만원 |
| 20% 이상 싸다 | 2.0 | +4만원 |

업비트 BTC/KRW 일봉으로 검증(`scripts/dca_eval.py`): 그냥 적립 대비 평균 단가가 최근 2년 −5.1%, 4년 −8.1%, 전체(2021~) −11.5%.
처음 시도한 "상승 추세에서 눌림 때만 추가" 방식은 세 구간 모두 평균 단가가 더 높아 폐기했다. 추가 투입은 여유 자금 범위에서.

## 텔레그램 명령

`/status` `/trend` `/levels` `/balance` `/report` `/dca` `/arm` `/disarm` `/mode signal|confirm|auto`
`/set p1 79000000 20` `/close` `/kill` `/help`

## 구조

```
boxgrid/
  strategy/levels.py   레벨 계산(순수 함수)
  strategy/grid.py     상태 머신(순수 로직, Decision 목록 반환)
  strategy/regime.py   장세 판별(일봉 볼린저 수렴/발산)
  strategy/dca.py      적립 추가 매수 지표(200일 평균 할인폭)
  notify/humanize.py   사람이 읽는 알림 문장·금액 표기
  core/engine.py       결정 실행·스케줄·영속화·재시작 대조
  exchange/            ccxt 어댑터(지정가·취소·조회), 페이퍼 거래소(주문장 시뮬)
  risk/guard.py        킬 스위치, 일손실·주간 손절 한도, 데이터 지연
  notify/              텔레그램(양방향), 카카오 나에게 보내기(발신), 라우터
  backtest/runner.py   1h 재생으로 실제 엔진을 그대로 돌리는 백테스트
```

## 백테스트 결과 (업비트 BTC/KRW, 2025-04-07 ~ 2026-09-14, 기본 파라미터)

수익률 +15.3% (단순 보유 −10.6%), 최대 낙폭 9.4%, 청산 7건(승 6, 재난 손절 1). 과거 성과가 미래를 보장하지 않는다.

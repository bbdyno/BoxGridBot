# 다른 맥으로 옮기기

코드는 이 저장소에 전부 있다. 저장소에 **없는 것**은 세 가지다: 비밀값(`.env`), 봇 상태 DB(`data/boxgrid.db`), 가상환경(`.venv`).

> ⚠️ 텔레그램 봇은 **한 번에 한 기기에서만** 돌려야 한다. 두 곳에서 켜면 409 Conflict 로 명령 수신이 깨진다.
> 새 맥에서 켜기 전에 기존 맥에서 반드시 1단계를 먼저 한다.

## 1. 기존 맥: 봇 멈추기 (+ 상태 DB 꺼내기)

```bash
cd ~/Documents/BoxGridBot
scripts/install_launchagent.sh remove
sqlite3 data/boxgrid.db "PRAGMA wal_checkpoint(TRUNCATE);"
```

모의 포지션·체결 이력을 이어가려면 `data/boxgrid.db` 와 `.env` 두 파일을 AirDrop 등으로 새 맥에 보낸다.
새로 시작해도 되면 `.env` 만 보내면 된다(봇이 빈 상태로 시작해 다음 09:00 판단부터 다시 돈다).

## 2. 새 맥: 받기·설치

```bash
brew install python@3.14            # 기본 python3 가 3.9 면 의존성이 안 깔린다
git clone git@github.com:bbdyno/BoxGridBot.git ~/Documents/BoxGridBot
cd ~/Documents/BoxGridBot
python3.14 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest -q       # 전부 통과해야 한다
```

## 3. 새 맥: 비밀값·상태 넣기

- 받은 `.env` 를 저장소 루트에, `boxgrid.db` 를 `data/` 에 둔다.
- `.env` 를 안 가져왔다면 `cp .env.example .env` 후 `TELEGRAM_BOT_TOKEN` 을 채우고(토큰은 BotFather `/token`),
  `.venv/bin/python scripts/telegram_setup.py` 로 chat_id 채우기와 발송 테스트를 한다.

## 4. 새 맥: 켜기

```bash
scripts/install_launchagent.sh
```

텔레그램에서 `/status` 에 답이 오면 끝. 맥이 잠들면 봇도 멈추므로 상시 가동 기기는 잠자기를 꺼 둔다.

## Claude Code 로 이어서 작업하기

대화 기록과 Claude 메모리는 기기별이라 따라오지 않는다. 대신 이 저장소의 `CLAUDE.md` 가 결정 사항·검증 결과·다음 할 일을 담고 있어,
새 맥에서 이 폴더로 Claude Code 를 열면 그 맥락으로 이어진다.

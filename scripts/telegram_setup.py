"""텔레그램 봇 초기 설정 도우미.

    .venv/bin/python scripts/telegram_setup.py

1) .env 의 TELEGRAM_BOT_TOKEN 으로 봇 정보를 확인한다.
2) 봇에게 아무 메시지나 보낸 사람의 chat_id 를 찾아 보여 주고, .env 의 TELEGRAM_CHAT_IDS 가 비어 있으면 채워 준다.
3) 그 chat 으로 테스트 메시지를 보낸다.
"""
from __future__ import annotations

import os
import sys

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from boxgrid.main import load_dotenv  # noqa: E402

ENV_PATH = ".env"


def _set_env_value(key: str, value: str) -> None:
    lines = open(ENV_PATH, encoding="utf-8").read().splitlines() if os.path.exists(ENV_PATH) else []
    done = False
    for i, line in enumerate(lines):
        if line.startswith(f"{key}="):
            lines[i] = f"{key}={value}"
            done = True
    if not done:
        lines.append(f"{key}={value}")
    open(ENV_PATH, "w", encoding="utf-8").write("\n".join(lines) + "\n")


def main() -> int:
    load_dotenv(ENV_PATH)
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        print(".env 에 TELEGRAM_BOT_TOKEN 이 없습니다. BotFather 가 준 토큰을 넣고 다시 실행하세요.")
        return 2
    api = f"https://api.telegram.org/bot{token}"
    me = requests.get(f"{api}/getMe", timeout=10).json()
    if not me.get("ok"):
        print("토큰이 잘못됐습니다:", me.get("description"))
        return 2
    bot = me["result"]
    print(f"봇 확인: @{bot['username']} ({bot['first_name']})")

    upd = requests.get(f"{api}/getUpdates", timeout=10).json().get("result", [])
    chats = {}
    for u in upd:
        msg = u.get("message") or u.get("edited_message") or {}
        chat = msg.get("chat")
        if chat:
            chats[chat["id"]] = f"{chat.get('first_name', '')} {chat.get('last_name', '')} @{chat.get('username', '')}".strip()
    if not chats:
        print(f"아직 봇에게 온 메시지가 없습니다. 텔레그램에서 @{bot['username']} 을 열어 /start 를 보낸 뒤 다시 실행하세요.")
        return 1
    for cid, name in chats.items():
        print(f"chat_id {cid}: {name}")

    existing = os.getenv("TELEGRAM_CHAT_IDS", "").strip()
    if not existing:
        cid = next(iter(chats))
        _set_env_value("TELEGRAM_CHAT_IDS", str(cid))
        print(f".env 의 TELEGRAM_CHAT_IDS 를 {cid} 로 채웠습니다.")
        existing = str(cid)
    for cid in [c.strip() for c in existing.split(",") if c.strip()]:
        r = requests.post(f"{api}/sendMessage", data={"chat_id": cid, "text": "BoxGridBot 연결 테스트 ✅"}, timeout=10).json()
        print(f"테스트 발송 → {cid}: {'성공' if r.get('ok') else r.get('description')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

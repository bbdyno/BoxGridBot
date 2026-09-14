"""카카오톡 '나에게 보내기' 발신 전용 채널.

수신(오픈빌더 등) 흉내는 내지 않는다. 토큰은 파일에 저장하고 만료 시 자동 갱신한다.
"""
from __future__ import annotations

import inspect
import json
import logging
import os
import time
from typing import Any, Awaitable, Callable

import requests

from ..core.events import Event

log = logging.getLogger(__name__)

SEND_URL = "https://kapi.kakao.com/v2/api/talk/memo/default/send"
TOKEN_URL = "https://kauth.kakao.com/oauth/token"
MAX_TEXT_LEN = 200
EXPIRING_SOON_SEC = 7 * 86400


class KakaoChannel:
    """카카오 '나에게 보내기' 채널. 발신 전용, 토큰 자동 갱신, 200자 요약."""

    name = "kakao"

    def __init__(
        self,
        app_key: str,
        token_path: str = "data/kakao_token.json",
        link_url: str = "https://t.me/",
        on_token_expiring: Callable[[], Any] | None = None,
        session: Any = None,
    ) -> None:
        self.app_key = app_key
        self.token_path = token_path
        self.link_url = link_url
        self.on_token_expiring: Callable[[], Awaitable[None] | None] | None = on_token_expiring
        self.session = session or requests
        self.enabled = True

    def _load_token(self) -> dict | None:
        if not os.path.exists(self.token_path):
            return None
        with open(self.token_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _save_token(self, token: dict) -> None:
        os.makedirs(os.path.dirname(self.token_path) or ".", exist_ok=True)
        with open(self.token_path, "w", encoding="utf-8") as f:
            json.dump(token, f, ensure_ascii=False, indent=2)

    def _refresh(self, token: dict) -> dict | None:
        """리프레시 토큰으로 액세스 토큰을 갱신한다. 만료 1개월 미만이면 응답에 새 리프레시 토큰이 온다."""
        try:
            resp = self.session.post(TOKEN_URL, data={
                "grant_type": "refresh_token",
                "client_id": self.app_key,
                "refresh_token": token["refresh_token"],
            }, timeout=10)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            log.warning("카카오 토큰 갱신 실패: %s", exc)
            return None
        now = time.time()
        token = dict(token)
        token["access_token"] = data["access_token"]
        token["access_expires_at"] = now + float(data.get("expires_in", 43199))
        if data.get("refresh_token"):
            token["refresh_token"] = data["refresh_token"]
            token["refresh_expires_at"] = now + float(data.get("refresh_token_expires_in", 5184000))
            log.info("카카오 리프레시 토큰이 교체되었습니다.")
        self._save_token(token)
        return token

    async def _ensure_token(self) -> dict | None:
        """유효한 액세스 토큰을 확보한다. 리프레시 만료면 채널을 비활성화한다."""
        token = self._load_token()
        if token is None:
            log.warning("카카오 토큰 파일이 없습니다: %s", self.token_path)
            self.enabled = False
            return None

        now = time.time()
        refresh_expires_at = float(token.get("refresh_expires_at", 0))
        if refresh_expires_at and refresh_expires_at < now:
            log.warning("카카오 리프레시 토큰이 만료되어 채널을 비활성화합니다.")
            self.enabled = False
            return None

        if refresh_expires_at and refresh_expires_at - now < EXPIRING_SOON_SEC and self.on_token_expiring:
            try:
                result = self.on_token_expiring()
                if inspect.isawaitable(result):
                    await result
            except Exception as exc:  # noqa: BLE001
                log.warning("만료 임박 콜백 실행 실패: %s", exc)

        if float(token.get("access_expires_at", 0)) < now:
            token = self._refresh(token)
            if token is None:
                self.enabled = False
                return None
        return token

    @staticmethod
    def _truncate(text: str) -> str:
        """200자 제한. 초과하면 197자 + '…'."""
        if len(text) <= MAX_TEXT_LEN:
            return text
        return text[:197] + "…"

    async def send(self, event: Event, text: str) -> bool:
        """'나에게 보내기'로 텍스트 템플릿을 발송한다."""
        if not self.enabled:
            log.warning("카카오 채널이 비활성 상태라 발송을 건너뜁니다.")
            return False

        token = await self._ensure_token()
        if token is None:
            return False

        template = {
            "object_type": "text",
            "text": self._truncate(text),
            "link": {"web_url": self.link_url, "mobile_web_url": self.link_url},
            "button_title": "텔레그램에서 상세 보기",
        }
        try:
            resp = self.session.post(
                SEND_URL,
                headers={"Authorization": f"Bearer {token['access_token']}"},
                data={"template_object": json.dumps(template, ensure_ascii=False)},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            log.warning("카카오 발송 실패: %s", exc)
            return False
        return data.get("result_code") == 0

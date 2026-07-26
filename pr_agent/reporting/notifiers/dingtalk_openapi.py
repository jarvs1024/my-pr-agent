"""DingTalk OpenAPI notifier — same auth model as the stream SDK.

Sends messages via DingTalk OpenAPI using an enterprise app's AppKey +
AppSecret (the same credentials used by ``dingtalk-stream``). This is
the recommended path for stream-mode robots and for enterprise apps
where you want per-user targeting / markdown cards / no rate-limit
headaches from the custom-robot webhook.

Required config (env vars):
    DINGTALK_OPENAPI_APP_KEY            app key from DingTalk Open Platform
    DINGTALK_OPENAPI_APP_SECRET        app secret
    DINGTALK_OPENAPI_ROBOT_CODE        the robot's code from the app's bot config
    DINGTALK_OPENAPI_OPEN_CONVERSATION_ID   target chat's openConversationId

Endpoint layout:
    POST https://api.dingtalk.com/v1.0/oauth2/accessToken
        body: {"appKey": ..., "appSecret": ...}
        resp: {"accessToken": "...", "expireIn": 7200}
    POST https://api.dingtalk.com/v1.0/robot/groupMessages/send
        headers: x-acs-dingtalk-access-token: <accessToken>
        body: {"robotCode": ..., "msgType": "markdown", "msgParam": "<json string>", "openConversationId": ...}

The access token is cached in-process until shortly before ``expireIn``.
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass

import requests

from pr_agent.log import get_logger

from .base import DeliveryResult, Notifier


_log = get_logger()


_API_BASE = "https://api.dingtalk.com"
_TOKEN_PATH = "/v1.0/oauth2/accessToken"
_SEND_GROUP_PATH = "/v1.0/robot/groupMessages/send"


class _TokenCache:
    """Thread-safe access-token cache with TTL refresh."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._token: str | None = None
        self._expires_at: float = 0.0

    def get(self, refresh_fn) -> str:
        with self._lock:
            now = time.time()
            if self._token and now < self._expires_at - 60:
                return self._token
            token, expire_in = refresh_fn()
            self._token = token
            self._expires_at = now + float(expire_in)
            return token


@dataclass
class DingTalkOpenAPINotifier:
    """Send markdown via DingTalk OpenAPI (enterprise-app robot)."""

    app_key: str = ""
    app_secret: str = ""
    robot_code: str = ""
    open_conversation_id: str = ""
    retry_attempts: int = 3
    dry_run: bool = False
    timeout: float = 10.0
    name: str = "dingtalk_openapi"

    def __post_init__(self) -> None:
        self._tokens = _TokenCache()

    def _refresh_token(self) -> tuple[str, int]:
        resp = requests.post(
            f"{_API_BASE}{_TOKEN_PATH}",
            json={"appKey": self.app_key, "appSecret": self.app_secret},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        token = data.get("accessToken")
        expire = int(data.get("expireIn", 7200))
        if not token:
            raise RuntimeError(f"accessToken missing in response: {data}")
        return token, expire

    def _send_one_chunk(self, title: str, body: str, token: str) -> dict:
        msg_param = json.dumps(
            {"title": title, "text": body},
            ensure_ascii=False,
        )
        payload = {
            "msgParam": msg_param,
            "msgType": "markdown",
            "robotCode": self.robot_code,
            "openConversationId": self.open_conversation_id,
        }
        resp = requests.post(
            f"{_API_BASE}{_SEND_GROUP_PATH}",
            headers={
                "Content-Type": "application/json",
                "x-acs-dingtalk-access-token": token,
            },
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            timeout=self.timeout,
        )
        if resp.headers.get("content-type", "").startswith("application/json"):
            return resp.json()
        return {"_http_status": resp.status_code, "_text": resp.text[:500]}

    def send(self, title: str, markdown_chunks: list[str]) -> DeliveryResult:
        if not markdown_chunks:
            return DeliveryResult(success=True, chunks_sent=0, chunks_total=0)

        if self.dry_run:
            _log.info(
                "DingTalk OpenAPI dry-run: would POST %d chunk(s) to robotCode=%r openConversationId=%r",
                len(markdown_chunks),
                self.robot_code,
                self.open_conversation_id,
            )
            for idx, chunk in enumerate(markdown_chunks, start=1):
                _log.info(
                    "OpenAPI dry-run chunk %d/%d (%d bytes):\n%s",
                    idx, len(markdown_chunks), len(chunk.encode("utf-8")), chunk,
                )
            return DeliveryResult(
                success=True,
                chunks_sent=len(markdown_chunks),
                chunks_total=len(markdown_chunks),
                meta={"dry_run": True},
            )

        if not (self.app_key and self.app_secret and self.robot_code and self.open_conversation_id):
            return DeliveryResult(
                success=False,
                chunks_sent=0,
                chunks_total=len(markdown_chunks),
                error=(
                    "missing one of DINGTALK_OPENAPI_APP_KEY / APP_SECRET / "
                    "ROBOT_CODE / OPEN_CONVERSATION_ID"
                ),
            )

        chunks_sent = 0
        last_error: str | None = None

        for idx, chunk in enumerate(markdown_chunks, start=1):
            chunk_title = title if len(markdown_chunks) == 1 else f"{title} ({idx}/{len(markdown_chunks)})"
            for attempt in range(1, self.retry_attempts + 1):
                try:
                    token = self._tokens.get(self._refresh_token)
                    data = self._send_one_chunk(chunk_title, chunk, token)
                    # DingTalk OpenAPI returns {"errcode": 0} on success (some
                    # endpoints return other shapes; we accept either no
                    # errcode or errcode==0).
                    errcode = data.get("errcode", data.get("code", 0))
                    if not errcode:
                        chunks_sent += 1
                        last_error = None
                        break
                    last_error = data.get("errmsg") or data.get("message") or str(data)[:200]
                    _log.warning(
                        "DingTalk OpenAPI send attempt %d/%d: %s",
                        attempt, self.retry_attempts, last_error,
                    )
                except Exception as exc:  # noqa: BLE001
                    last_error = f"{type(exc).__name__}: {exc}"
                    _log.warning(
                        "DingTalk OpenAPI send attempt %d/%d raised: %s",
                        attempt, self.retry_attempts, last_error,
                    )
                if attempt < self.retry_attempts:
                    time.sleep(1.5 ** attempt)
            else:
                # exhausted retries for this chunk
                pass

        success = chunks_sent == len(markdown_chunks)
        return DeliveryResult(
            success=success,
            chunks_sent=chunks_sent,
            chunks_total=len(markdown_chunks),
            error=None if success else last_error,
        )


__all__ = ["DingTalkOpenAPINotifier"]

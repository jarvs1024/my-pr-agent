"""DingTalk custom-robot webhook notifier.

Implements the Notifier protocol from ``.base``. Supports:

* Optional ``加签`` (HMAC-SHA256) signing when ``secret`` is configured.
* Up to N retries with exponential backoff on network / 5xx errors.
* ``dry_run`` mode that records the payload but never POSTs — used by
  tests and local development.

The pattern follows the AI-Codereview-Gitlab DingTalk client but uses
only ``requests`` so no extra SDK is required.
"""
from __future__ import annotations

from pr_agent import config_loader as _cfg_loader  # noqa: F401  # breaks circular import
import base64
import hashlib
import hmac
import json
import time
import urllib.parse
from dataclasses import dataclass

import requests

from pr_agent.log import get_logger

from .base import DeliveryResult, Notifier


_log = get_logger()


def _sign_url(webhook_url: str, secret: str) -> str:
    """Apply DingTalk 加签 signing to a webhook URL."""
    timestamp = str(round(time.time() * 1000))
    string_to_sign = f"{timestamp}\n{secret}"
    hmac_code = hmac.new(
        secret.encode("utf-8"),
        string_to_sign.encode("utf-8"),
        digestmod=hashlib.sha256,
    ).digest()
    sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
    sep = "&" if "?" in webhook_url else "?"
    return f"{webhook_url}{sep}timestamp={timestamp}&sign={sign}"


@dataclass
class DingTalkNotifier:
    """Deliver markdown messages to a DingTalk custom robot webhook."""

    webhook_url: str = ""
    secret: str = ""
    retry_attempts: int = 3
    dry_run: bool = False
    timeout: float = 10.0
    name: str = "dingtalk"

    def send(self, title: str, markdown_chunks: list[str]) -> DeliveryResult:
        if not markdown_chunks:
            return DeliveryResult(success=True, chunks_sent=0, chunks_total=0)

        if self.dry_run:
            _log.info(
                "DingTalk dry-run: would send %d chunk(s) titled %r",
                len(markdown_chunks),
                title,
            )
            for idx, chunk in enumerate(markdown_chunks, start=1):
                _log.info(
                    "DingTalk dry-run chunk %d/%d (%d bytes):\n%s",
                    idx,
                    len(markdown_chunks),
                    len(chunk.encode("utf-8")),
                    chunk,
                )
            return DeliveryResult(
                success=True,
                chunks_sent=len(markdown_chunks),
                chunks_total=len(markdown_chunks),
                meta={"dry_run": True},
            )

        if not self.webhook_url:
            return DeliveryResult(
                success=False,
                chunks_sent=0,
                chunks_total=len(markdown_chunks),
                error="DINGTALK_WEEKLY_WEBHOOK_URL not configured",
            )

        url = _sign_url(self.webhook_url, self.secret) if self.secret else self.webhook_url
        last_error: str | None = None
        chunks_sent = 0

        for idx, chunk in enumerate(markdown_chunks, start=1):
            chunk_title = title if len(markdown_chunks) == 1 else f"{title} ({idx}/{len(markdown_chunks)})"
            payload = {
                "msgtype": "markdown",
                "markdown": {"title": chunk_title, "text": chunk},
                "at": {"isAtAll": False},
            }

            for attempt in range(1, self.retry_attempts + 1):
                try:
                    resp = requests.post(
                        url,
                        headers={"Content-Type": "application/json; charset=utf-8"},
                        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                        timeout=self.timeout,
                    )
                    data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
                    if resp.status_code < 400 and data.get("errmsg") == "ok":
                        chunks_sent += 1
                        last_error = None
                        break

                    err = data.get("errmsg") or f"http {resp.status_code}"
                    last_error = err
                    _log.warning(
                        "DingTalk send attempt %d/%d failed: %s",
                        attempt,
                        self.retry_attempts,
                        err,
                    )
                except Exception as exc:  # noqa: BLE001
                    last_error = f"{type(exc).__name__}: {exc}"
                    _log.warning(
                        "DingTalk send attempt %d/%d raised: %s",
                        attempt,
                        self.retry_attempts,
                        last_error,
                    )

                if attempt < self.retry_attempts:
                    backoff = 1.5 ** attempt
                    time.sleep(backoff)
            else:
                # All retries exhausted for this chunk
                pass

        success = chunks_sent == len(markdown_chunks)
        return DeliveryResult(
            success=success,
            chunks_sent=chunks_sent,
            chunks_total=len(markdown_chunks),
            error=None if success else last_error,
        )


__all__ = ["DingTalkNotifier"]

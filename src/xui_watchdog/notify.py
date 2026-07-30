"""Optional per-action notifications, so admins get an audit trail without
having to tail logs. Both channels are best-effort: a notification failure
is logged and swallowed, never raised — a Telegram outage should not stop
the watchdog from enforcing quotas.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from .enforcer import ActionTaken, EnforcementResult

logger = logging.getLogger("xui_watchdog.notify")


@dataclass
class NotifyConfig:
    webhook_url: str | None = None
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
    notify_on: tuple[ActionTaken, ...] = (
        ActionTaken.GRPC_REMOVE_USER,
        ActionTaken.REST_DISABLE,
        ActionTaken.RESTART_XRAY,
        ActionTaken.READMITTED,
        ActionTaken.FAILED,
    )


class Notifier:
    def __init__(self, config: NotifyConfig):
        self.config = config

    async def notify(self, result: EnforcementResult) -> None:
        if result.action not in self.config.notify_on:
            return
        message = _format_message(result)
        if self.config.webhook_url:
            await self._send_webhook(message, result)
        if self.config.telegram_bot_token and self.config.telegram_chat_id:
            await self._send_telegram(message)

    async def _send_webhook(self, message: str, result: EnforcementResult) -> None:
        payload = {
            "email": result.verdict.email,
            "inbound_tag": result.verdict.inbound_tag,
            "reason": result.verdict.reason.value,
            "action": result.action.value,
            "detail": result.detail,
            "used_bytes": result.verdict.used_bytes,
            "total_bytes": result.verdict.total_bytes,
            "message": message,
        }
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.post(self.config.webhook_url, json=payload)  # type: ignore[arg-type]
                resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001 — notifications must never break enforcement
            logger.warning("webhook notification failed: %s", exc)

    async def _send_telegram(self, message: str) -> None:
        url = f"https://api.telegram.org/bot{self.config.telegram_bot_token}/sendMessage"
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.post(
                    url,
                    json={
                        "chat_id": self.config.telegram_chat_id,
                        "text": message,
                        "parse_mode": "Markdown",
                    },
                )
                resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            logger.warning("telegram notification failed: %s", exc)


def _format_message(result: EnforcementResult) -> str:
    v = result.verdict
    action_labels = {
        ActionTaken.GRPC_REMOVE_USER: "removed (gRPC)",
        ActionTaken.REST_DISABLE: "disabled (REST)",
        ActionTaken.RESTART_XRAY: "⚠️ Xray RESTARTED (nuclear fallback)",
        ActionTaken.READMITTED: "readmitted",
        ActionTaken.FAILED: "❌ enforcement FAILED",
        ActionTaken.DRY_RUN: "would act (dry-run)",
        ActionTaken.NONE: "no action",
    }
    label = action_labels.get(result.action, result.action.value)
    return (
        f"*3xui-watchdog*: `{v.email}` on `{v.inbound_tag}` — {label}\n"
        f"reason: {v.reason.value} | usage: {v.used_bytes}/{v.total_bytes} bytes\n"
        f"{result.detail}"
    )

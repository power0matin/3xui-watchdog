"""Loads watchdog configuration from a YAML file, with environment
variables overriding matching keys. Env vars use the prefix `XUIWD_` and
double-underscore for nesting, e.g. `XUIWD_PANEL__PASSWORD` overrides
`panel.password`. This mirrors the config.example.yaml shape exactly.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

_ENV_PREFIX = "XUIWD_"


@dataclass
class WatchdogConfig:
    poll_interval_seconds: int = 10
    once: bool = False
    dry_run: bool = False
    log_level: str = "INFO"
    log_json: bool = False

    panel_base_url: str = "http://127.0.0.1:2053"
    panel_auth_mode: str = "password"  # "password" | "token"
    panel_username: str | None = None
    panel_password: str | None = None
    panel_api_token: str | None = None
    panel_verify_tls: bool = True

    xray_grpc_enabled: bool = True
    xray_grpc_host: str = "127.0.0.1"
    xray_grpc_port: int = 10085

    enable_restart_fallback: bool = False
    restart_grace_period_seconds: int = 120

    webhook_url: str | None = None
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None

    raw: dict[str, Any] = field(default_factory=dict)


def load_config(path: str | Path | None) -> WatchdogConfig:
    data: dict[str, Any] = {}
    if path is not None and Path(path).exists():
        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}

    def get(*keys: str, default: Any = None) -> Any:
        env_key = _ENV_PREFIX + "__".join(k.upper() for k in keys)
        if env_key in os.environ:
            return _coerce(os.environ[env_key], default)
        node: Any = data
        for k in keys:
            if not isinstance(node, dict) or k not in node:
                return default
            node = node[k]
        return node

    cfg = WatchdogConfig(
        poll_interval_seconds=int(get("poll_interval_seconds", default=10)),
        once=bool(get("once", default=False)),
        dry_run=bool(get("dry_run", default=False)),
        log_level=str(get("log_level", default="INFO")),
        log_json=bool(get("log_json", default=False)),
        panel_base_url=str(get("panel", "base_url", default="http://127.0.0.1:2053")),
        panel_auth_mode=str(get("panel", "auth_mode", default="password")),
        panel_username=get("panel", "username"),
        panel_password=get("panel", "password"),
        panel_api_token=get("panel", "api_token"),
        panel_verify_tls=bool(get("panel", "verify_tls", default=True)),
        xray_grpc_enabled=bool(get("xray_grpc", "enabled", default=True)),
        xray_grpc_host=str(get("xray_grpc", "host", default="127.0.0.1")),
        xray_grpc_port=int(get("xray_grpc", "port", default=10085)),
        enable_restart_fallback=bool(get("restart_fallback", "enabled", default=False)),
        restart_grace_period_seconds=int(
            get("restart_fallback", "grace_period_seconds", default=120)
        ),
        webhook_url=get("notify", "webhook_url"),
        telegram_bot_token=get("notify", "telegram_bot_token"),
        telegram_chat_id=get("notify", "telegram_chat_id"),
        raw=data,
    )
    return cfg


def _coerce(value: str, default: Any) -> Any:
    """Environment variables arrive as strings; coerce to match the
    default's type so e.g. XUIWD_DRY_RUN=true works as a bool, not a
    truthy non-empty string check on "false"."""
    if isinstance(default, bool):
        return value.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(default, int):
        return int(value)
    return value

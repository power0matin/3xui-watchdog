"""CLI entrypoint and the daemon poll loop.

    xui-watchdog --config config.yaml            # run forever, poll every N seconds
    xui-watchdog --config config.yaml --once      # single pass, for cron
    xui-watchdog --config config.yaml --dry-run   # log only, take no action
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
import sys
from typing import Any

from .config import WatchdogConfig, load_config
from .enforcer import Enforcer, EnforcerConfig, EnforcementResult
from .notify import NotifyConfig, Notifier
from .panel_client import PanelClient, PanelConfig
from .xray_grpc_client import XrayGRPCClient, XrayGRPCConfig, XrayGRPCUnavailable

logger = logging.getLogger("xui_watchdog")


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload)


def _setup_logging(level: str, as_json: bool) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        _JsonFormatter()
        if as_json
        else logging.Formatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s")
    )
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="xui-watchdog",
        description="Real-time traffic & expiry enforcement watchdog for 3X-UI/Xray-core.",
    )
    parser.add_argument("--config", "-c", default="config.yaml", help="path to config.yaml")
    parser.add_argument("--once", action="store_true", help="run a single pass and exit (cron mode)")
    parser.add_argument("--dry-run", action="store_true", help="log actions only, take none")
    parser.add_argument(
        "--poll-interval", type=int, default=None, help="override poll_interval_seconds"
    )
    return parser


async def _run_cycle(
    cfg: WatchdogConfig, enforcer: Enforcer, panel: PanelClient, notifier: Notifier
) -> list[EnforcementResult]:
    clients = await panel.list_client_states()
    results = await enforcer.enforce_cycle(clients)
    for result in results:
        if result.action.value != "none":
            logger.info(
                "action=%s email=%s reason=%s detail=%s",
                result.action.value,
                result.verdict.email,
                result.verdict.reason.value,
                result.detail,
            )
        await notifier.notify(result)
    return results


async def async_main(cfg: WatchdogConfig) -> int:
    _setup_logging(cfg.log_level, cfg.log_json)
    logger.info(
        "starting 3xui-watchdog (dry_run=%s, poll_interval=%ss, once=%s)",
        cfg.dry_run,
        cfg.poll_interval_seconds,
        cfg.once,
    )

    panel_config = PanelConfig(
        base_url=cfg.panel_base_url,
        auth_mode=cfg.panel_auth_mode,
        username=cfg.panel_username,
        password=cfg.panel_password,
        api_token=cfg.panel_api_token,
        verify_tls=cfg.panel_verify_tls,
    )

    grpc_client: XrayGRPCClient | None = None
    if cfg.xray_grpc_enabled:
        grpc_client = XrayGRPCClient(
            XrayGRPCConfig(host=cfg.xray_grpc_host, port=cfg.xray_grpc_port)
        )
        try:
            grpc_client.connect()
            logger.info("connected to Xray gRPC API at %s:%s", cfg.xray_grpc_host, cfg.xray_grpc_port)
        except XrayGRPCUnavailable as exc:
            logger.warning(
                "Xray gRPC API unavailable (%s) — will use REST fallback for all actions", exc
            )
            grpc_client = None

    enforcer_config = EnforcerConfig(
        dry_run=cfg.dry_run,
        enable_restart_fallback=cfg.enable_restart_fallback,
        restart_grace_period_seconds=cfg.restart_grace_period_seconds,
    )
    notify_config = NotifyConfig(
        webhook_url=cfg.webhook_url,
        telegram_bot_token=cfg.telegram_bot_token,
        telegram_chat_id=cfg.telegram_chat_id,
    )
    notifier = Notifier(notify_config)

    stop_event = asyncio.Event()

    def _handle_signal(*_: object) -> None:
        logger.info("shutdown signal received")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handle_signal)
        except NotImplementedError:
            # add_signal_handler isn't available on Windows' default loop;
            # --once/cron mode still works fine there.
            pass

    exit_code = 0
    async with PanelClient(panel_config) as panel:
        enforcer = Enforcer(enforcer_config, panel, grpc_client)
        try:
            if cfg.once:
                await _run_cycle(cfg, enforcer, panel, notifier)
            else:
                while not stop_event.is_set():
                    try:
                        await _run_cycle(cfg, enforcer, panel, notifier)
                    except Exception:  # noqa: BLE001 — one bad cycle must not kill the daemon
                        logger.exception("poll cycle failed, will retry next interval")
                    try:
                        await asyncio.wait_for(
                            stop_event.wait(), timeout=cfg.poll_interval_seconds
                        )
                    except asyncio.TimeoutError:
                        pass
        finally:
            if grpc_client is not None:
                grpc_client.close()

    return exit_code


def cli() -> None:
    parser = build_arg_parser()
    args = parser.parse_args()

    cfg = load_config(args.config)
    if args.once:
        cfg.once = True
    if args.dry_run:
        cfg.dry_run = True
    if args.poll_interval is not None:
        cfg.poll_interval_seconds = args.poll_interval

    exit_code = asyncio.run(async_main(cfg))
    sys.exit(exit_code)


if __name__ == "__main__":
    cli()

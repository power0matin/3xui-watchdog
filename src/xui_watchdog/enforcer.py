"""Safe per-client enforcement with gRPC and 3x-ui REST fallbacks."""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from .panel_client import PanelClient
from .policy import ClientState, Verdict, evaluate_client, evaluate_clients, should_readmit
from .xray_grpc_client import XrayGRPCClient, XrayGRPCUnavailable

logger = logging.getLogger("xui_watchdog.enforcer")


class ActionTaken(str, Enum):
    NONE = "none"
    GRPC_REMOVE_USER = "grpc_remove_user"
    REST_DISABLE = "rest_disable"
    RESTART_XRAY = "restart_xray"
    DRY_RUN = "dry_run"
    READMITTED = "readmitted"
    FAILED = "failed"


@dataclass
class EnforcementResult:
    verdict: Verdict
    action: ActionTaken
    detail: str = ""


@dataclass
class EnforcerConfig:
    dry_run: bool = False
    enable_restart_fallback: bool = False
    restart_grace_period_seconds: int = 120
    state_file: Path | None = None


class Enforcer:
    def __init__(
        self,
        config: EnforcerConfig,
        panel: PanelClient,
        grpc_client: XrayGRPCClient | None = None,
    ):
        self.config = config
        self.panel = panel
        self.grpc_client = grpc_client
        self._actioned: dict[str, float] = {}
        self._action_sources: dict[str, str] = {}
        self._first_violation_seen: dict[str, float] = {}
        self._load_actioned_state()

    async def enforce_cycle(self, clients: list[ClientState]) -> list[EnforcementResult]:
        """Evaluate one panel snapshot, enforce new violations, and reconcile."""
        now = time.time()
        verdicts = evaluate_clients(clients)
        results: list[EnforcementResult] = []
        by_email = {client.email: client for client in clients}

        for verdict in verdicts:
            client = by_email[verdict.email]
            already_actioned = verdict.email in self._actioned

            # A client disabled by an admin must not be re-disabled by the
            # watchdog. Previously actioned clients are the exception: their
            # disabled flag may be the result of our own REST action.
            if not client.enable and not already_actioned:
                continue

            if not verdict.is_violation:
                if already_actioned and should_readmit(client, was_actioned=True):
                    # If the prior action was direct gRPC, the panel setting
                    # should still be enabled. A false value now indicates a
                    # manual admin disable; never override it.
                    source = self._action_sources.get(verdict.email, "")
                    if source == ActionTaken.GRPC_REMOVE_USER.value and not client.enable:
                        continue

                    result = await self._readmit(client)
                    results.append(result)
                    if result.action is ActionTaken.READMITTED:
                        self._forget_actioned(verdict.email)
                continue

            if already_actioned:
                continue

            self._first_violation_seen.setdefault(verdict.email, now)
            result = await self._act(verdict, client)
            results.append(result)

            if result.action in (
                ActionTaken.GRPC_REMOVE_USER,
                ActionTaken.REST_DISABLE,
                ActionTaken.RESTART_XRAY,
            ):
                self._actioned[verdict.email] = now
                self._action_sources[verdict.email] = result.action.value
                self._persist_actioned_state()

        return results

    async def _act(self, verdict: Verdict, client: ClientState) -> EnforcementResult:
        if self.config.dry_run:
            logger.info(
                "[dry-run] would action %s (reason=%s, used=%d/%d)",
                verdict.email,
                verdict.reason.value,
                verdict.used_bytes,
                verdict.total_bytes,
            )
            return EnforcementResult(verdict, ActionTaken.DRY_RUN)

        # Priority 1: direct Xray gRPC. Catch transport/RPC exceptions too:
        # grpc.RpcError is not necessarily XrayGRPCUnavailable.
        if self.grpc_client is not None and self.grpc_client.is_available():
            try:
                self.grpc_client.remove_user(verdict.inbound_tag, verdict.email)
                return EnforcementResult(
                    verdict, ActionTaken.GRPC_REMOVE_USER, "removed via Xray gRPC HandlerService"
                )
            except Exception as exc:  # noqa: BLE001 — always try the REST fallback
                logger.warning(
                    "gRPC RemoveUser failed for %s; trying panel REST fallback: %s",
                    verdict.email,
                    exc,
                )

        # Priority 2: panel REST updateClient. The panel itself uses its
        # configured Xray API to remove/add this one user without restarting
        # the core when that API is healthy.
        if (
            client.inbound_id is not None
            and client.client_id
            and client.client_settings is not None
        ):
            try:
                ok = await self.panel.disable_client(
                    client.inbound_id, client.client_id, client.client_settings
                )
                if ok:
                    return EnforcementResult(
                        verdict, ActionTaken.REST_DISABLE, "disabled via 3x-ui REST updateClient"
                    )
                rest_detail = "3x-ui REST updateClient returned success=false"
            except Exception as exc:  # noqa: BLE001 — surfaced in result, not raised
                rest_detail = f"3x-ui REST updateClient failed: {exc}"
                logger.error("REST disable failed for %s: %s", verdict.email, exc)
        else:
            rest_detail = "REST fallback unavailable: inbound/client metadata is missing"

        # Priority 3: explicitly opt-in full-core restart, never the default.
        if self.config.enable_restart_fallback:
            first_seen = self._first_violation_seen.get(verdict.email, time.time())
            elapsed = time.time() - first_seen
            if elapsed >= self.config.restart_grace_period_seconds:
                logger.warning(
                    "NUCLEAR OPTION: restarting Xray to enforce %s "
                    "(gRPC and REST failed; grace period elapsed)",
                    verdict.email,
                )
                try:
                    if await self.panel.restart_xray():
                        return EnforcementResult(
                            verdict,
                            ActionTaken.RESTART_XRAY,
                            "gRPC and REST unavailable; full Xray restart accepted by panel",
                        )
                    rest_detail += "; restart endpoint returned success=false"
                except Exception as exc:  # noqa: BLE001
                    logger.error("restart_xray failed: %s", exc)
                    rest_detail += f"; restart failed: {exc}"

        return EnforcementResult(verdict, ActionTaken.FAILED, rest_detail)

    async def _readmit(self, client: ClientState) -> EnforcementResult:
        verdict = evaluate_client(client)
        if (
            client.inbound_id is None
            or not client.client_id
            or client.client_settings is None
        ):
            return EnforcementResult(
                verdict, ActionTaken.FAILED, "readmission unavailable: client metadata is missing"
            )

        try:
            ok = await self.panel.enable_client(
                client.inbound_id, client.client_id, client.client_settings
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("readmit failed for %s: %s", client.email, exc)
            return EnforcementResult(verdict, ActionTaken.FAILED, f"readmit failed: {exc}")

        if ok:
            logger.info("readmitted %s (quota/expiry now valid)", client.email)
            return EnforcementResult(verdict, ActionTaken.READMITTED, "re-enabled via 3x-ui REST API")
        return EnforcementResult(
            verdict, ActionTaken.FAILED, "3x-ui REST updateClient rejected readmission"
        )

    def _forget_actioned(self, email: str) -> None:
        self._actioned.pop(email, None)
        self._action_sources.pop(email, None)
        self._first_violation_seen.pop(email, None)
        self._persist_actioned_state()

    def _load_actioned_state(self) -> None:
        path = self.config.state_file
        if path is None or not path.exists():
            return
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if payload.get("version") != 1 or not isinstance(payload.get("actioned"), dict):
                raise ValueError("unsupported watchdog state format")
            for email, record in payload["actioned"].items():
                if not isinstance(record, dict):
                    continue
                timestamp = float(record.get("at", 0))
                source = str(record.get("source", ""))
                if email and timestamp > 0:
                    self._actioned[str(email)] = timestamp
                    self._action_sources[str(email)] = source
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            # Do not guess ownership of disabled users when state is damaged.
            logger.error("could not load watchdog action state from %s: %s", path, exc)

    def _persist_actioned_state(self) -> None:
        path = self.config.state_file
        if path is None or self.config.dry_run:
            return
        payload = {
            "version": 1,
            "actioned": {
                email: {"at": timestamp, "source": self._action_sources.get(email, "")}
                for email, timestamp in self._actioned.items()
            },
        }
        temporary_path: str | None = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=path.parent,
                prefix=f".{path.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
                temporary_path = handle.name
            os.chmod(temporary_path, 0o600)
            os.replace(temporary_path, path)
        except OSError as exc:
            logger.error("could not persist watchdog action state to %s: %s", path, exc)
            if temporary_path is not None:
                try:
                    os.unlink(temporary_path)
                except OSError:
                    pass

"""Decides and executes the action for each flagged client, in the priority
order from the spec:

  1. Preferred:   Xray gRPC HandlerService.RemoveUser  (surgical, no restart)
  2. Fallback A:  3x-ui REST API client disable        (if gRPC unreachable)
  3. Fallback B:  full Xray restart via the panel       (opt-in, off by default,
                  "nuclear option" — disconnects every user on the server)

Never falls through to a config-rewrite-and-restart as the default path;
Fallback B requires explicit config (`enable_restart_fallback: true`) plus
its own grace period, and is logged distinctly so it's never mistaken for
routine enforcement in an audit trail.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum

from .panel_client import PanelClient
from .policy import ClientState, Verdict, evaluate_clients, should_readmit
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
    # inbound_id / client_uuid are needed for the REST disable/enable calls
    # but not for gRPC RemoveUser (which only needs tag + email). The panel
    # client is expected to supply these via list_client_states() metadata
    # in a future version; for now the enforcer resolves them via a lookup
    # map passed in at call time (see `client_meta` in enforce_cycle).
    client_meta: dict[str, tuple[int, str]] = field(default_factory=dict)  # email -> (inbound_id, client_uuid)


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
        # Tracks clients actioned this run so we don't spam RemoveUser calls
        # or double-log every poll cycle — cleared only when a client is
        # confirmed readmitted (see reconcile()).
        self._actioned: dict[str, float] = {}  # email -> epoch seconds actioned
        self._first_violation_seen: dict[str, float] = {}  # email -> epoch seconds

    async def enforce_cycle(self, clients: list[ClientState]) -> list[EnforcementResult]:
        """One full poll cycle: evaluate every client, act on new violations,
        skip ones already actioned, and reconcile any that are valid again.
        """
        now = time.time()
        verdicts = evaluate_clients(clients)
        results: list[EnforcementResult] = []

        by_email = {c.email: c for c in clients}

        for verdict in verdicts:
            already_actioned = verdict.email in self._actioned

            if not verdict.is_violation:
                if already_actioned:
                    # Reconcile: client is valid again (quota bumped /
                    # expiry extended by an admin) — readmit it rather than
                    # requiring a manual restart.
                    client = by_email[verdict.email]
                    if should_readmit(client, was_actioned=True):
                        result = await self._readmit(client)
                        results.append(result)
                        del self._actioned[verdict.email]
                        self._first_violation_seen.pop(verdict.email, None)
                continue

            if already_actioned:
                # Already handled this violation earlier — don't re-call
                # RemoveUser/disable every poll interval. This is exactly
                # the "already disabled but still returned by the API" loop
                # the spec calls out; we track it ourselves instead of
                # trusting the panel to have converged yet.
                continue

            self._first_violation_seen.setdefault(verdict.email, now)
            result = await self._act(verdict)
            results.append(result)
            if result.action not in (ActionTaken.FAILED, ActionTaken.NONE):
                self._actioned[verdict.email] = now

        return results

    async def _act(self, verdict: Verdict) -> EnforcementResult:
        if self.config.dry_run:
            logger.info(
                "[dry-run] would action %s (reason=%s, used=%d/%d)",
                verdict.email,
                verdict.reason.value,
                verdict.used_bytes,
                verdict.total_bytes,
            )
            return EnforcementResult(verdict, ActionTaken.DRY_RUN)

        # Priority 1: direct gRPC RemoveUser.
        if self.grpc_client is not None and self.grpc_client.is_available():
            try:
                self.grpc_client.remove_user(verdict.inbound_tag, verdict.email)
                return EnforcementResult(
                    verdict, ActionTaken.GRPC_REMOVE_USER, "removed via Xray gRPC HandlerService"
                )
            except XrayGRPCUnavailable as exc:
                logger.warning(
                    "gRPC RemoveUser failed for %s, falling back to REST: %s",
                    verdict.email,
                    exc,
                )

        # Priority 2: REST API disable.
        meta = self.config.client_meta.get(verdict.email)
        if meta is not None:
            inbound_id, client_uuid = meta
            try:
                ok = await self.panel.disable_client(inbound_id, client_uuid, verdict.email)
                if ok:
                    return EnforcementResult(
                        verdict, ActionTaken.REST_DISABLE, "disabled via 3x-ui REST API"
                    )
            except Exception as exc:  # noqa: BLE001 — surfaced via result, not raised
                logger.error("REST disable failed for %s: %s", verdict.email, exc)

        # Priority 3 (opt-in only): full Xray restart, after a grace period.
        if self.config.enable_restart_fallback:
            first_seen = self._first_violation_seen.get(verdict.email, time.time())
            elapsed = time.time() - first_seen
            if elapsed >= self.config.restart_grace_period_seconds:
                logger.warning(
                    "NUCLEAR OPTION: restarting Xray to enforce %s "
                    "(gRPC and REST both unavailable, grace period elapsed)",
                    verdict.email,
                )
                try:
                    await self.panel.restart_xray()
                    return EnforcementResult(
                        verdict,
                        ActionTaken.RESTART_XRAY,
                        "gRPC and REST fallback both unavailable; full restart triggered",
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.error("restart_xray failed: %s", exc)

        return EnforcementResult(
            verdict, ActionTaken.FAILED, "no enforcement path succeeded this cycle"
        )

    async def _readmit(self, client: ClientState) -> EnforcementResult:
        from .policy import evaluate_client

        verdict = evaluate_client(client)
        meta = self.config.client_meta.get(client.email)

        if self.grpc_client is not None and self.grpc_client.is_available():
            # Real readmission via gRPC needs the client's protocol account
            # object (VLess/VMess/Trojan), which the watchdog does not
            # generate — 3x-ui owns credential issuance. In practice this
            # path re-enables via REST (which the panel then reconciles
            # into Xray on its own next sync) rather than reconstructing
            # account details here; see the AddUser docstring in
            # xray_grpc_client.py.
            pass

        if meta is not None:
            inbound_id, client_uuid = meta
            try:
                ok = await self.panel.enable_client(inbound_id, client_uuid, client.email)
                if ok:
                    logger.info("readmitted %s (quota/expiry now valid)", client.email)
                    return EnforcementResult(verdict, ActionTaken.READMITTED, "re-enabled via REST API")
            except Exception as exc:  # noqa: BLE001
                logger.error("readmit failed for %s: %s", client.email, exc)

        return EnforcementResult(verdict, ActionTaken.FAILED, "readmit attempted but no path succeeded")

"""Pure policy functions and normalized 3x-ui client state."""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum
from typing import Any


class ViolationReason(str, Enum):
    """Why a client was flagged."""

    NONE = "none"
    TRAFFIC_DEPLETED = "traffic_depleted"
    EXPIRED = "expired"


@dataclass(frozen=True, slots=True)
class ClientState:
    """Snapshot of one panel client.

    `enable` is the client's configured 3x-ui setting, not the separate
    ClientTraffic.Enable flag. 3x-ui changes the latter when its own quota
    checker blocks a user; using it as the configured flag would make
    reconciliation impossible.

    REST updateClient needs the owning inbound, protocol-specific client ID,
    and the full client object so that the panel can update exactly one
    client while preserving its credentials and protocol settings.
    """

    email: str
    inbound_tag: str
    total: int
    up: int
    down: int
    expiry_time_ms: int
    enable: bool = True
    inbound_id: int | None = None
    client_id: str | None = None
    protocol: str | None = None
    client_settings: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class Verdict:
    """Result of evaluating one ClientState."""

    email: str
    inbound_tag: str
    reason: ViolationReason
    used_bytes: int
    total_bytes: int
    seconds_past_expiry: int

    @property
    def is_violation(self) -> bool:
        return self.reason is not ViolationReason.NONE


def is_traffic_depleted(total: int, up: int, down: int) -> bool:
    """A non-positive total is unlimited; quota is depleted at used >= total."""
    if total <= 0:
        return False
    return (up + down) >= total


def is_expired(expiry_time_ms: int, now_ms: int | None = None) -> bool:
    """A non-positive expiry is unset or unresolved, not an absolute timestamp."""
    if expiry_time_ms <= 0:
        return False
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    return now_ms >= expiry_time_ms


def evaluate_client(client: ClientState, now_ms: int | None = None) -> Verdict:
    """Evaluate quota and expiry independently of the panel's enable flag.

    The enforcer decides whether a disabled client is owned by this watchdog
    before taking action. Keeping policy evaluation independent allows a
    watchdog-disabled client to be recognized as healthy after its quota or
    expiry is corrected and then safely readmitted.
    """
    if now_ms is None:
        now_ms = int(time.time() * 1000)

    used = client.up + client.down
    depleted = is_traffic_depleted(client.total, client.up, client.down)
    expired = is_expired(client.expiry_time_ms, now_ms)

    if depleted:
        reason = ViolationReason.TRAFFIC_DEPLETED
    elif expired:
        reason = ViolationReason.EXPIRED
    else:
        reason = ViolationReason.NONE

    seconds_past_expiry = 0
    if client.expiry_time_ms > 0 and now_ms >= client.expiry_time_ms:
        seconds_past_expiry = (now_ms - client.expiry_time_ms) // 1000

    return Verdict(
        email=client.email,
        inbound_tag=client.inbound_tag,
        reason=reason,
        used_bytes=used,
        total_bytes=max(client.total, 0),
        seconds_past_expiry=seconds_past_expiry,
    )


def evaluate_clients(
    clients: list[ClientState], now_ms: int | None = None
) -> list[Verdict]:
    """Return one deterministic verdict for every client, preserving order."""
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    return [evaluate_client(client, now_ms) for client in clients]


def should_readmit(
    client: ClientState, was_actioned: bool, now_ms: int | None = None
) -> bool:
    """Readmit only clients this watchdog previously actioned and now valid.

    The panel's configured `enable` flag may be false because the watchdog
    itself changed it through updateClient. Ownership is therefore determined
    by the enforcer's action state, not by that flag.
    """
    if not was_actioned:
        return False
    return not evaluate_client(client, now_ms).is_violation

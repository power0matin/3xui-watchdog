"""Pure, dependency-free policy functions for deciding whether a client is
depleted (over quota) or expired.

These mirror the logic 3x-ui itself uses internally
(`total > 0 && (up + down) >= total`, and `expiryTime != 0 && now > expiryTime`),
but are computed independently here so the watchdog never depends on the
panel having already noticed. Keeping this file free of I/O makes it trivial
to unit test exhaustively — every branch below is exercised in
tests/test_policy.py with no network, no mocks, no fixtures beyond plain data.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum


class ViolationReason(str, Enum):
    """Why a client was flagged. Kept as an explicit enum (rather than a
    bare bool) so downstream logging/notifications can say *why*, and so a
    client that is both over quota and expired still reports a single,
    unambiguous primary reason.
    """

    NONE = "none"
    TRAFFIC_DEPLETED = "traffic_depleted"
    EXPIRED = "expired"


@dataclass(frozen=True, slots=True)
class ClientState:
    """A snapshot of one client's quota/expiry state, sourced from either
    the 3x-ui REST API or Xray's gRPC StatsService. Both call sites should
    normalize into this shape before calling `evaluate_client`.

    Fields intentionally mirror 3x-ui's own client model:
      - total: traffic quota in bytes. 0 (or negative) means "unlimited".
      - up / down: bytes transferred in each direction so far.
      - expiry_time_ms: epoch milliseconds when the client expires.
        0 means "never expires". 3x-ui stores this in milliseconds, not
        seconds — a very easy off-by-1000 bug, so it's called out here.
      - enable: the panel's own enabled/disabled flag for this client.
        A client explicitly disabled by an admin (not by us) should not be
        re-flagged as a fresh violation; see `evaluate_client`.
    """

    email: str
    inbound_tag: str
    total: int
    up: int
    down: int
    expiry_time_ms: int
    enable: bool = True


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
    """total <= 0 means unlimited traffic — never a violation on that basis.
    Otherwise, a client has depleted their quota once cumulative up+down
    reaches or exceeds total (matches 3x-ui's own `>=` comparison, not `>`,
    so behavior is bit-for-bit consistent with what the panel would decide).
    """
    if total <= 0:
        return False
    return (up + down) >= total


def is_expired(expiry_time_ms: int, now_ms: int | None = None) -> bool:
    """expiry_time_ms <= 0 means "never expires". 3x-ui also supports
    negative expiry values as a "disabled after N days from first use"
    marker in some client flows; those are intentionally NOT treated as
    "expired" here since they haven't been resolved to an absolute
    timestamp yet — resolving that is the panel's job, not the watchdog's.
    """
    if expiry_time_ms <= 0:
        return False
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    return now_ms > expiry_time_ms


def evaluate_client(client: ClientState, now_ms: int | None = None) -> Verdict:
    """Evaluate a single client and return a Verdict.

    Order of checks: traffic depletion is checked before expiry, so a client
    that is both over quota and expired is reported with
    TRAFFIC_DEPLETED as the primary reason (arbitrary but deterministic —
    callers that need both facts can inspect used_bytes/seconds_past_expiry
    regardless of which `reason` won).
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
    if client.expiry_time_ms > 0:
        seconds_past_expiry = max(0, (now_ms - client.expiry_time_ms) // 1000)

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
    """Convenience batch wrapper. Returns a Verdict per client, including
    non-violations — filtering is left to the caller (enforcer.py) so this
    module never has to know about "already actioned this cycle" state.
    """
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    return [evaluate_client(c, now_ms) for c in clients]


def should_readmit(
    client: ClientState, was_actioned: bool, now_ms: int | None = None
) -> bool:
    """Decide whether a previously-actioned client should be re-added to the
    running inbound on the next reconcile pass. True when the client is
    still `enable`d in the panel (i.e. an admin didn't disable them for an
    unrelated reason) and no longer violates either check.
    """
    if not was_actioned:
        return False
    if not client.enable:
        return False
    verdict = evaluate_client(client, now_ms)
    return not verdict.is_violation

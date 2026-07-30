"""Unit tests for xui_watchdog.policy — pure functions, no I/O, no mocks.
Written against stdlib unittest so they run with zero extra dependencies
(`python -m unittest discover`), and are also auto-discovered by pytest
for CI, which additionally runs mypy/ruff over the whole src tree.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xui_watchdog.policy import (  # noqa: E402
    ClientState,
    ViolationReason,
    evaluate_client,
    evaluate_clients,
    is_expired,
    is_traffic_depleted,
    should_readmit,
)

NOW_MS = 1_700_000_000_000  # fixed reference instant for deterministic tests


def make_client(**overrides: object) -> ClientState:
    defaults: dict[str, object] = dict(
        email="alice@example.com",
        inbound_tag="vless-in",
        total=0,
        up=0,
        down=0,
        expiry_time_ms=0,
        enable=True,
    )
    defaults.update(overrides)
    return ClientState(**defaults)  # type: ignore[arg-type]


class TestIsTrafficDepleted(unittest.TestCase):
    def test_unlimited_quota_never_depleted(self) -> None:
        self.assertFalse(is_traffic_depleted(total=0, up=10**12, down=10**12))
        self.assertFalse(is_traffic_depleted(total=-1, up=10**12, down=10**12))

    def test_under_quota_not_depleted(self) -> None:
        self.assertFalse(is_traffic_depleted(total=1000, up=300, down=300))

    def test_exactly_at_quota_is_depleted(self) -> None:
        # 3x-ui uses >=, not >, so exact boundary is a violation.
        self.assertTrue(is_traffic_depleted(total=1000, up=500, down=500))

    def test_over_quota_is_depleted(self) -> None:
        self.assertTrue(is_traffic_depleted(total=1000, up=800, down=800))

    def test_zero_usage_zero_quota_not_depleted(self) -> None:
        self.assertFalse(is_traffic_depleted(total=0, up=0, down=0))


class TestIsExpired(unittest.TestCase):
    def test_zero_expiry_never_expires(self) -> None:
        self.assertFalse(is_expired(0, now_ms=NOW_MS))

    def test_negative_expiry_treated_as_not_resolved(self) -> None:
        # 3x-ui uses negative expiryTime for "N days from first connect,
        # not yet resolved" — not an absolute past timestamp.
        self.assertFalse(is_expired(-86400000, now_ms=NOW_MS))

    def test_future_expiry_not_expired(self) -> None:
        self.assertFalse(is_expired(NOW_MS + 1000, now_ms=NOW_MS))

    def test_past_expiry_is_expired(self) -> None:
        self.assertTrue(is_expired(NOW_MS - 1000, now_ms=NOW_MS))

    def test_exact_now_is_not_yet_expired(self) -> None:
        # Strict '>' matches the spec's `now > expiryTime`.
        self.assertFalse(is_expired(NOW_MS, now_ms=NOW_MS))

    def test_defaults_to_real_clock_when_now_ms_omitted(self) -> None:
        # A client that "expired" in 1970 should register as expired
        # against the real current time without needing now_ms passed in.
        self.assertTrue(is_expired(1))


class TestEvaluateClient(unittest.TestCase):
    def test_healthy_client_no_violation(self) -> None:
        client = make_client(total=1000, up=100, down=100, expiry_time_ms=NOW_MS + 10_000)
        verdict = evaluate_client(client, now_ms=NOW_MS)
        self.assertEqual(verdict.reason, ViolationReason.NONE)
        self.assertFalse(verdict.is_violation)

    def test_depleted_traffic_flagged(self) -> None:
        client = make_client(total=1000, up=600, down=600)
        verdict = evaluate_client(client, now_ms=NOW_MS)
        self.assertEqual(verdict.reason, ViolationReason.TRAFFIC_DEPLETED)
        self.assertTrue(verdict.is_violation)
        self.assertEqual(verdict.used_bytes, 1200)

    def test_expired_client_flagged(self) -> None:
        client = make_client(total=0, expiry_time_ms=NOW_MS - 5000)
        verdict = evaluate_client(client, now_ms=NOW_MS)
        self.assertEqual(verdict.reason, ViolationReason.EXPIRED)
        self.assertGreaterEqual(verdict.seconds_past_expiry, 5)

    def test_both_depleted_and_expired_reports_traffic_first(self) -> None:
        client = make_client(total=100, up=100, down=100, expiry_time_ms=NOW_MS - 1000)
        verdict = evaluate_client(client, now_ms=NOW_MS)
        self.assertEqual(verdict.reason, ViolationReason.TRAFFIC_DEPLETED)
        # seconds_past_expiry is still reported even though it wasn't the
        # winning reason, so callers/notifications have both facts.
        self.assertGreater(verdict.seconds_past_expiry, 0)

    def test_seconds_past_expiry_zero_when_not_expired(self) -> None:
        client = make_client(expiry_time_ms=NOW_MS + 100_000)
        verdict = evaluate_client(client, now_ms=NOW_MS)
        self.assertEqual(verdict.seconds_past_expiry, 0)

    def test_seconds_past_expiry_zero_when_no_expiry_set(self) -> None:
        client = make_client(expiry_time_ms=0)
        verdict = evaluate_client(client, now_ms=NOW_MS)
        self.assertEqual(verdict.seconds_past_expiry, 0)


class TestEvaluateClients(unittest.TestCase):
    def test_batch_preserves_order_and_includes_non_violations(self) -> None:
        clients = [
            make_client(email="a@x.com", total=100, up=50, down=0),
            make_client(email="b@x.com", total=100, up=200, down=0),
            make_client(email="c@x.com", expiry_time_ms=NOW_MS - 1),
        ]
        verdicts = evaluate_clients(clients, now_ms=NOW_MS)
        self.assertEqual([v.email for v in verdicts], ["a@x.com", "b@x.com", "c@x.com"])
        self.assertEqual(verdicts[0].reason, ViolationReason.NONE)
        self.assertEqual(verdicts[1].reason, ViolationReason.TRAFFIC_DEPLETED)
        self.assertEqual(verdicts[2].reason, ViolationReason.EXPIRED)


class TestShouldReadmit(unittest.TestCase):
    def test_not_actioned_never_readmitted(self) -> None:
        client = make_client(total=100, up=50, down=0)
        self.assertFalse(should_readmit(client, was_actioned=False, now_ms=NOW_MS))

    def test_admin_disabled_client_not_readmitted(self) -> None:
        # Quota is fine now, but an admin explicitly disabled this client
        # for an unrelated reason — the watchdog must not override that.
        client = make_client(total=100, up=50, down=0, enable=False)
        self.assertFalse(should_readmit(client, was_actioned=True, now_ms=NOW_MS))

    def test_still_violating_not_readmitted(self) -> None:
        client = make_client(total=100, up=200, down=0)
        self.assertFalse(should_readmit(client, was_actioned=True, now_ms=NOW_MS))

    def test_quota_bumped_is_readmitted(self) -> None:
        # Admin raised the quota after action was taken.
        client = make_client(total=10_000, up=200, down=0)
        self.assertTrue(should_readmit(client, was_actioned=True, now_ms=NOW_MS))

    def test_expiry_extended_is_readmitted(self) -> None:
        client = make_client(expiry_time_ms=NOW_MS + 50_000)
        self.assertTrue(should_readmit(client, was_actioned=True, now_ms=NOW_MS))


if __name__ == "__main__":
    unittest.main()

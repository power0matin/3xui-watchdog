"""Regression tests for the enforcement and recovery lifecycle."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from xui_watchdog.enforcer import ActionTaken, Enforcer, EnforcerConfig
from xui_watchdog.policy import ClientState


class FakePanel:
    def __init__(self, *, disable_ok: bool = True, enable_ok: bool = True) -> None:
        self.disable_ok = disable_ok
        self.enable_ok = enable_ok
        self.disable_calls: list[tuple[int, str, dict[str, Any]]] = []
        self.enable_calls: list[tuple[int, str, dict[str, Any]]] = []
        self.restart_calls = 0

    async def disable_client(
        self, inbound_id: int, client_id: str, client_settings: dict[str, Any]
    ) -> bool:
        self.disable_calls.append((inbound_id, client_id, dict(client_settings)))
        return self.disable_ok

    async def enable_client(
        self, inbound_id: int, client_id: str, client_settings: dict[str, Any]
    ) -> bool:
        self.enable_calls.append((inbound_id, client_id, dict(client_settings)))
        return self.enable_ok

    async def restart_xray(self) -> bool:
        self.restart_calls += 1
        return True


class FailingGrpc:
    def is_available(self) -> bool:
        return True

    def remove_user(self, inbound_tag: str, email: str) -> None:
        raise RuntimeError("simulated gRPC transport failure")


def make_client(**overrides: Any) -> ClientState:
    defaults: dict[str, Any] = {
        "email": "alice@example.test",
        "inbound_tag": "vless-in",
        "total": 100,
        "up": 120,
        "down": 0,
        "expiry_time_ms": 0,
        "enable": True,
        "inbound_id": 17,
        "client_id": "alice-uuid",
        "protocol": "vless",
        "client_settings": {
            "id": "alice-uuid",
            "email": "alice@example.test",
            "totalGB": 100,
            "expiryTime": 0,
            "enable": True,
            "flow": "xtls-rprx-vision",
        },
    }
    defaults.update(overrides)
    return ClientState(**defaults)


class TestEnforcer(unittest.IsolatedAsyncioTestCase):
    async def test_depleted_client_uses_rest_without_restart(self) -> None:
        panel = FakePanel()
        enforcer = Enforcer(EnforcerConfig(), panel)  # type: ignore[arg-type]

        results = await enforcer.enforce_cycle([make_client()])

        self.assertEqual([result.action for result in results], [ActionTaken.REST_DISABLE])
        self.assertEqual(len(panel.disable_calls), 1)
        self.assertEqual(panel.disable_calls[0][0:2], (17, "alice-uuid"))
        self.assertEqual(panel.restart_calls, 0)

    async def test_generic_grpc_error_falls_back_to_rest(self) -> None:
        panel = FakePanel()
        enforcer = Enforcer(EnforcerConfig(), panel, FailingGrpc())  # type: ignore[arg-type]

        results = await enforcer.enforce_cycle([make_client()])

        self.assertEqual(results[0].action, ActionTaken.REST_DISABLE)
        self.assertEqual(len(panel.disable_calls), 1)
        self.assertEqual(panel.restart_calls, 0)

    async def test_missing_rest_metadata_fails_safely_without_restart(self) -> None:
        panel = FakePanel()
        enforcer = Enforcer(EnforcerConfig(), panel)  # type: ignore[arg-type]
        client = make_client(inbound_id=None, client_id=None, client_settings=None)

        results = await enforcer.enforce_cycle([client])

        self.assertEqual(results[0].action, ActionTaken.FAILED)
        self.assertIn("metadata is missing", results[0].detail)
        self.assertEqual(panel.disable_calls, [])
        self.assertEqual(panel.restart_calls, 0)

    async def test_admin_disabled_client_is_not_actioned(self) -> None:
        panel = FakePanel()
        enforcer = Enforcer(EnforcerConfig(), panel)  # type: ignore[arg-type]
        client = make_client(enable=False)

        results = await enforcer.enforce_cycle([client])

        self.assertEqual(results, [])
        self.assertEqual(panel.disable_calls, [])
        self.assertEqual(panel.restart_calls, 0)

    async def test_readmission_survives_watchdog_process_restart(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = Path(temp_dir) / "state.json"
            first_panel = FakePanel()
            first = Enforcer(
                EnforcerConfig(state_file=state_file), first_panel  # type: ignore[arg-type]
            )

            disabled = await first.enforce_cycle([make_client()])
            self.assertEqual(disabled[0].action, ActionTaken.REST_DISABLE)
            self.assertTrue(state_file.exists())
            stored = json.loads(state_file.read_text(encoding="utf-8"))
            self.assertIn("alice@example.test", stored["actioned"])

            # Simulate the panel returning the same account after its quota
            # was increased. updateClient has set its configured enable=false.
            fixed_client_settings = make_client().client_settings.copy()  # type: ignore[union-attr]
            fixed_client_settings["totalGB"] = 1000
            fixed_client_settings["enable"] = False
            healthy = make_client(
                total=1000,
                up=120,
                enable=False,
                client_settings=fixed_client_settings,
            )
            second_panel = FakePanel()
            restarted = Enforcer(
                EnforcerConfig(state_file=state_file), second_panel  # type: ignore[arg-type]
            )

            results = await restarted.enforce_cycle([healthy])

            self.assertEqual(results[0].action, ActionTaken.READMITTED)
            self.assertEqual(len(second_panel.enable_calls), 1)
            self.assertTrue(second_panel.enable_calls[0][2]["enable"] is False)
            self.assertEqual(second_panel.restart_calls, 0)
            self.assertEqual(json.loads(state_file.read_text(encoding="utf-8"))["actioned"], {})

    async def test_dry_run_never_mutates_or_persists_action_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = Path(temp_dir) / "state.json"
            panel = FakePanel()
            enforcer = Enforcer(
                EnforcerConfig(dry_run=True, state_file=state_file),  # type: ignore[arg-type]
                panel,
            )

            results = await enforcer.enforce_cycle([make_client()])

            self.assertEqual(results[0].action, ActionTaken.DRY_RUN)
            self.assertEqual(panel.disable_calls, [])
            self.assertEqual(panel.enable_calls, [])
            self.assertEqual(panel.restart_calls, 0)
            self.assertFalse(state_file.exists())

    async def test_restart_fallback_is_only_attempted_when_explicitly_enabled(self) -> None:
        panel = FakePanel(disable_ok=False)
        enforcer = Enforcer(
            EnforcerConfig(
                enable_restart_fallback=True,
                restart_grace_period_seconds=0,
            ),
            panel,  # type: ignore[arg-type]
        )

        results = await enforcer.enforce_cycle([make_client()])

        self.assertEqual(results[0].action, ActionTaken.RESTART_XRAY)
        self.assertEqual(panel.restart_calls, 1)


if __name__ == "__main__":
    unittest.main()

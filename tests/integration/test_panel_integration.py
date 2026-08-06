"""End-to-end tests against a real 3x-ui + Xray-core instance (brought up
by docker-compose.ci.yml in this directory). Marked `integration` so the
fast `pytest tests/` unit run (no Docker required) never picks these up —
only the dedicated CI job does, via `pytest tests/integration -m integration`.

These are scaffolded but intentionally left as a small starting set rather
than a full suite: exercising the actual RemoveUser/disable/restart paths
against a live panel needs a per-test-run 3x-ui admin session and at least
one pre-seeded inbound+client, which is environment setup that belongs to
whoever wires this into their own CI credentials — see the "Contributing"
section of the README for the specific `good first issue` tracking this.
"""

from __future__ import annotations

import os

import pytest

from xui_watchdog.panel_client import PanelClient, PanelConfig

pytestmark = pytest.mark.integration

PANEL_URL = os.environ.get("XUIWD_TEST_PANEL_URL", "http://127.0.0.1:2053")


@pytest.mark.asyncio
async def test_login_succeeds_against_live_panel() -> None:
    config = PanelConfig(
        base_url=PANEL_URL,
        auth_mode="password",
        username=os.environ.get("XUIWD_TEST_PANEL_USER", "admin"),
        password=os.environ.get("XUIWD_TEST_PANEL_PASS", "admin"),
    )
    async with PanelClient(config) as panel:
        assert panel._authenticated is True


@pytest.mark.asyncio
async def test_list_client_states_returns_a_list() -> None:
    config = PanelConfig(
        base_url=PANEL_URL,
        auth_mode="password",
        username=os.environ.get("XUIWD_TEST_PANEL_USER", "admin"),
        password=os.environ.get("XUIWD_TEST_PANEL_PASS", "admin"),
    )
    async with PanelClient(config) as panel:
        states = await panel.list_client_states()
        assert isinstance(states, list)

"""Smoke tests against a real 3x-ui + Xray-core instance.

The CI stack is started by docker-compose.ci.yml. These tests currently
verify only panel authentication and inbound listing; they do not create a
client or mutate its state through updateClient.

The live disable/readmission round-trip, including checks that unrelated
clients and usage counters remain unchanged, is tracked in issue #9. The
request payload and enforcer lifecycle are currently covered by unit tests
using httpx.MockTransport and test doubles.
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

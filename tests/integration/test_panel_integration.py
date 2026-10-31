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

import asyncio
import json
import os
import secrets
import uuid

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


async def _get_xray_uptime(panel: PanelClient) -> int:
    response = await panel._client.post(panel._panel_path("/panel/api/server/status"))
    response.raise_for_status()
    body = response.json()
    assert body.get("success") is True, body
    uptime = body["obj"]["appStats"]["uptime"]
    return int(uptime)


@pytest.mark.asyncio
async def test_update_client_disable_and_readmit_only_target_client() -> None:
    """Exercise the real 3x-ui updateClient endpoint against a disposable inbound."""
    config = PanelConfig(
        base_url=PANEL_URL,
        auth_mode="password",
        username=os.environ.get("XUIWD_TEST_PANEL_USER", "admin"),
        password=os.environ.get("XUIWD_TEST_PANEL_PASS", "admin"),
    )

    run_id = uuid.uuid4().hex
    target_email = f"watchdog-target-{run_id}@example.test"
    peer_email = f"watchdog-peer-{run_id}@example.test"
    target_id = str(uuid.uuid4())
    peer_id = str(uuid.uuid4())
    inbound_port = 40000 + secrets.randbelow(20000)
    inbound_id: int | None = None

    clients = [
        {
            "id": target_id,
            "email": target_email,
            "enable": True,
            "alterId": 0,
            "totalGB": 0,
            "expiryTime": 0,
        },
        {
            "id": peer_id,
            "email": peer_email,
            "enable": True,
            "alterId": 0,
            "totalGB": 0,
            "expiryTime": 0,
        },
    ]
    inbound_payload = {
        "up": 0,
        "down": 0,
        "total": 0,
        "remark": f"watchdog-ci-{run_id}",
        "enable": True,
        "expiryTime": 0,
        "listen": "127.0.0.1",
        "port": inbound_port,
        "protocol": "vmess",
        "settings": json.dumps({"clients": clients}),
        "streamSettings": json.dumps({"network": "tcp", "security": "none"}),
        "sniffing": json.dumps({"enabled": False, "destOverride": []}),
        "allocate": json.dumps(
            {"strategy": "always", "refresh": 5, "concurrency": 3}
        ),
    }

    async with PanelClient(config) as panel:
        add_response = await panel._client.post(
            panel._panel_path("/panel/api/inbounds/add"),
            json=inbound_payload,
        )
        add_response.raise_for_status()
        add_body = add_response.json()
        assert add_body.get("success") is True, add_body
        inbound_id = int(add_body["obj"]["id"])

        try:
            states = await panel.list_client_states()
            target = next(client for client in states if client.email == target_email)
            peer = next(client for client in states if client.email == peer_email)

            assert target.inbound_id == inbound_id
            assert target.client_id == target_id
            assert target.protocol == "vmess"
            assert target.enable is True
            assert peer.enable is True

            target_usage_before = (target.up, target.down)
            peer_usage_before = (peer.up, peer.down)
            uptime_before = await _get_xray_uptime(panel)
            assert uptime_before > 0, "Xray must be running for the live API round-trip"

            assert target.client_settings is not None
            disabled = await panel.disable_client(
                inbound_id,
                target.client_id,
                target.client_settings,
            )
            assert disabled is True

            disabled_states = await panel.list_client_states()
            target_disabled = next(
                client for client in disabled_states if client.email == target_email
            )
            peer_after_disable = next(
                client for client in disabled_states if client.email == peer_email
            )
            assert target_disabled.enable is False
            assert target_disabled.client_settings is not None
            assert target_disabled.client_settings["enable"] is False
            assert (target_disabled.up, target_disabled.down) == target_usage_before
            assert peer_after_disable.enable is True
            assert (peer_after_disable.up, peer_after_disable.down) == peer_usage_before

            enabled = await panel.enable_client(
                inbound_id,
                target_disabled.client_id,
                target_disabled.client_settings,
            )
            assert enabled is True

            enabled_states = await panel.list_client_states()
            target_enabled = next(
                client for client in enabled_states if client.email == target_email
            )
            peer_after_enable = next(
                client for client in enabled_states if client.email == peer_email
            )
            assert target_enabled.enable is True
            assert target_enabled.client_settings is not None
            assert target_enabled.client_settings["enable"] is True
            assert (target_enabled.up, target_enabled.down) == target_usage_before
            assert peer_after_enable.enable is True
            assert (peer_after_enable.up, peer_after_enable.down) == peer_usage_before

            # 3x-ui refreshes its cached status periodically. A reset in
            # process uptime would indicate that the panel fell back to a
            # full Xray restart instead of applying the individual client
            # operations through its internal Xray API.
            await asyncio.sleep(2.5)
            uptime_after = await _get_xray_uptime(panel)
            assert uptime_after >= uptime_before, (
                f"Xray uptime decreased during updateClient round-trip: "
                f"{uptime_before} -> {uptime_after}"
            )
        finally:
            if inbound_id is not None:
                delete_response = await panel._client.post(
                    panel._panel_path(f"/panel/api/inbounds/del/{inbound_id}")
                )
                delete_response.raise_for_status()
                delete_body = delete_response.json()
                assert delete_body.get("success") is True, delete_body

"""Regression tests for 3x-ui REST parsing and updateClient requests."""

from __future__ import annotations

import asyncio
import json
import sys
import unittest
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xui_watchdog.panel_client import PanelClient, PanelConfig  # noqa: E402


class TestPanelWebBasePath(unittest.TestCase):
    def _request_paths(self, web_base_path: str) -> list[str]:
        paths: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            paths.append(request.url.path)
            if request.url.path.endswith("/login"):
                return httpx.Response(200, json={"success": True}, request=request)
            return httpx.Response(200, json={"success": True, "obj": []}, request=request)

        async def run() -> None:
            panel = PanelClient(
                PanelConfig(
                    base_url="https://panel.example:2087",
                    auth_mode="password",
                    username="test-user",
                    password="test-password",
                    web_base_path=web_base_path,
                ),
                transport=httpx.MockTransport(handler),
            )
            try:
                await panel.authenticate()
                self.assertEqual(await panel.list_client_states(), [])
            finally:
                await panel.close()

        asyncio.run(run())
        return paths

    def test_root_base_path_preserves_existing_routes(self) -> None:
        self.assertEqual(
            self._request_paths("/"),
            ["/login", "/panel/api/inbounds/list"],
        )

    def test_custom_base_path_prefixes_login_and_api_routes(self) -> None:
        self.assertEqual(
            self._request_paths("/private-panel/"),
            ["/private-panel/login", "/private-panel/panel/api/inbounds/list"],
        )

    def test_custom_base_path_without_slashes_is_normalized(self) -> None:
        self.assertEqual(
            self._request_paths("private-panel"),
            ["/private-panel/login", "/private-panel/panel/api/inbounds/list"],
        )


class TestPanelClientMetadata(unittest.TestCase):
    def _load_states(self, inbound_objects: list[dict[str, Any]]) -> list[Any]:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"success": True, "obj": inbound_objects},
                request=request,
            )

        async def run() -> list[Any]:
            panel = PanelClient(
                PanelConfig(base_url="https://panel.example", auth_mode="token", api_token="test"),
                transport=httpx.MockTransport(handler),
            )
            panel._authenticated = True
            try:
                return await panel.list_client_states()
            finally:
                await panel.close()

        return asyncio.run(run())

    def test_keeps_client_config_enable_separate_from_traffic_enable(self) -> None:
        states = self._load_states(
            [
                {
                    "id": 11,
                    "tag": "vless-in",
                    "protocol": "vless",
                    "settings": json.dumps(
                        {
                            "clients": [
                                {
                                    "id": "vless-uuid",
                                    "email": "vless@example.test",
                                    "totalGB": 1000,
                                    "expiryTime": 2000000000000,
                                    "enable": True,
                                    "flow": "xtls-rprx-vision",
                                }
                            ]
                        }
                    ),
                    "clientStats": [
                        {
                            "email": "vless@example.test",
                            "up": 10,
                            "down": 20,
                            "total": 1000,
                            "enable": False,
                        }
                    ],
                }
            ]
        )

        self.assertEqual(len(states), 1)
        client = states[0]
        self.assertTrue(client.enable)
        self.assertEqual(client.inbound_id, 11)
        self.assertEqual(client.client_id, "vless-uuid")
        self.assertEqual(client.protocol, "vless")
        self.assertEqual(client.up, 10)
        self.assertEqual(client.down, 20)
        self.assertEqual(client.total, 1000)
        self.assertEqual(client.client_settings["flow"], "xtls-rprx-vision")

    def test_protocol_specific_client_identifiers(self) -> None:
        fixtures = [
            ("vless", {"id": "vless-id", "email": "vless@example.test"}, "vless-id"),
            ("vmess", {"id": "vmess-id", "email": "vmess@example.test"}, "vmess-id"),
            ("trojan", {"password": "trojan-secret", "email": "trojan@example.test"}, "trojan-secret"),
            ("shadowsocks", {"email": "ss@example.test"}, "ss@example.test"),
        ]
        inbounds = [
            {
                "id": index,
                "tag": f"{protocol}-in",
                "protocol": protocol,
                "settings": json.dumps({"clients": [dict(client, enable=True)]}),
                "clientStats": [],
            }
            for index, (protocol, client, _) in enumerate(fixtures, start=1)
        ]

        states = self._load_states(inbounds)
        self.assertEqual([state.client_id for state in states], [item[2] for item in fixtures])

    def test_client_without_enable_field_defaults_to_enabled(self) -> None:
        states = self._load_states(
            [
                {
                    "id": 5,
                    "tag": "vmess-in",
                    "protocol": "vmess",
                    "settings": json.dumps(
                        {"clients": [{"id": "client-id", "email": "user@example.test"}]}
                    ),
                    "clientStats": [],
                }
            ]
        )
        self.assertTrue(states[0].enable)
        self.assertTrue(states[0].client_settings["enable"])


class TestPanelClientUpdate(unittest.TestCase):
    def test_disable_sends_full_client_settings_as_json_string(self) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json={"success": True, "msg": "Client updated"}, request=request)

        async def run() -> bool:
            panel = PanelClient(
                PanelConfig(
                    base_url="https://panel.example:2087",
                    auth_mode="token",
                    api_token="test",
                    web_base_path="/hidden-panel/",
                ),
                transport=httpx.MockTransport(handler),
            )
            panel._authenticated = True
            try:
                return await panel.disable_client(
                    42,
                    "client-uuid",
                    {
                        "id": "client-uuid",
                        "email": "alice@example.test",
                        "totalGB": 1073741824,
                        "expiryTime": 2000000000000,
                        "enable": True,
                        "flow": "xtls-rprx-vision",
                    },
                )
            finally:
                await panel.close()

        self.assertTrue(asyncio.run(run()))
        self.assertEqual(len(requests), 1)
        request = requests[0]
        self.assertEqual(request.method, "POST")
        self.assertEqual(
            request.url.path,
            "/hidden-panel/panel/api/inbounds/updateClient/client-uuid",
        )

        body = json.loads(request.content)
        self.assertEqual(body["id"], 42)
        self.assertIsInstance(body["settings"], str)
        client = json.loads(body["settings"])["clients"][0]
        self.assertEqual(client["id"], "client-uuid")
        self.assertEqual(client["email"], "alice@example.test")
        self.assertEqual(client["totalGB"], 1073741824)
        self.assertEqual(client["flow"], "xtls-rprx-vision")
        self.assertFalse(client["enable"])

    def test_trojan_client_id_is_url_encoded_and_reenabled(self) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json={"success": True}, request=request)

        async def run() -> bool:
            panel = PanelClient(
                PanelConfig(base_url="https://panel.example", auth_mode="token", api_token="test"),
                transport=httpx.MockTransport(handler),
            )
            panel._authenticated = True
            try:
                return await panel.enable_client(
                    7,
                    "secret/with space",
                    {"password": "secret/with space", "email": "user@example.test", "enable": False},
                )
            finally:
                await panel.close()

        self.assertTrue(asyncio.run(run()))
        self.assertEqual(
            requests[0].url.raw_path.decode(),
            "/panel/api/inbounds/updateClient/secret%2Fwith%20space",
        )
        client = json.loads(json.loads(requests[0].content)["settings"])["clients"][0]
        self.assertTrue(client["enable"])


if __name__ == "__main__":
    unittest.main()

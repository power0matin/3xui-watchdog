"""Regression tests for 3x-ui panel URL construction."""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()

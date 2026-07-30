"""Thin async wrapper around the 3x-ui REST API.

Deliberately isolated in its own module with versioned endpoint paths
(see `_ENDPOINTS_V3` below) so that a future 3x-ui v4 API shape is a
contained change here, not a rewrite of enforcer.py or main.py, which only
ever talk to the `PanelClient` interface below.

Auth compatibility: 3x-ui has shipped two auth surfaces across its history —
a username/password session-cookie login (the long-standing default) and,
in some releases, a static secret-token header. This client supports both;
which one is used is explicit config, not sniffed at runtime, because
silently guessing auth mode against a security-relevant endpoint is worse
than a clear config error.

This module intentionally never touches 3x-ui's SQLite/Postgres database
file directly — HTTP is the only supported transport, by design (see the
project README's non-negotiables).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

from .policy import ClientState

logger = logging.getLogger("xui_watchdog.panel_client")

# Versioned so a v4 panel API can be added as `_ENDPOINTS_V4` without
# touching call sites.
_ENDPOINTS_V3 = {
    "login": "/login",
    "list_inbounds": "/panel/api/inbounds/list",
    "get_inbound": "/panel/api/inbounds/get/{inbound_id}",
    "update_client": "/panel/api/inbounds/updateClient/{client_uuid}",
    "delete_client": "/panel/api/inbounds/{inbound_id}/delClient/{client_uuid}",
    "get_client_traffic_by_email": "/panel/api/inbounds/getClientTraffics/{email}",
}


class PanelAuthError(RuntimeError):
    """Raised when login fails or a session/token is rejected."""


class PanelAPIError(RuntimeError):
    """Raised for any non-auth panel API failure (bad response shape, 5xx, etc.)."""


@dataclass
class PanelConfig:
    base_url: str
    auth_mode: str  # "password" or "token"
    username: str | None = None
    password: str | None = None
    api_token: str | None = None
    verify_tls: bool = True
    timeout_seconds: float = 10.0


class PanelClient:
    """Talks to one 3x-ui panel instance over HTTP(S).

    Usage:
        async with PanelClient(cfg) as panel:
            clients = await panel.list_client_states()
    """

    def __init__(self, config: PanelConfig, endpoints: dict[str, str] | None = None):
        self.config = config
        self._endpoints = endpoints or _ENDPOINTS_V3
        self._client = httpx.AsyncClient(
            base_url=config.base_url.rstrip("/"),
            verify=config.verify_tls,
            timeout=config.timeout_seconds,
        )
        self._authenticated = False

    async def __aenter__(self) -> "PanelClient":
        await self.authenticate()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    async def close(self) -> None:
        await self._client.aclose()

    # -- auth -----------------------------------------------------------

    async def authenticate(self) -> None:
        if self.config.auth_mode == "token":
            if not self.config.api_token:
                raise PanelAuthError("auth_mode is 'token' but no api_token configured")
            self._client.headers["Authorization"] = f"Bearer {self.config.api_token}"
            self._authenticated = True
            return

        if self.config.auth_mode == "password":
            if not (self.config.username and self.config.password):
                raise PanelAuthError(
                    "auth_mode is 'password' but username/password not configured"
                )
            resp = await self._client.post(
                self._endpoints["login"],
                data={"username": self.config.username, "password": self.config.password},
            )
            if resp.status_code != 200:
                raise PanelAuthError(f"login failed with HTTP {resp.status_code}")
            body = _safe_json(resp)
            if not body.get("success", False):
                raise PanelAuthError(f"login rejected: {body.get('msg', 'unknown error')}")
            # httpx.AsyncClient persists the Set-Cookie session cookie
            # automatically for subsequent requests on this client instance.
            self._authenticated = True
            return

        raise PanelAuthError(f"unknown auth_mode: {self.config.auth_mode!r}")

    # -- reads ------------------------------------------------------------

    async def list_client_states(self) -> list[ClientState]:
        """Fetch all inbounds and flatten every client into a ClientState,
        matching the shape policy.evaluate_client expects.
        """
        self._ensure_authenticated()
        resp = await self._client.get(self._endpoints["list_inbounds"])
        body = _safe_json(resp, endpoint="list_inbounds")
        if not body.get("success", False):
            raise PanelAPIError(f"list_inbounds failed: {body.get('msg', 'unknown error')}")

        states: list[ClientState] = []
        for inbound in body.get("obj", []):
            tag = inbound.get("tag") or f"inbound-{inbound.get('id')}"
            client_stats = {cs.get("email"): cs for cs in inbound.get("clientStats", []) or []}
            settings = _parse_settings(inbound.get("settings"))
            for client in settings.get("clients", []):
                email = client.get("email")
                if not email:
                    continue
                stat = client_stats.get(email, {})
                states.append(
                    ClientState(
                        email=email,
                        inbound_tag=tag,
                        total=int(client.get("totalGB", 0) or stat.get("total", 0) or 0),
                        up=int(stat.get("up", 0) or 0),
                        down=int(stat.get("down", 0) or 0),
                        expiry_time_ms=int(client.get("expiryTime", 0) or 0),
                        enable=bool(stat.get("enable", client.get("enable", True))),
                    )
                )
        return states

    # -- writes -----------------------------------------------------------

    async def disable_client(self, inbound_id: int, client_uuid: str, email: str) -> bool:
        """Fallback A: disable a single client via the REST API when the
        Xray gRPC HandlerService isn't reachable. Sets `enable: false` on
        that one client rather than deleting it, so it is trivially
        re-enabled by an admin (or by our own reconcile pass — see
        policy.should_readmit) without needing to recreate it from scratch.
        """
        self._ensure_authenticated()
        path = self._endpoints["update_client"].format(client_uuid=client_uuid)
        payload: dict[str, Any] = {
            "id": inbound_id,
            "settings": {"clients": [{"id": client_uuid, "email": email, "enable": False}]},
        }
        resp = await self._client.post(path, json=payload)
        body = _safe_json(resp, endpoint="update_client")
        ok = bool(body.get("success", False))
        if not ok:
            logger.warning("disable_client failed for %s: %s", email, body.get("msg"))
        return ok

    async def enable_client(self, inbound_id: int, client_uuid: str, email: str) -> bool:
        """Re-admit path used by the reconcile loop once an admin has fixed
        a client's quota/expiry (see policy.should_readmit)."""
        self._ensure_authenticated()
        path = self._endpoints["update_client"].format(client_uuid=client_uuid)
        payload: dict[str, Any] = {
            "id": inbound_id,
            "settings": {"clients": [{"id": client_uuid, "email": email, "enable": True}]},
        }
        resp = await self._client.post(path, json=payload)
        body = _safe_json(resp, endpoint="update_client")
        return bool(body.get("success", False))

    async def restart_xray(self) -> bool:
        """The 'nuclear option' — full Xray restart via the panel. Only ever
        called by enforcer.py when the operator has explicitly opted into
        Fallback B with a grace period; deliberately not exposed anywhere
        else in this client to keep the blast radius visible in one place.
        """
        self._ensure_authenticated()
        resp = await self._client.post("/panel/api/inbounds/restart")
        body = _safe_json(resp, endpoint="restart_xray")
        return bool(body.get("success", False))

    def _ensure_authenticated(self) -> None:
        if not self._authenticated:
            raise PanelAuthError("call authenticate() (or use `async with`) before making requests")


def _safe_json(resp: httpx.Response, endpoint: str = "") -> dict[str, Any]:
    try:
        return resp.json()
    except ValueError as exc:
        raise PanelAPIError(
            f"non-JSON response from {endpoint or resp.request.url} "
            f"(HTTP {resp.status_code})"
        ) from exc


def _parse_settings(raw: Any) -> dict[str, Any]:
    """3x-ui returns `settings` as a JSON-encoded string, not a nested
    object. Handle both, since a future API version might change this.
    """
    import json

    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}
    if isinstance(raw, dict):
        return raw
    return {}

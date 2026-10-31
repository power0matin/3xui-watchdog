"""Thin async wrapper around the 3x-ui REST API."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx

from .policy import ClientState

logger = logging.getLogger("xui_watchdog.panel_client")

_ENDPOINTS_V3 = {
    "login": "/login",
    "list_inbounds": "/panel/api/inbounds/list",
    "get_inbound": "/panel/api/inbounds/get/{inbound_id}",
    "update_client": "/panel/api/inbounds/updateClient/{client_uuid}",
    "delete_client": "/panel/api/inbounds/{inbound_id}/delClient/{client_uuid}",
    "get_client_traffic_by_email": "/panel/api/inbounds/getClientTraffics/{email}",
    "restart": "/panel/api/inbounds/restart",
}


class PanelAuthError(RuntimeError):
    """Raised when login fails or a session/token is rejected."""


class PanelAPIError(RuntimeError):
    """Raised for panel API failures or unexpected response shapes."""


@dataclass
class PanelConfig:
    base_url: str
    auth_mode: str
    username: str | None = None
    password: str | None = None
    api_token: str | None = None
    verify_tls: bool = True
    timeout_seconds: float = 10.0
    web_base_path: str = "/"


class PanelClient:
    """Talk to one 3x-ui panel instance over HTTP(S)."""

    def __init__(
        self,
        config: PanelConfig,
        endpoints: dict[str, str] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.config = config
        self._endpoints = endpoints or _ENDPOINTS_V3
        self._client = httpx.AsyncClient(
            base_url=config.base_url.rstrip("/"),
            verify=config.verify_tls,
            timeout=config.timeout_seconds,
            transport=transport,
        )
        self._authenticated = False

    def _panel_path(self, endpoint: str) -> str:
        """Prefix API paths with the configured 3x-ui WebBasePath."""
        prefix = self.config.web_base_path.strip().strip("/")
        prefix = f"/{prefix}" if prefix else ""
        return f"{prefix}/{endpoint.lstrip('/')}"

    async def __aenter__(self) -> "PanelClient":
        await self.authenticate()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    async def close(self) -> None:
        await self._client.aclose()

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
                self._panel_path(self._endpoints["login"]),
                data={"username": self.config.username, "password": self.config.password},
            )
            if resp.status_code != 200:
                raise PanelAuthError(f"login failed with HTTP {resp.status_code}")
            body = _safe_json(resp, endpoint="login")
            if not body.get("success", False):
                raise PanelAuthError(f"login rejected: {body.get('msg', 'unknown error')}")
            self._authenticated = True
            return

        raise PanelAuthError(f"unknown auth_mode: {self.config.auth_mode!r}")

    async def list_client_states(self) -> list[ClientState]:
        """Fetch and normalize all clients, including REST action metadata."""
        self._ensure_authenticated()
        resp = await self._client.get(self._panel_path(self._endpoints["list_inbounds"]))
        body = _safe_json(resp, endpoint="list_inbounds")
        if not body.get("success", False):
            raise PanelAPIError(f"list_inbounds failed: {body.get('msg', 'unknown error')}")

        inbounds = body.get("obj") or []
        if not isinstance(inbounds, list):
            raise PanelAPIError("list_inbounds returned an invalid inbounds collection")

        states: list[ClientState] = []
        for inbound in inbounds:
            if not isinstance(inbound, dict):
                continue
            inbound_id = _as_int(inbound.get("id"), default=0) or None
            protocol = str(inbound.get("protocol") or "").strip().lower()
            tag = str(inbound.get("tag") or f"inbound-{inbound.get('id')}")
            client_stats = {
                str(item.get("email")): item
                for item in (inbound.get("clientStats") or [])
                if isinstance(item, dict) and item.get("email")
            }
            settings = _parse_settings(inbound.get("settings"))
            clients = settings.get("clients") or []
            if not isinstance(clients, list):
                continue

            for raw_client in clients:
                if not isinstance(raw_client, dict):
                    continue
                email = raw_client.get("email")
                if not email:
                    continue

                # Preserve all client fields: updateClient requires the
                # complete account object, not a partial {id,email,enable}.
                client_settings = dict(raw_client)
                client_settings["enable"] = _as_bool(raw_client.get("enable"), default=True)
                client_id = _client_identifier(client_settings, protocol)
                stat = client_stats.get(str(email), {})

                total_raw = client_settings.get("totalGB")
                if total_raw in (None, "", 0, "0"):
                    total_raw = stat.get("total")

                states.append(
                    ClientState(
                        email=str(email),
                        inbound_tag=tag,
                        total=_as_int(total_raw),
                        up=_as_int(stat.get("up")),
                        down=_as_int(stat.get("down")),
                        expiry_time_ms=_as_int(client_settings.get("expiryTime")),
                        # ClientTraffic.Enable is a separate quota/expiry
                        # status flag. The configured client.enable value is
                        # what distinguishes an admin-disabled account.
                        enable=client_settings["enable"],
                        inbound_id=inbound_id,
                        client_id=client_id,
                        protocol=protocol or None,
                        client_settings=client_settings,
                    )
                )
        return states

    async def disable_client(
        self, inbound_id: int, client_id: str, client_settings: dict[str, Any]
    ) -> bool:
        """Disable one client through 3x-ui's updateClient API."""
        return await self.set_client_enabled(
            inbound_id, client_id, client_settings, enabled=False
        )

    async def enable_client(
        self, inbound_id: int, client_id: str, client_settings: dict[str, Any]
    ) -> bool:
        """Re-enable one previously watchdog-actioned client."""
        return await self.set_client_enabled(
            inbound_id, client_id, client_settings, enabled=True
        )

    async def set_client_enabled(
        self,
        inbound_id: int,
        client_id: str,
        client_settings: dict[str, Any],
        *,
        enabled: bool,
    ) -> bool:
        """Update one client's enable flag using the real 3x-ui v2.5.x contract.

        In v2.5.4 the updateClient handler binds an Inbound object and passes
        its JSON-encoded Settings string to UpdateInboundClient. A partial
        settings object is not accepted: the request must include the inbound
        ID and a settings string containing the full client object.
        """
        self._ensure_authenticated()
        if inbound_id <= 0:
            raise PanelAPIError("cannot update client without a valid inbound ID")
        if not client_id:
            raise PanelAPIError("cannot update client without a protocol-specific client ID")

        endpoint = self._endpoints["update_client"].format(
            client_uuid=quote(client_id, safe="")
        )
        path = self._panel_path(endpoint)
        client_payload = dict(client_settings)
        client_payload["enable"] = bool(enabled)
        settings_payload = json.dumps(
            {"clients": [client_payload]}, ensure_ascii=False, separators=(",", ":")
        )
        payload = {"id": inbound_id, "settings": settings_payload}

        try:
            resp = await self._client.post(path, json=payload)
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise PanelAPIError(
                f"updateClient failed with HTTP {exc.response.status_code}"
            ) from exc
        except httpx.RequestError as exc:
            raise PanelAPIError(f"updateClient request failed: {exc}") from exc

        body = _safe_json(resp, endpoint="updateClient")
        ok = body.get("success") is True
        if not ok:
            logger.warning(
                "updateClient rejected for %s: %s",
                client_payload.get("email", "<unknown>"),
                body.get("msg", "unknown error"),
            )
        return ok

    async def restart_xray(self) -> bool:
        """Explicit opt-in fallback; restarts every Xray client connection."""
        self._ensure_authenticated()
        resp = await self._client.post(self._panel_path(self._endpoints["restart"]))
        body = _safe_json(resp, endpoint="restart_xray")
        return bool(body.get("success", False))

    def _ensure_authenticated(self) -> None:
        if not self._authenticated:
            raise PanelAuthError(
                "call authenticate() (or use `async with`) before making requests"
            )


def _safe_json(resp: httpx.Response, endpoint: str = "") -> dict[str, Any]:
    try:
        payload = resp.json()
    except ValueError as exc:
        raise PanelAPIError(
            f"non-JSON response from {endpoint or resp.request.url} "
            f"(HTTP {resp.status_code})"
        ) from exc
    if not isinstance(payload, dict):
        raise PanelAPIError(f"invalid JSON response from {endpoint or resp.request.url}")
    return payload


def _parse_settings(raw: Any) -> dict[str, Any]:
    """3x-ui serializes inbound settings as JSON text."""
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    if isinstance(raw, dict):
        return raw
    return {}


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return default


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    normalized = str(value).strip().lower()
    if normalized in {"true", "1", "yes", "on"}:
        return True
    if normalized in {"false", "0", "no", "off", ""}:
        return False
    return default


def _client_identifier(client: dict[str, Any], protocol: str) -> str | None:
    """3x-ui uses different updateClient path IDs for different protocols."""
    if protocol == "trojan":
        value = client.get("password")
    elif protocol == "shadowsocks":
        value = client.get("email")
    else:
        value = client.get("id")
    return str(value) if value else None

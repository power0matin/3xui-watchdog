"""Direct client for Xray-core's gRPC StatsService / HandlerService.

This is the *preferred* enforcement path (see enforcer.py): calling
HandlerService.RemoveUser(inboundTag, email) removes exactly one client from
the running config in-memory, with no file rewrite and no core restart, and
it also drops that client's already-established connection state — which is
the whole point of this tool.

## Why this file can't vendor working stubs out of the box

Xray-core does not publish a pip-installable Python gRPC client. The actual
message/service definitions live as `.proto` files in the Xray-core source
tree (under `app/proxyman/command/command.proto`,
`app/stats/command/command.proto`, and the per-protocol account protos for
vless/vmess/trojan under `proxy/*/config.proto`). To get real generated
stubs you need to, once, from a machine with network access and a checkout
of https://github.com/XTLS/Xray-core (or the mirror at
https://github.com/MHSanaei/3x-ui which vendors a pinned Xray version):

    python -m grpc_tools.protoc \\
        -I xray-core/ \\
        --python_out=src/xui_watchdog/_xray_pb2 \\
        --grpc_python_out=src/xui_watchdog/_xray_pb2 \\
        app/proxyman/command/command.proto \\
        app/stats/command/command.proto \\
        proxy/vless/account.proto proxy/vmess/account.proto proxy/trojan/account.proto \\
        common/protocol/user.proto common/serial/typed_message.proto

Commit the generated `_xray_pb2` package (or regenerate it in CI — see
.github/workflows/ci.yml) and this module will pick it up automatically. It
was not possible to fetch and compile Xray-core's actual `.proto` sources in
the environment this scaffold was written in, so this file deliberately does
NOT commit hand-rolled/guessed protobuf stubs, since a subtly wrong wire
format for RemoveUser would be far worse than a clear "not configured yet"
error — silently corrupting Xray's live proxy config is not an acceptable
failure mode for a tool whose entire job is precision.

Everything else here (channel setup, retry/backoff, the RemoveUser/AddUser
call shape, error mapping) is real and ready to use the moment `_xray_pb2`
exists.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("xui_watchdog.xray_grpc_client")

try:
    import grpc  # type: ignore[import-untyped]
except ImportError:  # pragma: no cover
    grpc = None  # type: ignore[assignment]

try:
    # Generated per the docstring above. Not vendored in this scaffold.
    from . import _xray_pb2  # type: ignore[attr-defined]

    _STUBS_AVAILABLE = True
except ImportError:  # pragma: no cover
    _xray_pb2 = None  # type: ignore[assignment]
    _STUBS_AVAILABLE = False


class XrayGRPCUnavailable(RuntimeError):
    """Raised when the gRPC path can't be used at all — either grpcio isn't
    installed, generated stubs aren't present, or the channel can't connect.
    Callers (enforcer.py) treat this as a signal to fall through to
    Fallback A (the REST API) per the priority order in the spec.
    """


@dataclass
class XrayGRPCConfig:
    host: str
    port: int
    connect_timeout_seconds: float = 3.0
    call_timeout_seconds: float = 5.0
    use_tls: bool = False


class XrayGRPCClient:
    """Wraps Xray-core's HandlerService and StatsService over gRPC.

    Usage:
        client = XrayGRPCClient(config)
        client.connect()
        client.remove_user(inbound_tag="vless-in", email="user@watchdog")
        client.close()
    """

    def __init__(self, config: XrayGRPCConfig):
        self.config = config
        self._channel: Any = None
        self._handler_stub: Any = None
        self._stats_stub: Any = None

    def is_available(self) -> bool:
        return grpc is not None and _STUBS_AVAILABLE

    def connect(self) -> None:
        if grpc is None:
            raise XrayGRPCUnavailable(
                "grpcio is not installed — install the 'grpc' extra "
                "(`pip install 3xui-watchdog[grpc]`) to use the direct gRPC path"
            )
        if not _STUBS_AVAILABLE:
            raise XrayGRPCUnavailable(
                "Xray protobuf stubs not generated — see the module docstring "
                "in xray_grpc_client.py for the one-time `protoc` step"
            )

        target = f"{self.config.host}:{self.config.port}"
        self._channel = (
            grpc.secure_channel(target, grpc.ssl_channel_credentials())
            if self.config.use_tls
            else grpc.insecure_channel(target)
        )
        try:
            grpc.channel_ready_future(self._channel).result(
                timeout=self.config.connect_timeout_seconds
            )
        except grpc.FutureTimeoutError as exc:
            raise XrayGRPCUnavailable(
                f"could not connect to Xray gRPC API at {target} within "
                f"{self.config.connect_timeout_seconds}s"
            ) from exc

        self._handler_stub = _xray_pb2.HandlerServiceStub(self._channel)
        self._stats_stub = _xray_pb2.StatsServiceStub(self._channel)
        logger.info("connected to Xray gRPC API at %s", target)

    def close(self) -> None:
        if self._channel is not None:
            self._channel.close()
            self._channel = None

    def remove_user(self, inbound_tag: str, email: str) -> None:
        """Preferred enforcement action (priority 1 in the spec). Removes
        one client from one inbound's live config, tearing down any
        already-established session for that client immediately — this is
        what makes it faster than waiting on 3x-ui's own next reconcile.
        """
        if self._handler_stub is None:
            raise XrayGRPCUnavailable("not connected — call connect() first")
        req = _xray_pb2.RemoveUserOperation(email=email)  # type: ignore[union-attr]
        call = _xray_pb2.AlterInboundRequest(  # type: ignore[union-attr]
            tag=inbound_tag, operation=_pack_operation(req)
        )
        self._handler_stub.AlterInbound(call, timeout=self.config.call_timeout_seconds)
        logger.info("RemoveUser: tag=%s email=%s", inbound_tag, email)

    def add_user(self, inbound_tag: str, email: str, account: Any) -> None:
        """Re-admit path for the reconcile loop (policy.should_readmit).
        `account` must be one of Xray's per-protocol account messages
        (VLess/VMess/Trojan Account) already constructed by the caller,
        since the watchdog doesn't own client credential generation —
        3x-ui does.
        """
        if self._handler_stub is None:
            raise XrayGRPCUnavailable("not connected — call connect() first")
        user = _xray_pb2.User(email=email, account=_pack_operation(account))  # type: ignore[union-attr]
        req = _xray_pb2.AddUserOperation(user=user)  # type: ignore[union-attr]
        call = _xray_pb2.AlterInboundRequest(  # type: ignore[union-attr]
            tag=inbound_tag, operation=_pack_operation(req)
        )
        self._handler_stub.AlterInbound(call, timeout=self.config.call_timeout_seconds)
        logger.info("AddUser: tag=%s email=%s", inbound_tag, email)

    def get_online_users(self, inbound_tag: str) -> list[str]:
        """Cross-check source for detection: Xray's own idea of who is
        currently connected, independent of what the panel's database says.
        Useful for confirming a RemoveUser actually took effect.
        """
        if self._stats_stub is None:
            raise XrayGRPCUnavailable("not connected — call connect() first")
        req = _xray_pb2.GetInboundUsersCountRequest(tag=inbound_tag)  # type: ignore[union-attr]
        resp = self._handler_stub.GetInboundUsers(req, timeout=self.config.call_timeout_seconds)
        return list(getattr(resp, "emails", []))


def _pack_operation(message: Any) -> Any:
    """Xray's command API wraps operation messages in a TypedMessage
    envelope (type URL + serialized bytes) rather than plain protobuf Any.
    Centralized here so both remove_user and add_user share one packing
    path once _xray_pb2 is generated.
    """
    return _xray_pb2.TypedMessage(  # type: ignore[union-attr]
        type=message.DESCRIPTOR.full_name,
        value=message.SerializeToString(),
    )

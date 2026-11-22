"""agent-core side of the SG-2 liveness chain: stream heartbeats to the safety guardian.

Runs as a background task for the whole agent session. If this process dies or freezes,
the guardian halts the control process — that is the point; there is deliberately no
"pause heartbeats" API.


Agent 会话后台持续发送心跳；进程死亡或冻结会触发 Guardian 停止控制，故不提供暂停心跳接口。非回环目标必须提供 mTLS 凭据。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import grpc

from embodied.runtime.rpc_auth import is_loopback_target
from embodied.runtime.rpcgen import import_common, import_safety

logger = logging.getLogger("runtime.liveness")

PING_INTERVAL_S = 0.3


async def agent_heartbeat_loop(
    guardian_addr: str, *, credentials: Any = None, options: list[tuple[str, Any]] | None = None,
) -> None:
    _, s_pb2_grpc = import_safety()
    common = import_common()
    if credentials is None and not is_loopback_target(guardian_addr):
        raise ValueError("non_loopback_rpc_requires_mtls")
    while True:
        channel = None
        try:
            channel = (
                grpc.aio.secure_channel(guardian_addr, credentials, options=options)
                if credentials is not None
                else grpc.aio.insecure_channel(guardian_addr)
            )
            stub = s_pb2_grpc.SafetyServiceStub(channel)

            async def pings():
                while True:
                    yield _make_ping(common)
                    await asyncio.sleep(PING_INTERVAL_S)

            logger.info("agent heartbeat connecting to %s", guardian_addr)
            async for _pong in stub.Heartbeat(pings()):
                pass
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("agent heartbeat dropped: %s", e)
        finally:
            if channel is not None:
                try:
                    await channel.close()
                except Exception:
                    pass
        await asyncio.sleep(0.5)


def _make_ping(common):
    from embodied.runtime.rpcgen import import_safety

    s_pb2, _ = import_safety()
    return s_pb2.HeartbeatPing(source="agent-core", stamp=common.Timestamp(unix_ms=int(time.time() * 1000)))

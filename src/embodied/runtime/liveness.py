"""agent-core side of the SG-2 liveness chain: stream heartbeats to the safety guardian.

Runs as a background task for the whole agent session. If this process dies or freezes,
the guardian halts the control process — that is the point; there is deliberately no
"pause heartbeats" API.
"""

from __future__ import annotations

import asyncio
import logging
import time

import grpc

from embodied.runtime.rpcgen import import_common, import_safety

logger = logging.getLogger("runtime.liveness")

PING_INTERVAL_S = 0.3


async def agent_heartbeat_loop(guardian_addr: str) -> None:
    _, s_pb2_grpc = import_safety()
    common = import_common()
    while True:
        try:
            channel = grpc.aio.insecure_channel(guardian_addr)
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
            try:
                await channel.close()
            except Exception:
                pass
        await asyncio.sleep(0.5)


def _make_ping(common):
    from embodied.runtime.rpcgen import import_safety

    s_pb2, _ = import_safety()
    return s_pb2.HeartbeatPing(source="agent-core", stamp=common.Timestamp(unix_ms=int(time.time() * 1000)))

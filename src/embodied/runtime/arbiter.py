"""Atomic resource claims with fail-closed quarantine. 原子资源占用及拒绝式隔离。"""

from __future__ import annotations

import threading
from dataclasses import dataclass


class ResourceBusy(RuntimeError):
    pass


@dataclass(frozen=True)
class Lease:
    owner: str
    epoch: int
    resources: tuple[str, ...]


class ResourceArbiter:
    def __init__(self, quarantined: dict[str, tuple[str, ...]] | None = None):
        self._lock = threading.RLock()
        self._owners: dict[str, Lease] = {}
        self._quarantined: set[str] = set()
        for owner, resources in (quarantined or {}).items():
            lease = Lease(owner, 0, resources)
            for resource in resources:
                self._owners[resource] = lease
            self._quarantined.add(owner)

    def acquire(self, owner: str, epoch: int, resources: list[str] | tuple[str, ...]) -> Lease:
        names = tuple(sorted(set(resources)))
        with self._lock:
            conflicts = [name for name in names if name in self._owners]
            if conflicts:
                raise ResourceBusy(f"resource_unavailable:{','.join(conflicts)}")
            lease = Lease(owner, epoch, names)
            for name in names:
                self._owners[name] = lease
            return lease

    def valid(self, lease: Lease) -> bool:
        with self._lock:
            return lease.owner not in self._quarantined and all(
                self._owners.get(name) == lease for name in lease.resources
            )

    def release(self, lease: Lease) -> None:
        with self._lock:
            if lease.owner in self._quarantined:
                return
            for name in lease.resources:
                if self._owners.get(name) == lease:
                    del self._owners[name]

    def quarantine(self, lease: Lease) -> None:
        # 隔离不释放资源；只有未来受信停止对账才能解除。 / Quarantine retains claims until trusted reconciliation exists.
        with self._lock:
            self._quarantined.add(lease.owner)

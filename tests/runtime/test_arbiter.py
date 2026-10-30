"""Resource claim and epoch invariants. 资源占用与 epoch 不变量。"""

import pytest

from embodied.runtime.arbiter import ResourceArbiter, ResourceBusy


def test_all_or_nothing_claims_and_stale_owner_cannot_release_new_lease():
    arbiter = ResourceArbiter()
    first = arbiter.acquire("first", 1, ["arm"])
    with pytest.raises(ResourceBusy):
        arbiter.acquire("conflict", 2, ["workspace", "arm"])
    independent = arbiter.acquire("independent", 3, ["workspace"])
    assert arbiter.valid(independent)
    arbiter.release(first)
    newer = arbiter.acquire("new", 4, ["arm"])
    assert not arbiter.valid(first)
    arbiter.release(first)
    assert arbiter.valid(newer)


def test_quarantine_is_not_a_regular_release():
    arbiter = ResourceArbiter()
    lease = arbiter.acquire("owner", 1, ["arm", "workspace"])
    arbiter.quarantine(lease)
    arbiter.release(lease)
    assert not arbiter.valid(lease)
    with pytest.raises(ResourceBusy):
        arbiter.acquire("next", 2, ["arm"])

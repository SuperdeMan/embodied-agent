"""Recovery client rejects authority claims and ages transport. 恢复客户端拒绝授权声明并累计传输年龄。"""

import copy
import json
from types import SimpleNamespace

import pytest

grpc = pytest.importorskip("grpc")

from embodied.runtime.rpcgen import GEN_DIR  # noqa: E402

if not (GEN_DIR / "embodiedrpc").is_dir():
    pytest.skip("proto stubs not generated", allow_module_level=True)

from embodied.contracts.task import TaskJournalError  # noqa: E402
from embodied.skills.registry import SkillRegistry  # noqa: E402
from embodied.skills.remote import RemoteSkillRegistry  # noqa: E402


@pytest.mark.parametrize("fault", ["grant", "subject", "version", "missing", "wrong_task", "wrong_report", "automatic"])
async def test_remote_recovery_review_is_not_an_authorization_token(monkeypatch, fault):
    local = SkillRegistry()
    report = await local.reconcile_task("task")
    local.close()
    if fault == "grant":
        report["recovery_review"]["execution_authorized"] = True
    elif fault == "subject":
        report["recovery_review"]["authenticated_subject"] = "claimed-admin"
    elif fault == "version":
        report["recovery_review"]["api_version"] = "unknown"
    elif fault == "missing":
        report.pop("recovery_review")
    elif fault == "wrong_task":
        report["recovery_review"]["task_id"] = "another"
    elif fault == "wrong_report":
        report["task_id"] = "another"
    else:
        report["automatic_resume_allowed"] = True
    channel = grpc.aio.insecure_channel("127.0.0.1:1")
    remote = RemoteSkillRegistry(channel)

    async def reply(*args, **kwargs):
        return SimpleNamespace(error="", report_json=json.dumps(report))

    monkeypatch.setattr(remote, "_stub", SimpleNamespace(ReconcileTask=reply))
    try:
        with pytest.raises(TaskJournalError, match="unavailable_or_invalid"):
            await remote.reconcile_task("task")
    finally:
        await channel.close()


@pytest.mark.parametrize("age,elapsed,eligible", [(0.2, 0.3, True), (1.5, 0.6, False), (float("nan"), 0.1, False)])
async def test_transport_updates_age_without_cross_host_clock_comparison(monkeypatch, age, elapsed, eligible):
    local = SkillRegistry()
    report = await local.reconcile_task("task")
    local.close()
    report["steps"] = [{"current_effect": {"eligible": True, "verdict": "sat", "age_s": age, "reason": ""}}]
    clock = [100.0]
    monkeypatch.setattr("embodied.skills.remote.time", SimpleNamespace(monotonic=lambda: clock[0]))
    channel = grpc.aio.insecure_channel("127.0.0.1:1")
    remote = RemoteSkillRegistry(channel)

    async def reply(*args, **kwargs):
        clock[0] += elapsed
        return SimpleNamespace(error="", report_json=json.dumps(copy.deepcopy(report)))

    monkeypatch.setattr(remote, "_stub", SimpleNamespace(ReconcileTask=reply))
    try:
        result = await remote.reconcile_task("task")
        current = result["steps"][0]["current_effect"]
        assert current["eligible"] == eligible
        if eligible:
            assert current["age_s"] == pytest.approx(age + elapsed)
        else:
            assert current["verdict"] == "unknown"
        json.dumps(result, allow_nan=False)
    finally:
        await channel.close()
